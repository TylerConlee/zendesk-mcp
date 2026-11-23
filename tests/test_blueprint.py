"""Blueprint plan/apply/revert against a stateful fake Zendesk."""

import json

import httpx
import pytest
from mcp.client import Client

from conftest import data, make_settings, text
from fakestate import StatefulZendesk
from zendesk_mcp.server import build_server


@pytest.fixture
def zs():
    s = StatefulZendesk()
    s.add("groups", {"name": "Support"})
    s.add("ticket_fields", {"title": "Subject", "type": "subject", "removable": False})
    s.add("ticket_fields", {"title": "Product Area", "type": "tagger", "removable": True,
                            "custom_field_options": [{"id": 1, "name": "Billing", "value": "pa_billing"},
                                                     {"id": 2, "name": "Login", "value": "pa_login"}]})
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


BLUEPRINT = {
    "groups": [{"name": "Tier 2", "description": "Escalations"}, {"name": "Support"}],
    "custom_statuses": [{"agent_label": "Waiting on Eng", "end_user_label": "In progress", "status_category": "hold"}],
    "ticket_fields": [{"title": "Product Area", "type": "tagger",
                       "custom_field_options": [{"name": "Billing", "value": "pa_billing"},
                                                {"name": "API", "value": "pa_api"}]},
                      {"title": "Severity", "type": "tagger",
                       "custom_field_options": [{"name": "Sev 1", "value": "sev_1"}, {"name": "Sev 2", "value": "sev_2"}]}],
    "schedules": [{"name": "US Hours", "time_zone": "Pacific Time (US & Canada)",
                   "intervals": [{"start_time": 1980, "end_time": 2460}],
                   "holidays": [{"name": "Thanksgiving", "start_date": "2026-11-26", "end_date": "2026-11-26"}]}],
    "trigger_categories": [{"name": "Routing"}],
    "skills": [{"name": "Language", "values": ["English", "Spanish"]}],
    "ticket_forms": [{"name": "Bug report", "ticket_field_ids": ["@field:Subject", "@field:Severity"]}],
    "agents": [{"email": "sam@acme.com", "name": "Sam", "groups": ["Tier 2", "Support"], "default_group": "Tier 2",
                "custom_role": "Light Agent", "skills": ["Language/Spanish"]}],
    "triggers": [{"title": "Route Sev 1 to Tier 2", "category": "Routing",
                  "conditions": {"all": [{"field": "@custom_field:Severity", "operator": "is",
                                          "value": "@option:Severity/Sev 1"}]},
                  "actions": [{"field": "group_id", "value": "@group:Tier 2"},
                              {"field": "custom_status_id", "value": "@status:Waiting on Eng"}]}],
    "views": [{"title": "Tier 2 queue", "conditions": {"all": [{"field": "group_id", "operator": "is",
                                                                 "value": "@group:Tier 2"}]},
               "restriction": {"type": "Group", "ids": ["@group:Tier 2"]}}],
    "macros": [{"title": "Escalate", "actions": [{"field": "group_id", "value": "@group:Tier 2"}]}],
    "settings": {"tickets": {"comments_public_by_default": False}},
}


async def plan(cl, bp, **kw):
    return data(await cl.call_tool("blueprint_plan", {"blueprint": bp, **kw}))


async def test_plan_apply_is_idempotent_and_revertible(sconnect, zs):
    async with sconnect() as cl:
        p = await plan(cl, BLUEPRINT)
        assert p["status"] == "ready", p["errors"]
        s = p["summary"]
        assert s["groups"] == {"create": 1, "unchanged": 1}
        assert s["ticket_fields"] == {"update": 1, "create": 1}
        assert s["agents"] == {"create": 1} and s["triggers"] == {"create": 1} and s["settings"] == {"settings": 1}
        pa = next(x for x in p["steps"] if x["name"] == "Product Area")
        assert any("added: ['API']" in n for n in pa["notes"])
        trig = next(x for x in p["steps"] if x["section"] == "triggers")
        assert trig["changes"]["actions"][0]["value"] == "<new group: Tier 2>"

        r = data(await cl.call_tool("blueprint_apply", {"plan_id": p["plan_id"]}))
        assert r["status"] == "applied", r
        # --- objects exist with real ids wired together
        t2 = zs.find("groups", name="Tier 2")
        sev = zs.find("ticket_fields", title="Severity")
        status = zs.find("custom_statuses", agent_label="Waiting on Eng")
        cat = zs.find("trigger_categories", name="Routing")
        trig = zs.find("triggers", title="Route Sev 1 to Tier 2")
        assert trig["conditions"]["all"][0] == {"field": f"custom_fields_{sev['id']}", "operator": "is", "value": "sev_1"}
        assert trig["actions"] == [{"field": "group_id", "value": str(t2["id"])},
                                   {"field": "custom_status_id", "value": str(status["id"])}]
        assert trig["category_id"] == cat["id"]
        view = zs.find("views", title="Tier 2 queue")
        assert view["conditions"]["all"][0]["value"] == str(t2["id"]) and view["restriction"]["ids"] == [t2["id"]]
        pa = zs.find("ticket_fields", title="Product Area")
        assert [o["value"] for o in pa["custom_field_options"]] == ["pa_billing", "pa_api", "pa_login"]  # kept Login
        sch = zs.find("business_hours/schedules", name="US Hours")
        assert sch["intervals"] == [{"start_time": 1980, "end_time": 2460}]
        assert [h["name"] for h in zs.store["holidays"].values()] == ["Thanksgiving"]
        form = zs.find("ticket_forms", name="Bug report")
        assert form["ticket_field_ids"][1] == sev["id"]
        sam = zs.find("users", email="sam@acme.com")
        assert sam["role"] == "agent" and sam["custom_role_id"] == zs.find("custom_roles", name="Light Agent")["id"]
        ms = [g for g in zs.store["group_memberships"].values() if g["user_id"] == sam["id"]]
        assert {g["group_id"] for g in ms} == {t2["id"], zs.find("groups", name="Support")["id"]}
        assert next(g for g in ms if g["default"])["group_id"] == t2["id"]
        spanish = next(v for v in zs.store["attribute_values"].values() if v["name"] == "Spanish")
        assert zs.instance_values[str(sam["id"])] == [spanish["id"]]
        assert zs.settings["tickets"]["comments_public_by_default"] is False

        # --- re-planning the same blueprint finds nothing to do
        again = await plan(cl, BLUEPRINT)
        assert again["status"] == "no_changes", again["steps"]

        # --- the whole rollout can be undone as one batch
        changes = data(await cl.call_tool("list_changes", {"batch": p["plan_id"], "limit": 500}))["changes"]
        bad = [(c["method"], c["path"], c.get("why_not")) for c in changes if c["method"] in ("POST", "DELETE") and not c["revertible"]]
        assert changes and not bad, bad
        rv = data(await cl.call_tool("revert_change", {"batch": p["plan_id"]}))
        assert rv["summary"].get("failed") is None, rv
    assert zs.find("groups", name="Tier 2") is None and zs.find("triggers", title="Route Sev 1 to Tier 2") is None
    assert zs.find("users", email="sam@acme.com") is None
    assert [o["value"] for o in zs.find("ticket_fields", title="Product Area")["custom_field_options"]] == \
        ["pa_billing", "pa_login"]
    assert zs.settings["tickets"]["comments_public_by_default"] is True


async def test_update_trigger_sends_conditions_and_actions(sconnect, zs):
    g = zs.add("groups", {"name": "Tier 2"})
    zs.add("triggers", {"title": "Notify", "conditions": {"all": [{"field": "status", "operator": "is", "value": "new"}],
                                                           "any": []},
                        "actions": [{"field": "group_id", "value": str(g["id"])}]})
    bp = {"triggers": [{"title": "Notify", "actions": [{"field": "group_id", "value": "@group:Support"}]}]}
    async with sconnect() as cl:
        p = await plan(cl, bp)
        step = p["steps"][0]
        assert step["action"] == "update" and list(step["changes"]) == ["actions"]
        data(await cl.call_tool("blueprint_apply", {"plan_id": p["plan_id"]}))
    put = [c for c in zs.calls if c.method == "PUT" and "/triggers/" in c.url.path][-1]
    body = json.loads(put.content)["trigger"]
    assert set(body) == {"actions", "conditions"}  # both arrays always sent together
    assert body["conditions"]["all"][0]["value"] == "new"


async def test_drift_is_detected(sconnect, zs):
    bp = {"groups": [{"name": "Support", "description": "Front line"}]}
    async with sconnect() as cl:
        p = await plan(cl, bp)
        zs.find("groups", name="Support")["description"] = "changed by someone else"
        zs.find("groups", name="Support")["name"] = "Support"
        zs.add("groups", {"name": "Unrelated"})  # doesn't affect this plan
        same = data(await cl.call_tool("blueprint_apply", {"plan_id": p["plan_id"]}))
        assert same["status"] == "drift" and same["new_plan_id"]
        r = data(await cl.call_tool("blueprint_apply", {"plan_id": same["new_plan_id"]}))
        assert r["status"] == "applied"
    assert zs.find("groups", name="Support")["description"] == "Front line"


async def test_plan_errors_block(sconnect, zs):
    zs.add("groups", {"name": "Dup"})
    zs.add("groups", {"name": "Dup"})
    bp = {
        "triggers": [{"title": "Uses missing", "conditions": {"all": []},
                      "actions": [{"field": "group_id", "value": "@group:Nope"}]},
                     {"title": "BAD trigger", "conditions": {"all": [{"field": "status", "operator": "is", "value": "new"}]},
                      "actions": [{"field": "status", "value": "open"}]}],
        "automations": [{"title": "No timer", "conditions": {"all": [{"field": "status", "operator": "is", "value": "open"}]},
                         "actions": [{"field": "status", "value": "pending"}]}],
        "groups": [{"name": "Dup"}],
        "macros": [{"title": "Missing actions"}],
        "widgets": [],
    }
    async with sconnect() as cl:
        p = await plan(cl, bp)
    errs = " | ".join(p["errors"])
    assert p["status"] == "blocked" and "plan_id" not in p
    assert "@group:Nope" in errs and "time-based condition" in errs and "objects named 'Dup'" in errs
    assert "missing required keys" in errs and "Unknown blueprint keys ['widgets']" in errs
    # the invalid trigger is only validated once nothing else is wrong
    async with sconnect() as cl:
        p2 = await plan(cl, {"triggers": [bp["triggers"][1]]})
    assert "Zendesk says invalid" in " ".join(p2["errors"])


async def test_delete_warns_about_references_and_orders(sconnect, zs):
    old = zs.add("groups", {"name": "Old Team"})
    zs.add("macros", {"title": "Send to old", "actions": [{"field": "group_id", "value": str(old["id"])}]})
    zs.add("triggers", {"title": "A", "conditions": {"all": []}, "actions": [], "position": 0})
    zs.add("triggers", {"title": "B", "conditions": {"all": []}, "actions": [], "position": 1})
    async with sconnect() as cl:
        p = await plan(cl, {"delete": {"groups": ["Old Team", "Never existed"]}, "order": {"triggers": ["B", "A"]}})
        dele = next(s for s in p["steps"] if s["action"] == "delete")
        assert "Send to old" in dele["warnings"][0]
        assert any("Never existed" in w for w in p["warnings"])
        p2 = await plan(cl, {"delete": {"groups": ["Old Team"], "macros": ["Send to old"]}})
        assert [s["section"] for s in p2["steps"]] == ["macros", "groups"]  # rules before the config they use
        assert "warnings" not in p2["steps"][1]
        r = data(await cl.call_tool("blueprint_apply", {"plan_id": p["plan_id"]}))
        assert r["status"] == "applied"
    assert zs.find("groups", name="Old Team") is None
    assert zs.find("triggers", title="B")["position"] == 0 and zs.find("triggers", title="A")["position"] == 1


async def test_prune_options_warns(sconnect, zs):
    async with sconnect() as cl:
        p = await plan(cl, {"ticket_fields": [{"title": "Product Area", "prune_options": True,
                                               "custom_field_options": [{"name": "Billing", "value": "pa_billing"}]}]})
    step = p["steps"][0]
    assert "removes those values" in step["warnings"][0]
    assert step["changes"]["custom_field_options"] == {"from": ["Billing", "Login"], "to": ["Billing"]}


async def test_export_round_trips(sconnect, zs, tmp_path):
    async with sconnect() as cl:
        data(await cl.call_tool("blueprint_apply", {"plan_id": (await plan(cl, BLUEPRINT))["plan_id"]}))
        exp = data(await cl.call_tool("blueprint_export", {}))
        assert exp["file"].endswith(".yaml") and exp["counts"]["triggers"] == 1
        import yaml
        doc = yaml.safe_load((tmp_path / exp["blueprint_file"]).read_text())
        trig = doc["triggers"][0]
        assert trig["category"] == "Routing"
        assert trig["conditions"]["all"][0]["field"] == "@custom_field:Severity"
        assert trig["actions"][0]["value"] == "@group:Tier 2"
        assert doc["views"][0]["restriction"]["ids"] == ["@group:Tier 2"]
        assert doc["ticket_forms"][0]["ticket_field_ids"] == ["@field:Subject", "@field:Severity"]
        assert {"email": "sam@acme.com", "name": "Sam", "role": "agent", "custom_role": "Light Agent",
                "groups": ["Tier 2", "Support"], "default_group": "Tier 2"}.items() <= doc["agents"][0].items()
        # Planning the export against the same account changes nothing.
        p = data(await cl.call_tool("blueprint_plan", {"blueprint_file": exp["blueprint_file"]}))
    assert p["status"] == "no_changes", p["steps"][:5]


async def test_read_only_mode_can_plan_but_not_apply(sconnect, zs):
    async with sconnect(access_mode="read_only") as cl:
        names = {t.name for t in (await cl.list_tools()).tools}
        assert "blueprint_plan" in names and "blueprint_apply" not in names and "provision_agents" not in names
        p = await plan(cl, {"triggers": [{"title": "T", "conditions": {"all": [{"field": "status", "operator": "is",
                                                                                "value": "new"}]},
                                          "actions": [{"field": "status", "value": "open"}]}]})
        assert p["status"] == "ready"
    assert not any(c.method != "GET" and not c.url.path.endswith(("/validate",)) for c in zs.calls)
