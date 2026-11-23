"""Admin tools, change journal, revert and snapshots."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from mcp.client import Client

from conftest import data, make_settings, text
from fakestate import StatefulZendesk
from zendesk_mcp.server import build_server


@pytest.fixture
def zs():
    s = StatefulZendesk()
    s.add("groups", {"name": "Tier 1"})
    s.add("groups", {"name": "Tier 2"})
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


def gid(zs, name):
    return zs.find("groups", name=name)["id"]


# ----------------------------------------------------------------------------- agents
async def test_provision_creates_and_updates(sconnect, zs):
    zs.add("users", {"name": "Existing", "email": "ex@acme.com", "role": "end-user"})
    agents = [{"email": "new@acme.com", "name": "New Person", "groups": ["Tier 1", "Tier 2"], "default_group": "Tier 2",
               "custom_role": "Light Agent"},
              {"email": "ex@acme.com", "role": "admin", "groups": ["Tier 1"]},
              {"email": "bad@acme.com", "name": "Bad", "groups": ["Nope"]}]
    async with sconnect() as cl:
        dry = data(await cl.call_tool("provision_agents", {"agents": agents, "dry_run": True}))
        assert dry["summary"] == {"create": 1, "update": 1, "failed": 1}
        assert len(zs.store["users"]) == 1  # nothing written
        out = data(await cl.call_tool("provision_agents", {"agents": agents, "send_verification_email": False}))
    assert out["summary"] == {"created": 1, "updated": 1, "failed": 1}
    assert "No group named 'Nope'" in out["agents"][2]["error"]
    new = zs.find("users", email="new@acme.com")
    assert new["role"] == "agent" and new["custom_role_id"] == zs.find("custom_roles", name="Light Agent")["id"]
    created_call = next(c for c in zs.calls if c.method == "POST" and c.url.path.endswith("/users"))
    assert json.loads(created_call.content)["user"]["skip_verify_email"] is True
    ms = {m["group_id"]: m for m in zs.store["group_memberships"].values() if m["user_id"] == new["id"]}
    assert set(ms) == {gid(zs, "Tier 1"), gid(zs, "Tier 2")} and ms[gid(zs, "Tier 2")]["default"]
    assert zs.find("users", email="ex@acme.com")["role"] == "admin"


async def test_offboard_dry_run_then_execute(sconnect, zs):
    agent = zs.add("users", {"name": "Leaver", "email": "leaver@acme.com", "role": "agent"})
    zs.add("group_memberships", {"user_id": agent["id"], "group_id": gid(zs, "Tier 1"), "default": True})
    zs.on("GET", "/api/v2/search/count", {"count": 3})
    zs.on("GET", "/api/v2/search/export", {"results": [{"id": 1}, {"id": 2}, {"id": 3}], "meta": {"has_more": False}})
    zs.on("PUT", "/api/v2/tickets/update_many", {"job_status": {"id": "job1"}})
    async with sconnect() as cl:
        dry = data(await cl.call_tool("offboard_agent", {"user": "leaver@acme.com", "reassign_to_group": "Tier 2"}))
        assert dry["dry_run"] and dry["unsolved_assigned_tickets"] == 3
        assert "Reassign 3 unsolved tickets to group Tier 2 (unassigned)" in dry["steps"]
        assert zs.find("users", email="leaver@acme.com")["role"] == "agent"
        done = data(await cl.call_tool("offboard_agent", {"user": "leaver@acme.com", "reassign_to_group": "Tier 2",
                                                          "dry_run": False}))
    assert done["reassignment_jobs"] == ["job1"]
    upd = next(c for c in zs.calls if c.url.path.endswith("/tickets/update_many"))
    assert upd.url.params["ids"] == "1,2,3"
    assert json.loads(upd.content) == {"ticket": {"assignee_id": None, "group_id": gid(zs, "Tier 2")}}
    assert zs.find("users", email="leaver@acme.com")["role"] == "end-user"
    assert not [m for m in zs.store["group_memberships"].values() if m["user_id"] == agent["id"]]


async def test_list_agents(sconnect, zs):
    a = zs.add("users", {"name": "Ann", "email": "ann@acme.com", "role": "agent", "last_login_at": (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    zs.add("users", {"name": "Old", "email": "old@acme.com", "role": "admin", "last_login_at": "2026-01-01T00:00:00Z"})
    zs.add("users", {"name": "Cust", "email": "c@x.com", "role": "end-user"})
    zs.add("group_memberships", {"user_id": a["id"], "group_id": gid(zs, "Tier 2"), "default": True})
    async with sconnect() as cl:
        out = data(await cl.call_tool("list_agents", {"inactive_days": 30}))
        only = data(await cl.call_tool("list_agents", {"group": "Tier 2"}))
    assert out["totals"]["agents"] == 2 and out["totals"]["by_role"] == {"agent": 1, "admin": 1}
    assert [r["email"] for r in out["inactive"]] == ["old@acme.com"]
    assert [r["name"] for r in only["agents"]] == ["Ann"] and only["agents"][0]["default_group"] == "Tier 2"


# ----------------------------------------------------------------------------- rules
async def test_find_references(sconnect, zs):
    t2 = gid(zs, "Tier 2")
    f = zs.add("ticket_fields", {"title": "Severity", "type": "tagger"})
    zs.add("triggers", {"title": "Route", "active": True,
                        "conditions": {"all": [{"field": f"custom_fields_{f['id']}", "operator": "is", "value": "sev_1"}]},
                        "actions": [{"field": "group_id", "value": str(t2)}, {"field": "set_tags", "value": "vip urgent"}]})
    zs.add("views", {"title": "T2 view", "conditions": {"all": [], "any": []}, "restriction": {"type": "Group", "ids": [t2]}})
    zs.add("macros", {"title": "Other", "actions": [{"field": "group_id", "value": "1"}]})
    zs.add("ticket_forms", {"name": "Bugs", "ticket_field_ids": [f["id"]]})
    zs.on("GET", f"/api/v2/groups/{t2}/memberships", {"group_memberships": [{"id": 1}], "meta": {"has_more": False}})
    async with sconnect() as cl:
        g = data(await cl.call_tool("find_references", {"ref_type": "group", "value": "Tier 2"}))
        fld = data(await cl.call_tool("find_references", {"ref_type": "ticket_field", "value": "Severity"}))
        tag = data(await cl.call_tool("find_references", {"ref_type": "tag", "value": "urgent"}))
    assert g["references"]["triggers"][0]["where"] == ["actions[0]"]
    assert g["references"]["views"][0]["where"] == ["restriction"] and "macros" not in g["references"]
    assert g["group_members"] == 1
    assert fld["references"]["triggers"][0]["where"] == ["conditions.all[0]"]
    assert fld["references"]["ticket_forms"][0]["name"] == "Bugs"
    assert tag["total_references"] == 1


async def test_reorder_subset_and_revert(sconnect, zs):
    for i, t in enumerate(["A", "B", "C", "D"]):
        zs.add("triggers", {"title": t, "position": i * 10})
    async with sconnect() as cl:
        out = data(await cl.call_tool("reorder", {"kind": "triggers", "order": ["D", "B"]}))
        pos = {t["title"]: t["position"] for t in zs.store["triggers"].values()}
        assert pos == {"A": 0, "D": 10, "C": 20, "B": 30}  # D and B swap the slots they held
        rv = data(await cl.call_tool("revert_change", {"change_id": out["change_ids"][0]}))
        assert rv["summary"] == {"reverted": 1}
    assert {t["title"]: t["position"] for t in zs.store["triggers"].values()} == {"A": 0, "B": 10, "C": 20, "D": 30}


async def test_rule_definitions_and_preview(sconnect, zs):
    zs.on("GET", "/api/v2/triggers/definitions", {"definitions": {
        "conditions_all": [{"subject": "status", "title": "Status", "type": "list",
                            "operators": [{"value": "is"}], "values": [{"value": "new", "title": "New"}]},
                           {"subject": "group_id", "title": "Group", "type": "list", "operators": [{"value": "is"}],
                            "values": [{"value": str(i), "title": f"G{i}"} for i in range(80)]}],
        "actions": [{"subject": "status", "title": "Status"}]}})
    zs.on("POST", "/api/v2/views/preview/count", {"view_count": {"value": 42, "fresh": True}})
    zs.on("POST", "/api/v2/views/preview", {"columns": [{"id": "subject"}], "rows": [{"ticket": {"id": 1}}]})
    async with sconnect(access_mode="read_only") as cl:
        d = data(await cl.call_tool("rule_definitions", {"kind": "automations", "search": "group"}))
        p = data(await cl.call_tool("preview_view", {"conditions": {"all": [{"field": "status", "operator": "is",
                                                                              "value": "open"}]}}))
    assert [x["subject"] for x in d["conditions_all"]] == ["group_id"]
    assert len(d["conditions_all"][0]["values"]) == 50 and d["conditions_all"][0]["values_truncated"] == 80
    assert "time-based" in d["notes"]
    assert p == {"count": 42, "count_is_fresh": True, "columns": ["subject"], "rows": [{"ticket": {"id": 1}}]}


# ----------------------------------------------------------------------------- settings, journal, revert
async def test_account_settings_update_and_revert(sconnect, zs):
    async with sconnect() as cl:
        r = await cl.call_tool("account_settings", {"action": "update", "settings": {"nope": {"x": 1}}})
        assert r.is_error and "Unknown settings sections" in text(r)
        out = data(await cl.call_tool("account_settings", {"action": "update",
                                                            "settings": {"tickets": {"comments_public_by_default": False}}}))
        assert out["previous"] == {"tickets": {"comments_public_by_default": True}}
        assert zs.settings["tickets"]["comments_public_by_default"] is False
        data(await cl.call_tool("revert_change", {"change_id": out["change_id"]}))
    assert zs.settings["tickets"]["comments_public_by_default"] is True
    assert zs.settings["tickets"]["list_newest_comments_first"] is True  # untouched


async def test_journal_redacts_and_revert_handles_conflicts(sconnect, zs, tmp_path):
    hook = zs.add("webhooks", {"name": "Slack", "endpoint": "https://x"})
    async with sconnect() as cl:
        created = data(await cl.call_tool("create_resource", {"resource": "groups", "data": {"name": "Tier 3"}}))
        data(await cl.call_tool("update_resource", {"resource": "webhooks", "id": hook["id"], "data": {
            "name": "Slack 2", "authentication": {"type": "basic_auth", "data": {"username": "u", "password": "p4ss"}}}}))
        upd = data(await cl.call_tool("update_resource", {"resource": "groups", "id": gid(zs, "Tier 1"),
                                                          "data": {"name": "Tier One"}}))
        deleted = data(await cl.call_tool("delete_resource", {"resource": "groups", "id": gid(zs, "Tier 2")}))
        log = (tmp_path / "zendesk_changes.jsonl").read_text()
        assert "p4ss" not in log and "[redacted]" in log
        changes = data(await cl.call_tool("list_changes", {}))["changes"]
        assert [c["method"] for c in changes] == ["DELETE", "PUT", "PATCH", "POST"]
        # someone edits the group again -> revert refuses without force
        g1 = zs.find("groups", name="Tier One")
        g1["name"], g1["updated_at"] = "Tier Uno", "2027-01-01T00:00:00Z"
        rv = data(await cl.call_tool("revert_change", {"change_id": upd["change_id"]}))
        assert rv["results"][0]["status"] == "conflict"
        rv = data(await cl.call_tool("revert_change", {"change_id": upd["change_id"], "force": True}))
        assert rv["summary"] == {"reverted": 1} and zs.find("groups", id=g1["id"])["name"] == "Tier 1"
        rv = data(await cl.call_tool("revert_change", {"change_id": deleted["change_id"]}))
        assert rv["results"][0]["action"] == "re-created deleted object" and zs.find("groups", name="Tier 2")
        rv = data(await cl.call_tool("revert_change", {"change_id": created["change_id"]}))
        assert zs.find("groups", name="Tier 3") is None
    # webhook revert restores the name but never writes the redacted secret back
    async with sconnect() as cl:
        wh_change = next(c for c in data(await cl.call_tool("list_changes", {"path_contains": "webhooks"}))["changes"]
                         if c["method"] == "PATCH")
        rv = data(await cl.call_tool("revert_change", {"change_id": wh_change["id"], "force": True}))
    assert rv["results"][0]["restored_keys"] == ["name"] and rv["results"][0]["skipped_keys"] == ["authentication"]


async def test_snapshot_and_diff(sconnect, zs):
    async with sconnect() as cl:
        snap = data(await cl.call_tool("config_snapshot", {"sections": ["groups", "account_settings"], "label": "pre"}))
        assert snap["counts"] == {"groups": 2, "account_settings": 2} and snap["name"].endswith("-pre.json")
        zs.find("groups", name="Tier 1")["name"] = "Tier One"
        zs.add("groups", {"name": "Tier 3"})
        zs.settings["tickets"]["comments_public_by_default"] = False
        d = data(await cl.call_tool("snapshot_diff", {"before": snap["name"]}))
    assert d["summary"]["groups"] == {"added": 1, "removed": 0, "changed": 1}
    assert d["sections"]["groups"]["changed"][0]["values"]["name"] == {"from": "Tier 1", "to": "Tier One"}
    assert d["sections"]["account_settings"]["changed"][0]["changed_keys"] == ["comments_public_by_default"]


async def test_toolsets(sconnect):
    async with sconnect(toolsets=("admin",)) as cl:
        names = {t.name for t in (await cl.list_tools()).tools}
    assert "blueprint_plan" in names and "revert_change" in names
    assert "support_report" not in names and "get_ticket" not in names
    async with sconnect(toolsets=("core", "analytics")) as cl:
        names = {t.name for t in (await cl.list_tools()).tools}
    assert "get_ticket" in names and "support_report" in names and "list_changes" in names
    assert "blueprint_apply" not in names
