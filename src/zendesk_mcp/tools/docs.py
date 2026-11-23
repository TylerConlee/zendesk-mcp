"""Configuration documentation: a human-readable account of what every part of the configuration does.

Rules are rendered as plain sentences ("When Status is Open and Group is Tier 2 -> Set Priority to High") using
Zendesk's own field/operator/value labels from the definitions endpoints, with every id resolved to a name.
Each object lists what uses it, and a findings section flags likely problems (references to deleted objects,
unused fields and groups, rules that never fire, duplicate names, active triggers calling inactive webhooks)."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import Field

from ..client import ZendeskClient, ZendeskError
from ..toolkit import Toolkit
from .admin import item_references, rule_parts
from .advanced import _confine
from .changes import SNAPSHOT_SECTIONS

DOC_SECTIONS = ("overview", "settings", "groups", "agents", "custom_roles", "brands", "ticket_fields", "user_fields",
                "organization_fields", "ticket_forms", "custom_statuses", "schedules", "sla_policies", "triggers",
                "automations", "views", "macros", "webhooks", "routing", "cross_references", "findings")
DocSection = Literal[DOC_SECTIONS]  # type: ignore[valid-type]

DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
OPERATORS = {"is": "is", "is_not": "is not", "less_than": "is less than", "greater_than": "is greater than",
             "changed": "changed", "value": "changed to", "value_previous": "changed from",
             "not_changed": "did not change", "not_value": "did not change to",
             "not_value_previous": "did not change from", "includes": "includes", "not_includes": "does not include",
             "present": "is present", "not_present": "is not present", "within_previous_n_days":
             "is within the previous (days)", "is_business_hours": "is within business hours",
             "is_not_business_hours": "is outside business hours", "less_than_business_hours":
             "is less than (business hours)", "greater_than_business_hours": "is greater than (business hours)"}
TERMINAL = {"changed", "not_changed", "present", "not_present", "is_business_hours", "is_not_business_hours"}
TIME_FIELDS = {"NEW": "Hours since created", "OPEN": "Hours since open", "PENDING": "Hours since pending",
               "HOLD": "Hours since on-hold", "SOLVED": "Hours since solved", "CLOSED": "Hours since closed",
               "assigned_at": "Hours since assigned", "updated_at": "Hours since update",
               "requester_updated_at": "Hours since requester update", "assignee_updated_at":
               "Hours since assignee update", "due_date": "Hours since due date", "until_due_date": "Hours until due date",
               "until_sla_next_breach_at": "Hours until next SLA breach"}
FALLBACK_TITLES = {"status": "Status", "type": "Type", "priority": "Priority", "group_id": "Group",
                   "assignee_id": "Assignee", "requester_id": "Requester", "organization_id": "Organization",
                   "current_tags": "Tags", "set_tags": "Set tags", "add_tags": "Add tags", "remove_tags": "Remove tags",
                   "update_type": "Ticket", "via_id": "Channel", "brand_id": "Brand", "ticket_form_id": "Form",
                   "custom_status_id": "Ticket status", "comment_value": "Comment", "comment_value_html": "Comment",
                   "comment_mode_is_public": "Comment is public", "subject": "Subject", "satisfaction_score":
                   "Satisfaction", "role": "Current user role", "recipient": "Received at", "description_includes_word":
                   "Subject/description contains", "comment_includes_word": "Comment contains", "set_schedule":
                   "Schedule", "schedule_id": "Schedule"}
ID_KINDS = {"group_id": "group", "assignee_id": "user", "requester_id": "user", "submitter_id": "user",
            "organization_id": "org", "brand_id": "brand", "ticket_form_id": "form", "custom_status_id": "status",
            "set_schedule": "schedule", "schedule_id": "schedule"}
SPECIAL_USERS = {"current_user": "(current user)", "requester_id": "(requester)", "assignee_id": "(assignee)",
                 "all_agents": "(all agents)", "requester_and_ccs": "(requester and CCs)", "": "(none)"}
RULE_KINDS = ("triggers", "automations", "views", "macros", "sla_policies")


def _minutes(m: Any) -> str:
    try:
        m = int(m)
    except (TypeError, ValueError):
        return str(m)
    if m % 1440 == 0 and m >= 1440:
        return f"{m // 1440} d"
    if m % 60 == 0 and m >= 60:
        return f"{m // 60} h"
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"


def _clock(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def describe_hours(intervals: list[dict]) -> str:
    per_day: dict[int, list[str]] = defaultdict(list)
    for i in sorted(intervals or [], key=lambda x: x.get("start_time", 0)):
        s, e = int(i.get("start_time", 0)), int(i.get("end_time", 0))
        day = s // 1440
        per_day[day].append(f"{_clock(s - day * 1440)}–{_clock(e - day * 1440)}")
    if not per_day:
        return "No hours set"
    parts, run = [], []
    for d in range(7):
        hours = ", ".join(per_day.get(d, []))
        if run and run[-1][1] == hours and run[-1][2] == d - 1 and hours:
            run[-1][2] = d
        elif hours:
            run.append([d, hours, d])
    for start, hours, end in run:
        days = DAYS[start] if start == end else f"{DAYS[start]}–{DAYS[end]}"
        parts.append(f"{days} {hours}")
    return "; ".join(parts)


class Describer:
    """Turns conditions/actions into sentences using definitions + name maps; records unresolved ids."""

    def __init__(self, defs: list[dict], names: dict[str, dict[str, str]], field_titles: dict[str, str],
                 options: dict[str, dict[str, str]]):
        self.names, self.field_titles, self.options = names, field_titles, options
        self.titles: dict[str, str] = dict(FALLBACK_TITLES)
        self.ops: dict[str, str] = dict(OPERATORS)
        self.values: dict[str, dict[str, str]] = {}
        for d in defs:
            subject = d.get("subject") or d.get("value")
            if not isinstance(subject, str):
                continue
            if d.get("title"):
                self.titles.setdefault(subject, d["title"])
            for op in d.get("operators") or []:
                if isinstance(op, dict) and op.get("value") and op.get("title"):
                    self.ops.setdefault(op["value"], op["title"].lower())
            vals = d.get("values")
            if isinstance(vals, dict):
                vals = vals.get("list")
            for v in vals or []:
                if isinstance(v, dict) and v.get("value") is not None and v.get("title"):
                    self.values.setdefault(subject, {})[str(v["value"])] = v["title"]
        self.unresolved: list[tuple[str, str]] = []

    def field_title(self, field: str) -> str:
        if field.startswith("custom_fields_"):
            return self.field_titles.get(field[14:], f"Custom field {field[14:]} (deleted?)")
        base = field.removesuffix("_business_hours")
        if base in TIME_FIELDS:
            return TIME_FIELDS[base] + (" (business hours)" if field.endswith("_business_hours") else "")
        return self.titles.get(field, field.replace("_", " ").capitalize())

    def value(self, field: str, v: Any) -> str:
        if isinstance(v, list):
            return ", ".join(self.value(field, x) for x in v)
        sv = "" if v is None else str(v)
        if field.startswith("custom_fields_"):
            opts = self.options.get(field[14:], {})
            return f"“{opts[sv]}”" if sv in opts else f"“{sv}”"
        kind = ID_KINDS.get(field)
        if kind:
            if sv in SPECIAL_USERS:
                return SPECIAL_USERS[sv]
            name = self.names.get(kind, {}).get(sv)
            if name:
                return f"“{name}”"
            if self.values.get(field, {}).get(sv):
                return self.values[field][sv]
            if kind in ("group", "brand", "form", "status", "schedule") and sv.isdigit():
                self.unresolved.append((kind, sv))
                return f"{kind} {sv} (not found)"
            return sv
        if field in self.values and sv in self.values[field]:
            return self.values[field][sv]
        if field in ("set_tags", "add_tags", "remove_tags", "current_tags"):
            return ", ".join(f"`{t}`" for t in sv.split())
        return f"“{sv}”" if sv else "(empty)"

    def condition(self, c: dict) -> str:
        field = str(c.get("field") or "")
        op = str(c.get("operator") or "is")
        title = self.field_title(field)
        if field.removesuffix("_business_hours") in TIME_FIELDS:
            return f"{title} {self.ops.get(op, op)} {c.get('value')}"
        if op in TERMINAL:
            return f"{title} {self.ops.get(op, op)}"
        return f"{title} {self.ops.get(op, op.replace('_', ' '))} {self.value(field, c.get('value'))}"

    def action(self, a: dict) -> str:
        field = str(a.get("field") or "")
        v = a.get("value")
        vals = v if isinstance(v, list) else [v]
        if field == "notification_webhook":
            name = self.names.get("webhook", {}).get(str(vals[0]))
            if not name:
                self.unresolved.append(("webhook", str(vals[0])))
            body = " ".join(str(vals[1]).split())[:140] if len(vals) > 1 and vals[1] else ""
            return f"Notify webhook “{name or vals[0]}”" + (f" with payload: `{body}`" if body else "")
        if field == "notification_target":
            name = self.names.get("target", {}).get(str(vals[0]), vals[0])
            return f"Notify target “{name}”"
        if field in ("notification_user", "notification_group", "notification_sms_user", "notification_sms_group"):
            who = str(vals[0]) if vals else ""
            if field.endswith("group"):
                label = SPECIAL_USERS.get(who) or self.names.get("group", {}).get(who) or \
                    self.values.get(field, {}).get(who) or f"group {who}"
            else:
                label = SPECIAL_USERS.get(who) or self.names.get("user", {}).get(who) or \
                    self.values.get(field, {}).get(who) or who
            subject = f": “{vals[1]}”" if len(vals) > 1 and vals[1] and "sms" not in field else ""
            return f"{'Text' if 'sms' in field else 'Email'} {label}{subject}"
        if field in ("comment_value", "comment_value_html"):
            text = v[1] if isinstance(v, list) and len(v) > 1 else v
            return f"Add comment: “{' '.join(str(text).split())[:160]}”"
        if field in ("set_tags", "add_tags", "remove_tags"):
            return f"{self.field_title(field)}: {self.value(field, v)}"
        return f"{self.field_title(field)} → {self.value(field, v)}"

    def conditions(self, cond: dict | None) -> dict[str, list[str]]:
        cond = cond or {}
        return {"all": [self.condition(c) for c in cond.get("all") or [] if isinstance(c, dict)],
                "any": [self.condition(c) for c in cond.get("any") or [] if isinstance(c, dict)]}

    def restriction(self, r: dict | None) -> str:
        if not r:
            return "All agents"
        ids = [str(x) for x in (r.get("ids") or ([r["id"]] if r.get("id") is not None else []))]
        if r.get("type") == "Group":
            return "Groups: " + ", ".join(self.names.get("group", {}).get(i, f"group {i}") for i in ids)
        if r.get("type") == "User":
            return "Personal: " + ", ".join(self.names.get("user", {}).get(i, f"user {i}") for i in ids)
        return str(r)


# ----------------------------------------------------------------------------- data loading

async def _try_list(zd: ZendeskClient, name: str, usage: bool) -> tuple[list[dict] | dict | None, str | None]:
    path, key, cursor, params = SNAPSHOT_SECTIONS[name]
    if name == "account_settings":
        try:
            return (await zd.get(path)).get("settings", {}), None
        except ZendeskError as exc:
            return None, str(exc)[:200]
    attempts = [dict(params or {}, include="usage_30d")] if usage else []
    attempts.append(params)
    err = None
    for p in attempts:
        try:
            return (await zd.paginate(path, key, params=p, limit=100000, cursor=cursor))[key], None
        except ZendeskError as exc:
            err = str(exc)[:200]
    return None, err


async def _definitions(zd: ZendeskClient) -> list[dict]:
    out: list[dict] = []
    for path, pick in (("triggers/definitions", ("conditions_all", "conditions_any", "actions")),
                       ("views/definitions", ("conditions_all", "conditions_any")),
                       ("macros/actions", None)):
        try:
            body = await zd.get(path)
        except ZendeskError:
            continue
        if pick is None:
            out += [d for d in body.get("actions") or [] if isinstance(d, dict)]
        else:
            defs = body.get("definitions") or {}
            for k in pick:
                out += [d for d in defs.get(k) or [] if isinstance(d, dict)]
    return out


# ----------------------------------------------------------------------------- document model

def build_doc(host: str, live: dict[str, Any], defs: list[dict], unavailable: dict[str, str],
              include_agents: bool, include_inactive: bool) -> dict[str, Any]:
    names: dict[str, dict[str, str]] = defaultdict(dict)
    for g in live.get("groups") or []:
        names["group"][str(g["id"])] = g.get("name")
    for u in live.get("agents") or []:
        names["user"][str(u["id"])] = u.get("name")
    for b in live.get("brands") or []:
        names["brand"][str(b["id"])] = b.get("name")
    for f in live.get("ticket_forms") or []:
        names["form"][str(f["id"])] = f.get("name")
    for s in live.get("custom_statuses") or []:
        names["status"][str(s["id"])] = s.get("agent_label")
    for s in live.get("schedules") or []:
        names["schedule"][str(s["id"])] = s.get("name")
    for w in live.get("webhooks") or []:
        names["webhook"][str(w["id"])] = w.get("name")
    for t in live.get("targets") or []:
        names["target"][str(t["id"])] = t.get("title")
    for c in live.get("trigger_categories") or []:
        names["category"][str(c["id"])] = c.get("name")
    for r in live.get("custom_roles") or []:
        names["role"][str(r["id"])] = r.get("name")
    field_titles = {str(f["id"]): f.get("title") for f in live.get("ticket_fields") or []}
    options = {str(f["id"]): {str(o.get("value")): o.get("name") for o in f.get("custom_field_options") or []}
               for f in live.get("ticket_fields") or []}
    d = Describer(defs, names, field_titles, options)
    doc: dict[str, Any] = {"instance": host, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "unavailable": unavailable}
    keep = (lambda x: True) if include_inactive else (lambda x: x.get("active", True) is not False)  # noqa: E731

    # --- memberships
    members: dict[str, list[str]] = defaultdict(list)
    defaults: dict[str, int] = defaultdict(int)
    for m in live.get("group_memberships") or []:
        members[str(m["group_id"])].append(str(m["user_id"]))
        if m.get("default"):
            defaults[str(m["group_id"])] += 1

    # --- rules
    def usage(r: dict) -> Any:
        u = r.get("usage_30d")
        return u if isinstance(u, int) else None

    cat_pos = {str(c["id"]): (c.get("position") or 0, n) for n, c in enumerate(live.get("trigger_categories") or [])}
    triggers = sorted(filter(keep, live.get("triggers") or []),
                      key=lambda r: (cat_pos.get(str(r.get("category_id")), (10**9, 0)), r.get("position") or 0))
    doc["triggers"] = [{"id": r["id"], "title": r.get("title"), "active": r.get("active"), "position": r.get("position"),
                        "category": names["category"].get(str(r.get("category_id"))), "description": r.get("description"),
                        "when": d.conditions(r.get("conditions")), "then": [d.action(a) for a in r.get("actions") or []],
                        "fired_last_30d": usage(r), "updated_at": r.get("updated_at")} for r in triggers]
    doc["automations"] = [{"id": r["id"], "title": r.get("title"), "active": r.get("active"),
                           "position": r.get("position"), "when": d.conditions(r.get("conditions")),
                           "then": [d.action(a) for a in r.get("actions") or []], "fired_last_30d": usage(r),
                           "updated_at": r.get("updated_at")}
                          for r in sorted(filter(keep, live.get("automations") or []), key=lambda r: r.get("position") or 0)]
    doc["views"] = []
    for r in sorted(filter(keep, live.get("views") or []), key=lambda r: r.get("position") or 0):
        out = r.get("execution") or r.get("output") or {}
        cols = [c.get("title") or c.get("id") if isinstance(c, dict) else c for c in out.get("columns") or []]
        doc["views"].append({"id": r["id"], "title": r.get("title"), "active": r.get("active"),
                             "position": r.get("position"), "visible_to": d.restriction(r.get("restriction")),
                             "shows_tickets_where": d.conditions(r.get("conditions")), "columns": cols,
                             "grouped_by": (out.get("group") or {}).get("title") if isinstance(out.get("group"), dict)
                             else out.get("group_by"),
                             "sorted_by": (out.get("sort") or {}).get("title") if isinstance(out.get("sort"), dict)
                             else out.get("sort_by"), "updated_at": r.get("updated_at")})
    doc["macros"] = [{"id": r["id"], "title": r.get("title"), "active": r.get("active"),
                      "available_to": d.restriction(r.get("restriction")), "description": r.get("description"),
                      "does": [d.action(a) for a in r.get("actions") or []], "used_last_30d": usage(r),
                      "updated_at": r.get("updated_at")}
                     for r in sorted(filter(keep, live.get("macros") or []), key=lambda r: (r.get("title") or "").lower())]
    doc["sla_policies"] = []
    for r in sorted(live.get("sla_policies") or [], key=lambda r: r.get("position") or 0):
        targets = [{"priority": m.get("priority"), "metric": str(m.get("metric")).replace("_", " "),
                    "target": _minutes(m.get("target")), "hours": "business" if m.get("business_hours") else "calendar"}
                   for m in r.get("policy_metrics") or []]
        doc["sla_policies"].append({"id": r["id"], "title": r.get("title"), "position": r.get("position"),
                                    "description": r.get("description"), "applies_when": d.conditions(r.get("filter")),
                                    "targets": targets})

    # --- config objects
    doc["groups"] = [{"id": g["id"], "name": g.get("name"), "description": g.get("description"),
                      "members": len(members.get(str(g["id"]), [])), "default_for": defaults.get(str(g["id"]), 0),
                      "is_public": g.get("is_public")} for g in sorted(live.get("groups") or [],
                                                                       key=lambda g: (g.get("name") or "").lower())]
    agents = live.get("agents") or []
    doc["agents"] = {"total": len(agents),
                     "by_role": dict(sorted(defaultdict(int, {k: sum(1 for a in agents if a.get("role") == k)
                                                             for k in {a.get("role") for a in agents}}).items())),
                     "by_custom_role": {names["role"].get(str(k), str(k)): sum(1 for a in agents
                                                                               if a.get("custom_role_id") == k)
                                        for k in {a.get("custom_role_id") for a in agents} if k},
                     "suspended": sum(1 for a in agents if a.get("suspended"))}
    if include_agents:
        by_user: dict[str, list[str]] = defaultdict(list)
        for gid, uids in members.items():
            for u in uids:
                by_user[u].append(names["group"].get(gid, gid))
        doc["agents"]["list"] = [{"name": a.get("name"), "email": a.get("email"), "role": a.get("role"),
                                  "custom_role": names["role"].get(str(a.get("custom_role_id"))),
                                  "groups": sorted(by_user.get(str(a["id"]), [])), "suspended": a.get("suspended")}
                                 for a in sorted(agents, key=lambda a: (a.get("name") or "").lower())]
    doc["custom_roles"] = [{"name": r.get("name"), "description": r.get("description"),
                            "agents": sum(1 for a in agents if a.get("custom_role_id") == r["id"])}
                           for r in live.get("custom_roles") or []]
    doc["brands"] = [{"name": b.get("name"), "subdomain": b.get("subdomain"), "active": b.get("active"),
                      "default": b.get("default")} for b in live.get("brands") or []]

    def field_doc(f: dict) -> dict[str, Any]:
        row = {"id": f.get("id"), "title": f.get("title"), "key": f.get("key"), "type": f.get("type"),
               "active": f.get("active"), "description": f.get("description") or f.get("agent_description"),
               "required": f.get("required"), "required_in_portal": f.get("required_in_portal"),
               "visible_in_portal": f.get("visible_in_portal")}
        if f.get("custom_field_options"):
            row["options"] = [f"{o.get('name')} (`{o.get('value')}`)" for o in f["custom_field_options"]]
        return {k: v for k, v in row.items() if v not in (None, "")}

    doc["ticket_fields"] = [field_doc(f) for f in live.get("ticket_fields") or [] if f.get("removable", True) and keep(f)]
    doc["system_ticket_fields"] = [f.get("title") for f in live.get("ticket_fields") or [] if not f.get("removable", True)]
    doc["user_fields"] = [field_doc(f) for f in live.get("user_fields") or [] if keep(f)]
    doc["organization_fields"] = [field_doc(f) for f in live.get("organization_fields") or [] if keep(f)]

    doc["ticket_forms"] = []
    for f in sorted(live.get("ticket_forms") or [], key=lambda f: f.get("position") or 0):
        if not keep(f):
            continue
        logic = []
        for audience, key in (("agents", "agent_conditions"), ("end users", "end_user_conditions")):
            for c in f.get(key) or []:
                parent = str(c.get("parent_field_id"))
                val = options.get(parent, {}).get(str(c.get("value")), c.get("value"))
                kids = [field_titles.get(str(ch.get("id")), ch.get("id")) +
                        (" (required)" if ch.get("is_required") or (ch.get("required_on_statuses") or {}).get("type")
                         not in (None, "NO_STATUSES") else "") for ch in c.get("child_fields") or []]
                logic.append(f"For {audience}: when {field_titles.get(parent, parent)} is “{val}”, show "
                             + ", ".join(str(k) for k in kids))
        doc["ticket_forms"].append({"id": f["id"], "name": f.get("name"), "display_name": f.get("display_name"),
                                    "active": f.get("active"), "default": f.get("default"),
                                    "end_user_visible": f.get("end_user_visible"),
                                    "fields": [field_titles.get(str(i), f"field {i}") for i in f.get("ticket_field_ids") or []],
                                    "conditional_logic": logic})
    doc["custom_statuses"] = [{"category": s.get("status_category"), "agent_label": s.get("agent_label"),
                               "end_user_label": s.get("end_user_label"), "active": s.get("active"),
                               "default": s.get("default"), "description": s.get("description")}
                              for s in sorted(live.get("custom_statuses") or [],
                                              key=lambda s: (["new", "open", "pending", "hold", "solved"].index(
                                                  s.get("status_category")) if s.get("status_category") in
                                                  ["new", "open", "pending", "hold", "solved"] else 9,
                                                  s.get("agent_label") or ""))]
    doc["schedules"] = [{"id": s["id"], "name": s.get("name"), "time_zone": s.get("time_zone"),
                         "hours": describe_hours(s.get("intervals") or []),
                         "holidays": [f"{h.get('name')} ({h.get('start_date')}"
                                      + (f" – {h.get('end_date')}" if h.get("end_date") != h.get("start_date") else "") + ")"
                                      for h in s.get("holidays") or []]} for s in live.get("schedules") or []]
    doc["webhooks"] = [{"id": w["id"], "name": w.get("name"), "status": w.get("status"),
                        "endpoint": w.get("endpoint"), "method": w.get("http_method"), "format": w.get("request_format"),
                        "subscriptions": w.get("subscriptions")} for w in live.get("webhooks") or []]
    doc["routing"] = {"skills": [{"name": a.get("name"), "values": [v.get("name") for v in a.get("values") or []]}
                                 for a in live.get("routing_attributes") or []],
                      "queues": [{"name": q.get("name"), "priority": q.get("priority"),
                                  "primary_groups": [names["group"].get(str(g.get("id") if isinstance(g, dict) else g),
                                                                        str(g)) for g in q.get("primary_groups") or []]}
                                 for q in live.get("queues") or []]}
    settings = live.get("account_settings") or {}
    doc["settings"] = {sec: settings[sec] for sec in ("tickets", "agents", "routing", "active_features", "rules",
                                                      "localization", "user") if isinstance(settings.get(sec), dict)}

    # --- cross references
    rules = {k: live.get(k) or [] for k in RULE_KINDS}
    xref: dict[str, dict[str, list[str]]] = {}

    def users_of(ref_type: str, ident: str) -> list[str]:
        hits = []
        for kind, items in rules.items():
            for r in items:
                where = [loc for loc, p in rule_parts(kind, r) if item_references(p, ref_type, ident)]
                restr = r.get("restriction") if isinstance(r.get("restriction"), dict) else None
                if ref_type == "group" and restr and restr.get("type") == "Group" and \
                        ident in [str(x) for x in restr.get("ids") or []]:
                    where.append("visibility")
                if where:
                    state = "" if r.get("active", True) else " (inactive)"
                    hits.append(f"{kind[:-1].replace('_', ' ')} “{r.get('title')}”{state}")
        return hits

    for ref_type, kind, items, label in (("group", "group", live.get("groups"), "name"),
                                         ("ticket_field", "field", [f for f in live.get("ticket_fields") or []
                                                                    if f.get("removable", True)], "title"),
                                         ("custom_status", "status", live.get("custom_statuses"), "agent_label"),
                                         ("ticket_form", "form", live.get("ticket_forms"), "name"),
                                         ("brand", "brand", live.get("brands"), "name"),
                                         ("schedule", "schedule", live.get("schedules"), "name"),
                                         ("webhook", "webhook", live.get("webhooks"), "name")):
        section = {}
        for it in items or []:
            used = users_of(ref_type, str(it["id"]))
            if ref_type == "ticket_field":
                used += [f"form “{f.get('name')}”" for f in live.get("ticket_forms") or []
                         if it["id"] in (f.get("ticket_field_ids") or [])]
            section[str(it.get(label))] = used
        xref[f"{ref_type}s"] = section
    doc["cross_references"] = xref

    # --- findings
    findings: list[dict[str, str]] = []

    def add(severity: str, area: str, message: str) -> None:
        findings.append({"severity": severity, "area": area, "message": message})

    for kind, ident in sorted(set(d.unresolved)):
        add("high", "references", f"Rules refer to {kind} {ident}, which doesn't exist (deleted?). "
                                  f"Run find_references(ref_type='{kind}', value={ident}).")
    for kind in ("triggers", "automations", "views", "macros"):
        seen: dict[str, int] = defaultdict(int)
        for r in rules[kind]:
            seen[(r.get("title") or "").strip().lower()] += 1
        for title, n in seen.items():
            if n > 1 and title:
                add("medium", kind, f"{n} {kind} share the title “{title}”.")
        inactive = [r.get("title") for r in rules[kind] if r.get("active") is False]
        if inactive:
            add("info", kind, f"{_n(len(inactive), 'inactive ' + kind[:-1])}: {inactive[:10]}"
                              f"{'…' if len(inactive) > 10 else ''}")
    for kind, key in (("triggers", "fired_last_30d"), ("automations", "fired_last_30d"), ("macros", "used_last_30d")):
        idle = [r["title"] for r in doc[kind] if r.get("active") and r.get(key) == 0]
        if idle:
            add("low", kind, f"{_n(len(idle), 'active ' + kind[:-1])} unused in the last 30 days: {idle[:10]}"
                             f"{'…' if len(idle) > 10 else ''}")
    for g in doc["groups"]:
        if g["members"] == 0 and not xref["groups"].get(str(g["name"])):
            add("low", "groups", f"Group “{g['name']}” has no members and no rule uses it.")
        elif g["members"] == 0:
            add("medium", "groups", f"Group “{g['name']}” has no members but rules route tickets to/through it.")
    in_forms = {i for f in live.get("ticket_forms") or [] for i in f.get("ticket_field_ids") or []}
    for f in live.get("ticket_fields") or []:
        if f.get("removable", True) and f.get("active") and f["id"] not in in_forms and \
                not xref["ticket_fields"].get(str(f.get("title"))):
            add("low", "ticket_fields", f"Field “{f.get('title')}” is active but on no form and used by no rule.")
    hook_status = {str(w["id"]): w.get("status") for w in live.get("webhooks") or []}
    for r in rules["triggers"]:
        if r.get("active") is False:
            continue
        for _, p in rule_parts("triggers", r):
            if p.get("field") == "notification_webhook":
                hid = str((p.get("value") or [None])[0])
                if hook_status.get(hid) == "inactive":
                    add("high", "webhooks", f"Active trigger “{r.get('title')}” calls inactive webhook "
                                            f"“{names['webhook'].get(hid, hid)}”.")
    for r in rules["triggers"] + rules["automations"]:
        cond = r.get("conditions") or {}
        if r.get("active") is not False and not (cond.get("all") or cond.get("any")):
            add("medium", "rules", f"“{r.get('title')}” has no conditions, so it acts on every ticket update.")
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    doc["findings"] = sorted(findings, key=lambda f: (order[f["severity"]], f["area"]))
    doc["overview"] = {k: len(v) if isinstance(v, list) else v for k, v in {
        "groups": doc["groups"], "agents": doc["agents"]["total"], "ticket_fields": doc["ticket_fields"],
        "ticket_forms": doc["ticket_forms"], "custom_statuses": doc["custom_statuses"], "schedules": doc["schedules"],
        "sla_policies": doc["sla_policies"], "triggers": doc["triggers"], "automations": doc["automations"],
        "views": doc["views"], "macros": doc["macros"], "webhooks": doc["webhooks"]}.items()}
    doc["overview"]["active"] = {k: sum(1 for r in rules[k] if r.get("active") is not False)
                                 for k in ("triggers", "automations", "views", "macros")}
    doc["overview"]["findings"] = {s: sum(1 for f in findings if f["severity"] == s) for s in order}
    return doc


# ----------------------------------------------------------------------------- markdown

def _n(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _md_escape(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _when(cond: dict[str, list[str]]) -> list[str]:
    lines = []
    if cond.get("all"):
        lines.append("- **All of:**")
        lines += [f"  - {c}" for c in cond["all"]]
    if cond.get("any"):
        lines.append("- **Any of:**")
        lines += [f"  - {c}" for c in cond["any"]]
    return lines or ["- *(no conditions)*"]


def render_markdown(doc: dict[str, Any], sections: list[str]) -> str:
    out = [f"# Zendesk configuration — {doc['instance']}", "",
           f"_Generated {doc['generated_at']} by zendesk-mcp. Rules are described in plain language from Zendesk's own "
           "field and operator labels; ids are resolved to names._", ""]
    toc = [s for s in sections if s not in ("overview",)]
    out += ["**Contents:** " + " · ".join(f"[{s.replace('_', ' ').title()}](#{s.replace('_', '-')})" for s in toc), ""]

    def h(title: str, anchor: str) -> None:
        out.extend([f'<a id="{anchor}"></a>', f"## {title}", ""])

    if "overview" in sections:
        ov = doc["overview"]
        out += ["## Overview", "", "| Area | Count |", "|---|---|"]
        out += [f"| {k.replace('_', ' ').title()} | {v} |" for k, v in ov.items() if isinstance(v, int)]
        act = ov["active"]
        out += ["", "Active: " + ", ".join(_n(v, k[:-1]) for k, v in act.items()) + ".", "",
                "Findings: " + ", ".join(f"{v} {k}" for k, v in ov["findings"].items() if v) + "." if any(
                    ov["findings"].values()) else "No findings.", ""]
        if doc.get("unavailable"):
            out += ["_Not readable with these credentials/plan: " + ", ".join(doc["unavailable"]) + "._", ""]
    if "findings" in sections:
        h("Findings", "findings")
        if not doc["findings"]:
            out += ["Nothing flagged.", ""]
        else:
            out += ["| Severity | Area | Finding |", "|---|---|---|"]
            out += [f"| {f['severity']} | {f['area']} | {_md_escape(f['message'])} |" for f in doc["findings"]]
            out.append("")
    if "settings" in sections and doc["settings"]:
        h("Account settings", "settings")
        for sec, vals in doc["settings"].items():
            out.append(f"**{sec.replace('_', ' ').title()}**: " + "; ".join(
                f"{k.replace('_', ' ')} = `{v}`" for k, v in vals.items() if not isinstance(v, (dict, list))))
            out.append("")
    if "groups" in sections:
        h("Groups", "groups")
        out += ["| Group | Members | Default for | Used by | Description |", "|---|---|---|---|---|"]
        xr = doc["cross_references"].get("groups", {})
        for g in doc["groups"]:
            used = xr.get(str(g["name"]), [])
            out.append(f"| {_md_escape(g['name'])} | {g['members']} | {g['default_for']} | {len(used)} rules | "
                       f"{_md_escape(g.get('description') or '')} |")
        out.append("")
    if "agents" in sections:
        h("Agents and roles", "agents")
        a = doc["agents"]
        out.append(f"{_n(a['total'], 'agent')} ({', '.join(f'{v} {k}' for k, v in a['by_role'].items())}); "
                   f"{a['suspended']} suspended.")
        if a["by_custom_role"]:
            out.append("By custom role: " + ", ".join(f"{k} {v}" for k, v in a["by_custom_role"].items()) + ".")
        out.append("")
        if a.get("list"):
            out += ["| Name | Email | Role | Custom role | Groups |", "|---|---|---|---|---|"]
            out += [f"| {_md_escape(x['name'])} | {x['email']} | {x['role']} | {x.get('custom_role') or ''} | "
                    f"{_md_escape(', '.join(x['groups']))} |" for x in a["list"]]
            out.append("")
    if "custom_roles" in sections and doc["custom_roles"]:
        h("Custom roles", "custom-roles")
        out += [f"- **{r['name']}** ({r['agents']} agents){': ' + r['description'] if r.get('description') else ''}"
                for r in doc["custom_roles"]] + [""]
    if "brands" in sections and doc["brands"]:
        h("Brands", "brands")
        out += [f"- **{b['name']}** — `{b.get('subdomain')}`{' (default)' if b.get('default') else ''}"
                f"{'' if b.get('active', True) else ' (inactive)'}" for b in doc["brands"]] + [""]
    for key, title in (("ticket_fields", "Ticket fields"), ("user_fields", "User fields"),
                       ("organization_fields", "Organization fields")):
        if key in sections and doc[key]:
            h(title, key.replace("_", "-"))
            xr = doc["cross_references"].get("ticket_fields", {}) if key == "ticket_fields" else {}
            for f in doc[key]:
                flags = [x for x in ("required" if f.get("required") else "",
                                     "inactive" if f.get("active") is False else "") if x]
                out += [f"### {f['title']}{' — ' + ', '.join(flags) if flags else ''}", ""]
                if f.get("description"):
                    out += [f"> {_md_escape(f['description'])}", ""]
                out.append(f"- Type: `{f.get('type')}`" + (f" · key `{f['key']}`" if f.get("key") else "") +
                           (f" · id {f['id']}" if f.get("id") else ""))
                if f.get("options"):
                    out.append("- Options: " + ", ".join(f["options"]))
                if key == "ticket_fields":
                    used = xr.get(str(f["title"]), [])
                    out.append("- Used by: " + (", ".join(used) if used else "_nothing_"))
                out.append("")
            if key == "ticket_fields" and doc.get("system_ticket_fields"):
                out += ["System fields: " + ", ".join(doc["system_ticket_fields"]), ""]
    if "ticket_forms" in sections and doc["ticket_forms"]:
        h("Ticket forms", "ticket-forms")
        for f in doc["ticket_forms"]:
            tags = [x for x in ("default" if f.get("default") else "", "inactive" if f.get("active") is False else "",
                                "agents only" if f.get("end_user_visible") is False else "") if x]
            out += [f"### {f['name']}{' — ' + ', '.join(tags) if tags else ''}", "",
                    "- Fields, in order: " + ", ".join(str(x) for x in f["fields"])]
            out += [f"- {line}" for line in f["conditional_logic"]]
            out.append("")
    if "custom_statuses" in sections and doc["custom_statuses"]:
        h("Ticket statuses", "custom-statuses")
        out += ["| Category | Agent label | Customer label | Active |", "|---|---|---|---|"]
        out += [f"| {s['category']} | {_md_escape(s['agent_label'])}{' (default)' if s.get('default') else ''} | "
                f"{_md_escape(s['end_user_label'])} | {'yes' if s.get('active') else 'no'} |" for s in doc["custom_statuses"]]
        out.append("")
    if "schedules" in sections and doc["schedules"]:
        h("Business hours", "schedules")
        for s in doc["schedules"]:
            out += [f"### {s['name']}", "", f"- Hours: {s['hours']} ({s['time_zone']})"]
            if s["holidays"]:
                out.append("- Holidays: " + "; ".join(s["holidays"]))
            out.append("")
    if "sla_policies" in sections and doc["sla_policies"]:
        h("SLA policies (evaluated top to bottom; first match applies)", "sla-policies")
        for i, p in enumerate(doc["sla_policies"], 1):
            out += [f"### {i}. {p['title']}", ""]
            if p.get("description"):
                out += [f"> {_md_escape(p['description'])}", ""]
            out.append("**Applies when:**")
            out += _when(p["applies_when"])
            if p["targets"]:
                out += ["", "| Priority | Metric | Target | Hours |", "|---|---|---|---|"]
                out += [f"| {t['priority']} | {t['metric']} | {t['target']} | {t['hours']} |" for t in p["targets"]]
            out.append("")
    if "triggers" in sections and doc["triggers"]:
        h("Triggers (run in this order on every ticket create/update)", "triggers")
        current_cat = object()
        for i, r in enumerate(doc["triggers"], 1):
            if r.get("category") != current_cat and any(t.get("category") for t in doc["triggers"]):
                current_cat = r.get("category")
                out += [f"#### Category: {current_cat or '(none)'}", ""]
            state = "" if r.get("active") else " — *inactive*"
            out += [f"### {i}. {r['title']}{state}", ""]
            if r.get("description"):
                out += [f"> {_md_escape(r['description'])}", ""]
            out.append("**When:**")
            out += _when(r["when"])
            out += ["", "**Then:**"]
            out += [f"- {a}" for a in r["then"]] or ["- *(no actions)*"]
            out.append("")
            meta = [f"id {r['id']}"]
            if r.get("fired_last_30d") is not None:
                meta.append(f"fired {r['fired_last_30d']}× in 30 days")
            if r.get("updated_at"):
                meta.append(f"updated {str(r['updated_at'])[:10]}")
            out += [f"_{' · '.join(meta)}_", ""]
    if "automations" in sections and doc["automations"]:
        h("Automations (checked hourly against open tickets)", "automations")
        for i, r in enumerate(doc["automations"], 1):
            out += [f"### {i}. {r['title']}{'' if r.get('active') else ' — *inactive*'}", ""]
            out.append("**When:**")
            out += _when(r["when"])
            out += ["", "**Then:**"]
            out += [f"- {a}" for a in r["then"]]
            out.append("")
            meta = [f"id {r['id']}"] + ([f"fired {r['fired_last_30d']}× in 30 days"]
                                        if r.get("fired_last_30d") is not None else [])
            out += [f"_{' · '.join(meta)}_", ""]
    if "views" in sections and doc["views"]:
        h("Views", "views")
        for r in doc["views"]:
            out += [f"### {r['title']}{'' if r.get('active') else ' — *inactive*'}", ""]
            out += [f"Visible to: {r['visible_to']}", ""]
            out.append("**Shows tickets where:**")
            out += _when(r["shows_tickets_where"])
            out.append("")
            extras = []
            if r.get("columns"):
                extras.append("Columns: " + ", ".join(str(c) for c in r["columns"]))
            if r.get("grouped_by"):
                extras.append(f"grouped by {r['grouped_by']}")
            if r.get("sorted_by"):
                extras.append(f"sorted by {r['sorted_by']}")
            out += ([" · ".join(extras)] if extras else []) + [""]
    if "macros" in sections and doc["macros"]:
        h("Macros", "macros")
        for r in doc["macros"]:
            out += [f"### {r['title']}{'' if r.get('active') else ' — *inactive*'}", ""]
            if r.get("description"):
                out += [f"> {_md_escape(r['description'])}", ""]
            out.append(f"Available to: {r['available_to']}" + (
                f" · used {r['used_last_30d']}× in 30 days" if r.get("used_last_30d") is not None else ""))
            out += [""] + [f"- {a}" for a in r["does"]] + [""]
    if "webhooks" in sections and doc["webhooks"]:
        h("Webhooks", "webhooks")
        xr = doc["cross_references"].get("webhooks", {})
        for w in doc["webhooks"]:
            used = xr.get(str(w["name"]), [])
            out.append(f"- **{w['name']}** ({w.get('status')}) — `{w.get('method')} {w.get('endpoint')}`; "
                       f"called by {', '.join(used) if used else 'nothing'}")
        out.append("")
    if "routing" in sections and (doc["routing"]["skills"] or doc["routing"]["queues"]):
        h("Routing", "routing")
        out += [f"- Skill **{s['name']}**: {', '.join(s['values'])}" for s in doc["routing"]["skills"]]
        out += [f"- Queue **{q['name']}** (priority {q.get('priority')}): primary groups "
                f"{', '.join(q['primary_groups'])}" for q in doc["routing"]["queues"]]
        out.append("")
    if "cross_references" in sections:
        h("Where things are used", "cross-references")
        for kind, items in doc["cross_references"].items():
            used = {k: v for k, v in items.items() if v}
            if not used:
                continue
            out += [f"### {kind.replace('_', ' ').title()}", ""]
            out += [f"- **{k}**: {', '.join(v)}" for k, v in used.items()] + [""]
    return "\n".join(out).rstrip() + "\n"


# ----------------------------------------------------------------------------- tool

def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Document configuration", read_only_hint=False)
    async def document_config(
        sections: Annotated[list[DocSection] | None, Field(description="Sections to include (default all).")] = None,
        format: Annotated[Literal["markdown", "json", "both"], Field(description="Markdown for people, JSON for "
                                                                     "tools/diffing.")] = "both",
        include_inactive: bool = True,
        include_agents: Annotated[bool, Field(description="List agents by name/email (off by default so the document "
                                              "can be shared; counts are always included).")] = False,
        inline: Annotated[bool, Field(description="Return the Markdown in the response too (can be long).")] = False,
        filename: Annotated[str | None, Field(description="Base file name under ZENDESK_FILE_ROOT/docs "
                                              "(default: timestamped).")] = None,
    ) -> str:
        """Write documentation of the whole configuration: what every trigger, automation, view, macro and SLA policy
        does in plain language (ids resolved to names, in firing order with 30-day usage), plus groups, agents and
        roles, fields and options, forms and their conditional logic, statuses, business hours, webhooks, routing,
        key settings, where each group/field/status/form/webhook is used, and findings (broken references, unused
        objects, rules that never fire, duplicates). Saved as Markdown and/or JSON under ZENDESK_FILE_ROOT/docs."""
        chosen = list(sections or DOC_SECTIONS)
        need = set(SNAPSHOT_SECTIONS)  # cross references and name resolution need everything
        live: dict[str, Any] = {}
        unavailable: dict[str, str] = {}
        for name in sorted(need):
            items, err = await _try_list(zd, name, usage=name in ("triggers", "automations", "macros"))
            if err and items is None:
                unavailable[name] = err
                continue
            live[name] = items
        for sch in live.get("schedules") or []:
            try:
                sch["holidays"] = (await zd.get(f"business_hours/schedules/{sch['id']}/holidays")).get("holidays", [])
            except ZendeskError:
                sch["holidays"] = []
        for attr in live.get("routing_attributes") or []:
            try:
                attr["values"] = (await zd.get(f"routing/attributes/{attr['id']}/values")).get("attribute_values", [])
            except ZendeskError:
                attr["values"] = []
        defs = await _definitions(zd)
        doc = build_doc(tk.settings.host, live, defs, unavailable, include_agents, include_inactive)
        md = render_markdown(doc, chosen) if format in ("markdown", "both") else None
        result: dict[str, Any] = {"overview": doc["overview"], "findings": doc["findings"][:40]}
        if len(doc["findings"]) > 40:
            result["findings_note"] = f"{len(doc['findings'])} findings; all are in the document."
        if tk.settings.file_root:
            folder = _confine(tk.settings.file_root, "docs")
            folder.mkdir(parents=True, exist_ok=True)
            base = "".join(ch for ch in (filename or "") if ch.isalnum() or ch in "-_") or \
                f"zendesk-config-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
            files = []
            if md is not None:
                (folder / f"{base}.md").write_text(md)
                files.append(str(folder / f"{base}.md"))
            if format in ("json", "both"):
                (folder / f"{base}.json").write_text(json.dumps({k: doc[k] for k in doc if k in chosen or k in (
                    "instance", "generated_at", "unavailable")}, indent=1, default=str))
                files.append(str(folder / f"{base}.json"))
            result["files"] = files
        if inline or not tk.settings.file_root:
            result["markdown"] = md if md is not None else None
            if md is None:
                result["document"] = {k: doc[k] for k in chosen}
        return tk.out(result)
