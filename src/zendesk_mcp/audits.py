"""Reading ticket audits: what changed on a ticket, when, and who or what changed it (an agent, a trigger,
an automation), plus macro applications. Shared by ticket_brief, routing_report and macro_report."""

from __future__ import annotations

from typing import Any

from .client import ZendeskClient
from .metrics import parse_ts

TRACKED = {"status", "group_id", "assignee_id", "priority", "type", "ticket_form_id", "problem_id",
           "organization_id", "brand_id"}


async def load_audits(zd: ZendeskClient, ticket_id: int, limit: int = 1000) -> list[dict]:
    page = await zd.paginate(f"tickets/{ticket_id}/audits", "audits", limit=limit)
    return sorted(page["audits"], key=lambda a: a.get("created_at") or "")


def _by(audit: dict, event: dict) -> dict[str, Any]:
    """Who made a change: a business rule named in the event's (or audit's) via, else the audit author."""
    for via in (event.get("via"), audit.get("via")):
        src = (via or {}).get("source") or {}
        if src.get("rel") in ("trigger", "automation", "macro_reference"):
            frm = src.get("from") or {}
            kind = "macro" if src["rel"] == "macro_reference" else src["rel"]
            return {"type": kind, "id": frm.get("id"), "title": frm.get("title")}
    author = audit.get("author_id")
    if author in (None, -1):
        return {"type": "zendesk", "channel": (audit.get("via") or {}).get("channel")}
    return {"type": "user", "id": author}


def _val(v: Any) -> Any:
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return v


def parse_audits(audits: list[dict]) -> dict[str, Any]:
    """{'initial': {field: value at creation}, 'changes': [...], 'macros': [...], 'comments': [...]}"""
    initial: dict[str, Any] = {}
    changes: list[dict[str, Any]] = []
    macros: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    for a in audits:
        at = a.get("created_at")
        for e in a.get("events") or []:
            kind = e.get("type")
            field = e.get("field_name")
            if kind == "Create" and field:
                initial[field] = _val(e.get("value"))
            elif kind == "Change" and field in TRACKED:
                changes.append({"at": at, "field": field, "from": _val(e.get("previous_value")),
                                "to": _val(e.get("value")), "by": _by(a, e)})
            elif kind == "AgentMacroReference":
                macros.append({"at": at, "macro_id": _val(e.get("macro_id")), "title": e.get("macro_title"),
                               "deleted": e.get("macro_deleted"), "by": _by(a, {})})
            elif kind == "Comment":
                comments.append({"at": at, "public": e.get("public", True), "author_id": e.get("author_id"),
                                 "macro_applied": any(x.get("type") == "AgentMacroReference"
                                                      for x in a.get("events") or [])})
    return {"initial": initial, "changes": changes, "macros": macros, "comments": comments}


def group_path(parsed: dict[str, Any], final_group: int | None) -> list[dict[str, Any]]:
    """Every group the ticket sat in, in order, with when it arrived and who sent it there."""
    path: list[dict[str, Any]] = []
    first = parsed["initial"].get("group_id")
    if first:
        path.append({"group_id": first, "at": None, "by": {"type": "created"}})
    for c in parsed["changes"]:
        if c["field"] == "group_id" and c["to"] and (not path or path[-1]["group_id"] != c["to"]):
            path.append({"group_id": c["to"], "at": c["at"], "by": c["by"]})
    if final_group and (not path or path[-1]["group_id"] != final_group):
        path.append({"group_id": final_group, "at": None, "by": {"type": "unknown"}})
    return path


def minutes_between(a: str | None, b: str | None) -> float | None:
    ta, tb = parse_ts(a), parse_ts(b)
    return round((tb - ta).total_seconds() / 60, 1) if ta and tb else None
