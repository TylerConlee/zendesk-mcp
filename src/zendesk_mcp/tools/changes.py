"""Change safety: configuration snapshots, snapshot diffs, the change journal, and reverting changes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from ..client import ZendeskClient, ZendeskError
from ..journal import contains_redaction, is_clipped
from ..toolkit import Toolkit
from .advanced import _confine

# name: (path, list key, cursor pagination?, extra params)
SNAPSHOT_SECTIONS: dict[str, tuple[str, str, bool, dict | None]] = {
    "account_settings": ("account/settings", "settings", False, None),
    "groups": ("groups", "groups", True, None),
    "custom_roles": ("custom_roles", "custom_roles", False, None),
    "agents": ("users", "users", True, {"role[]": ["agent", "admin"]}),
    "group_memberships": ("group_memberships", "group_memberships", True, None),
    "brands": ("brands", "brands", True, None),
    "recipient_addresses": ("recipient_addresses", "recipient_addresses", True, None),
    "ticket_fields": ("ticket_fields", "ticket_fields", True, None),
    "user_fields": ("user_fields", "user_fields", True, None),
    "organization_fields": ("organization_fields", "organization_fields", True, None),
    "ticket_forms": ("ticket_forms", "ticket_forms", True, None),
    "custom_statuses": ("custom_statuses", "custom_statuses", False, None),
    "schedules": ("business_hours/schedules", "schedules", False, None),
    "sla_policies": ("slas/policies", "sla_policies", False, None),
    "group_sla_policies": ("group_slas/policies", "group_sla_policies", False, None),
    "trigger_categories": ("trigger_categories", "trigger_categories", True, None),
    "triggers": ("triggers", "triggers", True, None),
    "automations": ("automations", "automations", True, None),
    "views": ("views", "views", True, None),
    "macros": ("macros", "macros", True, None),
    "dynamic_content_items": ("dynamic_content/items", "items", True, None),
    "webhooks": ("webhooks", "webhooks", True, None),
    "targets": ("targets", "targets", False, None),
    "routing_attributes": ("routing/attributes", "attributes", False, None),
    "queues": ("queues", "queues", False, None),
}
AGENT_KEYS = ("id", "name", "email", "role", "custom_role_id", "default_group_id", "suspended", "time_zone", "locale",
              "alias", "tags", "user_fields", "created_at", "updated_at")
VOLATILE = {"updated_at", "url", "last_login_at", "usage_1h", "usage_24h", "usage_7d", "usage_30d"}
READ_ONLY = {"id", "url", "created_at", "updated_at", "raw_title", "raw_description", "raw_name", "raw_agent_label",
             "raw_end_user_label", "raw_display_name", "raw_end_user_description"}


async def take_snapshot(zd: ZendeskClient, sections: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    unavailable: dict[str, str] = {}
    for name in sections:
        path, key, cursor, params = SNAPSHOT_SECTIONS[name]
        try:
            if name == "account_settings":
                data[name] = (await zd.get(path)).get("settings", {})
                continue
            items = (await zd.paginate(path, key, params=params, limit=100000, cursor=cursor))[key]
            if name == "agents":
                items = [{k: u.get(k) for k in AGENT_KEYS} for u in items]
            if name == "schedules":
                for sch in items:
                    try:
                        sch["holidays"] = (await zd.get(f"business_hours/schedules/{sch['id']}/holidays")).get("holidays", [])
                    except ZendeskError:
                        sch["holidays"] = None
            if name == "routing_attributes":
                for attr in items:
                    try:
                        attr["values"] = (await zd.get(f"routing/attributes/{attr['id']}/values")).get("attribute_values", [])
                    except ZendeskError:
                        attr["values"] = None
            data[name] = items
        except ZendeskError as exc:
            unavailable[name] = str(exc)[:300]
    return {"data": data, "unavailable": unavailable}


def _index(items: Any) -> dict[str, dict]:
    if isinstance(items, dict):  # account settings: compare section by section
        return {k: v for k, v in items.items() if isinstance(v, dict)}
    out = {}
    for i in items or []:
        if isinstance(i, dict) and i.get("id") is not None:
            out[str(i["id"])] = i
    return out


def _label(item: dict) -> str:
    return str(item.get("title") or item.get("name") or item.get("agent_label") or item.get("email") or
               item.get("key") or item.get("id"))


def diff_snapshots(a: dict[str, Any], b: dict[str, Any], detail: int = 50) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for section in sorted(set(a) | set(b)):
        ia, ib = _index(a.get(section)), _index(b.get(section))
        added = [ib[k] for k in ib if k not in ia]
        removed = [ia[k] for k in ia if k not in ib]
        changed = []
        for k in ia.keys() & ib.keys():
            keys = sorted(f for f in set(ia[k]) | set(ib[k]) if f not in VOLATILE and ia[k].get(f) != ib[k].get(f))
            if keys:
                changed.append({"id": k, "name": _label(ib[k]) if section != "account_settings" else k,
                                "changed_keys": keys,
                                "values": {f: {"from": ia[k].get(f), "to": ib[k].get(f)} for f in keys
                                           if len(json.dumps([ia[k].get(f), ib[k].get(f)], default=str)) < 400}})
        if added or removed or changed:
            out[section] = {"added": [{"id": i.get("id"), "name": _label(i)} for i in added[:detail]],
                            "removed": [{"id": i.get("id"), "name": _label(i)} for i in removed[:detail]],
                            "changed": changed[:detail],
                            "counts": {"added": len(added), "removed": len(removed), "changed": len(changed)}}
    return out


def _clean_for_create(obj: dict) -> dict:
    out = {k: v for k, v in obj.items() if k not in READ_ONLY and not contains_redaction(v)}
    # Child objects (dropdown options) belonged to the deleted parent; create them fresh.
    for key in ("custom_field_options", "variants"):
        if isinstance(out.get(key), list):
            out[key] = [{k: v for k, v in o.items() if k not in READ_ONLY} if isinstance(o, dict) else o for o in out[key]]
    return out


def revertibility(e: dict) -> tuple[bool, str]:
    """Whether a journal entry can be undone, and why not."""
    if not e.get("ok"):
        return False, "the original change failed, so there's nothing to undo"
    method, path, wrapper = e.get("method"), (e.get("path") or "").split("?")[0], e.get("wrapper")
    before, request = e.get("before"), e.get("request")
    if is_clipped(before):
        return False, "the before-state was too large to record (older journal entry)"
    if method == "POST" and path.endswith("/instance_values"):
        return (before is not None, "" if before is not None else "previous skills weren't recorded")
    if method == "POST":
        if e.get("result_id") is None or path.endswith(("/merge", "/make_default", "/validate", "/preview", "/count",
                                                       "/jobs", "/instance_values")):
            return False, "this action has no single created object to delete"
        if e.get("status") not in (None, 201):
            return False, f"Zendesk answered {e.get('status')}, not 201: this call updated an existing object"
        if wrapper and isinstance(request, dict) and isinstance(request.get(wrapper), dict) and "id" in request[wrapper]:
            return False, "this was a create-or-update of an existing object"
        return True, ""
    if method in ("PUT", "PATCH"):
        if before is None:
            return False, "no before-state was captured (bulk ticket updates and some actions can't be undone)"
        if wrapper and not (e.get("restore_keys") or e.get("request_keys") or
                            (isinstance(request, dict) and isinstance(request.get(wrapper), dict))):
            return False, "the request wasn't recorded in enough detail to know what to restore"
        return True, ""
    if method == "DELETE":
        if not (before and wrapper):
            return False, "no before-state was captured for this delete"
        return True, ""
    return False, f"can't revert a {method}"


async def revert_entry(zd: ZendeskClient, e: dict, force: bool, note: str) -> dict[str, Any]:
    """Undo one journaled change. Returns {'status': 'reverted'|'partial'|'skipped'|'unsupported'|'conflict', ...}."""
    base = {"change_id": e.get("id"), "method": e.get("method"), "path": e.get("path")}
    ok, why = revertibility(e)
    if not ok:
        return {**base, "status": "skipped" if not e.get("ok") else "unsupported", "reason": why}
    method, path, wrapper = e.get("method"), (e.get("path") or "").split("?")[0], e.get("wrapper")
    before, request = e.get("before"), e.get("request")
    if method == "POST" and path.endswith("/instance_values"):
        if not force:  # replace-style: make sure nobody changed the skills since
            applied = sorted(str(x) for x in (request or {}).get("attribute_value_ids") or [])
            try:
                cur = sorted(str(v.get("id")) for v in (await zd.get(path)).get("attribute_values", []))
            except ZendeskError as exc:
                return {**base, "status": "conflict", "reason": f"couldn't read the agent's current skills: {exc}"}
            if cur != applied:
                return {**base, "status": "conflict", "reason": "the agent's skills changed after this change; pass "
                                                               "force=true to restore the earlier set anyway"}
        _, ch = await zd.write("POST", path, json_body=before, note=note)
        return {**base, "status": "reverted", "action": "restored previous skills", "revert_change_id": ch}
    if method == "POST":
        rid = e["result_id"]
        _, ch = await zd.write("DELETE", f"{path}/{rid}", note=note)
        return {**base, "status": "reverted", "action": f"deleted created object {rid}", "revert_change_id": ch}
    if method in ("PUT", "PATCH"):
        if wrapper:
            keys = e.get("restore_keys") or e.get("request_keys") or list(request[wrapper])
            restore = {k: before.get(k) for k in keys if k in before and not contains_redaction(before.get(k))}
            skipped = [k for k in keys if k not in restore]
            expected = e.get("result_updated_at")
            if not force and expected:  # only objects that report updated_at can be checked
                try:
                    cur = (await zd.get(path)).get(wrapper, {})
                except ZendeskError as exc:
                    return {**base, "status": "conflict", "reason": f"couldn't read the current object: {exc}"}
                if cur.get("updated_at") != expected:
                    return {**base, "status": "conflict",
                            "reason": "the object changed again after this change (updated_at differs); pass "
                                      "force=true to overwrite it with the earlier values anyway"}
            if not restore:
                return {**base, "status": "unsupported", "reason": f"nothing restorable (redacted keys: {skipped})"}
            if wrapper in ("trigger", "automation") and ({"conditions", "actions"} & restore.keys()):
                for k in ("conditions", "actions"):  # Zendesk clears both when either is sent
                    if k not in restore and before.get(k) is not None:
                        restore[k] = before[k]
            _, ch = await zd.write(method, path, json_body={wrapper: restore}, note=note)
            return {**base, "status": "reverted" if not skipped else "partial", "restored_keys": list(restore),
                    "skipped_keys": skipped, "revert_change_id": ch}
        params = {"sla_policy_ids[]": before["sla_policy_ids"]} if path.endswith("slas/policies/reorder") else None
        _, ch = await zd.write(method, path, json_body=before, params=params, note=note)
        return {**base, "status": "reverted", "action": "restored previous order/state", "revert_change_id": ch}
    # DELETE -> re-create
    collection = path.rsplit("/", 1)[0]
    body, ch = await zd.write("POST", collection, json_body={wrapper: _clean_for_create(before)}, wrapper=wrapper,
                              note=note)
    new_id = (body.get(wrapper) or {}).get("id") if isinstance(body, dict) else None
    out = {**base, "status": "reverted", "action": "re-created deleted object", "new_id": new_id,
           "old_id": before.get("id"), "warning": "It has a new ID: rules that referred to the old ID need updating "
                                                  "(find_references can help).", "revert_change_id": ch}
    if contains_redaction(before) or (wrapper == "webhook" and before.get("authentication")):
        out["status"] = "partial"
        out["warning"] += " Credentials/secrets weren't recorded, so re-enter them (e.g. the webhook's authentication)."
    return out


def register(tk: Toolkit) -> None:
    zd = tk.zd

    def snapshot_dir() -> Path:
        return _confine(tk.settings.file_root, "snapshots")

    def load_snapshot(ref: str) -> dict[str, Any]:
        folder = snapshot_dir()
        p = _confine(tk.settings.file_root, ref if "/" in ref else f"snapshots/{ref}")
        if not p.exists() and not ref.endswith(".json"):
            p = folder / f"{ref}.json"
        if not p.exists():
            available = sorted(x.name for x in folder.glob("*.json"))[-10:] if folder.exists() else []
            raise ZendeskError(f"Snapshot {ref!r} not found. Recent: {available}")
        return json.loads(p.read_text())

    @tk.tool("read", title="Snapshot configuration", read_only_hint=False)
    async def config_snapshot(
        sections: Annotated[list[Literal[tuple(SNAPSHOT_SECTIONS)]] | None,  # type: ignore[valid-type]
                            Field(description="Sections to export (default all).")] = None,
        label: Annotated[str | None, Field(description="Short label added to the file name, e.g. 'pre-rollout'.")] = None,
    ) -> str:
        """Export the account's configuration — settings, groups, roles, agents, fields, forms, statuses,
        schedules, SLAs, trigger categories, triggers, automations, views, macros, webhooks, routing — to a JSON
        file under ZENDESK_FILE_ROOT/snapshots. Take one before any rollout; compare later with snapshot_diff."""
        chosen = sections or list(SNAPSHOT_SECTIONS)
        snap = await take_snapshot(zd, chosen)
        folder = snapshot_dir()
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch for ch in (label or "") if ch.isalnum() or ch in "-_")[:40]
        path = folder / f"zendesk-config-{stamp}{('-' + safe) if safe else ''}.json"
        doc = {"instance": tk.settings.host, "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "label": label, **snap}
        path.write_text(json.dumps(doc, indent=1, default=str))
        counts = {k: (len(v) if isinstance(v, list) else len(v) if isinstance(v, dict) else 0)
                  for k, v in snap["data"].items()}
        return tk.out({"file": str(path), "name": path.name, "counts": counts, "unavailable": snap["unavailable"]})

    @tk.tool("read", title="Compare snapshots")
    async def snapshot_diff(
        before: Annotated[str, Field(description="Snapshot file name (from config_snapshot) or path inside "
                                     "ZENDESK_FILE_ROOT.")],
        after: Annotated[str, Field(description="Another snapshot, or 'live' to compare with the current account.")] = "live",
        detail: Annotated[int, Field(ge=1, le=500, description="Items listed per section and change type.")] = 50,
    ) -> str:
        """What changed between two snapshots (or a snapshot and the live account): objects added, removed and
        changed per section, with before/after values for small fields."""
        a = load_snapshot(before)
        if after == "live":
            b = {"data": (await take_snapshot(zd, list(a["data"].keys())))["data"], "taken_at": "live"}
        else:
            b = load_snapshot(after)
        diff = diff_snapshots(a["data"], b["data"], detail)
        return tk.out({"before": a.get("taken_at"), "after": b.get("taken_at"),
                       "summary": {k: v["counts"] for k, v in diff.items()}, "sections": diff})

    @tk.tool("read", title="List changes")
    async def list_changes(
        limit: Annotated[int, Field(ge=1, le=1000)] = 50,
        batch: Annotated[str | None, Field(description="Only changes tagged with this batch, e.g. a blueprint plan id "
                                           "or 'offboard:<user id>'.")] = None,
        path_contains: str | None = None,
        failed_only: bool = False,
        include_bodies: Annotated[bool, Field(description="Include request bodies and before-states.")] = False,
    ) -> str:
        """The change journal: every write this server has made to Zendesk (newest first), with outcome and whether
        it can be reverted."""
        rows = zd.journal.entries()
        if batch:
            rows = [r for r in rows if r.get("note") == batch]
        if path_contains:
            rows = [r for r in rows if path_contains in (r.get("path") or "")]
        if failed_only:
            rows = [r for r in rows if not r.get("ok")]
        rows = list(reversed(rows))[:limit]
        out = []
        for r in rows:
            item = {k: r.get(k) for k in ("id", "at", "method", "path", "ok", "note", "result_id", "error") if r.get(k) is not None}
            item["revertible"], why = revertibility(r)
            if not item["revertible"]:
                item["why_not"] = why
            if include_bodies:
                item.update(request=r.get("request"), before=r.get("before"))
            out.append(item)
        status = {"journal_file": str(zd.journal.path), "enabled": zd.journal.enabled}
        if zd.journal.last_error:
            status["last_write_error"] = zd.journal.last_error
        return tk.out({"journal": status, "changes": out})

    @tk.tool("write", title="Revert change", destructive_hint=True)
    async def revert_change(
        change_id: Annotated[str | None, Field(description="A change id from list_changes or a tool result.")] = None,
        batch: Annotated[str | None, Field(description="Revert every change in a batch (newest first), e.g. a "
                                           "blueprint plan id.")] = None,
        force: Annotated[bool, Field(description="Overwrite even if the object changed again since.")] = False,
        dry_run: bool = False,
    ) -> str:
        """Undo changes made through this server using the journal: updates are rolled back to their previous
        values, created objects are deleted, deleted objects are re-created (with new IDs), reorders are
        restored. Bulk ticket updates can't be undone."""
        if bool(change_id) == bool(batch):
            raise ZendeskError("Pass exactly one of change_id or batch.")
        entries = zd.journal.entries()
        if change_id:
            targets = [e for e in entries if e.get("id") == change_id]
            if not targets:
                raise ZendeskError(f"No change {change_id!r} in {zd.journal.path}.")
        else:
            targets = [e for e in reversed(entries) if e.get("note") == batch and e.get("ok")]
            if not targets:
                raise ZendeskError(f"No successful changes tagged {batch!r}.")
        if dry_run:
            return tk.out({"dry_run": True, "would_revert": [
                {k: e.get(k) for k in ("id", "at", "method", "path", "result_id")} for e in targets]})
        note = f"revert:{change_id or batch}"
        results = []
        for e in targets:
            try:
                results.append(await revert_entry(zd, e, force, note))
            except ZendeskError as exc:
                results.append({"change_id": e.get("id"), "status": "failed", "error": str(exc)})
        summary: dict[str, int] = {}
        for r in results:
            summary[r["status"]] = summary.get(r["status"], 0) + 1
        return tk.out({"summary": summary, "results": results, "batch": note})
