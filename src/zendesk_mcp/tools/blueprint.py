"""Blueprints: declarative, reviewable configuration changes.

A blueprint describes the configuration you want, by name: groups, fields, forms, statuses, schedules, SLA
policies, trigger categories, triggers, automations, views, macros, webhooks, skills, agents, settings —
plus explicit `order` and `delete` lists. Objects refer to each other with references like
"@group:Tier 2" or "@custom_field:Product Area", so nothing needs an ID that doesn't exist yet.

blueprint_plan compares the blueprint with the live account and lists every create/update/delete/reorder with
before→after values, warnings and blocking errors. blueprint_apply re-plans, refuses if the account drifted
since the plan was reviewed, then executes in dependency order. Every write is journaled under the plan id,
so revert_change(batch=<plan id>) rolls the rollout back.

Only keys you write in the blueprint are managed; anything else on an object is left alone. Nothing is deleted
unless it is listed under `delete`."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from ..client import ZendeskClient, ZendeskError
from ..journal import redact
from ..toolkit import Toolkit
from .admin import AUTOMATION_TIME_CONDITIONS, REORDERABLE, find_user, item_references, reorder_items, rule_parts
from .advanced import _confine

PLAN_TTL = 2 * 3600


# ----------------------------------------------------------------------------- sections

@dataclass(frozen=True)
class Section:
    name: str
    path: str
    list_key: str
    wrapper: str
    identity: str
    kind: str  # reference kind, e.g. @group:
    cursor: bool = True
    update_method: str = "PUT"
    required: tuple[str, ...] = ()
    special: tuple[str, ...] = ()  # blueprint-only keys, handled by code rather than sent as-is


SECTIONS: dict[str, Section] = {s.name: s for s in [
    Section("groups", "groups", "groups", "group", "name", "group", required=("name",)),
    Section("custom_roles", "custom_roles", "custom_roles", "custom_role", "name", "role", cursor=False,
            required=("name",)),
    Section("brands", "brands", "brands", "brand", "name", "brand", required=("name", "subdomain")),
    Section("schedules", "business_hours/schedules", "schedules", "schedule", "name", "schedule", cursor=False,
            required=("name", "time_zone"), special=("intervals", "holidays")),
    Section("trigger_categories", "trigger_categories", "trigger_categories", "trigger_category", "name", "category",
            update_method="PATCH", required=("name",)),
    Section("custom_statuses", "custom_statuses", "custom_statuses", "custom_status", "agent_label", "status",
            cursor=False, required=("agent_label", "end_user_label", "status_category")),
    Section("user_fields", "user_fields", "user_fields", "user_field", "key", "user_field",
            required=("key", "title", "type"), special=("prune_options",)),
    Section("organization_fields", "organization_fields", "organization_fields", "organization_field", "key",
            "org_field", required=("key", "title", "type"), special=("prune_options",)),
    Section("ticket_fields", "ticket_fields", "ticket_fields", "ticket_field", "title", "field",
            required=("title", "type"), special=("prune_options",)),
    Section("webhooks", "webhooks", "webhooks", "webhook", "name", "webhook", update_method="PATCH",
            required=("name", "endpoint", "http_method", "request_format", "status"), special=("rotate_authentication",)),
    Section("skills", "routing/attributes", "attributes", "attribute", "name", "skill_attribute", cursor=False,
            required=("name",), special=("values",)),
    Section("ticket_forms", "ticket_forms", "ticket_forms", "ticket_form", "name", "form", required=("name",)),
    Section("agents", "users", "users", "user", "email", "user", required=("email",),
            special=("custom_role", "groups", "default_group", "remove_other_groups", "skills", "attributes")),
    Section("sla_policies", "slas/policies", "sla_policies", "sla_policy", "title", "sla", cursor=False,
            required=("title", "filter", "policy_metrics")),
    Section("triggers", "triggers", "triggers", "trigger", "title", "trigger", required=("title", "conditions", "actions"),
            special=("category",)),
    Section("automations", "automations", "automations", "automation", "title", "automation",
            required=("title", "conditions", "actions")),
    Section("views", "views", "views", "view", "title", "view", required=("title", "conditions")),
    Section("macros", "macros", "macros", "macro", "title", "macro", required=("title", "actions")),
]}
DELETABLE = [n for n in SECTIONS if n not in ("agents",)]
TOP_LEVEL = set(SECTIONS) | {"version", "description", "settings", "order", "delete"}

# Reference kinds -> where to find names. 'field' etc. resolve to ids; see Registry.resolve for formats.
KIND_SOURCE = {
    "group": "groups", "role": "custom_roles", "brand": "brands", "schedule": "schedules",
    "category": "trigger_categories", "status": "custom_statuses", "user_field": "user_fields",
    "org_field": "organization_fields", "field": "ticket_fields", "custom_field": "ticket_fields",
    "option": "ticket_fields", "form": "ticket_forms", "webhook": "webhooks", "sla": "sla_policies",
    "trigger": "triggers", "automation": "automations", "view": "views", "macro": "macros",
    "skill": "skills", "target": "targets",
}
EXTRA_SOURCES = {"targets": ("targets", "targets", False, "title")}
REF = re.compile(r"^@([a-z_]+):(.+)$", re.S)
REF_HELP = ("@group:Name, @role:Custom role, @brand:Name, @schedule:Name, @category:Trigger category, "
            "@status:Agent label (custom status), @field:Ticket field title (-> id), @custom_field:Title "
            "(-> 'custom_fields_<id>' for rule conditions/actions), @option:Field title/Option name (-> option tag), "
            "@user_field:key, @org_field:key, @form:Name, @webhook:Name, @sla:Title, @trigger/@automation/@view/"
            "@macro:Title, @skill:Attribute/Value, @user:email, @org:Organization name, @target:Title. "
            "Write @@ to start a literal string with @.")


class Pending:
    """Placeholder for the id of an object this plan will create."""

    def __init__(self, kind: str, name: str, fmt: str | None = None, as_str: bool = False):
        self.kind, self.name, self.fmt, self.as_str = kind, name, fmt, as_str

    def __str__(self) -> str:
        return f"<new {self.kind}: {self.name}>"

    __repr__ = __str__


def _key(name: Any) -> str:
    return str(name).strip().lower()


class Registry:
    def __init__(self, zd: ZendeskClient):
        self.zd = zd
        self.names: dict[str, dict[str, list[Any]]] = {}
        self.options: dict[str, dict[str, str]] = {}  # field title -> option name -> tag
        self._users: dict[str, Any] = {}
        self._orgs: dict[str, Any] = {}

    def add(self, kind: str, name: Any, ident: Any) -> None:
        if name is None:
            return
        bucket = self.names.setdefault(kind, {}).setdefault(_key(name), [])
        if ident not in bucket:
            bucket.append(ident)

    def set(self, kind: str, name: Any, ident: Any) -> None:
        self.names.setdefault(kind, {})[_key(name)] = [ident]

    def lookup(self, kind: str, name: str) -> Any:
        hits = self.names.get(kind, {}).get(_key(name), [])
        if not hits:
            known = sorted(self.names.get(kind, {}))[:15]
            raise ZendeskError(f"Unknown reference @{kind}:{name} — not in the account or this blueprint"
                               f"{f' (known: {known})' if known else ''}.")
        if len(hits) > 1:
            raise ZendeskError(f"@{kind}:{name} is ambiguous ({len(hits)} matches: {hits}); rename one or use an id.")
        return hits[0]

    async def resolve(self, value: Any, as_str: bool = False) -> Any:
        if isinstance(value, dict):
            in_rule = "field" in value
            return {k: await self.resolve(v, as_str=in_rule and k == "value") for k, v in value.items()}
        if isinstance(value, list):
            return [await self.resolve(v, as_str) for v in value]
        if not isinstance(value, str):
            return value
        if value.startswith("@@"):
            return value[1:]
        m = REF.match(value)
        if not m:
            return value
        kind, name = m.group(1), m.group(2).strip()
        if kind == "user":
            return self._fmt(await self._user(name), None, as_str)
        if kind == "org":
            return self._fmt(await self._org(name), None, as_str)
        if kind == "option":
            if "/" not in name:
                raise ZendeskError(f"@option needs 'Field title/Option name', got {name!r}.")
            ftitle, oname = name.rsplit("/", 1)
            tag = self.options.get(_key(ftitle), {}).get(_key(oname))
            if tag is None:
                raise ZendeskError(f"Unknown option @option:{name}.")
            return tag
        if kind == "custom_field":
            return self._fmt(self.lookup("field", name), "custom_fields_{}", True)
        if kind == "category":
            return self._fmt(self.lookup("category", name), None, True)
        if kind not in KIND_SOURCE:
            raise ZendeskError(f"Unknown reference kind @{kind}. Supported: {REF_HELP}")
        return self._fmt(self.lookup(kind, name), None, as_str)

    @staticmethod
    def _fmt(ident: Any, fmt: str | None, as_str: bool) -> Any:
        if isinstance(ident, Pending):
            return Pending(ident.kind, ident.name, fmt, as_str)
        if fmt:
            return fmt.format(ident)
        return str(ident) if as_str else ident

    async def _user(self, email: str) -> Any:
        k = _key(email)
        if k in self.names.get("user", {}):
            return self.lookup("user", email)
        if k not in self._users:
            u = await find_user(self.zd, email)
            if not u:
                raise ZendeskError(f"Unknown reference @user:{email} — no such user.")
            self._users[k] = u["id"]
        return self._users[k]

    async def _org(self, name: str) -> Any:
        k = _key(name)
        if k not in self._orgs:
            res = await self.zd.get("organizations/autocomplete", name=name)
            hits = [o["id"] for o in res.get("organizations", []) if _key(o.get("name")) == k]
            if len(hits) != 1:
                raise ZendeskError(f"@org:{name} matched {len(hits)} organizations; use an id.")
            self._orgs[k] = hits[0]
        return self._orgs[k]

    def materialize(self, value: Any, created: dict[tuple[str, str], Any]) -> Any:
        if isinstance(value, Pending):
            real = created.get((value.kind, _key(value.name)))
            if real is None:
                raise ZendeskError(f"{value} hasn't been created yet (dependency order problem).")
            if value.fmt:
                return value.fmt.format(real)
            return str(real) if value.as_str else real
        if isinstance(value, dict):
            return {k: self.materialize(v, created) for k, v in value.items()}
        if isinstance(value, list):
            return [self.materialize(v, created) for v in value]
        return value


def has_pending(value: Any) -> bool:
    if isinstance(value, Pending):
        return True
    if isinstance(value, dict):
        return any(has_pending(v) for v in value.values())
    if isinstance(value, list):
        return any(has_pending(v) for v in value)
    return False


# ----------------------------------------------------------------------------- comparison

def _scalar(x: Any) -> Any:
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    if isinstance(x, (int, float)):
        return str(x)
    return str(x)


def same(desired: Any, live: Any) -> bool:
    """Desired matches live: dicts compare only the keys you specified, lists compare element-wise."""
    if isinstance(desired, Pending):
        return False
    if isinstance(desired, dict):
        return isinstance(live, dict) and all(same(v, live.get(k)) for k, v in desired.items())
    if isinstance(desired, list):
        return isinstance(live, list) and len(desired) == len(live) and all(same(a, b) for a, b in zip(desired, live))
    return _scalar(desired) == _scalar(live)


def norm_conditions(value: Any) -> dict:
    value = value or {}
    return {"all": list(value.get("all") or []), "any": list(value.get("any") or [])}


def _sorted_metrics(metrics: list[dict]) -> list[dict]:
    return sorted(metrics or [], key=lambda m: (str(m.get("priority")), str(m.get("metric"))))


def merge_options(desired: list[dict], live: list[dict], prune: bool) -> tuple[list[dict], dict[str, list]]:
    """Dropdown options: Zendesk replaces the whole list on update and drops values from tickets for any option
    left out, so by default live options not in the blueprint are kept."""
    by_value = {_key(o.get("value")): o for o in live or []}
    by_name = {_key(o.get("name")): o for o in live or []}
    merged, used = [], set()
    notes: dict[str, list] = {"added": [], "renamed": [], "removed": [], "kept": []}
    for d in desired:
        lv = by_value.get(_key(d.get("value"))) if d.get("value") is not None else by_name.get(_key(d.get("name")))
        if lv is None and d.get("value") is None:
            raise ZendeskError(f"Option {d.get('name')!r} needs a `value` (its tag).")
        item = {"name": d.get("name"), "value": d.get("value") or (lv or {}).get("value")}
        for k, v in d.items():
            if k not in ("name", "value"):
                item[k] = v
        if lv is not None:
            item["id"] = lv.get("id")
            used.add(lv.get("id"))
            if lv.get("name") != item["name"]:
                notes["renamed"].append(f"{lv.get('name')} -> {item['name']}")
        else:
            notes["added"].append(item["name"])
        merged.append(item)
    for o in live or []:
        if o.get("id") not in used:
            if prune:
                notes["removed"].append(o.get("name"))
            else:
                merged.append({"id": o.get("id"), "name": o.get("name"), "value": o.get("value")})
                notes["kept"].append(o.get("name"))
    return merged, notes


def _options_equal(merged: list[dict], live: list[dict]) -> bool:
    return [(_key(o.get("value")), o.get("name")) for o in merged] == \
        [(_key(o.get("value")), o.get("name")) for o in live or []]


def _clip(v: Any, n: int = 600) -> Any:
    text = json.dumps(v, default=str)
    return v if len(text) <= n else text[:n] + f"…({len(text)} chars)"


# ----------------------------------------------------------------------------- plan model

@dataclass
class Step:
    section: str
    action: str  # create | update | delete | reorder | settings
    name: str
    live_id: Any = None
    payload: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)
    live: dict | None = None

    def display(self, maps: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {"section": self.section, "action": self.action, "name": self.name}
        if self.live_id is not None:
            d["id"] = self.live_id
        if self.changes:
            shown = to_refs(self.changes, maps) if maps else self.changes  # ids -> @kind:Name for review
            d["changes"] = {k: _clip(redact({k: v})[k]) for k, v in shown.items()}
        for k in ("notes", "warnings"):
            if self.extra.get(k):
                d[k] = self.extra[k]
        return d

    def fingerprint(self) -> Any:
        # `changes` carries the live "from" values, so an edit made after review counts as drift.
        return [self.section, self.action, self.name, str(self.live_id),
                json.dumps(redact(self.payload), sort_keys=True, default=str),
                json.dumps(redact(self.changes), sort_keys=True, default=str),
                json.dumps(redact({k: v for k, v in self.extra.items() if k not in ("notes", "warnings")}),
                           sort_keys=True, default=str)]


@dataclass
class Plan:
    steps: list[Step]
    errors: list[str]
    warnings: list[str]
    unchanged: dict[str, int]
    registry: Registry
    live: dict[str, list[dict]]

    def display_maps(self) -> dict[str, dict[str, str]]:
        return _reverse_maps(self.live)

    @property
    def digest(self) -> str:
        blob = json.dumps([s.fingerprint() for s in self.steps], default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def summary(self) -> dict[str, Any]:
        out: dict[str, dict[str, int]] = {}
        for s in self.steps:
            out.setdefault(s.section, {}).setdefault(s.action, 0)
            out[s.section][s.action] += 1
        for sec, n in self.unchanged.items():
            if n:
                out.setdefault(sec, {})["unchanged"] = n
        return out


# ----------------------------------------------------------------------------- planner

class Planner:
    def __init__(self, tk: Toolkit, blueprint: dict[str, Any], validate: bool = True):
        self.tk, self.zd, self.bp, self.validate = tk, tk.zd, blueprint, validate
        self.reg = Registry(self.zd)
        self.live: dict[str, list[dict]] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.steps: list[Step] = []
        self.unchanged: dict[str, int] = {}

    # -------------------------------------------------- loading
    def _needed_sections(self) -> set[str]:
        need = {k for k in self.bp if k in SECTIONS}
        need |= {k for k in (self.bp.get("delete") or {}) if k in SECTIONS}
        need |= {k for k in (self.bp.get("order") or {}) if k in SECTIONS}
        refs = re.findall(r'"@([a-z_]+):', json.dumps(self.bp, default=str))
        for kind in set(refs):
            src = KIND_SOURCE.get(kind)
            if src:
                need.add(src)
        if "triggers" in self.bp:
            need.add("trigger_categories")
        if "agents" in self.bp:
            need |= {"groups", "custom_roles"}
            if any(a.get("skills") for a in self.bp["agents"] if isinstance(a, dict)):
                need.add("skills")
        if (self.bp.get("delete") or {}):
            need |= {"triggers", "automations", "views", "macros", "sla_policies"}
        return need

    async def load(self) -> None:
        for name in sorted(self._needed_sections()):
            if name == "agents":
                continue  # looked up per email
            if name == "targets":
                path, key, cursor, _ = EXTRA_SOURCES["targets"]
                sec_path, sec_key, sec_cursor = path, key, cursor
            else:
                sec = SECTIONS[name]
                sec_path, sec_key, sec_cursor = sec.path, sec.list_key, sec.cursor
            try:
                items = (await self.zd.paginate(sec_path, sec_key, limit=100000, cursor=sec_cursor))[sec_key]
            except ZendeskError as exc:
                if name in self.bp or name in (self.bp.get("delete") or {}):
                    self.errors.append(f"Can't read {name}: {exc}")
                else:
                    self.warnings.append(f"Couldn't read {name} for reference checks: {exc}")
                items = []
            self.live[name] = items
            if name == "schedules":
                for sch in items:
                    try:
                        sch["holidays"] = (await self.zd.get(f"business_hours/schedules/{sch['id']}/holidays")).get("holidays", [])
                    except ZendeskError:
                        sch["holidays"] = []
            if name == "skills":
                for attr in items:
                    try:
                        attr["values"] = (await self.zd.get(f"routing/attributes/{attr['id']}/values")).get("attribute_values", [])
                    except ZendeskError:
                        attr["values"] = []
        self._index_live()

    def _index_live(self) -> None:
        for name, items in self.live.items():
            if name == "targets":
                for t in items:
                    self.reg.add("target", t.get("title"), t["id"])
                continue
            sec = SECTIONS[name]
            for it in items:
                self.reg.add(sec.kind, it.get(sec.identity), it["id"])
                if name == "ticket_fields":
                    self.options_for(it.get("title"), it.get("custom_field_options"))
                if name == "skills":
                    for v in it.get("values") or []:
                        self.reg.add("skill", f"{it.get('name')}/{v.get('name')}", v.get("id"))

    def options_for(self, title: Any, options: list[dict] | None) -> None:
        if title is None:
            return
        bucket = self.reg.options.setdefault(_key(title), {})
        for o in options or []:
            if o.get("name") is not None:
                bucket[_key(o["name"])] = o.get("value")

    # -------------------------------------------------- planning
    def find_live(self, sec: Section, item: dict) -> dict | None:
        items = self.live.get(sec.name, [])
        if item.get("id") is not None:
            hit = [i for i in items if str(i["id"]) == str(item["id"])]
            if not hit:
                raise ZendeskError(f"{sec.name}: no object with id {item['id']}.")
            return hit[0]
        hits = [i for i in items if _key(i.get(sec.identity)) == _key(item.get(sec.identity))]
        if len(hits) > 1:
            raise ZendeskError(f"{sec.name}: {len(hits)} objects named {item.get(sec.identity)!r} "
                               f"(ids {[h['id'] for h in hits]}); add an `id` to say which one.")
        return hits[0] if hits else None

    async def plan(self) -> Plan:
        unknown = [k for k in self.bp if k not in TOP_LEVEL]
        if unknown:
            self.errors.append(f"Unknown blueprint keys {unknown}. Sections: {', '.join(SECTIONS)}, plus settings, "
                               "order, delete.")
        await self.load()
        # Register objects this blueprint will create, so later sections can reference them.
        for name, sec in SECTIONS.items():
            for item in self.bp.get(name) or []:
                if not isinstance(item, dict):
                    self.errors.append(f"{name}: every entry must be an object, got {item!r}.")
                    continue
                if name == "agents":
                    continue
                try:
                    live = self.find_live(sec, item)
                except ZendeskError as exc:
                    self.errors.append(str(exc))
                    continue
                if live is None and item.get(sec.identity) is not None:
                    self.reg.set(sec.kind, item[sec.identity], Pending(sec.kind, str(item[sec.identity])))
                if name == "ticket_fields":
                    self.options_for(item.get("title"), item.get("custom_field_options"))
                if name == "skills":
                    for v in item.get("values") or []:
                        key = f"{item.get('name')}/{v}"
                        if _key(key) not in self.reg.names.get("skill", {}):
                            self.reg.set("skill", key, Pending("skill", key))
        if "settings" in self.bp:
            await self._plan_settings()
        for name in SECTIONS:
            for item in self.bp.get(name) or []:
                if isinstance(item, dict):
                    try:
                        if name == "agents":
                            await self._plan_agent(item)
                        else:
                            await self._plan_item(SECTIONS[name], item)
                    except ZendeskError as exc:
                        self.errors.append(f"{name} {item.get(SECTIONS[name].identity)!r}: {exc}")
        self._plan_order()
        self._plan_deletes()
        if self.validate and not self.errors:
            await self._validate_triggers()
        self.errors = list(dict.fromkeys(self.errors))
        return Plan(self.steps, self.errors, self.warnings, self.unchanged, self.reg, self.live)

    async def _plan_settings(self) -> None:
        desired = self.bp["settings"]
        live = (await self.zd.get("account/settings")).get("settings", {})
        changes, payload = {}, {}
        for section, values in (desired or {}).items():
            if section not in live:
                self.errors.append(f"settings: unknown section {section!r}.")
                continue
            for k, v in (values or {}).items():
                if not same(v, live[section].get(k)):
                    payload.setdefault(section, {})[k] = v
                    changes[f"{section}.{k}"] = {"from": live[section].get(k), "to": v}
        if payload:
            self.steps.append(Step("settings", "settings", "account settings", payload=payload, changes=changes,
                                   live={s: {k: live[s].get(k) for k in v} for s, v in payload.items()}))

    async def _plan_item(self, sec: Section, item: dict) -> None:
        missing = [k for k in sec.required if k not in item and not (k == "conditions" and ("all" in item or "any" in item))]
        live = self.find_live(sec, item)
        if live is None and missing:
            raise ZendeskError(f"missing required keys for create: {missing}")
        desired = {k: v for k, v in item.items() if k not in sec.special and k != "id"}
        if sec.name in ("triggers", "automations", "views") and ("all" in desired or "any" in desired):
            desired["conditions"] = {"all": desired.pop("all", []), "any": desired.pop("any", [])}
        if sec.name == "triggers" and item.get("category") is not None:
            desired["category_id"] = f"@category:{item['category']}"
        if sec.name == "webhooks" and live is not None and not item.get("rotate_authentication"):
            desired.pop("authentication", None)
        resolved = await self.reg.resolve(desired)
        name = str(item.get(sec.identity))
        extra: dict[str, Any] = {"notes": [], "warnings": []}

        if sec.name == "automations":
            conds = norm_conditions(resolved.get("conditions"))
            if live is None or "conditions" in desired:
                fields = {str(c.get("field")) for c in conds["all"] if isinstance(c, dict)}
                if not any(f in AUTOMATION_TIME_CONDITIONS or f.endswith("_business_hours") for f in fields):
                    raise ZendeskError("automations need a time-based condition in `all` (e.g. hours since "
                                       "updated: {'field': 'updated_at', 'operator': 'greater_than', 'value': '48'}).")

        if sec.name in ("ticket_fields", "user_fields", "organization_fields") and "custom_field_options" in resolved:
            merged, notes = merge_options(resolved["custom_field_options"],
                                          (live or {}).get("custom_field_options") or [], bool(item.get("prune_options")))
            resolved["custom_field_options"] = merged if live is not None else \
                [{k: v for k, v in o.items() if k != "id"} for o in merged]
            if live is not None and _options_equal(merged, live.get("custom_field_options")):
                resolved.pop("custom_field_options")
            elif live is not None:
                extra["notes"] += [f"options {k}: {v}" for k, v in notes.items() if v and k != "kept"]
                if notes["removed"]:
                    extra["warnings"].append(f"Pruning options {notes['removed']} removes those values from every "
                                             "ticket and macro that uses them.")

        if live is None:
            step = Step(sec.name, "create", name, payload=resolved,
                        changes={k: v for k, v in resolved.items() if k not in ("custom_field_options",)} or resolved)
            if "custom_field_options" in resolved:
                step.changes["custom_field_options"] = [o.get("name") for o in resolved["custom_field_options"]]
        else:
            changes, payload = {}, {}
            for k, v in resolved.items():
                lv = live.get(k)
                if k in ("conditions", "filter"):
                    equal = same(norm_conditions(v), norm_conditions(lv))
                elif k == "policy_metrics":
                    equal = same(_sorted_metrics(v), _sorted_metrics(lv))
                elif k == "custom_field_options":
                    equal = False  # already compared by merge
                else:
                    equal = same(v, lv)
                if not equal:
                    payload[k] = v
                    changes[k] = {"from": lv, "to": v} if k != "custom_field_options" else \
                        {"from": [o.get("name") for o in lv or []], "to": [o.get("name") for o in v]}
            if sec.name in ("triggers", "automations") and ({"conditions", "actions"} & payload.keys()):
                # Zendesk clears both arrays when either is updated: always send both.
                payload.setdefault("conditions", resolved.get("conditions", live.get("conditions")))
                payload.setdefault("actions", resolved.get("actions", live.get("actions")))
            step = Step(sec.name, "update", name, live_id=live["id"], payload=payload, changes=changes, live=live)
        # Section-specific sub-resources
        if sec.name == "schedules":
            await self._plan_schedule(item, live, step)
        if sec.name == "skills":
            have = {_key(v.get("name")) for v in (live or {}).get("values") or []}
            new_vals = [v for v in item.get("values") or [] if _key(v) not in have]
            if new_vals:
                step.extra["new_values"] = new_vals
                step.changes["values_added"] = new_vals
        step.extra.update({k: v for k, v in extra.items() if v})
        if step.action == "update" and not step.payload and not any(
                step.extra.get(k) for k in ("intervals", "holidays_create", "holidays_update", "new_values")):
            self.unchanged[sec.name] = self.unchanged.get(sec.name, 0) + 1
            return
        if step.action == "update" and not step.payload:
            step.changes = step.changes or {}
        self.steps.append(step)

    async def _plan_schedule(self, item: dict, live: dict | None, step: Step) -> None:
        if "intervals" in item:
            want = [{"start_time": int(i["start_time"]), "end_time": int(i["end_time"])} for i in item["intervals"]]
            have = [{"start_time": i.get("start_time"), "end_time": i.get("end_time")} for i in (live or {}).get("intervals") or []]
            if want != have:
                step.extra["intervals"] = want
                step.changes["intervals"] = {"from": have, "to": want}
        if "holidays" in item:
            live_h = {_key(h.get("name")): h for h in (live or {}).get("holidays") or []}
            creates, updates = [], []
            for h in item["holidays"]:
                lh = live_h.get(_key(h.get("name")))
                if lh is None:
                    creates.append(h)
                elif not same(h, lh):
                    updates.append({**h, "id": lh["id"]})
            if creates:
                step.extra["holidays_create"] = creates
                step.changes["holidays_added"] = [h.get("name") for h in creates]
            if updates:
                step.extra["holidays_update"] = updates
                step.changes["holidays_changed"] = [h.get("name") for h in updates]

    async def _plan_agent(self, item: dict) -> None:
        email = item.get("email")
        if not email:
            raise ZendeskError("agents need an email.")
        user = await find_user(self.zd, email)
        desired: dict[str, Any] = {**(item.get("attributes") or {})}
        if item.get("role") is not None:  # only manage the role when the blueprint says so
            desired["role"] = item["role"]
        if item.get("name"):
            desired["name"] = item["name"]
        if item.get("custom_role") is not None:
            desired["custom_role_id"] = await self.reg.resolve(
                item["custom_role"] if str(item["custom_role"]).startswith("@") else f"@role:{item['custom_role']}")
        groups = [await self.reg.resolve(g if str(g).startswith("@") else f"@group:{g}") for g in item.get("groups") or []]
        default = item.get("default_group")
        default_id = await self.reg.resolve(default if str(default).startswith("@") else f"@group:{default}") \
            if default is not None else (groups[0] if groups else None)
        if default_id is not None and not any(_same_id(default_id, g) for g in groups):
            groups.append(default_id)
        skills = None
        if item.get("skills") is not None:
            skills = [await self.reg.resolve(s if str(s).startswith("@") else f"@skill:{s}") for s in item["skills"]]
        extra: dict[str, Any] = {"groups": groups, "default_group": default_id,
                                 "remove_other_groups": bool(item.get("remove_other_groups")),
                                 "send_verification_email": item.get("send_verification_email", True)}
        if user is None:
            if not item.get("name"):
                raise ZendeskError("`name` is required to create an agent.")
            self.reg.set("user", email, Pending("user", email))
            desired.setdefault("role", "agent")
            if skills is not None:
                extra["skills"] = skills
            shown = {k: v for k, v in desired.items() if k != "custom_role_id"}
            if item.get("custom_role") is not None:
                shown["custom_role"] = item["custom_role"]
            shown["groups"] = list(item.get("groups") or [])
            if item.get("default_group") is not None:
                shown["default_group"] = item["default_group"]
            step = Step("agents", "create", email, payload={**desired, "email": email}, changes=shown, extra=extra)
            if skills:
                step.changes["skills"] = list(item.get("skills") or [])
            self.steps.append(step)
            return
        self.reg.set("user", email, user["id"])
        diff = {k: v for k, v in desired.items() if not same(v, user.get(k))}
        changes = {k: {"from": user.get(k), "to": v} for k, v in diff.items()}
        members = (await self.zd.paginate(f"users/{user['id']}/group_memberships", "group_memberships",
                                          limit=1000))["group_memberships"]
        have = {str(m["group_id"]): m for m in members}
        add = [g for g in groups if isinstance(g, Pending) or str(g) not in have]
        remove = [m for gid, m in have.items() if extra["remove_other_groups"] and not any(str(g) == gid for g in groups)]
        cur_default = next((gid for gid, m in have.items() if m.get("default")), None)
        gname = {str(g["id"]): g.get("name") for g in self.live.get("groups", [])}
        label = lambda g: str(g) if isinstance(g, Pending) else gname.get(str(g), str(g))  # noqa: E731
        if add:
            changes["groups_added"] = [label(g) for g in add]
        if remove:
            changes["groups_removed"] = [label(m["group_id"]) for m in remove]
        if default_id is not None and str(default_id) != str(cur_default):
            changes["default_group"] = {"from": label(cur_default) if cur_default else None, "to": label(default_id)}
        if "custom_role_id" in changes:
            rname = {str(r["id"]): r.get("name") for r in self.live.get("custom_roles", [])}
            changes["custom_role_id"] = {k: rname.get(str(v), v) if v is not None and not isinstance(v, Pending) else v
                                         for k, v in changes["custom_role_id"].items()}
        extra.update(add=add, remove=remove, memberships=members)
        if skills is not None:
            try:
                cur = (await self.zd.get(f"routing/agents/{user['id']}/instance_values")).get("attribute_values", [])
            except ZendeskError as exc:
                # Setting skills replaces all of them, so never guess the current set.
                raise ZendeskError(f"couldn't read current skills ({exc}); can't safely manage `skills`.") from exc
            cur_ids = sorted(str(v.get("id")) for v in cur)
            if has_pending(skills) or sorted(str(s) for s in skills) != cur_ids:
                extra["skills"] = skills
                extra["skills_before"] = [v.get("id") for v in cur]
                changes["skills"] = {"from": cur_ids, "to": [str(s) for s in skills]}
        if not changes:
            self.unchanged["agents"] = self.unchanged.get("agents", 0) + 1
            return
        self.steps.append(Step("agents", "update", email, live_id=user["id"], payload=diff, changes=changes,
                               extra=extra, live=user))

    def _plan_order(self) -> None:
        for kind, order in (self.bp.get("order") or {}).items():
            if kind not in REORDERABLE:
                self.errors.append(f"order: can't reorder {kind!r}; supported: {', '.join(REORDERABLE)}.")
                continue
            sec = SECTIONS[kind if kind != "ticket_forms" else "ticket_forms"]
            live = self.live.get(kind, [])
            planned = {_key(i.get(sec.identity)) for i in self.bp.get(kind) or [] if isinstance(i, dict)}
            ids: list[Any] = []
            for ref in order or []:
                hits = [i["id"] for i in live if str(i["id"]) == str(ref) or _key(i.get(sec.identity)) == _key(ref)]
                if len(hits) == 1:
                    ids.append(hits[0])
                elif len(hits) > 1:
                    self.errors.append(f"order {kind}: {ref!r} is ambiguous (ids {hits}); use an id.")
                elif _key(ref) in planned:
                    ids.append(Pending(sec.kind, str(ref)))
                else:
                    self.errors.append(f"order {kind}: no {kind[:-1]} named {ref!r} in the account or this blueprint.")
            if len(set(map(str, ids))) != len(ids):
                self.errors.append(f"order {kind}: an item is listed twice.")
            self.steps.append(Step(kind, "reorder", f"{len(order)} items", payload={"order": ids},
                                   changes={"order": list(order)}))

    def _plan_deletes(self) -> None:
        deletes = self.bp.get("delete") or {}
        targets: dict[str, list[dict]] = {}
        for name, refs in deletes.items():
            if name not in DELETABLE:
                self.errors.append(f"delete: unsupported section {name!r}; supported: {', '.join(DELETABLE)}.")
                continue
            sec = SECTIONS[name]
            for ref in refs or []:
                items = self.live.get(name, [])
                hits = [i for i in items if str(i["id"]) == str(ref) or _key(i.get(sec.identity)) == _key(ref)]
                if not hits:
                    self.warnings.append(f"delete {name} {ref!r}: not found (already gone?).")
                    continue
                if len(hits) > 1:
                    self.errors.append(f"delete {name} {ref!r}: ambiguous, ids {[h['id'] for h in hits]}.")
                    continue
                targets.setdefault(name, []).append(hits[0])
        deleting = {(n, str(i["id"])) for n, items in targets.items() for i in items}
        ref_type = {"groups": "group", "ticket_fields": "ticket_field", "custom_statuses": "custom_status",
                    "ticket_forms": "ticket_form", "brands": "brand", "schedules": "schedule", "webhooks": "webhook"}
        # Rules first, then the config they might reference.
        order = ["triggers", "automations", "views", "macros", "sla_policies", "ticket_forms"] + \
                [n for n in DELETABLE if n not in ("triggers", "automations", "views", "macros", "sla_policies", "ticket_forms")]
        for name in order:
            for live in targets.get(name, []):
                sec = SECTIONS[name]
                step = Step(name, "delete", str(live.get(sec.identity)), live_id=live["id"], live=live)
                if name in ref_type:
                    users = []
                    for rname in ("triggers", "automations", "views", "macros", "sla_policies"):
                        for rule in self.live.get(rname, []):
                            if (rname, str(rule["id"])) in deleting:
                                continue
                            if any(item_references(p, ref_type[name], str(live["id"])) for _, p in rule_parts(rname, rule)):
                                users.append(f"{rname[:-1]} '{rule.get('title')}'")
                    if users:
                        step.extra["warnings"] = [f"Still referenced by {len(users)} rule(s): {users[:8]}"
                                                  f"{'…' if len(users) > 8 else ''}. Update those in this blueprint too."]
                self.steps.append(step)

    async def _validate_triggers(self) -> None:
        checks = [s for s in self.steps if s.section == "triggers" and s.action in ("create", "update")
                  and not has_pending(s.payload)]
        sem = asyncio.Semaphore(3)

        async def check(step: Step) -> None:
            body = step.payload if step.action == "create" else {
                "title": (step.live or {}).get("title"), "conditions": (step.live or {}).get("conditions"),
                "actions": (step.live or {}).get("actions"), **step.payload}
            async with sem:
                try:
                    res = await self.zd.request("POST", "triggers/validate", json_body={"trigger": body},
                                                access_method="GET")
                except ZendeskError as exc:
                    if "HTTP 404" in str(exc) or "HTTP 403" in str(exc):
                        self.warnings.append("Trigger validation endpoint unavailable; triggers weren't pre-validated.")
                        return
                    self.errors.append(f"triggers {step.name!r}: validation failed: {exc}")
                    return
            if isinstance(res, dict) and res.get("valid") is False:
                self.errors.append(f"triggers {step.name!r}: Zendesk says invalid: {_clip(res.get('errors'), 500)}")

        await asyncio.gather(*(check(s) for s in checks))


def _same_id(a: Any, b: Any) -> bool:
    if isinstance(a, Pending) or isinstance(b, Pending):
        return str(a) == str(b)
    return str(a) == str(b)


# ----------------------------------------------------------------------------- executor

class Executor:
    def __init__(self, tk: Toolkit, plan: Plan, plan_id: str):
        self.tk, self.zd, self.plan, self.note = tk, tk.zd, plan, plan_id
        self.created: dict[tuple[str, str], Any] = {}
        self.reg = plan.registry

    def m(self, value: Any) -> Any:
        return self.reg.materialize(value, self.created)

    async def run(self, step: Step) -> dict[str, Any]:
        out: dict[str, Any] = {"section": step.section, "action": step.action, "name": step.name, "change_ids": []}

        async def w(method: str, path: str, **kw: Any) -> Any:
            body, ch = await self.zd.write(method, path, note=self.note, **kw)
            if ch:
                out["change_ids"].append(ch)
            return body

        if step.action == "settings":
            await w("PUT", "account/settings", json_body={"settings": step.payload}, before=step.live, wrapper="settings")
            return out
        if step.action == "reorder":
            order = [self.m(o) for o in step.payload["order"]]  # ids resolved at plan time (new ones now)
            res = await reorder_items(self.zd, step.section, order, note=self.note)
            out["change_ids"] += res["change_ids"]
            return out
        if step.section == "agents":
            await self._agent(step, w, out)
            return out
        sec = SECTIONS[step.section]
        payload = self.m(step.payload)
        if step.section == "views" and "conditions" in payload:
            conds = payload.pop("conditions") or {}
            payload["all"], payload["any"] = conds.get("all") or [], conds.get("any") or []
        if step.action == "create":
            body = await w("POST", sec.path, json_body={sec.wrapper: payload}, wrapper=sec.wrapper)
            obj = (body or {}).get(sec.wrapper) or {}
            new_id = obj.get("id")
            if new_id is None:
                raise ZendeskError(f"Zendesk didn't return an id for the new {sec.wrapper}.")
            self.created[(sec.kind, _key(step.name))] = new_id
            out["id"] = new_id
            await self._after(step, new_id, w)
            return out
        if step.action == "update":
            if payload:
                before = {k: (step.live or {}).get(k) for k in payload}
                if step.section in ("views",) and ("all" in payload or "any" in payload):
                    before["all"] = ((step.live or {}).get("conditions") or {}).get("all", [])
                    before["any"] = ((step.live or {}).get("conditions") or {}).get("any", [])
                await w(sec.update_method, f"{sec.path}/{step.live_id}", json_body={sec.wrapper: payload},
                        before=before, wrapper=sec.wrapper)
            out["id"] = step.live_id
            await self._after(step, step.live_id, w)
            return out
        if step.action == "delete":
            await w("DELETE", f"{sec.path}/{step.live_id}", before=step.live, wrapper=sec.wrapper)
            out["id"] = step.live_id
            return out
        raise ZendeskError(f"Unknown action {step.action}")

    async def _after(self, step: Step, obj_id: Any, w: Any) -> None:
        if step.section == "schedules":
            if step.extra.get("intervals") is not None:
                before = {"intervals": [{"start_time": i.get("start_time"), "end_time": i.get("end_time")}
                                        for i in (step.live or {}).get("intervals") or []]} if step.live else None
                await w("PUT", f"business_hours/schedules/{obj_id}/workweek",
                        json_body={"workweek": {"intervals": step.extra["intervals"]}}, before=before,
                        wrapper="workweek")
            for h in step.extra.get("holidays_create") or []:
                await w("POST", f"business_hours/schedules/{obj_id}/holidays",
                        json_body={"holiday": {k: v for k, v in h.items() if k != "id"}}, wrapper="holiday")
            for h in step.extra.get("holidays_update") or []:
                live_h = next((x for x in (step.live or {}).get("holidays") or [] if x.get("id") == h["id"]), None)
                payload = {k: v for k, v in h.items() if k != "id"}
                await w("PUT", f"business_hours/schedules/{obj_id}/holidays/{h['id']}", json_body={"holiday": payload},
                        before={k: (live_h or {}).get(k) for k in payload}, wrapper="holiday")
        if step.section == "skills":
            values = step.extra.get("new_values") or ([] if step.action == "update" else
                                                      [v for v in (step.extra.get("all_values") or [])])
            for v in values:
                body = await w("POST", f"routing/attributes/{obj_id}/values",
                               json_body={"attribute_value": {"name": v}}, wrapper="attribute_value")
                vid = ((body or {}).get("attribute_value") or {}).get("id")
                self.created[("skill", _key(f"{step.name}/{v}"))] = vid

    async def _agent(self, step: Step, w: Any, out: dict) -> None:
        extra = step.extra
        if step.action == "create":
            payload = self.m(step.payload)
            if not extra.get("send_verification_email", True):
                payload["skip_verify_email"] = True
            body = await w("POST", "users", json_body={"user": payload}, wrapper="user")
            uid = body["user"]["id"]
            self.created[("user", _key(step.name))] = uid
            have: dict[str, dict] = {}
            add = extra["groups"]
            remove: list[dict] = []
        else:
            uid = step.live_id
            if step.payload:
                payload = self.m(step.payload)
                await w("PUT", f"users/{uid}", json_body={"user": payload},
                        before={k: (step.live or {}).get(k) for k in payload}, wrapper="user")
            have = {str(m["group_id"]): m for m in extra.get("memberships") or []}
            add, remove = extra.get("add") or [], extra.get("remove") or []
        out["id"] = uid
        new_members: dict[str, dict] = {}
        for g in add:
            gid = self.m(g)
            body = await w("POST", "group_memberships", json_body={"group_membership": {"user_id": uid, "group_id": gid}},
                           wrapper="group_membership")
            new_members[str(gid)] = (body or {}).get("group_membership") or {}
        for mem in remove:
            await w("DELETE", f"group_memberships/{mem['id']}", before=mem, wrapper="group_membership")
        default = extra.get("default_group")
        if default is not None:
            gid = str(self.m(default))
            current_default = next((k for k, v in have.items() if v.get("default")), None)
            if gid != current_default:
                mid = (new_members.get(gid) or have.get(gid) or {}).get("id")
                if mid:
                    await w("PUT", f"users/{uid}/group_memberships/{mid}/make_default")
        if extra.get("skills") is not None:
            ids = [str(self.m(s)) for s in extra["skills"]]
            before = {"attribute_value_ids": [str(x) for x in extra.get("skills_before") or []]}
            await w("POST", f"routing/agents/{uid}/instance_values", json_body={"attribute_value_ids": ids},
                    before=before)


# ----------------------------------------------------------------------------- export

def _reverse_maps(live: dict[str, list[dict]]) -> dict[str, dict[str, str]]:
    maps: dict[str, dict[str, str]] = {}
    for name, sec in SECTIONS.items():
        for it in live.get(name, []):
            if it.get(sec.identity) is not None:
                maps.setdefault(sec.kind, {})[str(it["id"])] = str(it.get(sec.identity))
    return maps


def to_refs(value: Any, maps: dict[str, dict[str, str]]) -> Any:
    """Turn ids in rule conditions/actions back into @references."""
    def ref(kind: str, v: Any) -> Any:
        name = maps.get(kind, {}).get(str(v))
        return f"@{kind}:{name}" if name else v

    if isinstance(value, list):
        return [to_refs(v, maps) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: to_refs(v, maps) for k, v in value.items()}
    f, v = value.get("field"), value.get("value")
    if isinstance(f, str):
        if f.startswith("custom_fields_") and f[14:] in maps.get("field", {}):
            out["field"] = f"@custom_field:{maps['field'][f[14:]]}"
        simple = {"group_id": "group", "ticket_form_id": "form", "brand_id": "brand", "set_schedule": "schedule",
                  "schedule_id": "schedule"}
        if f in simple and not isinstance(v, list):
            out["value"] = ref(simple[f], v)
        if f == "custom_status_id":
            out["value"] = [ref("status", x) for x in v] if isinstance(v, list) else ref("status", v)
        if f in ("notification_webhook", "notification_group") and isinstance(v, list) and v:
            out["value"] = [ref("webhook" if f == "notification_webhook" else "group", v[0])] + list(v[1:])
    r = value.get("restriction")
    if isinstance(r, dict) and r.get("type") == "Group":
        out["restriction"] = {**r, "ids": [ref("group", x) for x in r.get("ids") or []]}
        out["restriction"].pop("id", None)
    return out


EXPORT_KEYS = {
    "groups": ("name", "description", "is_public"),
    "custom_roles": ("name", "description", "configuration"),
    "brands": ("name", "subdomain", "brand_url", "active", "default"),
    "schedules": ("name", "time_zone", "intervals", "holidays"),
    "trigger_categories": ("name",),
    "custom_statuses": ("agent_label", "end_user_label", "status_category", "description", "active"),
    "user_fields": ("key", "title", "type", "description", "active", "custom_field_options"),
    "organization_fields": ("key", "title", "type", "description", "active", "custom_field_options"),
    "ticket_fields": ("title", "type", "description", "agent_description", "title_in_portal", "active", "required",
                      "required_in_portal", "visible_in_portal", "editable_in_portal", "custom_field_options", "tag"),
    "webhooks": ("name", "endpoint", "http_method", "request_format", "status", "subscriptions", "description"),
    "skills": ("name", "values"),
    "ticket_forms": ("name", "display_name", "active", "default", "end_user_visible", "in_all_brands",
                     "ticket_field_ids", "agent_conditions", "end_user_conditions"),
    "sla_policies": ("title", "description", "filter", "policy_metrics"),
    "triggers": ("title", "description", "active", "category_id", "conditions", "actions"),
    "automations": ("title", "active", "conditions", "actions"),
    "views": ("title", "description", "active", "conditions", "output", "restriction"),
    "macros": ("title", "description", "active", "actions", "restriction"),
}


def export_section(name: str, items: list[dict], maps: dict[str, dict[str, str]]) -> list[dict]:
    keys = EXPORT_KEYS[name]
    out = []
    for it in items:
        if name == "ticket_fields" and not it.get("removable", True):
            continue  # system fields (subject, status, ...) aren't managed
        row = {k: it.get(k) for k in keys if it.get(k) not in (None, [], {})}
        if "custom_field_options" in row:
            row["custom_field_options"] = [{"name": o.get("name"), "value": o.get("value")} for o in row["custom_field_options"]]
        if name == "schedules":
            row["intervals"] = [{"start_time": i.get("start_time"), "end_time": i.get("end_time")} for i in row.get("intervals") or []]
            row["holidays"] = [{"name": h.get("name"), "start_date": h.get("start_date"), "end_date": h.get("end_date")}
                               for h in it.get("holidays") or []]
        if name == "skills":
            row["values"] = [v.get("name") for v in it.get("values") or []]
        if name == "triggers" and row.get("category_id"):
            cat = maps.get("category", {}).get(str(row.pop("category_id")))
            if cat:
                row["category"] = cat
        if name == "ticket_forms":
            row["ticket_field_ids"] = [f"@field:{maps['field'][str(i)]}" if str(i) in maps.get("field", {}) else i
                                       for i in row.get("ticket_field_ids") or []]
            for cname in ("agent_conditions", "end_user_conditions"):
                if row.get(cname):
                    row[cname] = [{**c, "parent_field_id": f"@field:{maps['field'][str(c['parent_field_id'])]}"
                                   if str(c.get("parent_field_id")) in maps.get("field", {}) else c.get("parent_field_id"),
                                   "child_fields": [{**ch, "id": f"@field:{maps['field'][str(ch['id'])]}"
                                                     if str(ch.get("id")) in maps.get("field", {}) else ch.get("id")}
                                                    for ch in c.get("child_fields") or []]}
                                  for c in row[cname]]
        for k in ("conditions", "actions", "filter", "restriction"):
            if k in row:
                row[k] = to_refs(row[k], maps) if k != "restriction" else to_refs({"restriction": row[k]}, maps)["restriction"]
        out.append(row)
    return out


async def _export_agents(zd: ZendeskClient, maps: dict[str, dict[str, str]], include_suspended: bool) -> list[dict]:
    users = (await zd.paginate("users", "users", params={"role[]": ["agent", "admin"]}, limit=100000))["users"]
    members = (await zd.paginate("group_memberships", "group_memberships", limit=100000))["group_memberships"]
    by_user: dict[int, list[dict]] = {}
    for m in members:
        by_user.setdefault(m["user_id"], []).append(m)
    out = []
    for u in sorted(users, key=lambda x: (x.get("email") or "")):
        if u.get("suspended") and not include_suspended:
            continue
        ms = by_user.get(u["id"], [])
        row: dict[str, Any] = {"email": u.get("email"), "name": u.get("name"), "role": u.get("role")}
        if u.get("custom_role_id") and str(u["custom_role_id"]) in maps.get("role", {}):
            row["custom_role"] = maps["role"][str(u["custom_role_id"])]
        row["groups"] = [maps.get("group", {}).get(str(m["group_id"]), m["group_id"]) for m in ms]
        default = next((m for m in ms if m.get("default")), None)
        if default:
            row["default_group"] = maps.get("group", {}).get(str(default["group_id"]), default["group_id"])
        if u.get("suspended"):
            row["suspended"] = True
        out.append(row)
    return out


# ----------------------------------------------------------------------------- tools

def register(tk: Toolkit) -> None:
    zd = tk.zd
    plans: dict[str, dict[str, Any]] = tk.cache.setdefault("blueprint_plans", {})

    def read_blueprint(blueprint: dict | None, blueprint_file: str | None) -> dict[str, Any]:
        if bool(blueprint) == bool(blueprint_file):
            raise ZendeskError("Pass exactly one of `blueprint` (an object) or `blueprint_file` (a .json/.yaml path "
                               "inside ZENDESK_FILE_ROOT).")
        if blueprint:
            return blueprint
        p = _confine(tk.settings.file_root, blueprint_file)
        if not p.exists():
            raise ZendeskError(f"Blueprint file not found: {p}")
        text = p.read_text()
        if p.suffix.lower() in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        if not isinstance(data, dict):
            raise ZendeskError("A blueprint must be a mapping of sections.")
        return data

    def save(kind: str, name: str, doc: Any) -> str | None:
        if not tk.settings.file_root:
            return None
        folder = _confine(tk.settings.file_root, kind)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text(json.dumps(doc, indent=1, default=str))
        return str(path)

    @tk.tool("read", title="Plan a blueprint")
    async def blueprint_plan(
        blueprint: Annotated[dict[str, Any] | None, Field(description=(
            "Desired configuration. Sections (lists of objects, matched to existing ones by name): "
            + ", ".join(f"{n} (by {s.identity})" for n, s in SECTIONS.items())
            + ". Also `settings` ({section: {key: value}}), `order` ({triggers|automations|views|macros|ticket_forms|"
              "sla_policies: [titles]}) and `delete` ({section: [names or ids]}). Refer to other objects with "
            + REF_HELP + " Extras: triggers `category`; schedules `intervals` (minutes from Sunday 00:00) and "
              "`holidays`; skills `values`; agents `groups`, `default_group`, `custom_role`, `skills` "
              "('Attribute/Value'), `remove_other_groups`, `attributes`; dropdown fields `prune_options`."))] = None,
        blueprint_file: Annotated[str | None, Field(description="Or a .json/.yaml blueprint file inside ZENDESK_FILE_ROOT.")] = None,
        validate: Annotated[bool, Field(description="Ask Zendesk to validate triggers that don't depend on new objects.")] = True,
        show_steps: Annotated[int, Field(ge=0, le=2000, description="Max steps listed in the response.")] = 300,
    ) -> str:
        """Dry run of a configuration rollout: compares the blueprint with the live account and lists every
        create, update (before → after), reorder and delete, with warnings (e.g. deleting something rules still use)
        and blocking errors (unknown references, ambiguous names, missing required fields, invalid triggers).
        Nothing changes. If there are no errors it returns a plan_id for blueprint_apply."""
        bp = read_blueprint(blueprint, blueprint_file)
        plan = await Planner(tk, bp, validate).plan()
        result: dict[str, Any] = {"summary": plan.summary(), "errors": plan.errors, "warnings": plan.warnings}
        maps = plan.display_maps()
        result["steps"] = [s.display(maps) for s in plan.steps[:show_steps]]
        if len(plan.steps) > show_steps:
            result["steps_note"] = f"Showing {show_steps} of {len(plan.steps)} steps."
        if plan.errors:
            result["status"] = "blocked"
            result["next"] = "Fix the errors and plan again."
        elif not plan.steps:
            result["status"] = "no_changes"
        else:
            plan_id = "bp_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(2)
            plans[plan_id] = {"blueprint": bp, "digest": plan.digest, "created": time.time(), "validate": validate}
            result.update(status="ready", plan_id=plan_id, steps_total=len(plan.steps),
                          next=f"Review with the user, then blueprint_apply(plan_id='{plan_id}'). Plans expire in 2 hours.")
            saved = save("plans", f"{plan_id}.json", {"plan_id": plan_id, "blueprint": redact(bp),
                                                      "steps": [s.display(maps) for s in plan.steps],
                                                      "warnings": plan.warnings})
            if saved:
                result["plan_file"] = saved
        return tk.out(result)

    @tk.tool("write", title="Apply a blueprint", destructive_hint=True)
    async def blueprint_apply(
        plan_id: Annotated[str, Field(description="From blueprint_plan.")],
        continue_on_error: Annotated[bool, Field(description="Keep going after a failed step (default: stop).")] = False,
        allow_drift: Annotated[bool, Field(description="Apply even if the account changed since the plan was made "
                                           "(the fresh plan is applied).")] = False,
    ) -> str:
        """Carry out a reviewed blueprint plan. Re-plans first and refuses if the account changed since the plan
        was reviewed, then applies steps in dependency order. Every change is journaled under the plan id —
        revert_change(batch=plan_id) undoes the rollout."""
        stored = plans.get(plan_id)
        if not stored or time.time() - stored["created"] > PLAN_TTL:
            raise ZendeskError(f"Plan {plan_id!r} is unknown or expired; run blueprint_plan again.")
        plan = await Planner(tk, stored["blueprint"], stored["validate"]).plan()
        if plan.errors:
            return tk.out({"status": "blocked", "errors": plan.errors,
                           "next": "The account changed in a way that breaks the blueprint; fix and re-plan."})
        if plan.digest != stored["digest"] and not allow_drift:
            new_id = "bp_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(2)
            plans[new_id] = {**stored, "digest": plan.digest, "created": time.time()}
            return tk.out({"status": "drift", "message": "The account changed since this plan was reviewed, so the "
                           "steps differ. Review the new plan below; apply it with its plan_id.",
                           "new_plan_id": new_id, "summary": plan.summary(),
                           "steps": [s.display(plan.display_maps()) for s in plan.steps[:300]]})
        ex = Executor(tk, plan, plan_id)
        done, failed, skipped = [], [], []
        for i, step in enumerate(plan.steps):
            if failed and not continue_on_error:
                skipped.append({"section": step.section, "action": step.action, "name": step.name})
                continue
            try:
                done.append(await ex.run(step))
            except ZendeskError as exc:
                failed.append({"step": i + 1, "section": step.section, "action": step.action, "name": step.name,
                               "error": str(exc)})
        plans.pop(plan_id, None)
        result: dict[str, Any] = {
            "status": "applied" if not failed else ("partial" if done else "failed"),
            "batch": plan_id, "applied": len(done), "failed": failed, "skipped": len(skipped),
            "results": [{k: v for k, v in r.items() if v not in ([], None)} for r in done],
        }
        if skipped:
            result["skipped_steps"] = skipped[:50]
        if failed or done:
            result["undo"] = f"revert_change(batch='{plan_id}') rolls back everything this apply changed."
        saved = save("plans", f"{plan_id}-result.json", result)
        if saved:
            result["result_file"] = saved
        return tk.out(result)

    @tk.tool("read", title="Export as blueprint", read_only_hint=False)
    async def blueprint_export(
        sections: Annotated[list[Literal[tuple(EXPORT_KEYS) + ("agents",)]] | None,  # type: ignore[valid-type]
                            Field(description="Sections to export (default: all supported, including agents).")] = None,
        include_inactive: bool = True,
        filename: Annotated[str | None, Field(description="Write to this file under ZENDESK_FILE_ROOT/blueprints "
                                              "(.json or .yaml). Default: a timestamped .yaml.")] = None,
        inline: Annotated[bool, Field(description="Also return the blueprint in the response (can be large).")] = False,
    ) -> str:
        """Export the current configuration as a blueprint, with IDs turned into @references. Use it as the
        starting point for changes, to compare against an architecture plan, or as config-as-code."""
        chosen = sections or list(EXPORT_KEYS) + ["agents"]
        need = (set(chosen) - {"agents"}) | {"custom_roles"} | {"groups", "ticket_fields", "custom_statuses", "ticket_forms", "brands", "schedules",
                              "webhooks", "trigger_categories"}
        planner = Planner(tk, {n: [] for n in need}, validate=False)
        planner.bp = {n: [] for n in need}
        await planner.load()
        maps = _reverse_maps(planner.live)
        doc: dict[str, Any] = {"version": 1, "description": f"Exported from {tk.settings.host} on "
                                                            f"{datetime.now(timezone.utc).date().isoformat()}"}
        for name in chosen:
            if name == "agents":
                doc["agents"] = await _export_agents(zd, maps, include_inactive)
                continue
            items = planner.live.get(name, [])
            if not include_inactive:
                items = [i for i in items if i.get("active", True) is not False]
            doc[name] = export_section(name, items, maps)
        counts = {k: len(v) for k, v in doc.items() if isinstance(v, list)}
        result: dict[str, Any] = {"counts": counts}
        if tk.settings.file_root:
            name = filename or f"export-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.yaml"
            folder = _confine(tk.settings.file_root, "blueprints")
            folder.mkdir(parents=True, exist_ok=True)
            path = _confine(tk.settings.file_root, f"blueprints/{Path(name).name}")
            if path.suffix.lower() in (".yaml", ".yml"):
                import yaml
                path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120))
            else:
                path.write_text(json.dumps(doc, indent=1, default=str))
            result["file"] = str(path)
            result["blueprint_file"] = f"blueprints/{path.name}"
        if inline or not tk.settings.file_root:
            result["blueprint"] = doc
        return tk.out(result)
