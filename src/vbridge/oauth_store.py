"""Single-user OAuth 2.1 provider for claude.ai custom connectors.

Persists to one JSON file (atomic write, 0600). Tokens are stored hashed.
The owner approves each new client on /consent with a passphrase.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .job_store import check_private_file, ensure_private_dir, private_write

ACCESS_TTL = 60 * 60 * 24 * 7  # 7 days
REFRESH_TTL = 60 * 60 * 24 * 180  # 180 days
CODE_TTL = 300
PENDING_TTL = 600


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class OwnerOAuthProvider:
    def __init__(self, state_path: Path, public_url: str):
        self.path = state_path
        ensure_private_dir(self.path.parent)
        self.public_url = public_url.rstrip("/")
        self._lock = threading.Lock()
        self._s = {"clients": {}, "access": {}, "refresh": {}}
        self._codes: dict[str, AuthorizationCode] = {}
        self._pending: dict[str, tuple[float, OAuthClientInformationFull, AuthorizationParams]] = {}
        if self.path.exists():
            check_private_file(self.path)
            self._s.update(json.loads(self.path.read_text()))

    # ---- persistence ----
    def _save(self) -> None:
        private_write(self.path, json.dumps(self._s))

    def _gc(self) -> None:
        now = time.time()
        for k in ("access", "refresh"):
            self._s[k] = {h: v for h, v in self._s[k].items() if v["exp"] > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}
        self._pending = {k: v for k, v in self._pending.items() if v[0] > now}

    # ---- clients ----
    async def get_client(self, client_id: str):
        d = self._s["clients"].get(client_id)
        return OAuthClientInformationFull(**d) if d else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self._lock:
            if len(self._s["clients"]) >= 200:
                raise RegistrationError("invalid_client_metadata", "client capacity reached")
            if len(client_info.model_dump_json()) > 8192:
                raise RegistrationError("invalid_client_metadata", "metadata too large")
            self._s["clients"][client_info.client_id] = json.loads(client_info.model_dump_json())
            self._save()

    # ---- authorize: park request, send the browser to our consent page ----
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource is not None and params.resource.rstrip("/") != self.public_url + "/mcp":
            raise AuthorizeError("invalid_request", "resource must be this bridge's /mcp endpoint")
        params.resource = self.public_url + "/mcp"
        with self._lock:
            self._gc()
            if len(self._pending) >= 200:
                raise AuthorizeError("temporarily_unavailable", "pending authorization capacity reached")
            pid = secrets.token_urlsafe(24)
            self._pending[pid] = (time.time() + PENDING_TTL, client, params)
        return f"{self.public_url}/consent?{urlencode({'p': pid})}"

    def pending(self, pid: str):
        with self._lock:
            self._gc()
            return self._pending.get(pid)

    def approve(self, pid: str) -> str | None:
        """Called by /consent after the passphrase is verified."""
        with self._lock:
            item = self._pending.pop(pid, None)
            if not item:
                return None
            _, client, params = item
            code = secrets.token_urlsafe(32)
            self._codes[code] = AuthorizationCode(
                code=code,
                scopes=params.scopes or [],
                expires_at=time.time() + CODE_TTL,
                client_id=client.client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
            )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    def deny(self, pid: str) -> str | None:
        with self._lock:
            item = self._pending.pop(pid, None)
        if not item:
            return None
        _, _, params = item
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)

    # ---- code -> tokens ----
    async def load_authorization_code(self, client, authorization_code: str):
        with self._lock:
            c = self._codes.get(authorization_code)
        if c and c.client_id == client.client_id and c.expires_at > time.time():
            return c
        return None

    def _issue(self, client_id: str, scopes: list[str], resource) -> OAuthToken:
        now = int(time.time())
        at, rt = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
        self._s["access"][_h(at)] = {
            "client_id": client_id,
            "scopes": scopes,
            "exp": now + ACCESS_TTL,
            "resource": resource,
        }
        self._s["refresh"][_h(rt)] = {
            "client_id": client_id,
            "scopes": scopes,
            "exp": now + REFRESH_TTL,
            "resource": resource,
        }
        self._save()
        return OAuthToken(
            access_token=at,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=rt,
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        with self._lock:
            if self._codes.pop(authorization_code.code, None) is None:
                raise TokenError("invalid_grant", "code already used")
            return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    # ---- refresh ----
    async def load_refresh_token(self, client, refresh_token: str):
        with self._lock:
            d = self._s["refresh"].get(_h(refresh_token))
        if d and d["client_id"] == client.client_id and d["exp"] > time.time():
            return RefreshToken(
                token=refresh_token, client_id=d["client_id"], scopes=d["scopes"], expires_at=int(d["exp"])
            )
        return None

    async def exchange_refresh_token(
        self, client, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        with self._lock:
            old = self._s["refresh"].pop(_h(refresh_token.token), None)
            if old is None:
                raise TokenError("invalid_grant", "refresh token revoked")
            return self._issue(client.client_id, scopes or refresh_token.scopes, old.get("resource"))

    # ---- resource server side ----
    async def load_access_token(self, token: str):
        with self._lock:
            d = self._s["access"].get(_h(token))
        if d and d["exp"] > time.time():
            return AccessToken(
                token=token,
                client_id=d["client_id"],
                scopes=d["scopes"],
                expires_at=int(d["exp"]),
                resource=d.get("resource"),
            )
        return None

    async def revoke_token(self, token) -> None:
        with self._lock:
            # Single-owner connector: revoke this client's entire grant, including refresh.
            for kind in ("access", "refresh"):
                self._s[kind] = {k: v for k, v in self._s[kind].items() if v["client_id"] != token.client_id}
            self._save()

    def revoke_all(self) -> None:
        with self._lock:
            self._s["access"].clear()
            self._s["refresh"].clear()
            self._save()
