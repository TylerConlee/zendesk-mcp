"""Regression tests for the independent review of the write paths (one test per finding)."""

import json

import httpx
import pytest
from mcp.client import Client

from conftest import data, make_settings, text
from fakestate import StatefulZendesk
from zendesk_mcp.journal import redact
from zendesk_mcp.server import build_server


@pytest.fixture
def zs():
    s = StatefulZendesk()
    s.add("groups", {"name": "Support"})
    s.add("custom_roles", {"name": "Light Agent"})
    return s


@pytest.fixture
def sconnect(zs, tmp_path):
    def _c(**kw):
        kw.setdefault("file_root", str(tmp_path))
        kw.setdefault("journal", True)
        mcp, _ = build_server(make_settings(**kw), transport=httpx.MockTransport(zs.handle))
        return Client(mcp)
    return _c


def last_body(zs, method, fragment):
    call = [c for c in zs.calls if c.method == method and fragment in c.url.path][-1]
    return json.loads(call.content)


# 1 ---------------------------------------------------------------------------------------------
async def test_partial_rule_updates_keep_the_rest(sconnect, zs):
    t = zs.add("triggers", {"title": "T", "conditions": {"all": [{"field": "status", "operator": "is", "value": "new"}],
                                                          "any": []}, "actions": [{"field": "status", "value": "open"}]})
    v = zs.add("views", {"title": "V", "conditions": {"all": [{"field": "status", "operator": "is", "value": "open"}],
                                                       "any": [{"field": "priority", "operator": "is", "value": "high"}]}})
    f = zs.add("ticket_fields", {"title": "F", "type": "tagger", "custom_field_options": [
        {"id": 1, "name": "A", "value": "a"}, {"id": 2, "name": "B", "value": "b"}]})
    async with sconnect() as cl:
        out = data(await cl.call_tool("update_resource", {"resource": "triggers", "id": t["id"], "data": {
            "conditions": {"all": [{"field": "status", "operator": "is", "value": "pending"}]}}}))
        assert any("existing actions" in n for n in out["notes"])
        body = last_body(zs, "PUT", "/triggers/")["trigger"]
        assert body["actions"] == [{"field": "status", "value": "open"}] and body["conditions"]["any"] == []
        data(await cl.call_tool("update_resource", {"resource": "views", "id": v["id"], "data": {
            "all": [{"field": "status", "operator": "is", "value": "pending"}]}}))
        assert last_body(zs, "PUT", "/views/")["view"]["any"] == [{"field": "priority", "operator": "is", "value": "high"}]
        r = await cl.call_tool("update_resource", {"resource": "ticket_fields", "id": f["id"], "data": {
            "custom_field_options": [{"name": "A", "value": "a"}]}})
        assert r.is_error and "would delete options ['B']" in text(r)
        out = data(await cl.call_tool("update_resource", {"resource": "ticket_fields", "id": f["id"], "allow_option_removal": True,
                                                          "data": {"custom_field_options": [{"name": "A", "value": "a"}]}}))
        assert out["notes"] == ["removed options ['B']"]


# 2 ---------------------------------------------------------------------------------------------
async def test_revert_of_create_or_update_post_never_deletes(sconnect, zs):
    zs.on("POST", "/api/v2/ticket_fields/5/options",
          httpx.Response(200, json={"custom_field_option": {"id": 9, "name": "Renamed", "value": "x"}}))
    async with sconnect() as cl:
        out = data(await cl.call_tool("create_resource", {"resource": "ticket_field_options", "parent_id": 5,
                                                          "data": {"id": 9, "name": "Renamed"}}))
        rv = data(await cl.call_tool("revert_change", {"change_id": out["change_id"]}))
    assert rv["results"][0]["status"] == "unsupported"
    assert not [c for c in zs.calls if c.method == "DELETE"]


# 3 ---------------------------------------------------------------------------------------------
async def test_large_objects_are_fully_restorable(sconnect, zs):
    big = zs.add("ticket_fields", {"title": "Big", "type": "tagger", "custom_field_options": [
        {"id": i, "name": f"Option number {i} with a long label", "value": f"opt_{i}"} for i in range(800)]})
    async with sconnect() as cl:
        out = data(await cl.call_tool("delete_resource", {"resource": "ticket_fields", "id": big["id"]}))
        rv = data(await cl.call_tool("revert_change", {"change_id": out["change_id"]}))
    assert rv["results"][0]["status"] == "reverted"
    assert len(zs.find("ticket_fields", title="Big")["custom_field_options"]) == 800


# 4 ---------------------------------------------------------------------------------------------
async def test_provision_never_demotes_unless_asked(sconnect, zs):
    zs.add("users", {"name": "Boss", "email": "boss@acme.com", "role": "admin"})
    async with sconnect() as cl:
        out = data(await cl.call_tool("provision_agents", {"agents": [{"email": "boss@acme.com", "groups": ["Support"]}]}))
        assert zs.find("users", email="boss@acme.com")["role"] == "admin" and "role" not in out["agents"][0]["changes"]
        p = data(await cl.call_tool("blueprint_plan", {"blueprint": {"agents": [{"email": "boss@acme.com"}]}}))
    assert p["status"] == "no_changes"


# 5 + 6 -----------------------------------------------------------------------------------------
def test_redaction_rules():
    r = redact({"custom_headers": {"X-Api-Key": "k1", "Accept": "json"}, "x-api-key": "k2", "Authorization": "Bearer z",
                "api_token_access": True, "api_password_access": False, "private_attachments": True,
                "authentication": {"data": {"token": "t"}}})
    assert r == {"custom_headers": {"X-Api-Key": "[redacted]", "Accept": "[redacted]"}, "x-api-key": "[redacted]",
                 "Authorization": "[redacted]", "api_token_access": True, "api_password_access": False,
                 "private_attachments": True, "authentication": "[redacted]"}


async def test_plan_file_has_no_secrets(sconnect, zs, tmp_path):
    bp = {"webhooks": [{"name": "Pager", "endpoint": "https://p", "http_method": "POST", "request_format": "json",
                        "status": "active", "authentication": {"type": "bearer_token", "data": {"token": "SUPERSECRET"}},
                        "custom_headers": {"X-Api-Key": "HEADERSECRET"}}]}
    async with sconnect() as cl:
        p = data(await cl.call_tool("blueprint_plan", {"blueprint": bp}))
    saved = (tmp_path / "plans" / f"{p['plan_id']}.json").read_text()
    assert "SUPERSECRET" not in saved and "HEADERSECRET" not in saved
    assert "SUPERSECRET" not in json.dumps(p) and "HEADERSECRET" not in json.dumps(p)


async def test_security_settings_revert(sconnect, zs):
    zs.settings["api"] = {"api_token_access": True}
    async with sconnect() as cl:
        out = data(await cl.call_tool("account_settings", {"action": "update", "settings": {"api": {"api_token_access": False}}}))
        assert out["previous"] == {"api": {"api_token_access": True}}
        data(await cl.call_tool("revert_change", {"change_id": out["change_id"]}))
    assert zs.settings["api"]["api_token_access"] is True


# 7 ---------------------------------------------------------------------------------------------
async def test_order_is_resolved_at_plan_time(sconnect, zs):
    zs.add("triggers", {"title": "A", "position": 0, "conditions": {"all": []}, "actions": []})
    zs.add("triggers", {"title": "B", "position": 1, "conditions": {"all": []}, "actions": []})
    async with sconnect() as cl:
        p = data(await cl.call_tool("blueprint_plan", {"blueprint": {"order": {"triggers": ["B", "Typo"]}}}))
        assert p["status"] == "blocked" and "no trigger named 'Typo'" in p["errors"][0]
        new = {"title": "C", "conditions": {"all": [{"field": "status", "operator": "is", "value": "new"}]},
               "actions": [{"field": "status", "value": "open"}]}
        zs.add("triggers", {"title": "Z", "position": 2, "conditions": {"all": []}, "actions": []})
        p = data(await cl.call_tool("blueprint_plan", {"blueprint": {"triggers": [new], "order": {"triggers": ["Z", "C", "A"]}}}))
        assert p["status"] == "ready", p["errors"]
        r = data(await cl.call_tool("blueprint_apply", {"plan_id": p["plan_id"]}))
        assert r["status"] == "applied", r
    pos = {t["title"]: t["position"] for t in zs.store["triggers"].values()}
    assert pos["Z"] < pos["C"] < pos["A"]


# 8 ---------------------------------------------------------------------------------------------
async def test_retried_delete_that_succeeded_is_ok(sconnect, zs, monkeypatch):
    monkeypatch.setattr("zendesk_mcp.client.asyncio.sleep", lambda *_: _noop())
    g = zs.add("groups", {"name": "Gone"})
    attempts = {"n": 0}

    def flaky(req):
        attempts["n"] += 1
        if attempts["n"] == 1:
            zs.store["groups"].pop(str(g["id"]))  # the server did delete it...
            return httpx.Response(502)              # ...but the response was lost
        return httpx.Response(404, json={"error": "RecordNotFound"})

    zs.on("DELETE", f"/api/v2/groups/{g['id']}", flaky)
    async with sconnect() as cl:
        out = data(await cl.call_tool("delete_resource", {"resource": "groups", "id": g["id"]}))
        changes = data(await cl.call_tool("list_changes", {}))["changes"]
    assert out["note"] == "deleted (confirmed on retry)" and changes[0]["ok"] and changes[0]["revertible"]


async def _noop():
    return None


# 9 + 11 ----------------------------------------------------------------------------------------
async def test_offboard_revert_restores_custom_role(sconnect, zs):
    role = zs.find("custom_roles", name="Light Agent")
    u = zs.add("users", {"name": "Lee", "email": "lee@acme.com", "role": "agent", "custom_role_id": role["id"]})
    zs.on("GET", "/api/v2/search/count", {"count": 0})
    async with sconnect() as cl:
        out = data(await cl.call_tool("offboard_agent", {"user": "lee@acme.com", "mode": "downgrade_and_suspend",
                                                         "dry_run": False}))
        puts = [c for c in zs.calls if c.method == "PUT" and c.url.path.endswith(f"/users/{u['id']}")]
        assert len(puts) == 1 and json.loads(puts[0].content) == {"user": {"role": "end-user", "suspended": True}}
        zs.find("users", email="lee@acme.com")["custom_role_id"] = None  # Zendesk drops it on downgrade
        rv = data(await cl.call_tool("revert_change", {"batch": out["batch"]}))
        assert rv["summary"] == {"reverted": 1}, rv
    user = zs.find("users", email="lee@acme.com")
    assert (user["role"], user["suspended"], user["custom_role_id"]) == ("agent", False, role["id"])


async def test_offboard_refuses_up_front_in_no_delete(sconnect, zs):
    zs.add("users", {"name": "Lee", "email": "lee@acme.com", "role": "agent"})
    async with sconnect(access_mode="no_delete") as cl:
        r = await cl.call_tool("offboard_agent", {"user": "lee@acme.com", "dry_run": False})
        assert r.is_error and "remove_group_memberships=false" in text(r)
        r = await cl.call_tool("offboard_agent", {"user": "lee@acme.com", "mode": "delete",
                                                  "remove_group_memberships": False, "dry_run": False})
        assert r.is_error
    assert not [c for c in zs.calls if c.method != "GET"]


# 10 --------------------------------------------------------------------------------------------
async def test_unreadable_skills_block_the_plan(sconnect, zs):
    u = zs.add("users", {"name": "Sam", "email": "sam@acme.com", "role": "agent"})
    a = zs.add("routing/attributes", {"name": "Language"})
    zs.store["attribute_values"]["v1"] = {"id": "v1", "name": "Spanish", "attribute_id": str(a["id"])}
    zs.on("GET", f"/api/v2/routing/agents/{u['id']}/instance_values", httpx.Response(500, json={"error": "boom"}))
    async with sconnect(max_retries=0) as cl:
        p = data(await cl.call_tool("blueprint_plan", {"blueprint": {"agents": [{"email": "sam@acme.com",
                                                                                 "skills": ["Language/Spanish"]}]}}))
    assert p["status"] == "blocked" and "couldn't read current skills" in p["errors"][0]


# 12 --------------------------------------------------------------------------------------------
async def test_webhook_revert_detects_later_edits_and_flags_credentials(sconnect, zs):
    hook = zs.add("webhooks", {"name": "Pager", "endpoint": "https://p", "authentication": {"type": "basic_auth",
                                                                                            "data": {"username": "u"}}})
    async with sconnect() as cl:
        out = data(await cl.call_tool("update_resource", {"resource": "webhooks", "id": hook["id"], "data": {"name": "Pager 2"}}))
        zs.find("webhooks", id=hook["id"])["updated_at"] = "2030-01-01T00:00:00Z"  # edited later by someone
        rv = data(await cl.call_tool("revert_change", {"change_id": out["change_id"]}))
        assert rv["results"][0]["status"] == "conflict"
        out = data(await cl.call_tool("delete_resource", {"resource": "webhooks", "id": hook["id"]}))
        rv = data(await cl.call_tool("revert_change", {"change_id": out["change_id"]}))
    assert rv["results"][0]["status"] == "partial" and "re-enter" in rv["results"][0]["warning"]


# second review ---------------------------------------------------------------------------------
async def test_429_then_404_delete_is_an_error(sconnect, zs, monkeypatch):
    monkeypatch.setattr("zendesk_mcp.client.asyncio.sleep", lambda *_: _noop())
    calls = {"n": 0}

    def limited(req):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}) if calls["n"] == 1 else \
            httpx.Response(404, json={"error": "RecordNotFound"})

    zs.on("DELETE", "/api/v2/macros/999999", limited)
    async with sconnect() as cl:
        r = await cl.call_tool("delete_resource", {"resource": "macros", "id": 999999})
    assert r.is_error and "HTTP 404" in text(r)


async def test_schedule_hours_revert_has_no_false_conflict(sconnect, zs):
    zs.add("business_hours/schedules", {"name": "US", "time_zone": "UTC", "intervals": [{"start_time": 1980, "end_time": 2460}]})
    bp = {"schedules": [{"name": "US", "time_zone": "UTC", "intervals": [{"start_time": 2040, "end_time": 2520}]}]}
    async with sconnect() as cl:
        p = data(await cl.call_tool("blueprint_plan", {"blueprint": bp}))
        data(await cl.call_tool("blueprint_apply", {"plan_id": p["plan_id"]}))
        rv = data(await cl.call_tool("revert_change", {"batch": p["plan_id"]}))
    assert rv["summary"] == {"reverted": 1}, rv
    assert zs.find("business_hours/schedules", name="US")["intervals"] == [{"start_time": 1980, "end_time": 2460}]


async def test_recreated_field_gets_fresh_options(sconnect, zs):
    f = zs.add("ticket_fields", {"title": "F", "type": "tagger", "custom_field_options": [{"id": 77, "name": "A", "value": "a"}]})
    async with sconnect() as cl:
        out = data(await cl.call_tool("delete_resource", {"resource": "ticket_fields", "id": f["id"]}))
        data(await cl.call_tool("revert_change", {"change_id": out["change_id"]}))
    posted = last_body(zs, "POST", "/ticket_fields")["ticket_field"]
    assert posted["custom_field_options"] == [{"name": "A", "value": "a"}]


async def test_skills_revert_detects_later_edits(sconnect, zs):
    u = zs.add("users", {"name": "Sam", "email": "sam@acme.com", "role": "agent"})
    a = zs.add("routing/attributes", {"name": "Language"})
    for vid, name in (("v1", "English"), ("v2", "Spanish")):
        zs.store["attribute_values"][vid] = {"id": vid, "name": name, "attribute_id": str(a["id"])}
    zs.instance_values[str(u["id"])] = ["v1"]
    async with sconnect() as cl:
        p = data(await cl.call_tool("blueprint_plan", {"blueprint": {"agents": [{"email": "sam@acme.com",
                                                                                 "skills": ["Language/Spanish"]}]}}))
        data(await cl.call_tool("blueprint_apply", {"plan_id": p["plan_id"]}))
        zs.instance_values[str(u["id"])] = ["v2", "v1"]  # someone edits skills afterwards
        rv = data(await cl.call_tool("revert_change", {"batch": p["plan_id"]}))
        assert rv["results"][0]["status"] == "conflict"
        rv = data(await cl.call_tool("revert_change", {"batch": p["plan_id"], "force": True}))
    assert zs.instance_values[str(u["id"])] == ["v1"]


async def test_api_request_partial_trigger_put_is_guarded(sconnect, zs):
    t = zs.add("triggers", {"title": "T", "conditions": {"all": [], "any": []}, "actions": [{"field": "status", "value": "open"}]})
    async with sconnect() as cl:
        out = data(await cl.call_tool("api_request", {"method": "PUT", "path": f"/api/v2/triggers/{t['id']}.json",
                                                      "body": {"trigger": {"conditions": {"all": [{"field": "status",
                                                                                                    "operator": "is",
                                                                                                    "value": "new"}]}}}}))
    assert any("existing actions" in n for n in out["notes"])
    assert zs.find("triggers", title="T")["actions"] == [{"field": "status", "value": "open"}]
