"""Analytics tests. Expected numbers are computed by hand in the comments."""

from datetime import datetime, timezone

import pytest

from conftest import data
from zendesk_mcp.metrics import BusinessHours, analyze_conversation, summarize

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ROLES = {7: "end-user", 8: "agent", 9: "admin"}
USERS = [{"id": 7, "name": "Cust", "role": "end-user"}, {"id": 8, "name": "Alice", "role": "agent"},
         {"id": 9, "name": "Bob", "role": "admin"}]


def c(id, author, at, public=True, channel="web", rel=None):
    return {"id": id, "author_id": author, "public": public, "created_at": at,
            "via": {"channel": channel, "source": {"rel": rel}}}


# Ticket 42 (all 2026-10-14, UTC):
#  10:00 customer, 10:05 customer (still waiting since 10:00), 10:10 internal note, 10:11 trigger email,
#  10:30 Alice public  -> agent response 30 min
#  10:35 Alice public  -> (no new wait; last agent msg = 10:35)
#  12:35 customer      -> customer response 120 min (from 10:35)
#  13:35 Bob           -> agent response 60 min
#  14:00 customer      -> customer response 25 min; ticket open, now 15:00 -> awaiting agent 60 min
CONV_42 = [
    c(1, 7, "2026-10-14T10:00:00Z"), c(2, 7, "2026-10-14T10:05:00Z"),
    c(3, 8, "2026-10-14T10:10:00Z", public=False), c(4, 8, "2026-10-14T10:11:00Z", channel="rule", rel="trigger"),
    c(5, 8, "2026-10-14T10:30:00Z"), c(6, 8, "2026-10-14T10:35:00Z"), c(7, 7, "2026-10-14T12:35:00Z"),
    c(8, 9, "2026-10-14T13:35:00Z"), c(9, 7, "2026-10-14T14:00:00Z"),
]


def mon_fri_9_17_utc(holidays=None):
    iv = [{"start_time": d * 1440 + 540, "end_time": d * 1440 + 1020} for d in range(1, 6)]
    return BusinessHours.from_zendesk({"name": "Office", "time_zone": "UTC", "intervals": iv}, holidays)


# ----------------------------------------------------------------------------- pure engine
def test_conversation_gaps():
    conv = analyze_conversation(CONV_42, ROLES, status="open", now=NOW)
    assert [r["minutes"] for r in conv.agent_responses] == [30.0, 60.0]
    assert [r["agent_id"] for r in conv.agent_responses] == [8, 9]
    assert [r["minutes"] for r in conv.customer_responses] == [120.0, 25.0]
    assert conv.first_response["minutes"] == 30.0
    assert conv.awaiting_agent["minutes"] == 60.0 and conv.awaiting_customer is None
    assert conv.counted == {"customer_messages": 4, "agent_messages": 3, "ignored": 2}


def test_internal_notes_and_automation_options():
    conv = analyze_conversation(CONV_42, ROLES, status="open", include_internal=True, now=NOW)
    assert [r["minutes"] for r in conv.agent_responses] == [10.0, 60.0]  # note at 10:10 answers 10:00
    conv = analyze_conversation(CONV_42, ROLES, status="solved", include_automated=True, now=NOW)
    assert [r["minutes"] for r in conv.agent_responses] == [11.0, 60.0]  # trigger at 10:11
    assert conv.awaiting_agent is None  # solved tickets aren't "waiting"


def test_agent_started_ticket_measures_customer_first():
    conv = analyze_conversation([c(1, 8, "2026-10-14T09:00:00Z"), c(2, 7, "2026-10-14T09:45:00Z")],
                                ROLES, status="pending", now=NOW)
    assert conv.agent_responses == [] and conv.customer_responses[0]["minutes"] == 45.0


def test_business_hours_weekend_holiday_and_dst():
    bh = mon_fri_9_17_utc()
    # Fri 2026-10-09 16:30 -> Mon 10-12 09:15: calendar 64h45m = 3885 min; business 30 + 15 = 45
    conv = analyze_conversation([c(1, 7, "2026-10-09T16:30:00Z"), c(2, 8, "2026-10-12T09:15:00Z")],
                                ROLES, hours=bh, now=NOW)
    r = conv.agent_responses[0]
    assert r["minutes"] == 3885.0 and r["business_minutes"] == 45.0
    # Mon 10-12 as a holiday: Fri 16:30 -> Tue 10:00 = 30 (Fri) + 0 (Mon) + 60 (Tue)
    bh_h = mon_fri_9_17_utc([{"start_date": "2026-10-12", "end_date": "2026-10-12"}])
    assert bh_h.minutes_between(datetime(2026, 10, 9, 16, 30, tzinfo=timezone.utc),
                                datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)) == 90.0  # 30 Fri + 60 Tue
    # Rails zone name + DST: Pacific schedule, Fri 10-30 16:00 PDT -> Mon 11-02 10:00 PST = 60 + 60
    iv = [{"start_time": d * 1440 + 540, "end_time": d * 1440 + 1020} for d in range(1, 6)]
    pac = BusinessHours.from_zendesk({"time_zone": "Pacific Time (US & Canada)", "intervals": iv})
    assert pac.minutes_between(datetime.fromisoformat("2026-10-30T16:00:00-07:00"),
                               datetime.fromisoformat("2026-11-02T10:00:00-08:00")) == 120.0


def test_summarize_percentiles():
    assert summarize([10, 20, 30, 40, 100]) == {"n": 5, "mean": 40.0, "median": 30.0, "p90": 76.0,
                                                 "min": 10.0, "max": 100.0}
    assert summarize([None, None]) is None


# ----------------------------------------------------------------------------- tools
def _search_routes(fake, tickets):
    fake.on("GET", "/api/v2/search/count", {"count": len(tickets)})
    fake.on("GET", "/api/v2/search/export", {"results": tickets, "meta": {"has_more": False}, "links": {}})


def _metric(tid, frt, full, replies, reopens=0):
    return {"ticket_id": tid, "replies": replies, "reopens": reopens, "assignee_stations": 1, "group_stations": 1,
            "reply_time_in_minutes": {"calendar": frt[0], "business": frt[1]},
            "full_resolution_time_in_minutes": {"calendar": full[0], "business": full[1]},
            "first_resolution_time_in_minutes": {"calendar": full[0], "business": full[1]},
            "requester_wait_time_in_minutes": {"calendar": 50, "business": 40},
            "agent_wait_time_in_minutes": {"calendar": 5, "business": 5},
            "on_hold_time_in_minutes": {"calendar": 0, "business": 0}}


def _t(tid, status, assignee, score, channel="email"):
    return {"id": tid, "status": status, "assignee_id": assignee, "group_id": 3, "priority": "normal",
            "via": {"channel": channel}, "tags": [], "satisfaction_rating": {"score": score},
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-10T00:00:00Z", "subject": f"T{tid}"}


async def test_support_kpis(connect, fake):
    tickets = [_t(1, "solved", 8, "good"), _t(2, "solved", 9, "bad", "web"), _t(3, "open", 8, "offered"),
               _t(4, "closed", 8, "good")]
    _search_routes(fake, tickets)
    fake.on("GET", "/api/v2/tickets/show_many", {
        "tickets": tickets, "users": USERS, "groups": [{"id": 3, "name": "Tier 2"}],
        "metric_sets": [_metric(1, (30, 20), (600, 300), 1), _metric(2, (90, 60), (1200, 500), 3, reopens=1),
                        _metric(3, (None, None), (None, None), 0)],
    })
    # ticket 4's metrics aren't sideloaded -> fallback call
    fake.on("GET", "/api/v2/tickets/4/metrics", {"ticket_metric": _metric(4, (10, 10), (100, 100), 2)})
    async with connect() as cl:
        out = data(await cl.call_tool("support_kpis", {"start": "2026-10-01", "end": "2026-10-15",
                                                        "group_by": "assignee", "filter": "group:\"Tier 2\""}))
    q = [r for r in fake.calls if r.url.path == "/api/v2/search/export"][0].url.params["query"]
    assert q == 'created>=2026-10-01 created<2026-10-15 group:"Tier 2"'
    frt = out["first_reply_time_min"]
    # FRT calendar over [30, 90, 10] (ticket 3 has no reply): median 30, mean 43.3
    assert frt["calendar"]["n"] == 3 and frt["calendar"]["median"] == 30.0 and frt["calendar"]["mean"] == 43.3
    assert frt["business"]["median"] == 20.0  # [20, 60, 10]
    assert out["full_resolution_time_min"]["calendar"]["median"] == 600.0  # solved only: [600, 1200, 100]
    eff = out["efficiency"]
    assert eff["one_touch_resolution_pct"] == 33.3  # solved replies [1, 3, 2]
    assert eff["reopened_pct"] == 33.3 and eff["avg_replies_per_solved_ticket"] == 2.0 and eff["no_reply_yet"] == 1
    assert out["csat"] == {"good": 2, "bad": 1, "offered_no_response": 1, "score_pct": 66.7, "response_rate_pct": 75.0}
    assert out["volume"]["by_status"] == {"solved": 2, "open": 1, "closed": 1}
    alice = next(r for r in out["by_assignee"] if r["assignee"] == "Alice")
    assert alice["tickets"] == 3 and alice["solved"] == 2 and alice["frt_median_min"] == 20.0  # [30, 10]
    assert alice["csat_pct"] == 100.0


async def test_reply_time_report(connect, fake, monkeypatch):
    monkeypatch.setattr("zendesk_mcp.tools.analytics.utcnow", lambda: NOW)
    t42 = {**_t(42, "open", 8, None), "subject": "Printer"}
    t43 = {**_t(43, "solved", 8, None), "group_id": None}
    fake.on("GET", "/api/v2/tickets/show_many", {"tickets": [t42, t43], "users": USERS,
                                                 "groups": [{"id": 3, "name": "Tier 2"}], "metric_sets": []})
    fake.on("GET", "/api/v2/tickets/42/metrics", {"ticket_metric": {}})
    fake.on("GET", "/api/v2/tickets/43/metrics", {"ticket_metric": {}})
    fake.on("GET", "/api/v2/tickets/42/comments", {"comments": CONV_42, "users": USERS, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/tickets/43/comments", {
        "comments": [c(1, 7, "2026-10-14T09:00:00Z"), c(2, 8, "2026-10-14T09:20:00Z")],
        "users": USERS, "meta": {"has_more": False}})
    async with connect() as cl:
        out = data(await cl.call_tool("reply_time_report", {"ticket_ids": [42, 43]}))
    # agent responses: t42 Alice 30, Bob 60; t43 Alice 20
    assert out["agent_response_all_replies"]["calendar_min"]["n"] == 3
    assert out["agent_response_all_replies"]["calendar_min"]["median"] == 30.0
    assert out["first_response"]["calendar_min"]["median"] == 25.0  # [30, 20]
    assert out["customer_response"]["calendar_min"]["median"] == 72.5  # [120, 25]
    alice = next(r for r in out["by_agent"] if r["agent"] == "Alice")
    assert alice["responses"] == 2 and alice["median_min"] == 25.0
    assert {r["group"] for r in out["by_group"]} == {"Tier 2", "(no group)"}
    assert out["slowest_agent_responses"][0] == {"ticket_id": 42, "minutes": 60.0, "agent": "Bob",
                                                 "customer_at": "2026-10-14T12:35:00+00:00"}
    assert out["awaiting_agent_now"]["count"] == 1 and out["awaiting_agent_now"]["longest"][0]["minutes"] == 60.0


async def test_reply_time_report_window_filters_replies(connect, fake, monkeypatch):
    monkeypatch.setattr("zendesk_mcp.tools.analytics.utcnow", lambda: NOW)
    t = _t(50, "solved", 8, None)
    _search_routes(fake, [t])
    fake.on("GET", "/api/v2/tickets/show_many", {"tickets": [t], "users": USERS, "metric_sets": [{"ticket_id": 50}]})
    fake.on("GET", "/api/v2/tickets/50/comments", {"comments": [
        c(1, 7, "2026-10-01T09:00:00Z"), c(2, 8, "2026-10-01T10:00:00Z"),   # before window: excluded
        c(3, 7, "2026-10-10T09:00:00Z"), c(4, 8, "2026-10-10T09:15:00Z"),   # in window: 15 min
    ], "users": USERS, "meta": {"has_more": False}})
    async with connect() as cl:
        out = data(await cl.call_tool("reply_time_report", {"start": "2026-10-05", "end": "2026-10-15"}))
    assert out["agent_response_all_replies"]["calendar_min"]["n"] == 1
    assert out["agent_response_all_replies"]["calendar_min"]["median"] == 15.0
    assert out["first_response"]["calendar_min"] == {"n": 0}  # first response was before the window


async def test_ticket_reply_times_business_hours(connect, fake, monkeypatch):
    monkeypatch.setattr("zendesk_mcp.tools.analytics.utcnow", lambda: NOW)
    iv = [{"start_time": d * 1440 + 540, "end_time": d * 1440 + 1020} for d in range(1, 6)]
    fake.on("GET", "/api/v2/business_hours/schedules",
            {"schedules": [{"id": 1, "name": "Office", "time_zone": "London", "intervals": iv}]})
    fake.on("GET", "/api/v2/business_hours/schedules/1/holidays", {"holidays": []})
    fake.on("GET", "/api/v2/tickets/60", {"ticket": {"id": 60, "status": "solved", "subject": "Weekend"}})
    fake.on("GET", "/api/v2/tickets/60/metrics", {"ticket_metric": {"replies": 1}})
    # London is UTC+1 (BST) in October: Fri 16:30 BST = 15:30Z; Mon 09:15 BST = 08:15Z -> business 30 + 15 = 45
    fake.on("GET", "/api/v2/tickets/60/comments", {"comments": [
        c(1, 7, "2026-10-09T15:30:00Z"), c(2, 8, "2026-10-12T08:15:00Z")], "users": USERS,
        "meta": {"has_more": False}})
    async with connect() as cl:
        out = data(await cl.call_tool("ticket_reply_times", {"ticket_id": 60, "business_hours": True}))
    r = out["agent_responses"][0]
    assert r["business_minutes"] == 45.0 and r["minutes"] == 3885.0 and r["agent_name"] == "Alice"
    assert out["business_hours_schedule"] == "Office" and out["zendesk_metrics"] == {"replies": 1}


async def test_sla_report(connect, fake, monkeypatch):
    monkeypatch.setattr("zendesk_mcp.tools.analytics.utcnow", lambda: NOW)
    gold = {"target": 60, "business_hours": True, "policy": {"id": 1, "title": "Gold"}}

    def ev(tid, metric, typ, at, **kw):
        return {"ticket_id": tid, "metric": metric, "instance_id": 1, "type": typ, "time": at, **kw}

    page1 = [
        ev(1, "reply_time", "apply_sla", "2026-09-28T00:00:00Z", sla=gold),   # applied before window (lookback)
        ev(2, "reply_time", "apply_sla", "2026-10-02T00:00:00Z", sla=gold),
        ev(3, "reply_time", "apply_sla", "2026-10-02T00:00:00Z", sla=gold),
        ev(4, "requester_wait_time", "apply_sla", "2026-10-02T00:00:00Z", sla=gold),
    ]
    page2 = [
        ev(1, "reply_time", "fulfill", "2026-10-03T00:00:00Z"),                # achieved
        ev(2, "reply_time", "breach", "2026-10-03T01:00:00Z"),
        ev(2, "reply_time", "fulfill", "2026-10-03T05:00:00Z"),                # breached (fulfilled late)
        ev(3, "reply_time", "breach", "2026-10-04T00:00:00Z"),                 # breached, still open
        ev(4, "requester_wait_time", "breach", "2026-10-04T00:00:00Z", deleted=True),
        ev(4, "requester_wait_time", "fulfill", "2026-10-05T00:00:00Z"),       # achieved (breach deleted)
        ev(5, "reply_time", "fulfill", "2026-10-05T00:00:00Z"),                # no SLA applied: ignored
    ]

    def handler(req):
        if req.url.params["start_time"] == str(int(datetime(2026, 9, 17, tzinfo=timezone.utc).timestamp())):
            return {"ticket_metric_events": page1, "end_time": 1790000000, "end_of_stream": False}
        return {"ticket_metric_events": page2, "end_time": 1792000000, "end_of_stream": True}

    fake.on("GET", "/api/v2/incremental/ticket_metric_events", handler)
    async with connect() as cl:
        out = data(await cl.call_tool("sla_report", {"start": "2026-10-01", "end": "2026-10-15"}))
    rows = {r["metric"]: r for r in out["by_policy_and_metric"]}
    assert rows["reply_time"] == {"policy": "Gold", "metric": "reply_time", "achieved": 1, "breached": 2,
                                  "achievement_pct": 33.3}
    assert rows["requester_wait_time"]["achievement_pct"] == 100.0
    assert out["overall"] == {"achieved": 2, "breached": 2, "achievement_pct": 50.0}
    assert out["currently_breached_unfulfilled"]["count"] == 1
    assert out["currently_breached_unfulfilled"]["tickets"][0]["ticket_id"] == 3
    assert out["scope"]["complete"] is True and len(fake.calls) == 2


async def test_backlog_report(connect, fake, monkeypatch):
    monkeypatch.setattr("zendesk_mcp.tools.analytics.utcnow", lambda: NOW)
    tickets = [
        {**_t(1, "new", None, None), "created_at": "2026-10-14T10:00:00Z", "updated_at": "2026-10-14T10:00:00Z"},
        {**_t(2, "open", 8, None), "created_at": "2026-10-12T15:00:00Z", "updated_at": "2026-10-13T00:00:00Z"},
        {**_t(3, "pending", 8, None), "created_at": "2026-08-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z"},
    ]
    _search_routes(fake, tickets)
    fake.on("GET", "/api/v2/users/show_many", {"users": USERS})
    fake.on("GET", "/api/v2/groups", {"groups": [{"id": 3, "name": "Tier 2"}], "meta": {"has_more": False}})
    async with connect() as cl:
        out = data(await cl.call_tool("backlog_report", {"group_by": "assignee"}))
    # ages: 5h (<1d), 48h (1-3d), ~74 days (>30d)
    assert out["age_since_created"] == {"<1d": 1, "1-3d": 1, "3-7d": 0, "7-30d": 0, ">30d": 1}
    assert out["stale"]["count"] == 1 and out["unassigned"] == 1
    alice = next(r for r in out["by_assignee"] if r["assignee"] == "Alice")
    assert alice == {"assignee": "Alice", "total": 2, "open": 1, "pending": 1, "stale": 1}
    assert out["oldest"][0]["id"] == 3 and out["oldest"][0]["assignee"] == "Alice"
    q = [r for r in fake.calls if r.url.path == "/api/v2/search/export"][0].url.params["query"]
    assert q == "status<solved"


async def test_support_kpis_batches_show_many(connect, fake):
    tickets = [_t(i, "solved", 8, "good") for i in range(1, 251)]
    _search_routes(fake, tickets)

    def show_many(req):
        ids = [int(x) for x in req.url.params["ids"].split(",")]
        assert len(ids) <= 100
        return {"tickets": [_t(i, "solved", 8, "good") for i in ids], "users": USERS,
                "metric_sets": [_metric(i, (i, i), (i, i), 1) for i in ids]}

    fake.on("GET", "/api/v2/tickets/show_many", show_many)
    async with connect() as cl:
        out = data(await cl.call_tool("support_kpis", {"max_tickets": 5000}))
    assert len([r for r in fake.calls if r.url.path == "/api/v2/tickets/show_many"]) == 3
    assert out["tickets"] == 250 and out["first_reply_time_min"]["calendar"]["median"] == 125.5  # median of 1..250
    assert out["efficiency"]["one_touch_resolution_pct"] == 100.0
