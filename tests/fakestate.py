"""A small stateful fake of the Zendesk admin API, enough to test plan -> apply -> re-plan -> revert."""

from __future__ import annotations

import copy
import itertools
import json
import re
from typing import Any

import httpx

from conftest import FakeZendesk

COLLECTIONS = {  # path -> (list key, wrapper)
    "groups": ("groups", "group"),
    "custom_roles": ("custom_roles", "custom_role"),
    "brands": ("brands", "brand"),
    "business_hours/schedules": ("schedules", "schedule"),
    "trigger_categories": ("trigger_categories", "trigger_category"),
    "custom_statuses": ("custom_statuses", "custom_status"),
    "user_fields": ("user_fields", "user_field"),
    "organization_fields": ("organization_fields", "organization_field"),
    "ticket_fields": ("ticket_fields", "ticket_field"),
    "webhooks": ("webhooks", "webhook"),
    "routing/attributes": ("attributes", "attribute"),
    "ticket_forms": ("ticket_forms", "ticket_form"),
    "users": ("users", "user"),
    "slas/policies": ("sla_policies", "sla_policy"),
    "triggers": ("triggers", "trigger"),
    "automations": ("automations", "automation"),
    "views": ("views", "view"),
    "macros": ("macros", "macro"),
    "group_memberships": ("group_memberships", "group_membership"),
    "targets": ("targets", "target"),
    "queues": ("queues", "queue"),
    "dynamic_content/items": ("items", "item"),
    "recipient_addresses": ("recipient_addresses", "recipient_address"),
    "group_slas/policies": ("group_sla_policies", "group_sla_policy"),
}
NESTED = {  # regex -> (store key, list key, wrapper, parent field)
    r"business_hours/schedules/(\d+)/holidays": ("holidays", "holidays", "holiday", "schedule_id"),
    r"routing/attributes/(\d+)/values": ("attribute_values", "attribute_values", "attribute_value", "attribute_id"),
}


class StatefulZendesk(FakeZendesk):
    def __init__(self) -> None:
        super().__init__()
        self.store: dict[str, dict[str, dict]] = {k: {} for k in COLLECTIONS}
        self.store["holidays"] = {}
        self.store["attribute_values"] = {}
        self.instance_values: dict[str, list[str]] = {}
        self.settings: dict[str, dict] = {"tickets": {"comments_public_by_default": True, "list_newest_comments_first": True},
                                          "agents": {"agent_workspace": True}}
        self._ids = itertools.count(1000)
        self._clock = itertools.count(1)

    # ------------------------------------------------------------ seeding
    def add(self, path: str, obj: dict) -> dict:
        obj = copy.deepcopy(obj)
        obj.setdefault("id", next(self._ids) if path != "trigger_categories" else str(next(self._ids)))
        obj.setdefault("position", len(self.store[path]))
        obj["updated_at"] = self._now()
        self.store[path][str(obj["id"])] = obj
        return obj

    def find(self, path: str, **match: Any) -> dict | None:
        for o in self.store[path].values():
            if all(o.get(k) == v for k, v in match.items()):
                return o
        return None

    def _now(self) -> str:
        return f"2026-10-23T00:00:{next(self._clock):02d}Z"

    # ------------------------------------------------------------ dispatch
    def handle(self, req: httpx.Request) -> httpx.Response:
        for method, path, h in reversed(self.routes):
            if req.method == method and req.url.path == path:
                self.calls.append(req)
                return h(req)
        self.calls.append(req)
        rel = req.url.path.removeprefix("/api/v2/").removesuffix(".json")
        body = json.loads(req.content) if req.content else {}
        try:
            out = self._route(req.method, rel, body, req)
        except KeyError as exc:
            return httpx.Response(404, json={"error": "RecordNotFound", "description": str(exc)})
        if isinstance(out, httpx.Response):
            return out
        return httpx.Response(200, json=out)

    def _route(self, method: str, rel: str, body: dict, req: httpx.Request) -> Any:
        q = req.url.params
        if rel == "account/settings":
            if method == "PUT":
                for sec, vals in body["settings"].items():
                    self.settings.setdefault(sec, {}).update(vals)
            return {"settings": self.settings}
        if rel == "triggers/validate":
            t = body["trigger"]
            ok = not str(t.get("title", "")).startswith("BAD")
            return {"valid": ok, "errors": [] if ok else [{"description": "Bad trigger"}]}
        if rel == "users/search":
            needle = q["query"].lower()
            return {"users": [u for u in self.store["users"].values() if needle in (u.get("email") or "").lower()]}
        m = re.fullmatch(r"(triggers|automations|views|macros)/update_many", rel)
        if m:
            for upd in body[m.group(1)]:
                self.store[m.group(1)][str(upd["id"])].update(upd)
            return {m.group(1): []}
        if rel == "ticket_forms/reorder":
            for pos, fid in enumerate(body["ticket_form_ids"]):
                self.store["ticket_forms"][str(fid)]["position"] = pos
            return {"ticket_forms": []}
        m = re.fullmatch(r"business_hours/schedules/(\d+)/workweek", rel)
        if m:
            self.store["business_hours/schedules"][m.group(1)]["intervals"] = body["workweek"]["intervals"]
            return {"workweek": body["workweek"]}
        m = re.fullmatch(r"users/(\d+)/group_memberships", rel)
        if m:
            return {"group_memberships": [g for g in self.store["group_memberships"].values()
                                          if str(g["user_id"]) == m.group(1)], "meta": {"has_more": False}}
        m = re.fullmatch(r"users/(\d+)/group_memberships/(\d+)/make_default", rel)
        if m:
            for g in self.store["group_memberships"].values():
                if str(g["user_id"]) == m.group(1):
                    g["default"] = str(g["id"]) == m.group(2)
            return {"group_memberships": []}
        m = re.fullmatch(r"routing/agents/(\d+)/instance_values", rel)
        if m:
            if method == "POST":
                self.instance_values[m.group(1)] = list(body["attribute_value_ids"])
            ids = self.instance_values.get(m.group(1), [])
            return {"attribute_values": [{"id": i} for i in ids]}
        for pattern, (store, list_key, wrapper, parent) in NESTED.items():
            m = re.fullmatch(pattern + r"(?:/(\w+))?", rel)
            if m:
                pid, oid = m.group(1), m.group(2)
                items = self.store[store]
                if oid is None and method == "GET":
                    return {list_key: [o for o in items.values() if str(o[parent]) == pid]}
                if oid is None and method == "POST":
                    obj = {**body[wrapper], "id": str(next(self._ids)), parent: pid}
                    items[obj["id"]] = obj
                    return httpx.Response(201, json={wrapper: obj})
                if method in ("PUT", "PATCH"):
                    items[oid].update(body[wrapper])
                    return {wrapper: items[oid]}
                if method == "DELETE":
                    items.pop(oid)
                    return httpx.Response(204)
        for path, (list_key, wrapper) in sorted(COLLECTIONS.items(), key=lambda kv: -len(kv[0])):
            if rel == path:
                if method == "GET":
                    items = list(self.store[path].values())
                    if path == "users" and q.get_list("role[]"):
                        items = [u for u in items if u.get("role") in q.get_list("role[]")]
                    return {list_key: items, "meta": {"has_more": False}, "links": {}}
                if method == "POST":
                    obj = copy.deepcopy(body[wrapper])
                    obj.pop("skip_verify_email", None)
                    if path == "views":
                        obj["conditions"] = {"all": obj.pop("all", []), "any": obj.pop("any", [])}
                    if path in ("ticket_fields", "user_fields", "organization_fields"):
                        for opt in obj.get("custom_field_options") or []:
                            opt.setdefault("id", next(self._ids))
                    if path == "group_memberships":
                        obj.setdefault("default", not any(g["user_id"] == obj["user_id"]
                                                          for g in self.store[path].values()))
                    created = self.add(path, obj)
                    return httpx.Response(201, json={wrapper: created})
            if rel.startswith(path + "/") and "/" not in rel[len(path) + 1:]:
                oid = rel[len(path) + 1:]
                obj = self.store[path][oid]
                if method == "GET":
                    return {wrapper: obj}
                if method in ("PUT", "PATCH"):
                    upd = copy.deepcopy(body[wrapper])
                    if path == "views" and ("all" in upd or "any" in upd):
                        obj["conditions"] = {"all": upd.pop("all", []), "any": upd.pop("any", [])}
                    if path in ("ticket_fields", "user_fields", "organization_fields"):
                        for opt in upd.get("custom_field_options") or []:
                            opt.setdefault("id", next(self._ids))
                    obj.update(upd)
                    obj["updated_at"] = self._now()
                    if path == "webhooks":
                        return httpx.Response(204)
                    return {wrapper: obj}
                if method == "DELETE":
                    self.store[path].pop(oid)
                    return httpx.Response(204)
        raise KeyError(f"{method} {rel}")
