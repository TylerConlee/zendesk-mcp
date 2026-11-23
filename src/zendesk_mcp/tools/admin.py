"""Administration: agent provisioning and offboarding, business-rule helpers (definitions, view previews,
reordering, reference checks) and account settings.

Every write goes through ZendeskClient.write with the object's prior state, so each change lands in the
change journal and can be undone with revert_change."""

from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from ..client import ZendeskClient, ZendeskError
from ..toolkit import Toolkit
from .resources import fetch_before

RuleKind = Literal["triggers", "automations", "views", "macros"]
SINGULAR = {"triggers": "trigger", "automations": "automation", "views": "view", "macros": "macro",
            "ticket_forms": "ticket_form", "sla_policies": "sla_policy"}

# Time-based conditions automations accept (Zendesk docs: Automations, "Conditions reference").
AUTOMATION_TIME_CONDITIONS = ["NEW", "OPEN", "PENDING", "SOLVED", "assigned_at", "updated_at", "requester_updated_at",
                              "assignee_updated_at", "due_date", "until_due_date"]


# ----------------------------------------------------------------------------- lookups

async def all_items(zd: ZendeskClient, path: str, key: str, *, cursor: bool = True,
                    params: dict | None = None, limit: int = 20000) -> list[dict]:
    return (await zd.paginate(path, key, params=params, limit=limit, cursor=cursor))[key]


async def group_map(zd: ZendeskClient) -> dict[int, str]:
    return {g["id"]: g.get("name") for g in await all_items(zd, "groups", "groups")}


async def custom_role_map(zd: ZendeskClient) -> dict[int, str]:
    try:
        return {r["id"]: r.get("name") for r in await all_items(zd, "custom_roles", "custom_roles", cursor=False)}
    except ZendeskError:  # custom roles need Enterprise
        return {}


def resolve_name(value: int | str, mapping: dict[int, str], kind: str) -> int:
    """Accept an ID or an exact (case-insensitive) name; refuse ambiguous names."""
    if isinstance(value, int) or str(value).isdigit():
        vid = int(value)
        if mapping and vid not in mapping:
            raise ZendeskError(f"No {kind} with id {vid}.")
        return vid
    wanted = str(value).strip().lower()
    hits = [i for i, n in mapping.items() if (n or "").strip().lower() == wanted]
    if not hits:
        sample = ", ".join(sorted(n for n in mapping.values() if n)[:25])
        raise ZendeskError(f"No {kind} named {value!r}. Known: {sample}{'...' if len(mapping) > 25 else ''}")
    if len(hits) > 1:
        raise ZendeskError(f"{len(hits)} {kind}s are named {value!r} (ids {hits}); use an id.")
    return hits[0]


async def find_user(zd: ZendeskClient, ref: int | str) -> dict | None:
    if isinstance(ref, int) or str(ref).isdigit():
        try:
            return (await zd.get(f"users/{int(ref)}"))["user"]
        except ZendeskError:
            return None
    email = str(ref).strip().lower()
    res = await zd.get("users/search", query=email)
    for u in res.get("users", []):
        if (u.get("email") or "").lower() == email:
            return u
    return None


async def memberships_of(zd: ZendeskClient, user_id: int) -> list[dict]:
    return await all_items(zd, f"users/{user_id}/group_memberships", "group_memberships", limit=1000)


# ----------------------------------------------------------------------------- rule walking

def rule_parts(kind: str, rule: dict) -> list[tuple[str, dict]]:
    """(location, item) for every condition/action in a rule, regardless of payload style."""
    out: list[tuple[str, dict]] = []
    cond = rule.get("conditions") if isinstance(rule.get("conditions"), dict) else rule
    for bucket in ("all", "any"):
        for i, c in enumerate((cond or {}).get(bucket) or []):
            if isinstance(c, dict):
                out.append((f"conditions.{bucket}[{i}]", c))
    filt = rule.get("filter")
    if isinstance(filt, dict):
        for bucket in ("all", "any"):
            for i, c in enumerate(filt.get(bucket) or []):
                if isinstance(c, dict):
                    out.append((f"filter.{bucket}[{i}]", c))
    for i, a in enumerate(rule.get("actions") or []):
        if isinstance(a, dict):
            out.append((f"actions[{i}]", a))
    return out


def _vals(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)] if v is not None else []


def item_references(item: dict, ref_type: str, ref: str) -> bool:
    field = str(item.get("field") or "")
    value = item.get("value")
    vals = _vals(value)
    if ref_type == "group":
        return (field == "group_id" and ref in vals) or (field == "notification_group" and vals[:1] == [ref])
    if ref_type == "ticket_field":
        return field == f"custom_fields_{ref}"
    if ref_type == "custom_status":
        return field == "custom_status_id" and ref in vals
    if ref_type == "ticket_form":
        return field == "ticket_form_id" and ref in vals
    if ref_type == "brand":
        return field == "brand_id" and ref in vals
    if ref_type == "schedule":
        return field in ("set_schedule", "schedule_id", "schedule") and ref in vals
    if ref_type == "webhook":
        return field == "notification_webhook" and vals[:1] == [ref]
    if ref_type == "target":
        return field == "notification_target" and vals[:1] == [ref]
    if ref_type == "user":
        return (field in ("assignee_id", "requester_id", "submitter_id", "follower") and ref in vals) or \
            (field == "notification_user" and vals[:1] == [ref])
    if ref_type == "organization":
        return field == "organization_id" and ref in vals
    if ref_type == "tag":
        if field in ("current_tags", "set_tags", "add_tags", "remove_tags", "tags"):
            return ref in " ".join(vals).split()
        return field.startswith("custom_fields_") and ref in vals  # dropdown option tags
    return False


def restriction_references(rule: dict, ref_type: str, ref: str) -> bool:
    r = rule.get("restriction")
    if not isinstance(r, dict):
        return False
    ids = [str(x) for x in (r.get("ids") or [])] + ([str(r["id"])] if r.get("id") is not None else [])
    return (ref_type == "group" and r.get("type") == "Group" and ref in ids) or \
        (ref_type == "user" and r.get("type") == "User" and ref in ids)


# ----------------------------------------------------------------------------- agents

class AgentSpec(BaseModel):
    email: str = Field(description="Agent's email (used to find an existing user).")
    name: str | None = Field(None, description="Required when creating a new user.")
    role: Literal["agent", "admin"] | None = Field(None, description="Only changed when given; new users default to "
                                                    "'agent'. Leave empty to keep an existing user's role.")
    custom_role: int | str | None = Field(None, description="Custom role name or id (Enterprise), e.g. 'Light Agent'.")
    groups: list[int | str] | None = Field(None, description="Group names or ids the agent should belong to.")
    default_group: int | str | None = Field(None, description="Group name or id; defaults to the first of `groups`.")
    remove_other_groups: bool = Field(False, description="Also remove memberships in groups not listed.")
    attributes: dict[str, Any] | None = Field(None, description="Other user attributes, e.g. {'time_zone': "
                                              "'Pacific Time (US & Canada)', 'alias': 'Sam', 'user_fields': {...}}.")


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="List agents")
    async def list_agents(
        role: Literal["all", "agent", "admin"] = "all",
        group: Annotated[int | str | None, Field(description="Only agents in this group (name or id).")] = None,
        include_suspended: bool = True,
        inactive_days: Annotated[int, Field(ge=1, description="Flag agents with no login in this many days.")] = 30,
        limit: Annotated[int, Field(ge=1, le=10000)] = 2000,
    ) -> str:
        """Agent roster for audits and planning: role, custom role, groups (default marked), suspended,
        last login — plus totals by role and custom role, and who hasn't logged in recently."""
        roles = ["agent", "admin"] if role == "all" else [role]
        users = await all_items(zd, "users", "users", params={"role[]": roles}, limit=limit)
        groups, roles_map = await group_map(zd), await custom_role_map(zd)
        members = await all_items(zd, "group_memberships", "group_memberships")
        by_user: dict[int, list[dict]] = defaultdict(list)
        for m in members:
            by_user[m["user_id"]].append(m)
        gid = resolve_name(group, groups, "group") if group is not None else None
        from ..metrics import parse_ts, utcnow
        now = utcnow()
        rows, stale = [], []
        for u in users:
            ms = by_user.get(u["id"], [])
            if gid is not None and gid not in {m["group_id"] for m in ms}:
                continue
            if u.get("suspended") and not include_suspended:
                continue
            last = parse_ts(u.get("last_login_at"))
            row = {"id": u["id"], "name": u.get("name"), "email": u.get("email"), "role": u.get("role"),
                   "custom_role": roles_map.get(u.get("custom_role_id"), u.get("custom_role_id")),
                   "groups": sorted(groups.get(m["group_id"], m["group_id"]) for m in ms),
                   "default_group": next((groups.get(m["group_id"]) for m in ms if m.get("default")), None),
                   "suspended": u.get("suspended", False), "last_login_at": u.get("last_login_at")}
            rows.append(row)
            if not u.get("suspended") and (last is None or (now - last).days >= inactive_days):
                stale.append({"name": row["name"], "email": row["email"], "last_login_at": row["last_login_at"]})
        count = lambda key: dict(sorted(  # noqa: E731
            ((str(k), sum(1 for r in rows if r[key] == k)) for k in {r[key] for r in rows}), key=lambda kv: -kv[1]))
        return tk.out({
            "totals": {"agents": len(rows), "by_role": count("role"), "by_custom_role": count("custom_role"),
                       "suspended": sum(1 for r in rows if r["suspended"]),
                       f"no_login_{inactive_days}d": len(stale)},
            "inactive": stale[:50],
            "agents": sorted(rows, key=lambda r: (r["name"] or "").lower()),
        })

    @tk.tool("write", title="Provision agents", destructive_hint=True)
    async def provision_agents(
        agents: Annotated[list[AgentSpec], Field(min_length=1, max_length=50)],
        send_verification_email: Annotated[bool, Field(description="New users get Zendesk's verification email. "
                                                       "Set false when you use SSO.")] = True,
        dry_run: Annotated[bool, Field(description="Show what would change without changing anything.")] = False,
    ) -> str:
        """Create or update agents in one step: role, custom role, group memberships and default group.
        Existing users are matched by email and only changed where they differ (an end user is upgraded).
        Adding agents consumes Support seats; Zendesk rejects new agents when no seats are free."""
        groups, roles_map = await group_map(zd), await custom_role_map(zd)
        results = []
        for spec in agents:
            res: dict[str, Any] = {"email": spec.email}
            try:
                res.update(await _provision_one(tk, spec, groups, roles_map, send_verification_email, dry_run))
            except ZendeskError as exc:
                res.update(action="failed", error=str(exc))
            results.append(res)
            if res.get("action") == "failed" and not dry_run:
                pass  # keep going: one bad row shouldn't block the rest
        summary = defaultdict(int)
        for r in results:
            summary[r.get("action", "?")] += 1
        return tk.out({"dry_run": dry_run, "summary": dict(summary), "agents": results})

    @tk.tool("write", title="Offboard agent", destructive_hint=True)
    async def offboard_agent(
        user: Annotated[int | str, Field(description="Agent id or email.")],
        mode: Annotated[Literal["downgrade", "suspend", "downgrade_and_suspend", "delete"],
                        Field(description="downgrade = make end-user (frees the seat, keeps history); "
                                          "delete = permanent removal (needs full access mode).")] = "downgrade",
        reassign_to_group: Annotated[int | str | None, Field(description="Move their unsolved tickets to this group "
                                                             "(name or id), unassigned.")] = None,
        reassign_to_agent: Annotated[int | str | None, Field(description="Or assign them to this agent (id or email).")] = None,
        remove_group_memberships: bool = True,
        dry_run: Annotated[bool, Field(description="Default true: report the impact first. Run again with "
                                       "dry_run=false to do it.")] = True,
    ) -> str:
        """Offboard an agent safely, in Zendesk's recommended order: reassign their unsolved tickets, remove group
        memberships, then downgrade/suspend/delete. Defaults to a dry run that shows the impact."""
        mode_name = tk.settings.access_mode
        if mode_name != "full" and (mode == "delete" or remove_group_memberships):
            raise ZendeskError(f"Access mode is {mode_name!r}, which blocks deletes: "
                               + ("mode='delete' isn't possible." if mode == "delete" else
                                  "removing group memberships is a delete — pass remove_group_memberships=false.")
                               + " Nothing was changed.")
        u = await find_user(zd, user)
        if not u:
            raise ZendeskError(f"No user found for {user!r}.")
        if u.get("role") not in ("agent", "admin"):
            raise ZendeskError(f"{u.get('name')} is a {u.get('role')}, not an agent.")
        groups = await group_map(zd)
        uid = u["id"]
        count = (await zd.get("search/count", query=f"type:ticket assignee:{uid} status<solved")).get("count", 0)
        ms = await memberships_of(zd, uid)
        target_group = resolve_name(reassign_to_group, groups, "group") if reassign_to_group is not None else None
        target_agent = None
        if reassign_to_agent is not None:
            ta = await find_user(zd, reassign_to_agent)
            if not ta or ta.get("role") not in ("agent", "admin"):
                raise ZendeskError(f"Reassignment target {reassign_to_agent!r} isn't an agent.")
            target_agent = ta
        steps = []
        if count:
            if target_group is None and target_agent is None:
                steps.append(f"WARNING: {count} unsolved tickets stay assigned to them; Zendesk moves them to their "
                             "group when the agent is removed. Pass reassign_to_group or reassign_to_agent to control it.")
            else:
                who = f"agent {target_agent['name']}" if target_agent else f"group {groups.get(target_group)} (unassigned)"
                steps.append(f"Reassign {count} unsolved tickets to {who}")
        if remove_group_memberships and ms:
            steps.append(f"Remove {len(ms)} group memberships: {sorted(groups.get(m['group_id'], m['group_id']) for m in ms)}")
        steps.append({"downgrade": "Change role to end-user", "suspend": "Suspend the user",
                      "downgrade_and_suspend": "Change role to end-user, then suspend",
                      "delete": "Delete the user (not recoverable)"}[mode])
        impact = {"agent": {"id": uid, "name": u.get("name"), "email": u.get("email"), "role": u.get("role")},
                  "unsolved_assigned_tickets": count, "groups": [groups.get(m["group_id"]) for m in ms], "steps": steps}
        if dry_run:
            return tk.out({"dry_run": True, **impact, "next": "Call again with dry_run=false to carry this out."})

        changes, jobs = [], []
        if count and (target_group is not None or target_agent is not None):
            page = await zd.paginate("search/export", "results",
                                     params={"query": f"assignee:{uid} status<solved", "filter[type]": "ticket"},
                                     limit=100000)
            ids = [t["id"] for t in page["results"]]
            update = {"assignee_id": target_agent["id"]} if target_agent else {"assignee_id": None, "group_id": target_group}
            if target_agent and target_group is not None:
                update["group_id"] = target_group
            for i in range(0, len(ids), 100):
                body, ch = await zd.write("PUT", "tickets/update_many", params={"ids": ",".join(map(str, ids[i:i + 100]))},
                                          json_body={"ticket": update}, note=f"offboard:{uid}")
                changes.append(ch)
                jobs.append((body.get("job_status") or {}).get("id"))
        if remove_group_memberships:
            for m in ms:
                _, ch = await zd.write("DELETE", f"group_memberships/{m['id']}", before=m, wrapper="group_membership",
                                       note=f"offboard:{uid}")
                changes.append(ch)
        before = {k: u.get(k) for k in ("role", "suspended", "custom_role_id", "default_group_id")}
        before["suspended"] = bool(before.get("suspended"))  # absent means not suspended; never restore null
        update: dict[str, Any] = {}
        if mode in ("downgrade", "downgrade_and_suspend"):
            update["role"] = "end-user"
        if mode in ("suspend", "downgrade_and_suspend"):
            update["suspended"] = True
        if update:
            # One PUT, and a revert that also restores the custom role (Zendesk drops it on downgrade).
            restore = list(update) + (["custom_role_id"] if "role" in update and before.get("custom_role_id") else [])
            _, ch = await zd.write("PUT", f"users/{uid}", json_body={"user": update}, before=before, wrapper="user",
                                   note=f"offboard:{uid}", restore_keys=restore)
            changes.append(ch)
        if mode == "delete":
            _, ch = await zd.write("DELETE", f"users/{uid}", before=u, wrapper="user", note=f"offboard:{uid}")
            changes.append(ch)
        return tk.out({"dry_run": False, **impact, "done": True, "reassignment_jobs": [j for j in jobs if j],
                       "change_ids": [c for c in changes if c], "batch": f"offboard:{uid}"})

    # ------------------------------------------------------------------ business rules

    @tk.tool("read", title="Rule definitions")
    async def rule_definitions(
        kind: RuleKind,
        search: Annotated[str | None, Field(description="Only definitions whose title/subject contains this text.")] = None,
        max_values: Annotated[int, Field(ge=0, le=500, description="Allowed values listed per definition.")] = 50,
    ) -> str:
        """Valid conditions and actions for building triggers, automations, views and macros: each field's subject,
        operators and allowed values (group ids, statuses, custom fields...). Use before creating rules.
        Automations use the trigger definitions plus time-based conditions (see notes)."""
        path = {"triggers": "triggers/definitions", "automations": "triggers/definitions",
                "views": "views/definitions", "macros": "macros/definitions"}[kind]
        defs = (await zd.get(path)).get("definitions", {})
        needle = (search or "").lower()

        def slim(d: dict) -> dict | None:
            title = d.get("title") or d.get("subject") or ""
            if needle and needle not in f"{title} {d.get('subject', '')}".lower():
                return None
            out = {"subject": d.get("subject"), "title": title, "type": d.get("type"),
                   "operators": [o.get("value") for o in d.get("operators") or [] if isinstance(o, dict)] or None,
                   "group": d.get("group")}
            vals = d.get("values")
            if isinstance(vals, list):
                out["values"] = [{"value": v.get("value"), "title": v.get("title")} if isinstance(v, dict) else v
                                 for v in vals[:max_values]]
                if len(vals) > max_values:
                    out["values_truncated"] = len(vals)
            return out

        result = {k: [x for x in (slim(d) for d in v if isinstance(d, dict)) if x] for k, v in defs.items()
                  if isinstance(v, list)}
        if kind == "automations":
            result["notes"] = ("Automations need at least one time-based condition in `all` "
                               f"({', '.join(AUTOMATION_TIME_CONDITIONS)} with is/less_than/greater_than or the "
                               "*_business_hours variants), at least one condition on status, type, group_id, "
                               "assignee_id or requester_id, and an action that nullifies a condition (or a "
                               "condition true only once) so it doesn't fire repeatedly. Time-based conditions "
                               "are only allowed in `all`.")
        result["format"] = ("Conditions: {'all': [{'field','operator','value'}], 'any': [...]}; actions: "
                            "[{'field','value'}]. Custom fields: 'custom_fields_<id>'. Webhook action: "
                            "{'field': 'notification_webhook', 'value': ['<webhook_id>', '<json body>']}. "
                            "Updating conditions or actions replaces the whole array — send all of them.")
        return tk.out(result)

    @tk.tool("read", title="Preview a view")
    async def preview_view(
        conditions: Annotated[dict[str, Any], Field(description="{'all': [...], 'any': [...]} as for a view. Without "
                                                    "view_id, `all` needs a condition on status, type, group_id, "
                                                    "assignee_id, requester_id or organization_id.")],
        columns: Annotated[list[str] | None, Field(description="Output columns, e.g. ['subject','requester',"
                                                   "'updated','status'].")] = None,
        sort_by: str | None = None,
        sort_order: Literal["asc", "desc"] | None = None,
        count_only: bool = False,
        rows: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> str:
        """Test view conditions before creating or changing a view: how many tickets match and a sample of them.
        Zendesk allows about 5 previews per minute."""
        view = {"all": conditions.get("all") or [], "any": conditions.get("any") or []}
        count = (await zd.request("POST", "views/preview/count", json_body={"view": view},
                                  access_method="GET")).get("view_count", {})
        result: dict[str, Any] = {"count": count.get("value"), "count_is_fresh": count.get("fresh")}
        if not count_only:
            output = {k: v for k, v in {"columns": columns or ["subject", "requester", "status", "updated"],
                                        "sort_by": sort_by, "sort_order": sort_order}.items() if v}
            data = await zd.request("POST", "views/preview", json_body={"view": {**view, "output": output}},
                                    access_method="GET")
            result["columns"] = [c.get("id") if isinstance(c, dict) else c for c in data.get("columns", [])]
            result["rows"] = data.get("rows", [])[:rows]
        return tk.out(result)

    @tk.tool("write", title="Reorder rules or forms", idempotent=True)
    async def reorder(
        kind: Literal["triggers", "automations", "views", "macros", "ticket_forms", "sla_policies"],
        order: Annotated[list[int | str], Field(min_length=1, description="IDs or exact titles/names in the desired "
                                                "order. A subset keeps everything else where it is: the listed items "
                                                "swap among the positions they already hold.")],
    ) -> str:
        """Change the order of triggers, automations, views, macros, ticket forms or SLA policies (order matters:
        triggers fire top to bottom, SLA policies match top to bottom). Previous positions are journaled."""
        return tk.out(await reorder_items(zd, kind, order))

    @tk.tool("read", title="Find references")
    async def find_references(
        ref_type: Literal["group", "ticket_field", "custom_status", "ticket_form", "brand", "schedule", "webhook",
                          "target", "user", "organization", "tag"],
        value: Annotated[int | str, Field(description="ID, exact name/title, email (users), or the tag itself.")],
        include_inactive: bool = True,
    ) -> str:
        """Everything that depends on a group, field, status, form, brand, schedule, webhook, user, organization or
        tag: triggers, automations, views, macros, SLA policies, ticket forms and queues. Check this before
        renaming, deactivating or deleting anything."""
        ref = await _resolve_ref(zd, ref_type, value)
        found: dict[str, list[dict]] = {}
        sources = [("triggers", "triggers", True), ("automations", "automations", True), ("views", "views", True),
                   ("macros", "macros", False), ("sla_policies", "slas/policies", False)]
        for label, path, cursor in sources:
            try:
                items = await all_items(zd, path, label if label != "sla_policies" else "sla_policies", cursor=cursor)
            except ZendeskError:
                continue
            hits = []
            for rule in items:
                if not include_inactive and rule.get("active") is False:
                    continue
                where = [loc for loc, part in rule_parts(label, rule) if item_references(part, ref_type, ref)]
                if restriction_references(rule, ref_type, ref):
                    where.append("restriction")
                if where:
                    hits.append({"id": rule["id"], "title": rule.get("title"), "active": rule.get("active"),
                                 "where": where})
            if hits:
                found[label] = hits
        if ref_type in ("ticket_field", "custom_status"):
            forms = await all_items(zd, "ticket_forms", "ticket_forms", cursor=False)
            hits = []
            for f in forms:
                where = []
                if ref_type == "ticket_field" and int(ref) in (f.get("ticket_field_ids") or []):
                    where.append("ticket_field_ids")
                for cname in ("agent_conditions", "end_user_conditions"):
                    for c in f.get(cname) or []:
                        if ref_type == "ticket_field" and (str(c.get("parent_field_id")) == ref or any(
                                str(ch.get("id")) == ref for ch in c.get("child_fields") or [])):
                            where.append(cname)
                if where:
                    hits.append({"id": f["id"], "name": f.get("name"), "active": f.get("active"), "where": sorted(set(where))})
            if hits:
                found["ticket_forms"] = hits
        if ref_type == "group":
            try:
                queues = await all_items(zd, "queues", "queues", cursor=False)
                hits = [{"id": q["id"], "name": q.get("name")} for q in queues
                        if ref in [str(g.get("id") if isinstance(g, dict) else g)
                                   for g in (q.get("primary_groups") or []) + (q.get("secondary_groups") or [])]]
                if hits:
                    found["queues"] = hits
            except ZendeskError:
                pass
            members = await all_items(zd, f"groups/{ref}/memberships", "group_memberships", limit=5000)
            found_members = len(members)
        else:
            found_members = None
        result = {"ref_type": ref_type, "id": ref, "total_references": sum(len(v) for v in found.values()),
                  "references": found}
        if found_members is not None:
            result["group_members"] = found_members
        return tk.out(result)

    # ------------------------------------------------------------------ settings

    @tk.tool("write", title="Account settings", idempotent=True)
    async def account_settings(
        action: Literal["get", "update"] = "get",
        section: Annotated[str | None, Field(description="For 'get': only this section, e.g. 'tickets', "
                                             "'agents', 'routing', 'brands', 'user'.")] = None,
        settings: Annotated[dict[str, Any] | None, Field(description="For 'update': {section: {setting: value}}, "
                                                         "e.g. {'tickets': {'comments_public_by_default': false}}.")] = None,
    ) -> str:
        """Read or change account-level Support settings (tickets, agents, routing, CSAT, chat, etc.).
        Previous values are journaled so the change can be reverted."""
        current = (await zd.get("account/settings")).get("settings", {})
        if action == "get":
            if section:
                if section not in current:
                    raise ZendeskError(f"No settings section {section!r}. Sections: {', '.join(sorted(current))}")
                return tk.out({section: current[section]})
            return tk.out({"sections": sorted(current), "settings": current})
        if not settings:
            raise ZendeskError("Pass `settings` to update, e.g. {'tickets': {'comments_public_by_default': false}}.")
        unknown = [s for s in settings if s not in current]
        if unknown:
            raise ZendeskError(f"Unknown settings sections {unknown}. Sections: {', '.join(sorted(current))}")
        before = {s: {k: current[s].get(k) for k in v} if isinstance(v, dict) else current.get(s)
                  for s, v in settings.items()}
        body, ch = await zd.write("PUT", "account/settings", json_body={"settings": settings},
                                  before=before, wrapper="settings")
        after = (body or {}).get("settings", {})
        return tk.out({"updated": {s: {k: (after.get(s) or {}).get(k) for k in v} if isinstance(v, dict) else after.get(s)
                                   for s, v in settings.items()},
                       "previous": before, "change_id": ch})


REORDERABLE = ("triggers", "automations", "views", "macros", "ticket_forms", "sla_policies")


async def reorder_items(zd: ZendeskClient, kind: str, order: list[int | str], note: str | None = None) -> dict[str, Any]:
    """Re-deal the positions the listed items already hold, in the requested order (a full list = full reorder)."""
    path, key, cursor = {"triggers": ("triggers", "triggers", True), "automations": ("automations", "automations", True),
                         "views": ("views", "views", True), "macros": ("macros", "macros", False),
                         "ticket_forms": ("ticket_forms", "ticket_forms", False),
                         "sla_policies": ("slas/policies", "sla_policies", False)}[kind]
    items = await all_items(zd, path, key, cursor=cursor)
    names = {i["id"]: i.get("title") or i.get("name") for i in items}
    ids = [resolve_name(o, names, kind[:-1]) for o in order]
    if len(set(ids)) != len(ids):
        raise ZendeskError("`order` lists the same item twice.")
    current = sorted(items, key=lambda i: (i.get("position") or 0, i["id"]))
    slots = [i["id"] for i in current]
    chosen = set(ids)
    it = iter(ids)
    new_full = [next(it) if i in chosen else i for i in slots]
    old_pos = {i["id"]: i.get("position") for i in items}
    if kind in ("ticket_forms", "sla_policies"):
        if kind == "ticket_forms":
            _, ch = await zd.write("PUT", "ticket_forms/reorder", json_body={"ticket_form_ids": new_full},
                                   before={"ticket_form_ids": slots}, note=note)
        else:
            # Zendesk's docs show this both as a query parameter and as a JSON body; send both.
            _, ch = await zd.write("PUT", "slas/policies/reorder", params={"sla_policy_ids[]": new_full},
                                   json_body={"sla_policy_ids": new_full}, before={"sla_policy_ids": slots},
                                   note=note)
        changes = [ch]
    else:
        # Same position values, re-dealt among the chosen items in the requested order.
        chosen_positions = sorted(old_pos[i] if old_pos[i] is not None else n for n, i in enumerate(slots) if i in chosen)
        updates = [{"id": i, "position": p} for i, p in zip(ids, chosen_positions)]
        changes = []
        for n in range(0, len(updates), 100):
            batch = updates[n:n + 100]
            before = {key: [{"id": u["id"], "position": old_pos[u["id"]]} for u in batch]}
            _, ch = await zd.write("PUT", f"{path}/update_many", json_body={key: batch}, before=before, note=note)
            changes.append(ch)
    return {"kind": kind, "new_order": [{"id": i, "title": names[i]} for i in new_full if i in chosen],
            "change_ids": [c for c in changes if c]}


async def _resolve_ref(zd: ZendeskClient, ref_type: str, value: int | str) -> str:
    if ref_type == "tag":
        return str(value).strip()
    if isinstance(value, int) or str(value).isdigit():
        return str(int(value))
    if ref_type == "user":
        u = await find_user(zd, value)
        if not u:
            raise ZendeskError(f"No user {value!r}.")
        return str(u["id"])
    if ref_type == "organization":
        res = await zd.get("organizations/autocomplete", name=str(value))
        hits = [o for o in res.get("organizations", []) if (o.get("name") or "").lower() == str(value).lower()]
        if len(hits) != 1:
            raise ZendeskError(f"Couldn't find exactly one organization named {value!r}; use its id.")
        return str(hits[0]["id"])
    path, key, cursor, name_key = {
        "group": ("groups", "groups", True, "name"), "ticket_field": ("ticket_fields", "ticket_fields", True, "title"),
        "custom_status": ("custom_statuses", "custom_statuses", False, "agent_label"),
        "ticket_form": ("ticket_forms", "ticket_forms", False, "name"), "brand": ("brands", "brands", True, "name"),
        "schedule": ("business_hours/schedules", "schedules", False, "name"),
        "webhook": ("webhooks", "webhooks", True, "name"), "target": ("targets", "targets", False, "title"),
    }[ref_type]
    items = await all_items(zd, path, key, cursor=cursor)
    return str(resolve_name(value, {i["id"]: i.get(name_key) for i in items}, ref_type.replace("_", " ")))


async def _provision_one(tk: Toolkit, spec: AgentSpec, groups: dict[int, str], roles_map: dict[int, str],
                         send_email: bool, dry_run: bool) -> dict[str, Any]:
    zd = tk.zd
    want_groups = [resolve_name(g, groups, "group") for g in spec.groups or []]
    default = resolve_name(spec.default_group, groups, "group") if spec.default_group is not None else \
        (want_groups[0] if want_groups else None)
    if default is not None and default not in want_groups:
        want_groups.append(default)
    custom_role_id = None
    if spec.custom_role is not None:
        if not roles_map:
            raise ZendeskError("Custom roles aren't available on this account (Enterprise feature).")
        custom_role_id = resolve_name(spec.custom_role, roles_map, "custom role")

    desired: dict[str, Any] = {**(spec.attributes or {})}
    if spec.role is not None:
        desired["role"] = spec.role
    if spec.name:
        desired["name"] = spec.name
    if custom_role_id is not None:
        desired["custom_role_id"] = custom_role_id

    user = await find_user(zd, spec.email)
    out: dict[str, Any] = {}
    change_ids: list[str | None] = []
    if user is None:
        if not spec.name:
            raise ZendeskError("`name` is required to create a new agent.")
        desired.setdefault("role", "agent")
        out["action"] = "create"
        out["changes"] = desired
        members: list[dict] = []
        if not dry_run:
            payload = {**desired, "email": spec.email}
            if not send_email:
                payload["skip_verify_email"] = True
            body, ch = await zd.write("POST", "users", json_body={"user": payload}, wrapper="user",
                                      note=f"provision:{spec.email}")
            change_ids.append(ch)
            user = body["user"]
            out["action"] = "created"
    else:
        diff = {k: v for k, v in desired.items() if user.get(k) != v}
        out["action"] = "update" if diff else "unchanged"
        out["changes"] = {k: {"from": user.get(k), "to": v} for k, v in diff.items()}
        members = await memberships_of(zd, user["id"])
        if diff and not dry_run:
            before = {k: user.get(k) for k in diff}
            _, ch = await zd.write("PUT", f"users/{user['id']}", json_body={"user": diff}, before=before,
                                   wrapper="user", note=f"provision:{spec.email}")
            change_ids.append(ch)
            out["action"] = "updated"
    have = {m["group_id"]: m for m in members}
    add = [g for g in want_groups if g not in have]
    remove = [m for g, m in have.items() if spec.remove_other_groups and g not in want_groups]
    current_default = next((g for g, m in have.items() if m.get("default")), None)
    out["groups_added"] = [groups[g] for g in add]
    out["groups_removed"] = [groups.get(m["group_id"]) for m in remove]
    if default is not None and default != current_default:
        out["default_group"] = groups[default]
    if (add or remove or out.get("default_group")) and out["action"] == "unchanged":
        out["action"] = "update" if dry_run else "updated"
    if dry_run:
        out["user_id"] = user["id"] if user else None
        return out
    uid = user["id"]
    new_members = {}
    for g in add:
        body, ch = await zd.write("POST", "group_memberships",
                                  json_body={"group_membership": {"user_id": uid, "group_id": g}},
                                  wrapper="group_membership", note=f"provision:{spec.email}")
        change_ids.append(ch)
        new_members[g] = body.get("group_membership", {})
    for m in remove:
        _, ch = await zd.write("DELETE", f"group_memberships/{m['id']}", before=m, wrapper="group_membership",
                               note=f"provision:{spec.email}")
        change_ids.append(ch)
    if out.get("default_group"):
        mid = (new_members.get(default) or have.get(default) or {}).get("id")
        if mid:
            _, ch = await zd.write("PUT", f"users/{uid}/group_memberships/{mid}/make_default",
                                   note=f"provision:{spec.email}")
            change_ids.append(ch)
    out["user_id"] = uid
    out["change_ids"] = [c for c in change_ids if c]
    return out
