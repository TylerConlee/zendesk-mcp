"""Append-only change journal.

Every write the server sends to Zendesk (POST/PUT/PATCH/DELETE) is recorded as one JSON line:
when, what, the request body (secrets redacted), the outcome, and — where a tool captured it —
the object's state *before* the change. That before-state is what makes revert_change possible.

The journal never blocks a change: if the file can't be written, the change still happens and the
failure is reported by list_changes."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("zendesk_mcp.journal")

SECRET_KEY = re.compile(r"password|passwd|passphrase|secret|token|authentication|authorization|credential|"
                        r"api[-_]?key|private[-_]?key", re.I)
ALWAYS_REDACT_VALUES = {"custom_headers", "headers"}  # header values often carry API keys under any name
REDACTED = "[redacted]"
MAX_BODY_CHARS = 40_000


def journal_path(file_root: str | None) -> Path:
    if os.getenv("ZENDESK_JOURNAL_FILE"):
        return Path(os.path.expanduser(os.environ["ZENDESK_JOURNAL_FILE"]))
    if file_root:
        return Path(file_root) / "zendesk_changes.jsonl"
    return Path.home() / ".zendesk-mcp" / "changes.jsonl"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in ALWAYS_REDACT_VALUES and isinstance(v, dict):
                out[k] = {hk: REDACTED for hk in v}
            elif SECRET_KEY.search(str(k)) and v not in (None, "", {}, []) and not isinstance(v, bool):
                out[k] = REDACTED  # booleans (e.g. api_token_access) are settings, not secrets
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def contains_redaction(value: Any) -> bool:
    if value == REDACTED:
        return True
    if isinstance(value, dict):
        return any(contains_redaction(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_redaction(v) for v in value)
    return False


def is_clipped(value: Any) -> bool:
    return isinstance(value, dict) and value.get("_clipped") is True


def _clip(value: Any) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= MAX_BODY_CHARS:
        return value
    return {"_clipped": True, "chars": len(text), "preview": text[:2000]}


class Journal:
    def __init__(self, file_root: str | None, enabled: bool = True):
        self.path = journal_path(file_root)
        self.enabled = enabled
        self.last_error: str | None = None
        self._lock = threading.Lock()

    def record(self, entry: dict[str, Any]) -> str | None:
        if not self.enabled:
            return None
        change_id = "chg_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + secrets.token_hex(3)
        row = {"id": change_id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        # `before` is never clipped: it's what revert restores. Large requests are clipped, but their top-level
        # keys are kept separately (request_keys) so a revert still knows what to restore.
        if row.get("before") is not None:
            row["before"] = redact(row["before"])
        for key in ("request", "response"):
            if key in row and row[key] is not None:
                row[key] = _clip(redact(row[key]))
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, default=str) + "\n")
            self.last_error = None
            return change_id
        except OSError as exc:  # never let auditing break the actual change
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Could not write change journal %s: %s", self.path, exc)
            return None

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def get(self, change_id: str) -> dict[str, Any] | None:
        for e in reversed(self.entries()):
            if e.get("id") == change_id:
                return e
        return None
