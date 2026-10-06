import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from vbridge.oauth_store import OwnerOAuthProvider


def client():
    return OAuthClientInformationFull(
        client_id="unit-client",
        client_name="unit",
        redirect_uris=["https://example.com/callback"],
        token_endpoint_auth_method="none",
    )


@pytest.mark.asyncio
async def test_pkce_issue_refresh_revoke(tmp_path):
    provider = OwnerOAuthProvider(tmp_path / "oauth.json", "https://bridge.example.com")
    c = client()
    await provider.register_client(c)
    params = AuthorizationParams(
        state="state",
        scopes=["bridge"],
        code_challenge="unit-challenge",
        redirect_uri=AnyUrl("https://example.com/callback"),
        redirect_uri_provided_explicitly=True,
        resource="https://bridge.example.com/mcp",
    )
    url = await provider.authorize(c, params)
    from urllib.parse import parse_qs, urlparse

    pid = parse_qs(urlparse(url).query)["p"][0]
    redirect = provider.approve(pid)
    code = parse_qs(urlparse(redirect).query)["code"][0]
    authorization = await provider.load_authorization_code(c, code)
    assert authorization is not None
    token = await provider.exchange_authorization_code(c, authorization)
    assert await provider.load_authorization_code(c, code) is None
    access = await provider.load_access_token(token.access_token)
    assert access.resource == "https://bridge.example.com/mcp"
    refresh = await provider.load_refresh_token(c, token.refresh_token)
    rotated = await provider.exchange_refresh_token(c, refresh, ["bridge"])
    assert await provider.load_refresh_token(c, token.refresh_token) is None
    await provider.revoke_token(await provider.load_refresh_token(c, rotated.refresh_token))
    assert await provider.load_access_token(token.access_token) is None
    assert await provider.load_access_token(rotated.access_token) is None


@pytest.mark.asyncio
async def test_audience_rejected(tmp_path):
    provider = OwnerOAuthProvider(tmp_path / "oauth.json", "https://bridge.example.com")
    params = AuthorizationParams(
        state=None,
        scopes=["bridge"],
        code_challenge="challenge",
        redirect_uri=AnyUrl("https://example.com/callback"),
        redirect_uri_provided_explicitly=True,
        resource="https://other.example.com/mcp",
    )
    with pytest.raises(AuthorizeError):
        await provider.authorize(client(), params)
