"""Entry point: `zendesk-mcp` (stdio) or `zendesk-mcp --transport http`."""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import sys

from .config import ConfigError, Settings
from .server import build_server

log = logging.getLogger("zendesk_mcp")


class BearerAuth:
    """Minimal ASGI middleware: require `Authorization: Bearer <token>` on everything except /health."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/health":
            headers = dict(scope.get("headers") or [])
            got = headers.get(b"authorization", b"")
            ok = got.startswith(b"Bearer ") and hmac.compare_digest(got[7:].strip(), self.token)
            if not ok:
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def build_http_app(mcp, *, host: str, path: str, auth_token: str | None, allowed_hosts: list[str]):
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request):  # noqa: ANN001
        return JSONResponse({"status": "ok"})

    if allowed_hosts:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts,
                                             allowed_origins=[f"https://{h}" for h in allowed_hosts]
                                             + [f"http://{h}" for h in allowed_hosts])
    else:
        # Bearer auth already blocks DNS-rebinding style attacks; Host checks would
        # just break access via LAN IPs, Docker hostnames and reverse proxies.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    app = mcp.streamable_http_app(streamable_http_path=path, transport_security=security, host=host,
                                  stateless_http=True, json_response=True)
    return BearerAuth(app, auth_token) if auth_token else app


def _check_writable(settings: Settings) -> None:
    """The change journal, snapshots, docs and CSVs live under ZENDESK_FILE_ROOT; say so loudly if it's read-only."""
    from .journal import journal_path

    for path in filter(None, [settings.file_root and os.path.join(settings.file_root, ".write-test"),
                              str(journal_path(settings.file_root)) + ".write-test" if settings.journal else None]):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("ok")
            os.remove(path)
        except OSError as exc:
            log.warning("Cannot write to %s (%s). The change journal, revert, snapshots and docs need this folder to "
                        "be writable — in Docker run `chmod a+rwX data` on the host.", os.path.dirname(path), exc)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="zendesk-mcp", description="Full-access Zendesk MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default=os.getenv("MCP_TRANSPORT", "stdio"))
    p.add_argument("--host", default=os.getenv("MCP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8000")))
    p.add_argument("--path", default=os.getenv("MCP_PATH", "/mcp"))
    p.add_argument("--insecure-no-auth", action="store_true",
                   help="Allow HTTP mode on a non-loopback interface without MCP_AUTH_TOKEN (not recommended).")
    args = p.parse_args(argv)

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO (including search queries); keep that out of logs by default.
    logging.getLogger("httpx").setLevel(os.getenv("HTTPX_LOG_LEVEL", "WARNING"))
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        sys.exit(f"zendesk-mcp configuration error: {exc}")

    _check_writable(settings)
    mcp, tk = build_server(settings)
    log.info("Zendesk MCP for %s | mode=%s | %d tools", settings.host, settings.access_mode, len(tk.registered))

    if args.transport == "stdio":
        mcp.run("stdio")
        return

    token = os.getenv("MCP_AUTH_TOKEN") or None
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not token and not loopback and not args.insecure_no_auth:
        sys.exit("Refusing to expose full Zendesk access on a network interface without auth. "
                 "Set MCP_AUTH_TOKEN (e.g. `openssl rand -hex 32`) or pass --insecure-no-auth.")
    allowed = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    app = build_http_app(mcp, host=args.host, path=args.path, auth_token=token, allowed_hosts=allowed)

    import uvicorn

    log.info("Listening on http://%s:%d%s (auth: %s)", args.host, args.port, args.path, "bearer" if token else "none")
    uvicorn.run(app, host=args.host, port=args.port, log_level=os.getenv("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
