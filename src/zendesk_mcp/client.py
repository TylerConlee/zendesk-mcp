"""Async Zendesk API client: auth, access-mode enforcement, retries and pagination."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from .config import Settings
from .journal import Journal


class ZendeskError(ToolError):
    """An error worth showing to the model verbatim (ToolError messages are passed
    through to the client; other exceptions are masked by the SDK)."""


class AccessDenied(ZendeskError):
    pass


_IDEMPOTENT = {"GET", "HEAD", "PUT", "DELETE"}
_RETRY_STATUS = {500, 502, 503, 504}


class ZendeskClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._test_transport = transport
        self.journal = Journal(settings.file_root, enabled=settings.journal)
        self._uses_client_creds = bool(settings.client_id and settings.client_secret)
        self._token_expires_at: float | None = None  # monotonic time; None = no expiry known
        self._token_lock = asyncio.Lock()
        headers = {"Accept": "application/json", "User-Agent": "zendesk-mcp/1.0"}
        if self._uses_client_creds:
            pass  # fetched on first request (see _ensure_token)
        elif settings.oauth_token:
            headers["Authorization"] = f"Bearer {settings.oauth_token}"
        else:
            raw = f"{settings.email}/token:{settings.api_token}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        self._http = httpx.AsyncClient(
            base_url=settings.base_url + "/",
            headers=headers,
            timeout=settings.timeout,
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ OAuth client credentials
    TOKEN_TTL = 7200  # seconds requested per token (Zendesk allows 300-172800)
    REFRESH_MARGIN = 120  # renew this long before expiry

    async def _ensure_token(self, force: bool = False) -> None:
        """For OAuth client credentials: obtain an access token, and renew it shortly before it
        expires (Zendesk issues no refresh token for this grant, so we simply request a new one)."""
        if not self._uses_client_creds:
            return
        async with self._token_lock:
            fresh = "Authorization" in self._http.headers and (
                self._token_expires_at is None or time.monotonic() < self._token_expires_at - self.REFRESH_MARGIN)
            if fresh and not force:
                return
            try:
                req = self._http.build_request(
                    "POST", f"https://{self.settings.host}/oauth/tokens",
                    json={"grant_type": "client_credentials", "client_id": self.settings.client_id,
                          "client_secret": self.settings.client_secret, "scope": self.settings.oauth_scope,
                          "expires_in": self.TOKEN_TTL},
                )
                req.headers.pop("Authorization", None)  # never send the old bearer to the token endpoint
                resp = await self._http.send(req)
            except httpx.TransportError as exc:
                raise ZendeskError(f"Network error requesting a Zendesk OAuth token: {exc}") from exc
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:500]
            if not resp.is_success or not isinstance(body, dict) or not body.get("access_token"):
                raise ZendeskError(
                    f"Zendesk refused the OAuth client credentials (HTTP {resp.status_code}: {_error_text(body)}). "
                    "Check ZENDESK_CLIENT_ID is the client's Identifier (not its name), ZENDESK_CLIENT_SECRET is "
                    "current, the client is 'Confidential', and its allowed scopes include "
                    f"'{self.settings.oauth_scope}' (or set ZENDESK_OAUTH_SCOPE).")
            self._http.headers["Authorization"] = f"Bearer {body['access_token']}"
            ttl = body.get("expires_in")
            self._token_expires_at = time.monotonic() + float(ttl) if ttl else None

    # ------------------------------------------------------------------ paths
    def normalize_path(self, path: str) -> str:
        """Turn any of 'tickets/1', '/api/v2/tickets/1.json' or a full instance URL
        into a path relative to /api/v2. Refuses URLs on other hosts so credentials
        never leave the configured Zendesk instance."""
        path = path.strip()
        if path.startswith(("http://", "https://")):
            parts = urlsplit(path)
            if parts.hostname != self.settings.host:
                raise ZendeskError(f"Refusing to call host {parts.hostname!r}; only {self.settings.host!r} is allowed.")
            path = parts.path + (f"?{parts.query}" if parts.query else "")
        path = path.lstrip("/")
        if path.startswith("api/v2/"):
            path = path[len("api/v2/"):]
        elif path.startswith("api/"):
            raise ZendeskError("Only /api/v2 endpoints are supported.")
        if ".." in path.split("?")[0].split("/"):
            raise ZendeskError("Path traversal is not allowed.")
        return path

    def _check_access(self, method: str) -> None:
        mode = self.settings.access_mode
        if mode == "read_only" and method not in ("GET", "HEAD"):
            raise AccessDenied(f"Server is in read_only mode; {method} requests are disabled (ZENDESK_ACCESS_MODE).")
        if mode == "no_delete" and method == "DELETE":
            raise AccessDenied("Server is in no_delete mode; DELETE requests are disabled (ZENDESK_ACCESS_MODE).")

    # --------------------------------------------------------------- requests
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        access_method: str | None = None,
        before: Any = None,
        wrapper: str | None = None,
        note: str | None = None,
    ) -> Any:
        body, _ = await self.write(method, path, params=params, json_body=json_body, content=content,
                                   headers=headers, access_method=access_method, before=before,
                                   wrapper=wrapper, note=note)
        return body

    async def write(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        access_method: str | None = None,
        before: Any = None,
        wrapper: str | None = None,
        note: str | None = None,
        restore_keys: list[str] | None = None,
    ) -> tuple[Any, str | None]:
        """Like request(), but also returns the change-journal id for non-GET calls.
        `before` (the object's prior state) and `wrapper` (its JSON wrapper key, e.g. 'trigger')
        make the change revertible; `note` tags it (e.g. a blueprint plan id)."""
        method = method.upper()
        if method in ("GET", "HEAD") or (access_method or "").upper() in ("GET", "HEAD"):
            # Reads, and POSTs that only read (previews, validation), aren't changes: no journal entry.
            return await self._send(method, path, params=params, json_body=json_body, content=content,
                                    headers=headers, access_method=access_method), None
        self._check_access(access_method or method)  # refused calls are not journaled
        rel = self.normalize_path(path)
        wrap = wrapper or _wrapper_of(json_body) or _wrapper_of(before)
        entry: dict[str, Any] = {"method": method, "path": rel, "params": params or None, "note": note,
                                 "wrapper": wrap, "request": json_body if content is None else {"_bytes": len(content)},
                                 "before": before}
        if wrap and isinstance(json_body, dict) and isinstance(json_body.get(wrap), dict):
            entry["request_keys"] = list(json_body[wrap])
        if restore_keys:
            entry["restore_keys"] = restore_keys
        try:
            body, status = await self._send_status(method, path, params=params, json_body=json_body, content=content,
                                                   headers=headers, access_method=access_method)
        except ZendeskError as exc:
            self.journal.record({**entry, "ok": False, "error": str(exc)[:1000]})
            raise
        entry["status"] = status
        obj = body.get(entry["wrapper"]) if isinstance(body, dict) and entry["wrapper"] else None
        if obj is None and method in ("PUT", "PATCH") and wrap and status == 204 and self.journal.enabled:
            # e.g. webhooks: no body on update. Read it back so revert can detect later edits.
            try:
                after = await self._send("GET", path.split("?")[0])
                obj = after.get(wrap) if isinstance(after, dict) else None
            except ZendeskError:
                obj = None
        if isinstance(obj, dict):
            entry["result_id"] = obj.get("id")
            entry["result_updated_at"] = obj.get("updated_at")
        if isinstance(body, dict) and isinstance(body.get("job_status"), dict):
            entry["job_id"] = body["job_status"].get("id")
        change_id = self.journal.record({**entry, "ok": True})
        return body, change_id

    async def _send(self, method: str, path: str, **kw: Any) -> Any:
        body, _ = await self._send_status(method, path, **kw)
        return body

    async def _send_status(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        access_method: str | None = None,
    ) -> tuple[Any, int]:
        method = method.upper()
        self._check_access(access_method or method)
        rel = self.normalize_path(path)
        params = {k: v for k, v in (params or {}).items() if v is not None}

        attempt = 0
        reauthed = False
        maybe_applied = False  # an earlier attempt may have reached Zendesk (timeout/5xx), unlike a 429
        await self._ensure_token()
        while True:
            try:
                resp = await self._http.request(
                    method, rel, params=params or None, json=json_body, content=content, headers=headers
                )
            except httpx.TransportError as exc:
                if method in _IDEMPOTENT and attempt < self.settings.max_retries:
                    attempt += 1
                    maybe_applied = True
                    await asyncio.sleep(min(2**attempt, 20))
                    continue
                raise ZendeskError(f"Network error calling Zendesk: {exc}") from exc

            if resp.status_code == 401 and self._uses_client_creds and not reauthed:
                reauthed = True  # token revoked or expired early: get a new one and retry once
                await self._ensure_token(force=True)
                continue
            if resp.status_code == 429 and attempt < self.settings.max_retries:
                attempt += 1
                await asyncio.sleep(_retry_after(resp, attempt))
                continue
            if resp.status_code in _RETRY_STATUS and method in _IDEMPOTENT and attempt < self.settings.max_retries:
                attempt += 1
                maybe_applied = True
                await asyncio.sleep(_retry_after(resp, attempt))
                continue
            if method == "DELETE" and maybe_applied and resp.status_code == 404:
                # An earlier attempt timed out or got a 5xx but actually succeeded; the object is gone.
                return {"ok": True, "status": 204, "note": "deleted (confirmed on retry)"}, 204
            if resp.status_code == 401 and self.settings.oauth_token and not self._uses_client_creds:
                raise ZendeskError(
                    f"Zendesk rejected ZENDESK_OAUTH_TOKEN (HTTP 401: {_error_text(_json_or_text(resp))}). That must be "
                    "an OAuth *access token*. If you pasted an OAuth client's secret, set ZENDESK_CLIENT_ID and "
                    "ZENDESK_CLIENT_SECRET instead and the server will fetch tokens itself.")
            return _parse(resp, method, rel), resp.status_code

    async def download(self, url: str, max_bytes: int) -> tuple[bytes, str]:
        """Download attachment content. Credentials are only sent to the configured
        Zendesk host; httpx drops the Authorization header on cross-origin redirects
        (e.g. to Zendesk's signed-URL CDN)."""
        await self._ensure_token()
        host = urlsplit(url).hostname
        if urlsplit(url).scheme != "https":
            raise ZendeskError("Only https attachment URLs are supported.")
        if host == self.settings.host:
            ctx = self._http.stream("GET", url, follow_redirects=True)
        else:
            plain = httpx.AsyncClient(timeout=self.settings.timeout, follow_redirects=True,
                                      transport=self._test_transport)
            ctx = _closing_stream(plain, url)
        async with ctx as resp:
            if not resp.is_success:
                raise ZendeskError(f"Download failed with HTTP {resp.status_code}.")
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ZendeskError(f"Attachment exceeds {max_bytes // 1_000_000} MB limit.")
                chunks.append(chunk)
            return b"".join(chunks), resp.headers.get("Content-Type", "application/octet-stream")

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def paginate(
        self,
        path: str,
        key: str | None = None,
        *,
        params: dict[str, Any] | None = None,
        limit: int | None = None,
        cursor: bool = True,
    ) -> dict[str, Any]:
        """Fetch up to `limit` items from a list endpoint, following cursor or
        offset pagination. Returns {key: [...], "count": n, "has_more": bool,
        "next": <url or None>} plus any sideloaded collections from page one."""
        limit = limit or self.settings.default_limit
        params = dict(params or {})
        page_size = min(100, limit)
        if cursor:
            params.setdefault("page[size]", page_size)
        else:
            params.setdefault("per_page", page_size)

        items: list[Any] = []
        extras: dict[str, Any] = {}
        next_url: str | None = None
        total: int | None = None
        data = await self.request("GET", path, params=params)
        first = True
        while True:
            if key is None:
                key = _guess_list_key(data)
            batch = data.get(key) or []
            items.extend(batch)
            # Merge sideloads (users, groups, ...) from every page, de-duplicated by id.
            for k, v in data.items():
                if isinstance(v, list) and k != key:
                    seen = {x.get("id") for x in extras.get(k, []) if isinstance(x, dict)}
                    extras.setdefault(k, []).extend(
                        x for x in v if not (isinstance(x, dict) and x.get("id") in seen and x.get("id") is not None)
                    )
            if first:
                total = data.get("count") if isinstance(data.get("count"), int) else None
                first = False
            next_url = _next_link(data)
            if not next_url or len(items) >= limit or not batch:
                break
            data = await self.request("GET", next_url)

        has_more = bool(next_url) or len(items) > limit
        result: dict[str, Any] = {key: items[:limit], "returned": min(len(items), limit), "has_more": has_more}
        if total is not None:
            result["total_count"] = total
        if has_more and next_url:
            result["next_page_url"] = next_url
        result.update(extras)
        return result


class _closing_stream:
    """Stream from a throwaway client and close the client afterwards."""

    def __init__(self, client: httpx.AsyncClient, url: str):
        self._client, self._url, self._cm = client, url, None

    async def __aenter__(self) -> httpx.Response:
        self._cm = self._client.stream("GET", self._url)
        return await self._cm.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        try:
            await self._cm.__aexit__(*exc)
        finally:
            await self._client.aclose()


def _guess_list_key(data: dict[str, Any]) -> str:
    for k, v in data.items():
        if isinstance(v, list) and k not in ("users", "groups", "organizations", "sideloads"):
            return k
    for k, v in data.items():
        if isinstance(v, list):
            return k
    raise ZendeskError(f"Could not find a list in the response (keys: {list(data)}).")


def _next_link(data: dict[str, Any]) -> str | None:
    meta = data.get("meta")
    links = data.get("links")
    if isinstance(meta, dict) and "has_more" in meta:
        if meta.get("has_more") and isinstance(links, dict):
            return links.get("next")
        return None
    nxt = data.get("next_page")
    if isinstance(nxt, str) and nxt:
        return nxt
    return None


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("Retry-After")
    try:
        if header is not None:
            return min(float(header), 60.0)
    except ValueError:
        pass
    return float(min(2**attempt, 30))


def _parse(resp: httpx.Response, method: str, rel: str) -> Any:
    if resp.status_code == 204 or not resp.content:
        if resp.is_success:
            return {"ok": True, "status": resp.status_code}
    if resp.is_redirect:
        return {"status": resp.status_code, "location": resp.headers.get("Location")}
    try:
        body: Any = resp.json()
    except (json.JSONDecodeError, ValueError):
        body = resp.text[:2000]
    if resp.is_success:
        return body
    raise ZendeskError(f"Zendesk API {method} /api/v2/{rel} failed with HTTP {resp.status_code}: {_error_text(body)}")


def _wrapper_of(body: Any) -> str | None:
    """The single wrapper key of a Zendesk object payload, e.g. {'trigger': {...}} -> 'trigger'."""
    if isinstance(body, dict) and len(body) == 1:
        (key, val), = body.items()
        if isinstance(val, dict):
            return key
    return None


def _json_or_text(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        parts = []
        for k in ("error", "title", "description", "message"):
            v = body.get(k)
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, dict):
                parts.append(json.dumps(v))
        if body.get("details"):
            parts.append("details=" + json.dumps(body["details"], default=str)[:1500])
        if body.get("errors"):
            parts.append("errors=" + json.dumps(body["errors"], default=str)[:1500])
        if parts:
            return " | ".join(parts)
    return str(body)[:2000]
