"""Machine-specific configuration lives outside the repository."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from .job_store import check_private_file


def settings() -> dict:
    home = Path(os.environ.get("VBRIDGE_HOME", Path.home() / ".vbridge")).expanduser().resolve()
    config_path = home / "settings.json"
    if config_path.exists():
        check_private_file(config_path)
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    port = int(os.environ.get("VBRIDGE_PORT", config.get("port", 8799)))
    if not 1024 <= port <= 65535:
        raise ValueError("port must be an unprivileged TCP port (1024..65535)")
    public_url = os.environ.get(
        "VBRIDGE_PUBLIC_URL", config.get("public_url", f"http://127.0.0.1:{port}")
    ).rstrip("/")
    parsed = urlparse(public_url)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("public_url must be an origin URL with no path, credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Remote public_url requires HTTPS")

    def flag(name: str, default: bool) -> bool:
        value = os.environ.get("VBRIDGE_" + name.upper(), config.get(name, default))
        return str(value).lower() in ("1", "true", "yes")

    return {
        "home": home,
        "port": port,
        "public_url": public_url,
        "desktop_enabled": flag("desktop_enabled", True),
        "ui_enabled": flag("ui_enabled", False),
    }
