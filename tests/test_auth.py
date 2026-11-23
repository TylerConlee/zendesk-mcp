"""OAuth client-credentials flow and auth configuration."""

import json

import httpx
import pytest

from conftest import data, make_settings, text
from zendesk_mcp.config import ConfigError, Settings

TOKEN_PATH = "/oauth/tokens"


def cc(**kw):
    return dict(email=None, api_token=None, client_id="zendesk_mcp", client_secret="s3cret", **kw)


def token_route(fake, tokens, expires_in=7200):
    issued = iter(tokens)

    def h(req):
        return {"access_token": next(issued), "token_type": "bearer", "scope": "read write", "expires_in": expires_in}
    fake.on("POST", TOKEN_PATH, h, status=201)


async def test_client_credentials_fetch_reuse_and_body(connect, fake):
    token_route(fake, ["tok1", "tok2"])
    fake.on("GET", "/api/v2/users/me", {"user": {"id": 1, "name": "Me"}})
    async with connect(**cc()) as c:
        data(await c.call_tool("get_user", {"user_id": "me", "include_organizations": False}))
        data(await c.call_tool("get_user", {"user_id": "me", "include_organizations": False}))
    paths = [r.url.path for r in fake.calls]
    assert paths == [TOKEN_PATH, "/api/v2/users/me", "/api/v2/users/me"]  # one token, reused
    tok_req = fake.calls[0]
    assert json.loads(tok_req.content) == {"grant_type": "client_credentials", "client_id": "zendesk_mcp",
                                           "client_secret": "s3cret", "scope": "read write", "expires_in": 7200}
    assert "authorization" not in tok_req.headers
    assert fake.calls[1].headers["authorization"] == "Bearer tok1"


async def test_client_credentials_renews_before_expiry(connect, fake):
    token_route(fake, ["tok1", "tok2"], expires_in=60)  # inside the 120s renewal margin -> renew every call
    fake.on("GET", "/api/v2/users/me", {"user": {"id": 1}})
    async with connect(**cc()) as c:
        data(await c.call_tool("get_user", {"user_id": "me", "include_organizations": False}))
        data(await c.call_tool("get_user", {"user_id": "me", "include_organizations": False}))
    assert [r.url.path for r in fake.calls].count(TOKEN_PATH) == 2
    assert fake.calls[-1].headers["authorization"] == "Bearer tok2"
    assert "authorization" not in fake.calls[2].headers  # old bearer not sent to token endpoint


async def test_client_credentials_retries_once_on_401(connect, fake):
    token_route(fake, ["revoked", "fresh"])
    fake.on("GET", "/api/v2/users/me", lambda r: {"user": {"id": 1}} if r.headers["authorization"] == "Bearer fresh"
            else httpx.Response(401, json={"error": "invalid_token"}))
    async with connect(**cc()) as c:
        out = data(await c.call_tool("get_user", {"user_id": "me", "include_organizations": False}))
    assert out["user"]["id"] == 1
    assert [r.url.path for r in fake.calls] == [TOKEN_PATH, "/api/v2/users/me", TOKEN_PATH, "/api/v2/users/me"]


async def test_client_credentials_bad_secret_message(connect, fake):
    fake.on("POST", TOKEN_PATH, {"error": "invalid_client", "error_description": "Client authentication failed"},
            status=401)
    async with connect(**cc()) as c:
        r = await c.call_tool("get_user", {"user_id": "me"})
    assert r.is_error and "refused the OAuth client credentials" in text(r) and "Identifier" in text(r)


async def test_read_only_requests_read_scope(connect, fake):
    token_route(fake, ["t"])
    fake.on("GET", "/api/v2/users/me", {"user": {"id": 1}})
    async with connect(**cc(), access_mode="read_only", oauth_scope="read") as c:
        data(await c.call_tool("get_user", {"user_id": "me", "include_organizations": False}))
    assert json.loads(fake.calls[0].content)["scope"] == "read"


async def test_oauth_token_401_hints_client_secret(connect, fake):
    fake.on("GET", "/api/v2/users/me", {"error": "invalid_token"}, status=401)
    async with connect(email=None, api_token=None, oauth_token="abc") as c:
        r = await c.call_tool("get_user", {"user_id": "me"})
    assert r.is_error and "ZENDESK_CLIENT_ID" in text(r) and "secret" in text(r)


def test_config_client_credentials(monkeypatch):
    for k in ("ZENDESK_EMAIL", "ZENDESK_API_TOKEN", "ZENDESK_OAUTH_TOKEN", "ZENDESK_OAUTH_SCOPE", "ZENDESK_DOMAIN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("dotenv.find_dotenv", lambda **k: "")
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_CLIENT_ID", "zendesk_mcp")
    with pytest.raises(ConfigError, match="both"):
        Settings.from_env()
    monkeypatch.setenv("ZENDESK_CLIENT_SECRET", "s")
    s = Settings.from_env()
    assert (s.client_id, s.oauth_scope) == ("zendesk_mcp", "read write")
    monkeypatch.setenv("ZENDESK_ACCESS_MODE", "read_only")
    assert Settings.from_env().oauth_scope == "read"
