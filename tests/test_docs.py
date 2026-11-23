"""document_config against a seeded account."""

import json

import httpx
import pytest
from mcp.client import Client

from conftest import data, make_settings
from fakestate import StatefulZendesk
from zendesk_mcp.server import build_server
from zendesk_mcp.tools.docs import describe_hours


@pytest.fixture
def zs():
    s = StatefulZendesk()
    t2 = s.add("groups", {"name": "Tier 2", "description": "Escalations"})
    s.add("groups", {"name": "Empty"})
    agent = s.add("users", {"name": "Ann", "email": "ann@acme.com", "role": "agent"})
    s.add("group_memberships", {"user_id": agent["id"], "group_id": t2["id"], "default": True})
    sev = s.add("ticket_fields", {"title": "Severity", "type": "tagger", "active": True, "removable": True,
                                  "custom_field_options": [{"name": "Sev 1", "value": "sev_1"}]})
    s.add("ticket_fields", {"title": "Orphan", "type": "text", "active": True, "removable": True})
    s.add("ticket_fields", {"title": "Subject", "type": "subject", "removable": False})
    s.add("ticket_forms", {"name": "Bugs", "active": True, "ticket_field_ids": [sev["id"]],
                           "agent_conditions": [{"parent_field_id": sev["id"], "value": "sev_1",
                                                 "child_fields": [{"id": sev["id"], "is_required": True}]}]})
    hook = s.add("webhooks", {"name": "Pager", "status": "inactive", "endpoint": "https://p", "http_method": "POST"})
    cat = s.add("trigger_categories", {"name": "Routing"})
    s.add("triggers", {"title": "Route Sev 1", "active": True, "category_id": cat["id"], "usage_30d": 12,
                       "conditions": {"all": [{"field": "update_type", "operator": "is", "value": "Create"},
                                              {"field": f"custom_fields_{sev['id']}", "operator": "is", "value": "sev_1"}],
                                      "any": []},
                       "actions": [{"field": "group_id", "value": str(t2["id"])},
                                   {"field": "notification_webhook", "value": [str(hook["id"]), '{"a": 1}']},
                                   {"field": "add_tags", "value": "vip escalated"}]})
    s.add("triggers", {"title": "Broken", "active": True, "usage_30d": 0,
                       "conditions": {"all": [{"field": "status", "operator": "changed"}]},
                       "actions": [{"field": "group_id", "value": "999"}]})
    s.add("automations", {"title": "Close stale", "active": True,
                          "conditions": {"all": [{"field": "SOLVED", "operator": "greater_than", "value": "96"}]},
                          "actions": [{"field": "status", "value": "closed"}]})
    s.add("views", {"title": "Tier 2 open", "active": True, "restriction": {"type": "Group", "ids": [t2["id"]]},
                    "conditions": {"all": [{"field": "group_id", "operator": "is", "value": str(t2["id"])}]},
                    "execution": {"columns": [{"id": "subject", "title": "Subject"}], "group": {"title": "Priority"}}})
    s.add("macros", {"title": "Escalate", "active": True, "usage_30d": 0,
                     "actions": [{"field": "comment_value", "value": "We've escalated this."}]})
    s.add("business_hours/schedules", {"name": "US", "time_zone": "Pacific Time (US & Canada)",
                                       "intervals": [{"start_time": d * 1440 + 540, "end_time": d * 1440 + 1020}
                                                     for d in range(1, 6)]})
    s.add("slas/policies", {"title": "Urgent", "filter": {"all": [{"field": "priority", "operator": "is",
                                                                     "value": "urgent"}], "any": []},
                            "policy_metrics": [{"priority": "urgent", "metric": "first_reply_time", "target": 60,
                                                "business_hours": False}]})
    s.on("GET", "/api/v2/triggers/definitions", {"definitions": {
        "conditions_all": [{"subject": "update_type", "title": "Ticket", "operators": [{"value": "is", "title": "Is"}],
                            "values": [{"value": "Create", "title": "Created"}]},
                           {"subject": "priority", "title": "Priority", "values": [{"value": "urgent", "title": "Urgent"}]}],
        "actions": [{"subject": "status", "title": "Status", "values": [{"value": "closed", "title": "Closed"}]}]}})
    return s


@pytest.fixture
def sconnect(zs, tmp_path):
    def _c(**kw):
        kw.setdefault("file_root", str(tmp_path))
        mcp, _ = build_server(make_settings(**kw), transport=httpx.MockTransport(zs.handle))
        return Client(mcp)
    return _c


def test_describe_hours():
    iv = [{"start_time": d * 1440 + 540, "end_time": d * 1440 + 1020} for d in range(1, 6)]
    assert describe_hours(iv) == "Mon–Fri 09:00–17:00"
    assert describe_hours(iv + [{"start_time": 6 * 1440 + 600, "end_time": 6 * 1440 + 720}]) == \
        "Mon–Fri 09:00–17:00; Sat 10:00–12:00"


async def test_document_config(sconnect, tmp_path):
    async with sconnect() as cl:
        out = data(await cl.call_tool("document_config", {}))
    md_path = next(f for f in out["files"] if f.endswith(".md"))
    md = open(md_path).read()
    doc = json.loads(open(next(f for f in out["files"] if f.endswith(".json"))).read())
    # plain-language rules with names resolved
    assert "  - Ticket is Created" in md
    assert "  - Severity is “Sev 1”" in md
    assert "- Group → “Tier 2”" in md
    assert "- Notify webhook “Pager” with payload: `{\"a\": 1}`" in md
    assert "- Add tags: `vip`, `escalated`" in md
    assert "  - Hours since solved is greater than 96" in md
    assert "- Status → Closed" in md
    assert "#### Category: Routing" in md and "fired 12× in 30 days" in md
    assert "Visible to: Groups: Tier 2" in md and "grouped by Priority" in md
    assert "Mon–Fri 09:00–17:00 (Pacific Time (US & Canada))" in md
    assert "| urgent | first reply time | 1 h | calendar |" in md
    assert "For agents: when Severity is “Sev 1”, show Severity (required)" in md
    assert "Ann" not in md  # agent names off by default
    # cross references
    assert doc["cross_references"]["groups"]["Tier 2"] == ["trigger “Route Sev 1”", "view “Tier 2 open”"]
    assert "form “Bugs”" in doc["cross_references"]["ticket_fields"]["Severity"]
    # findings
    msgs = " | ".join(f["message"] for f in out["findings"])
    assert "group 999, which doesn't exist" in msgs
    assert "calls inactive webhook “Pager”" in msgs
    assert "Group “Empty” has no members and no rule uses it." in msgs
    assert "Field “Orphan” is active but on no form" in msgs
    assert "1 active trigger unused in the last 30 days: ['Broken']" in msgs and "['Escalate']" in msgs
    assert out["findings"][0]["severity"] == "high"


async def test_document_config_inline_sections(sconnect):
    async with sconnect() as cl:
        out = data(await cl.call_tool("document_config", {"sections": ["triggers"], "format": "markdown",
                                                          "inline": True, "include_agents": True}))
    assert "## Triggers" in out["markdown"] and "## Macros" not in out["markdown"]
