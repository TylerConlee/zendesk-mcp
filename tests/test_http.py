"""End-to-end check of the HTTP transport: real process, real port, bearer auth."""

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

TOKEN = "s3cret-token"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "MCP-Protocol-Version": "2025-06-18"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(port: int, extra_env: dict, args: list[str]) -> subprocess.Popen:
    env = {**os.environ, "ZENDESK_SUBDOMAIN": "acme", "ZENDESK_EMAIL": "a@acme.com", "ZENDESK_API_TOKEN": "x",
           "LOG_LEVEL": "WARNING", **extra_env}
    return subprocess.Popen([sys.executable, "-m", "zendesk_mcp", "--transport", "http", "--port", str(port), *args],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


@pytest.fixture
def server():
    port = _free_port()
    proc = _spawn(port, {"MCP_AUTH_TOKEN": TOKEN}, ["--host", "0.0.0.0"])
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError(proc.stderr.read().decode())
    yield base
    proc.terminate()
    proc.wait(5)


def test_http_requires_bearer(server):
    assert httpx.post(f"{server}/mcp", json=INIT, headers=HEADERS).status_code == 401
    bad = {**HEADERS, "Authorization": "Bearer nope"}
    assert httpx.post(f"{server}/mcp", json=INIT, headers=bad).status_code == 401


def test_http_lists_tools_with_token(server):
    h = {**HEADERS, "Authorization": f"Bearer {TOKEN}"}
    # Non-loopback Host header (as seen behind Docker / a LAN IP) must be accepted.
    h_lan = {**h, "Host": "homeserver.lan:8000"}
    r = httpx.post(f"{server}/mcp", json=INIT, headers=h_lan)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "zendesk"
    r = httpx.post(f"{server}/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=h)
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {"search", "get_ticket", "api_request"} <= names


def test_refuses_network_bind_without_token():
    proc = _spawn(_free_port(), {"MCP_AUTH_TOKEN": ""}, ["--host", "0.0.0.0"])
    _, err = proc.communicate(timeout=20)
    assert proc.returncode != 0 and b"MCP_AUTH_TOKEN" in err
