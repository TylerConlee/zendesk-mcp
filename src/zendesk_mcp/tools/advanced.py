"""Escape hatch (raw API), incremental exports, and attachments."""

import base64
import mimetypes
import re
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Image
from pydantic import Field

from ..client import ZendeskError
from ..formatting import project
from ..toolkit import Toolkit

MAX_UPLOAD = 50_000_000
GUARDED_PATH = re.compile(r"^(triggers|automations|views|ticket_fields|user_fields|organization_fields)/(\d+)(?:\.json)?(?:\?.*)?$")
MAX_DOWNLOAD = 25_000_000
INLINE_TEXT_LIMIT = 200_000
INLINE_IMAGE_LIMIT = 5_000_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _confine(root: str | None, path: str) -> Path:
    if not root:
        raise ZendeskError("Local file access is disabled. Set ZENDESK_FILE_ROOT to a directory to enable it, "
                           "or pass content_base64/text instead.")
    root_p = Path(root).resolve()
    p = (root_p / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    if p != root_p and root_p not in p.parents:
        raise ZendeskError(f"Path must be inside ZENDESK_FILE_ROOT ({root_p}).")
    return p


def _to_unix(start_time: int | str) -> int:
    if isinstance(start_time, int) or str(start_time).isdigit():
        return int(start_time)
    dt = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


INCREMENTAL = {
    # name: (path, key, cursor_based)
    "tickets": ("incremental/tickets/cursor", "tickets", True),
    "users": ("incremental/users/cursor", "users", True),
    "organizations": ("incremental/organizations", "organizations", False),
    "ticket_events": ("incremental/ticket_events", "ticket_events", False),
    "ticket_metric_events": ("incremental/ticket_metric_events", "ticket_metric_events", False),
}


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Raw Zendesk API request", read_only_hint=False, destructive_hint=True)
    async def api_request(
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        path: Annotated[str, Field(description="Path under /api/v2, e.g. 'tickets/123/collaborators', "
                                   "'account/settings', 'channels/voice/stats/current_queue_activity', "
                                   "'help_center/en-us/sections'. A full URL on this instance also works.")],
        params: Annotated[dict[str, Any] | None, Field(description="Query string parameters.")] = None,
        body: Annotated[Any, Field(description="JSON request body (already wrapped, e.g. {'ticket': {...}}).")] = None,
        paginate: Annotated[bool, Field(description="For GET list endpoints: follow pagination up to `limit` items.")] = False,
        limit: Annotated[int, Field(ge=1, le=10000)] = 100,
        list_key: Annotated[str | None, Field(description="Response key holding the list when paginating (auto-detected if omitted).")] = None,
    ) -> str:
        """Call ANY Zendesk Support / Help Center / Talk API endpoint on this instance. Use this for anything the
        dedicated tools don't cover (side conversation replies, ticket skips, collaborators, account settings,
        voice, routing, deletion restores, etc.). Access mode (ZENDESK_ACCESS_MODE) is still enforced."""
        if paginate and method == "GET":
            return tk.out(await zd.paginate(path, list_key, params=params, limit=limit))
        m = GUARDED_PATH.match(zd.normalize_path(path)) if method in ("PUT", "PATCH") else None
        if m and isinstance(body, dict):
            # Same protection as update_resource: Zendesk replaces whole condition/action/option arrays.
            from .resources import guard_update, fetch_before
            resource, oid = m.group(1), m.group(2)
            wrapper = {"triggers": "trigger", "automations": "automation", "views": "view",
                       "ticket_fields": "ticket_field", "user_fields": "user_field",
                       "organization_fields": "organization_field"}[resource]
            if isinstance(body.get(wrapper), dict):
                before = await fetch_before(tk, f"{resource}/{oid}", wrapper, force=True)
                safe, notes = guard_update(resource, body[wrapper], before, allow_option_removal=False)
                out, change = await zd.write(method, path, params=params, json_body={**body, wrapper: safe},
                                             before=before, wrapper=wrapper)
                result = out if not isinstance(out, dict) else {**out, **({"notes": notes} if notes else {}),
                                                                **({"change_id": change} if change else {})}
                return tk.out(result)
        return tk.out(await zd.request(method, path, params=params, json_body=body))

    @tk.tool("read", title="Incremental export")
    async def incremental_export(
        resource: Literal["tickets", "users", "organizations", "ticket_events", "ticket_metric_events"],
        start_time: Annotated[int | str | None, Field(description="Unix timestamp or ISO date (e.g. '2026-10-01'). "
                                                      "Required unless continuing with `cursor`.")] = None,
        cursor: Annotated[str | None, Field(description="after_cursor from a previous tickets/users page.")] = None,
        per_page: Annotated[int, Field(ge=1, le=1000)] = 100,
        include: Annotated[str | None, Field(description="Sideloads, e.g. 'metric_sets' or 'comment_events' (ticket_events).")] = None,
        fields: list[str] | None = None,
    ) -> str:
        """Export everything that changed since a point in time — one page per call, for reporting and sync.
        Response includes the token to continue (after_cursor or next start_time) and end_of_stream."""
        path, key, cursor_based = INCREMENTAL[resource]
        params: dict[str, Any] = {"per_page": per_page, "include": include}
        if cursor:
            if not cursor_based:
                raise ZendeskError(f"{resource} uses time-based export; continue with start_time=end_time.")
            params["cursor"] = cursor
        elif start_time is not None:
            params["start_time"] = _to_unix(start_time)
        else:
            raise ZendeskError("Provide start_time or cursor.")
        data = await zd.request("GET", path, params=params)
        items = project(data.get(key) or [], fields)
        result: dict[str, Any] = {key: items, "returned": len(items), "end_of_stream": data.get("end_of_stream")}
        if cursor_based:
            result["after_cursor"] = data.get("after_cursor")
        else:
            result["next_start_time"] = data.get("end_time")
        for k, v in data.items():
            if isinstance(v, list) and k != key:
                result[k] = v
        return tk.out(result)

    @tk.tool("write", title="Upload attachment")
    async def upload_attachment(
        filename: str,
        file_path: Annotated[str | None, Field(description="Local file (must be inside ZENDESK_FILE_ROOT).")] = None,
        content_base64: str | None = None,
        text: Annotated[str | None, Field(description="Plain-text file content.")] = None,
        content_type: str | None = None,
        token: Annotated[str | None, Field(description="Existing upload token to add this file to (multi-file).")] = None,
    ) -> str:
        """Upload a file and get a token. Attach it by passing the token in `upload_tokens` of create_ticket,
        update_ticket or add_ticket_comment. Tokens expire after 60 minutes."""
        sources = [x is not None for x in (file_path, content_base64, text)]
        if sum(sources) != 1:
            raise ZendeskError("Provide exactly one of file_path, content_base64, text.")
        if file_path:
            p = _confine(tk.settings.file_root, file_path)
            if p.stat().st_size > MAX_UPLOAD:
                raise ZendeskError("File exceeds 50 MB.")
            data = p.read_bytes()
        elif content_base64:
            data = base64.b64decode(content_base64)
        else:
            data = (text or "").encode()
        ctype = content_type or mimetypes.guess_type(filename)[0] or "application/binary"
        resp = await zd.request("POST", "uploads", params={"filename": filename, "token": token}, content=data,
                                headers={"Content-Type": ctype})
        up = resp["upload"]
        att = up.get("attachment") or {}
        return tk.out({"token": up["token"], "expires_at": up.get("expires_at"), "attachment_id": att.get("id"),
                       "file_name": att.get("file_name"), "size": att.get("size")})

    @tk.tool("read", title="Download / view attachment")
    async def download_attachment(
        attachment_id: int,
        save_to: Annotated[str | None, Field(description="Save to this path inside ZENDESK_FILE_ROOT instead of returning content.")] = None,
    ) -> Any:
        """Fetch an attachment (IDs come from list_ticket_comments). Images are returned so you can see them,
        text files are returned inline; anything else must be saved with save_to."""
        meta = (await zd.get(f"attachments/{attachment_id}"))["attachment"]
        info = {k: meta.get(k) for k in ("id", "file_name", "content_type", "size", "malware_scan_result")}
        if meta.get("malware_access_override") is False and meta.get("malware_scan_result") == "malware_found":
            raise ZendeskError("Zendesk flagged this attachment as malware; refusing to download.")
        data, ctype = await zd.download(meta["content_url"], MAX_DOWNLOAD)
        ctype = (meta.get("content_type") or ctype).split(";")[0].strip()
        if save_to:
            p = _confine(tk.settings.file_root, save_to)
            if p.is_dir():
                p = p / (meta.get("file_name") or f"attachment-{attachment_id}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return tk.out({**info, "saved_to": str(p)})
        if ctype in IMAGE_TYPES and len(data) <= INLINE_IMAGE_LIMIT:
            return [tk.out(info), Image(data=data, format=ctype.split("/")[1])]
        if ctype.startswith("text/") or ctype in ("application/json", "application/xml", "application/csv"):
            text = data[:INLINE_TEXT_LIMIT].decode("utf-8", errors="replace")
            return tk.out({**info, "content": text, "truncated": len(data) > INLINE_TEXT_LIMIT})
        raise ZendeskError(f"{ctype} can't be shown inline; pass save_to (requires ZENDESK_FILE_ROOT). Metadata: {info}")
