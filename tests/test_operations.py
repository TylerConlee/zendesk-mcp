"""routing_report, macro_report, agent_profile, capacity_forecast. Sept 2026 = period; 'now' is 2026-10-14."""

from datetime import datetime, timezone

import pytest

from conftest import data

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    for mod in ("periods", "tools.analytics", "tools.reporting", "tools.customers"):
        monkeypatch.setattr(f"zendesk_mcp.{mod}.utcnow", lambda: NOW)


USERS = [{"id": 7, "name": "Jane Doe", "role": "end-user"}, {"id": 8, "name": "Alice Smith", "role": "agent"},
         {"id": 9, "name": "Bob Jones", "role": "agent"}]
GROUPS = [{"id": 3, "name": "Tier 1"}, {"id": 4, "name": "Tier 2"}, {"id": 5, "name": "Billing"}]


def T(tid, created, group, *, form=1, channel="email", csat=None, assignee=8, status="solved", priority="normal"):
    return {"id": tid, "subject": f"Ticket {tid}", "status": status, "created_at": created, "updated_at": created,
            "group_id": group, "ticket_form_id": form, "via": {"channel": channel}, "assignee_id": assignee,
            "requester_id": 7, "organization_id": 50, "priority": priority, "tags": [], "custom_fields": [],
            "satisfaction_rating": {"score": csat} if csat else None}


def M(tid, frt, replies=1, reopens=0, full=100):
    return {"ticket_id": tid, "replies": replies, "reopens": reopens, "assignee_stations": 1, "group_stations": 1,
            "reply_time_in_minutes": {"calendar": frt, "business": frt},
            "full_resolution_time_in_minutes": {"calendar": full, "business": full},
            "first_resolution_time_in_minutes": {"calendar": full, "business": full},
            "requester_wait_time_in_minutes": {"calendar": 50, "business": 50}}


TICKETS = [T(1, "2026-09-01T10:00:00Z", 3, csat="good"), T(2, "2026-09-02T10:00:00Z", 5, form=2, csat="bad"),
           T(3, "2026-09-03T10:00:00Z", 4, channel="web", assignee=9), T(4, "2026-09-04T10:00:00Z", 4, assignee=9)]
METRICS_ = {1: M(1, 10), 2: M(2, 30, replies=3, reopens=1), 3: M(3, 90, replies=4), 4: M(4, 20)}
TRIGGER = lambda title: {"channel": "rule", "source": {"rel": "trigger", "from": {"id": 1, "title": title}}}  # noqa


def A(at, author, events, via=None):
    return {"created_at": at, "author_id": author, "via": via or {"channel": "web"}, "events": events}


def chg(field, frm, to, via=None):
    e = {"type": "Change", "field_name": field, "value": to, "previous_value": frm}
    if via:
        e["via"] = via
    return e


AUDITS = {
    1: [A("2026-09-01T10:00:00Z", 7, [{"type": "Create", "field_name": "group_id", "value": "3"}]),
        A("2026-09-01T11:00:00Z", 8, [{"type": "AgentMacroReference", "macro_id": "70", "macro_title": "Password reset"},
                                      {"type": "Comment", "public": True, "author_id": 8}])],
    2: [A("2026-09-02T10:00:00Z", 7, [{"type": "Create", "field_name": "group_id", "value": "3"},
                                      chg("group_id", "3", "5", TRIGGER("Route billing"))]),
        A("2026-09-02T12:00:00Z", 8, [{"type": "AgentMacroReference", "macro_id": "71", "macro_title": "Refund"}])],
    3: [A("2026-09-03T10:00:00Z", 7, [{"type": "Create", "field_name": "group_id", "value": None},
                                      chg("group_id", None, "3", TRIGGER("Default routing"))]),
        A("2026-09-03T12:00:00Z", 8, [chg("group_id", "3", "4")]),
        A("2026-09-03T14:00:00Z", 9, [chg("group_id", "4", "3")]),
        A("2026-09-04T10:00:00Z", 8, [chg("group_id", "3", "4"),
                                      {"type": "AgentMacroReference", "macro_id": "70", "macro_title": "Password reset"}])],
    4: [A("2026-09-04T10:00:00Z", 7, [{"type": "Create", "field_name": "group_id", "value": "4"}])],
}


def install(fake):
    ALL = {t["id"]: t for t in TICKETS}
    fake.on("GET", "/api/v2/search/count", lambda r: {"count": len(TICKETS) if "2026-09-01" in r.url.params["query"]
                                                       else 0})
    fake.on("GET", "/api/v2/search/export", lambda r: {"results": TICKETS if "2026-09-01" in r.url.params["query"]
                                                       else [], "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/tickets/show_many", lambda r: {
        "tickets": [ALL[int(i)] for i in r.url.params["ids"].split(",")], "users": USERS, "groups": GROUPS,
        "metric_sets": [METRICS_[int(i)] for i in r.url.params["ids"].split(",")]})
    fake.on("GET", "/api/v2/groups", {"groups": GROUPS, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/ticket_forms", {"ticket_forms": [{"id": 1, "name": "Default"}, {"id": 2, "name": "Billing"}]})
    for tid, au in AUDITS.items():
        fake.on("GET", f"/api/v2/tickets/{tid}/audits", {"audits": au, "meta": {"has_more": False}})


async def test_routing_report(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("routing_report", {"period": "2026-09"}))
        named = data(await cl.call_tool("routing_report", {"period": "2026-09", "team_level": False}))
    s = out["summary"]
    assert (s["landed_in_final_group_first_pct"], s["transferred_pct"], s["avg_transfers_when_moved"]) == (50.0, 50.0, 2.0)
    assert (s["ping_pong_tickets"], s["created_without_group_pct"]) == (1, 25.0)
    assert s["time_to_final_group_median_min"] == 720  # ticket 2: 0 min, ticket 3: 1440 min
    assert {p["path"]: p["tickets"] for p in out["top_paths"]} == {"Tier 1 → Billing": 1,
                                                                 "Tier 1 → Tier 2 → Tier 1 → Tier 2": 1}
    assert out["by_first_group"][0] == {"first_group": "Tier 1", "tickets": 3, "solved_there_pct": 33.3,
                                        "passed_on_to": [{"group": "Billing", "tickets": 1},
                                                         {"group": "Tier 2", "tickets": 1}]}
    assert {m["moved_by"]: m["transfers"] for m in out["who_moves_tickets"]} == {"agent": 3,
                                                                                "trigger: Route billing": 1}
    assert {m["moved_by"] for m in named["who_moves_tickets"]} == {"agent: Alice Smith", "agent: Bob Jones",
                                                                   "trigger: Route billing"}
    c = out["cost_of_transfers"]
    assert (c["not_transferred"]["frt_median_min"], c["transferred"]["frt_median_min"]) == (15.0, 60.0)
    assert out["most_transferred"][0]["id"] == 3 and out["most_transferred"][0]["hours_to_final_group"] == 24.0
    assert {f["value"]: f["transferred_pct"] for f in out["by_form"]} == {"Default": 33.3, "Billing": 100.0}


# ----------------------------------------------------------------------------- macro_report

PW_ACTIONS = [{"field": "comment_value", "value": "Reset it here"}, {"field": "status", "value": "solved"}]
MACROS = [
    {"id": 70, "title": "Password reset", "active": True, "usage_30d": 12, "usage_7d": 4, "actions": PW_ACTIONS,
     "updated_at": "2026-08-01T00:00:00Z", "restriction": None},
    {"id": 71, "title": "Refund", "active": True, "usage_30d": 3, "usage_7d": 1, "updated_at": "2026-06-01T00:00:00Z",
     "actions": [{"field": "group_id", "value": "5"}], "restriction": None},
    {"id": 72, "title": "Password Reset (copy)", "active": True, "usage_30d": 0, "actions": PW_ACTIONS,
     "updated_at": "2026-08-02T00:00:00Z", "restriction": None},
    {"id": 73, "title": "Old greeting", "active": True, "usage_30d": 0, "updated_at": "2024-01-01T00:00:00Z",
     "actions": [{"field": "comment_value", "value": "Hello!"}], "restriction": {"type": "User", "id": 9}},
    {"id": 74, "title": "Retired", "active": False, "usage_30d": 0, "actions": [], "updated_at": "2023-01-01T00:00:00Z"},
]


async def test_macro_report(connect, fake):
    install(fake)
    fake.on("GET", "/api/v2/macros", {"macros": MACROS, "meta": {"has_more": False}})
    async with connect() as cl:
        out = data(await cl.call_tool("macro_report", {"period": "2026-09", "min_uses": 1, "team_level": False}))
    s = out["summary"]
    assert (s["active"], s["inactive"], s["personal"], s["unused_30d"], s["not_edited_in_365_days"]) == (4, 1, 1, 2, 1)
    assert s["tickets_with_a_macro_pct"] == 75.0  # 1, 2, 3 had a macro; 4 didn't
    assert [m["title"] for m in out["most_used_30d"]] == ["Password reset", "Refund"]
    by = {m["macro"]: m for m in out["outcomes"]["by_macro"]}
    assert by["Password reset"]["tickets"] == 2 and by["Password reset"]["replies_avg"] == 2.5
    assert by["Refund"]["flags"] == ["reopened more often", "more back-and-forth than usual"]
    assert out["outcomes"]["tickets_without_macros"]["tickets"] == 1
    dup_whys = sorted(d["why"] for d in out["likely_duplicates"])
    assert dup_whys == ["near-identical titles", "same actions"]
    assert all({m["id"] for m in d["macros"]} == {70, 72} for d in out["likely_duplicates"])
    assert out["stale"][0]["title"] == "Old greeting" and out["stale"][0]["personal"] is True
    assert out["by_agent"] == [{"agent": "Alice Smith", "macro_uses": 3}]
