"""A fake Zendesk API served through httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest
from mcp.client import Client

from zendesk_mcp.config import Settings
from zendesk_mcp.server import build_server

HOST = "acme.zendesk.com"
BASE = f"https://{HOST}/api/v2"


class FakeZendesk:
    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = []
        self.calls: list[httpx.Request] = []

    def on(self, method: str, path: str, handler: Callable[[httpx.Request], Any] | Any, status: int = 200):
        """Register a handler. `path` matches the URL path exactly (without query)."""
        def h(req: httpx.Request) -> httpx.Response:
            out = handler(req) if callable(handler) else handler
            if isinstance(out, httpx.Response):
                return out
            return httpx.Response(status, json=out)
        self.routes.append((method, path, h))
        return self

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        for method, path, h in reversed(self.routes):
            if req.method == method and req.url.path == path:
                return h(req)
        return httpx.Response(404, json={"error": "RecordNotFound", "description": f"No route {req.method} {req.url.path}"})

    def body(self, i: int = -1) -> Any:
        return json.loads(self.calls[i].content)


def make_settings(**kw: Any) -> Settings:
    base = dict(host=HOST, email="agent@acme.com", api_token="tok", oauth_token=None, max_retries=2, journal=False)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def fake() -> FakeZendesk:
    return FakeZendesk()


@pytest.fixture
def connect(fake: FakeZendesk):
    """connect(**settings) -> async context manager yielding an MCP client."""
    def _connect(**kw: Any):
        mcp, _ = build_server(make_settings(**kw), transport=httpx.MockTransport(fake.handle))
        return Client(mcp)
    return _connect


def text(result: Any) -> str:
    return result.content[0].text


def data(result: Any) -> Any:
    assert not result.is_error, text(result)
    return json.loads(text(result))
