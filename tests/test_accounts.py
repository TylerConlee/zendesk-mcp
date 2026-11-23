"""account_health and customer_timeline. Sept 2026 = current, Aug 2026 = previous; 'now' is 2026-10-14."""

from datetime import datetime, timezone

import pytest

from conftest import data
from zendesk_mcp.tools.accounts import band, health

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    for mod in ("periods", "tools.analytics", "tools.reporting", "tools.customers", "tools.accounts"):
        monkeypatch.setattr(f"zendesk_mcp.{mod}.utcnow", lambda: NOW)


def T(tid, created, org, *, csat=None, priority="normal", status="solved", updated=None, subject=None, problem=None,
      tags=()):
    return {"id": tid, "subject": subject or f"Ticket {tid}", "status": status, "created_at": created,
            "updated_at": updated or created, "requester_id": 7, "assignee_id": 8, "group_id": 3,
            "organization_id": org, "priority": priority, "type": "incident" if problem else "question",
            "problem_id": problem, "via": {"channel": "email"}, "tags": list(tags),
            "satisfaction_rating": csat, "custom_fields": []}


def M(tid, frt, replies=1, reopens=0, solved_at=None):
    return {"ticket_id": tid, "replies": replies, "reopens": reopens, "assignee_stations": 1, "group_stations": 1,
            "solved_at": solved_at, "reply_time_in_minutes": {"calendar": frt, "business": frt},
            "full_resolution_time_in_minutes": {"calendar": 100, "business": 100},
            "first_resolution_time_in_minutes": {"calendar": 100, "business": 100},
            "requester_wait_time_in_minutes": {"calendar": 50, "business": 50}}


CUR = [
    T(1, "2026-09-02T17:00:00Z", 50, csat={"score": "good"}),
    T(2, "2026-09-03T17:30:00Z", 50, csat={"score": "bad", "comment": "Slow"}, priority="urgent", problem=900),
    T(3, "2026-09-20T17:00:00Z", 50, priority="high", status="open"),  # open, idle since 9/20
    T(5, "2026-09-25T17:00:00Z", 50, csat={"score": "bad"}),
    T(4, "2026-09-10T12:00:00Z", 51, csat={"score": "good"}),
]
PREV = [T(11, "2026-08-05T12:00:00Z", 50), T(12, "2026-08-20T12:00:00Z", 50), T(13, "2026-08-21T12:00:00Z", 51)]
OLDER = [T(21, "2026-05-01T12:00:00Z", 50, subject="Old SSO question", csat={"score": "good", "comment": "Thanks!"})]
ALL = {t["id"]: t for t in CUR + PREV + OLDER}
MET = {1: M(1, 10, solved_at="2026-09-03T00:00:00Z"), 2: M(2, 30, replies=5, reopens=1, solved_at="2026-09-13T17:30:00Z"),
       3: M(3, 60, replies=2), 5: M(5, 15, solved_at="2026-09-26T00:00:00Z"), 4: M(4, 20), 11: M(11, 40),
       12: M(12, 40), 13: M(13, 40), 21: M(21, 30, solved_at="2026-05-02T12:00:00Z")}
ORGS = [
    {"id": 50, "name": "Acme Corp", "tags": ["enterprise"],
     "organization_fields": {"renewal_date": "2026-11-30", "plan_type": "enterprise_plan", "arr": 120000}},
    {"id": 51, "name": "Globex", "tags": [], "organization_fields": {"renewal_date": "2027-06-01",
                                                                     "plan_type": "pro_plan"}},
    {"id": 53, "name": "Initech", "tags": ["enterprise"],
     "organization_fields": {"renewal_date": "2026-11-01", "plan_type": "enterprise_plan"}},
]
ORG_FIELDS = [
    {"key": "renewal_date", "title": "Renewal date", "type": "date"},
    {"key": "plan_type", "title": "Plan type", "type": "dropdown", "custom_field_options": [
        {"name": "Enterprise", "value": "enterprise_plan"}, {"name": "Pro", "value": "pro_plan"}]},
    {"key": "arr", "title": "ARR", "type": "decimal"},
    {"key": "favourite_colour", "title": "Favourite colour", "type": "text"},
]


def install(fake):
    def pick(req):
        q = req.url.params["query"]
        if "status<solved" in q:
            return [t for t in CUR if t["status"] != "solved" and ("organization:" not in q or
                                                                    f"organization:{t['organization_id']}" in q)]
        pool = CUR if "created>=2026-09-01" in q else PREV if "created>=2026-08-01" in q else \
            CUR + PREV + OLDER if "created>=2025-10-15" in q else []
        if "organization:" in q:
            pool = [t for t in pool if f"organization:{t['organization_id']}" in q]
        return pool

    fake.on("GET", "/api/v2/search/count", lambda r: {"count": len(pick(r))})
    fake.on("GET", "/api/v2/search/export", lambda r: {"results": pick(r), "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/tickets/show_many", lambda r: {
        "tickets": [ALL[int(i)] for i in r.url.params["ids"].split(",") if int(i) in ALL],
        "metric_sets": [MET[int(i)] for i in r.url.params["ids"].split(",") if int(i) in MET],
        "users": [{"id": 7, "name": "Jane Doe", "role": "end-user"}, {"id": 8, "name": "Alice", "role": "agent"}],
        "groups": [{"id": 3, "name": "Tier 1"}]})
    fake.on("GET", "/api/v2/ticket_fields", {"ticket_fields": [], "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/organization_fields", {"organization_fields": ORG_FIELDS, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/organizations", {"organizations": ORGS, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/organizations/show_many", lambda r: {
        "organizations": [o for o in ORGS if str(o["id"]) in r.url.params["ids"].split(",")]})
    fake.on("GET", "/api/v2/organizations/50", {"organization": ORGS[0]})
    fake.on("GET", "/api/v2/organizations/autocomplete", lambda r: {
        "organizations": [o for o in ORGS if o["name"].lower().startswith(r.url.params["name"].lower())]})


# ----------------------------------------------------------------------------- account_health

def test_health_scoring():
    m = {"tickets": 4, "solved": 3, "csat_good": 1, "csat_bad": 2, "csat_pct": 33.3, "urgent_high_open": 1,
         "stale_open": 1, "change_pct": 100.0, "reopened_pct": 33.3, "frt_median_min": 22.5,
         "negative_sentiment_pct": 0}
    score, why = health(m, 20)
    # 100 - 20 (CSAT, capped) - 10 (2 bad) - 8 (urgent/high) - 4 (idle); volume needs 5+ tickets, reopens 5+ solved
    assert score == 58 and band(score) == "at_risk"
    assert why == ["CSAT 33.3% (3 ratings)", "2 bad rating(s)", "1 urgent/high ticket(s) open",
                   "1 open ticket(s) idle 7+ days"]
    assert band(None) == "quiet" and band(80) == "healthy" and band(79) == "watch"


async def test_account_health_ranks_all_customers(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("account_health", {"period": "2026-09"}))
    rows = out["customers"]
    assert [r["organization"] for r in rows] == ["Acme Corp", "Globex"]  # worst first; Initech has no tickets
    acme = rows[0]
    assert (acme["score"], acme["band"], acme["tickets"], acme["previous_tickets"]) == (58, "at_risk", 4, 2)
    assert acme["fields"] == {"Renewal date": "2026-11-30", "Plan type": "Enterprise", "ARR": 120000}
    assert acme["days_to_renewal"] == 47 and acme["open_now"] == 1 and acme["stale_open"] == 1
    assert rows[1]["band"] == "healthy" and rows[1]["reasons"] == []
    assert out["summary"] == {"at_risk": 1, "watch": 0, "healthy": 1, "quiet": 0}
    assert out["all_customer_frt_median_min"] == 20  # median of 10, 30, 60, 15, 20


async def test_account_health_renewal_and_segment_filters(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("account_health", {"period": "2026-09", "renewing_within_days": 60,
                                                         "field_filters": {"Plan type": "enterprise"}}))
        anon = data(await cl.call_tool("account_health", {"period": "2026-09", "org_tags": ["ENTERPRISE"],
                                                          "include_quiet": False, "anonymize_orgs": True}))
        bad = await cl.call_tool("account_health", {"renewing_within_days": 30, "renewal_field": "nope"})
    # Globex renews in 2027 and is on Pro; Initech renews in 18 days with no tickets -> quiet
    assert [(r["organization"], r["band"]) for r in out["customers"]] == [("Acme Corp", "at_risk"), ("Initech", "quiet")]
    assert out["customers"][1]["days_to_renewal"] == 18 and out["customers"][1].get("score") is None
    assert out["scope"]["filters"]["renewal_field"] == "Renewal date"
    assert [r["organization"] for r in anon["customers"]] == ["Customer 1"] and "id" not in anon["customers"][0]
    assert bad.is_error and "No organization field called 'nope'" in bad.content[0].text


# ----------------------------------------------------------------------------- customer_timeline

async def test_customer_timeline(connect, fake):
    install(fake)
    fake.on("GET", "/api/v2/organizations/50/related", {"organization_related": {"users_count": 4}})
    async with connect() as cl:
        out = data(await cl.call_tool("customer_timeline", {"organization": "Acme Corp"}))
    # last 365 days = 2025-10-15 .. 2026-10-14: Acme's tickets 21 (May), 11, 12 (Aug), 1, 2, 3, 5 (Sept)
    s = out["summary"]
    assert (s["tickets"], s["still_open"], s["urgent_or_high"]) == (7, 1, 2)
    assert s["csat"] == {"good": 2, "bad": 2, "score_pct": 50.0}
    assert s["busiest_month"] == {"month": "2026-09", "tickets": 4}
    assert s["first_ticket"] == "2026-05-01T12:00:00Z"
    ev = [(e["event"], e.get("ticket_id")) for e in out["timeline"]]
    # routine tickets 11 and 12 aren't listed by default; 1 and 21 are (rated)
    assert ("ticket opened", 11) not in ev and ("ticket opened", 21) in ev and ("rated good", 21) in ev
    assert ("ticket opened", 2) in ev and ("rated bad", 2) in ev and ("solved", 2) in ev
    solved2 = next(e for e in out["timeline"] if e["event"] == "solved" and e["ticket_id"] == 2)
    assert solved2["reopened"] == 1
    opened3 = next(e for e in out["timeline"] if e["event"] == "ticket opened" and e["ticket_id"] == 3)
    assert opened3["still_open"] and opened3["url"].endswith("/agent/tickets/3")
    quiet = [e for e in out["timeline"] if e["event"].startswith("quiet")]
    # 2025-10-15 -> May 1 (198 days) and May 1 -> Aug 5 (96 days)
    assert [q["event"] for q in quiet] == ["quiet for 198 days", "quiet for 96 days"]
    assert len(out["monthly"]) == 13 and out["monthly"][-2] == {
        "month": "2026-09", "tickets": 4, "solved": 3, "urgent_or_high": 2, "csat_good": 1, "csat_bad": 2,
        "frt_median_min": 22.5, "full_resolution_median_min": 100}
    assert out["customer"]["users"] == 4


async def test_customer_timeline_all_tickets(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("customer_timeline", {"organization": "50", "include_routine": True,
                                                            "period": "2026-08"}))
    assert [e["ticket_id"] for e in out["timeline"] if e["event"] == "ticket opened"] == [11, 12]
