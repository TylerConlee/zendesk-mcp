"""Reporting tests. One fake instance: Sept 2026 = current period, Aug 2026 = previous.
Expected values are worked out by hand in comments."""

import json
from datetime import date, datetime, timezone

import pytest

from conftest import data, text
from zendesk_mcp.periods import buckets, resolve_period
from zendesk_mcp.privacy import Redactor
from zendesk_mcp.targets import compare, status

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)  # a Wednesday


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    for mod in ("periods", "tools.analytics", "tools.reporting"):
        monkeypatch.setattr(f"zendesk_mcp.{mod}.utcnow", lambda: NOW)


USERS = [{"id": 7, "name": "Jane Doe", "role": "end-user"}, {"id": 8, "name": "Alice Smith", "role": "agent"},
         {"id": 9, "name": "Bob Jones", "role": "agent"}]
GROUPS = [{"id": 3, "name": "Tier 1"}, {"id": 4, "name": "Tier 2"}]


def T(tid, created, *, cat=None, tags=(), channel="email", csat=None, org=None, problem=None, group=3,
      priority="normal", status="solved", desc="hello"):
    t = {"id": tid, "subject": f"Ticket {tid}", "status": status, "created_at": created, "updated_at": created,
         "requester_id": 7, "assignee_id": 8, "group_id": group, "organization_id": org, "problem_id": problem,
         "priority": priority, "type": "incident" if problem else "question", "via": {"channel": channel},
         "tags": list(tags) + ([cat] if cat else []), "satisfaction_rating": {"score": csat} if csat else None,
         "custom_fields": [{"id": 100, "value": cat}], "description": desc}
    return t


def M(tid, frt, replies=1, reopens=0, full=100):
    return {"ticket_id": tid, "replies": replies, "reopens": reopens, "assignee_stations": 1, "group_stations": 1,
            "solved_at": None, "reply_time_in_minutes": {"calendar": frt, "business": frt},
            "full_resolution_time_in_minutes": {"calendar": full, "business": full},
            "first_resolution_time_in_minutes": {"calendar": full, "business": full},
            "requester_wait_time_in_minutes": {"calendar": 50, "business": 50}}


# Current (Sept): created Wed 9/2 10:00 PDT, Thu 9/3 10:30 PDT, Mon 9/14 19:00 PDT (=9/15 02:00Z), Tue 9/29 09:00 PDT
CUR = [
    T(1, "2026-09-02T17:00:00Z", cat="cat_billing", tags=["vip"], csat="good", org=50,
      desc="Hi I'm Jane Doe, email jane@acme.com, card 4111 1111 1111 1111"),
    T(2, "2026-09-03T17:30:00Z", cat="cat_billing", channel="web", csat="bad", org=50, problem=900, priority="urgent"),
    T(3, "2026-09-15T02:00:00Z", cat="cat_bug", org=51, problem=900, status="open"),
    T(4, "2026-09-29T16:00:00Z", cat="cat_billing", csat="good"),
]
CUR_M = {1: M(1, 10, replies=1), 2: M(2, 30, replies=4, reopens=1), 3: M(3, 60, replies=2), 4: M(4, 20, replies=1)}
PREV = [T(11, "2026-08-05T12:00:00Z", cat="cat_bug"), T(12, "2026-08-20T12:00:00Z", cat="cat_bug", csat="good")]
PREV_M = {11: M(11, 40), 12: M(12, 40)}
BACKLOG = [T(3, "2026-09-15T02:00:00Z", status="open")]
ALL = {t["id"]: t for t in CUR + PREV + [{"id": 900, "subject": "Login outage", "status": "open",
                                          "created_at": "2026-08-30T00:00:00Z"}]}
ALL_M = {**CUR_M, **PREV_M}
FIELDS = [{"id": 100, "title": "Category", "type": "tagger", "active": True,
           "custom_field_options": [{"name": "Billing", "value": "cat_billing"}, {"name": "Bug", "value": "cat_bug"}]}]


def install(fake):
    def pick(req):
        q = req.url.params["query"]
        if "status<solved" in q:
            return BACKLOG
        if "created>=2026-09-01" in q:
            return CUR
        if "created>=2026-08-01" in q:
            return PREV
        return []

    fake.on("GET", "/api/v2/search/count", lambda r: {"count": len(pick(r))})
    fake.on("GET", "/api/v2/search/export", lambda r: {"results": pick(r), "meta": {"has_more": False}})

    def show_many(req):
        ids = [int(x) for x in req.url.params["ids"].split(",")]
        return {"tickets": [ALL[i] for i in ids if i in ALL], "users": USERS, "groups": GROUPS,
                "metric_sets": [ALL_M[i] for i in ids if i in ALL_M]}

    fake.on("GET", "/api/v2/tickets/show_many", show_many)
    fake.on("GET", "/api/v2/ticket_fields", {"ticket_fields": FIELDS, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/organizations/show_many", {"organizations": [{"id": 50, "name": "Acme"},
                                                                           {"id": 51, "name": "Globex"}]})
    fake.on("GET", "/api/v2/users/show_many", {"users": USERS})
    fake.on("GET", "/api/v2/groups", {"groups": GROUPS, "meta": {"has_more": False}})


# ----------------------------------------------------------------------------- pure helpers
def test_periods():
    q3 = resolve_period("last_quarter")
    assert (q3.start, q3.end, q3.label) == (date(2026, 7, 1), date(2026, 10, 1), "2026-Q3")
    q2 = q3.previous()
    assert (q2.start, q2.end, q2.label) == (date(2026, 4, 1), date(2026, 7, 1), "2026-Q2")
    qtd = resolve_period("this_quarter")
    assert (qtd.start, qtd.end, qtd.partial) == (date(2026, 10, 1), date(2026, 10, 15), True)
    assert (qtd.previous().start, qtd.previous().end) == (date(2026, 7, 1), date(2026, 7, 15))  # same 14 days
    sep = resolve_period("2026-09")
    assert (sep.previous().start, sep.previous().end) == (date(2026, 8, 1), date(2026, 9, 1))
    r = resolve_period("last_30_days")
    assert (r.start, r.end, r.previous().start) == (date(2026, 9, 15), date(2026, 10, 15), date(2026, 8, 16))
    assert resolve_period("2026-Q4").label == "2026-Q4 to date"
    assert resolve_period(None, "2026-01-01", "2026-02-01").days == 31
    with pytest.raises(Exception, match="Unrecognised"):
        resolve_period("last_fortnight")
    # Sept 1 2026 is a Tuesday, so weekly buckets start Monday Aug 31
    assert buckets(sep, "week") == [date(2026, 8, 31), date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21),
                                    date(2026, 9, 28)]


def test_redactor():
    red = Redactor(["Jane Doe"])
    out = red("Hi, I'm Jane Doe (jane@x.com), call +1 (503) 555-0199 on 2026-10-14, order 4111111111111111, "
              "see https://x.co/a?b=1. Thanks, Jane")
    assert out == ("Hi, I'm [name] ([email]), call [number] on 2026-10-14, order [number], see [link] "
                   "Thanks, [name]")


def test_targets_and_compare():
    targets = {"csat_pct": {"goal": 92, "amber_pct": 10}, "frt_median_min": {"goal": 60, "amber_pct": 10}}
    assert status("csat_pct", 90, targets)["status"] == "amber"  # >= 82.8
    assert status("csat_pct", 80, targets)["status"] == "red"
    assert status("frt_median_min", 50, targets)["status"] == "green"
    assert status("frt_median_min", 65, targets)["status"] == "amber"  # <= 66
    assert status("frt_median_min", 70, targets)["status"] == "red"
    assert compare("frt_median_min", 50, 60) == {"current": 50, "previous": 60, "change": -10, "change_pct": -16.7,
                                                 "trend": "improved"}
    assert compare("tickets_created", 4, 2)["trend"] == "up"
    assert compare("csat_pct", 66.7, 100)["trend"] == "worsened"


# ----------------------------------------------------------------------------- tools
async def test_kpi_trends(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("kpi_trends", {"period": "2026-09", "interval": "week", "split_by": "channel",
                                                     "csv": "inline"}))
    rows = {r["period_start"]: r for r in out["trend"]}
    assert list(rows) == ["2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"]
    assert rows["2026-08-31"]["tickets"] == 2 and rows["2026-08-31"]["frt_median_min"] == 20.0  # [10, 30]
    assert rows["2026-09-07"]["tickets"] == 0
    vs = out["vs_previous"]
    # FRT median: current [10,30,60,20] -> 25; previous [40,40] -> 40
    assert vs["frt_median_min"] == {"current": 25.0, "previous": 40.0, "change": -15.0, "change_pct": -37.5,
                                    "trend": "improved"}
    assert vs["tickets_created"]["change_pct"] == 100.0 and vs["tickets_created"]["trend"] == "up"
    # CSAT current good 2 / (2+1) = 66.7 vs previous 100 -> worsened
    assert vs["csat_pct"]["trend"] == "worsened"
    split = [r for r in out["volume_by_channel"] if r["period_start"] == "2026-08-31"]
    assert {r["channel"]: r["tickets"] for r in split} == {"email": 1, "web": 1}
    assert out["csv"]["trend"].splitlines()[0].startswith("period_start,tickets,solved")


async def test_dataset_cache_avoids_refetch(connect, fake):
    install(fake)
    async with connect() as cl:
        data(await cl.call_tool("kpi_trends", {"period": "2026-09"}))
        n = len(fake.calls)
        data(await cl.call_tool("ticket_topics", {"period": "2026-09"}))
    # second tool reuses both cached datasets; only ticket_fields is new
    assert [r.url.path for r in fake.calls[n:]] == ["/api/v2/ticket_fields"]


async def test_ticket_topics(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("ticket_topics", {"period": "2026-09"}))
    topics = {r["topic"]: r for r in out["topics"]}
    # cat_billing tag is the dropdown's value, so it is counted once (as the field), not again as a tag
    assert "tag: cat_billing" not in topics
    assert topics["Category: Billing"] == {"topic": "Category: Billing", "tickets": 3, "share_pct": 75.0,
                                           "csat_pct": 66.7, "frt_median_min": 20.0,
                                           "full_resolution_median_min": 100.0, "previous": 0, "change": 3}
    assert topics["Category: Bug"]["previous"] == 2 and topics["Category: Bug"]["change_pct"] == -50.0
    assert topics["tag: vip"]["tickets"] == 1
    assert [r["topic"] for r in out["rising"]] == ["Category: Billing"]
    assert out["falling"] == []  # Bug moved 2 -> 1, below the 3-ticket noise floor


async def test_sample_ticket_text_redacts(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("sample_ticket_text", {"period": "2026-09", "sample_size": 4}))
    assert out["total_in_scope"] == 4 and out["sample_size"] == 4
    t1 = next(t for t in out["tickets"] if t["id"] == 1)
    assert t1["excerpt"] == "Hi I'm [name], email [email], card [number]"


async def test_csat_feedback(connect, fake):
    ratings = [
        {"id": 1, "score": "good", "comment": "Thanks Alice, great help!", "ticket_id": 1, "assignee_id": 8,
         "requester_id": 7, "group_id": 3, "created_at": "2026-09-03T00:00:00Z"},
        {"id": 2, "score": "good", "comment": "Fast", "ticket_id": 4, "assignee_id": 8, "group_id": 3,
         "created_at": "2026-09-30T00:00:00Z"},
        {"id": 3, "score": "good", "comment": None, "ticket_id": 5, "assignee_id": 9, "group_id": 4,
         "created_at": "2026-09-10T00:00:00Z"},
        {"id": 4, "score": "bad", "comment": "Jane here, took a week", "reason_id": 1, "ticket_id": 2,
         "assignee_id": 9, "requester_id": 7, "group_id": 4, "created_at": "2026-09-12T00:00:00Z"},
        {"id": 5, "score": "bad", "comment": None, "ticket_id": 6, "group_id": 4, "created_at": "2026-09-13T00:00:00Z"},
    ]
    fake.on("GET", "/api/v2/satisfaction_ratings", {"satisfaction_ratings": ratings, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/satisfaction_reasons", {"reasons": [{"id": 1, "value": "Took too long"}]})
    fake.on("GET", "/api/v2/users/show_many", {"users": USERS})
    fake.on("GET", "/api/v2/groups", {"groups": GROUPS, "meta": {"has_more": False}})
    async with connect() as cl:
        out = data(await cl.call_tool("csat_feedback", {"period": "2026-09"}))
    params = [r for r in fake.calls if r.url.path == "/api/v2/satisfaction_ratings"][0].url.params
    assert params["score"] == "received" and params["start_time"] == "1788220800"  # 2026-09-01T00:00Z
    assert out["totals"]["csat_pct"] == 60.0 and out["totals"]["with_comment"] == 3
    assert out["bad_reasons"] == [{"reason": "Took too long", "count": 1, "share_pct": 50.0},
                                  {"reason": "No reason given", "count": 1, "share_pct": 50.0}]
    groups = {r["group"]: r for r in out["by_group"]}
    assert groups["Tier 1"]["csat_pct"] == 100.0 and groups["Tier 2"]["csat_pct"] == 33.3
    good = out["verbatims"]["good"]
    assert [q["ticket_id"] for q in good] == [4, 1]  # newest first, comments only
    assert good[1]["comment"] == "Thanks [name], great help!" and "agent" not in good[1]
    assert out["verbatims"]["bad"][0] == {"comment": "[name] here, took a week", "ticket_id": 2,
                                          "created_at": "2026-09-12T00:00:00Z", "group": "Tier 2",
                                          "reason": "Took too long"}
    assert "by_agent" not in out


async def test_volume_heatmap(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("volume_heatmap", {"period": "2026-09", "time_zone": "America/Los_Angeles"}))
    grid = out["matrix"]["values"]
    assert grid[2][10] == 1 and grid[3][10] == 1 and grid[0][19] == 1 and grid[1][9] == 1  # Wed, Thu, Mon, Tue
    assert sum(map(sum, grid)) == 4
    # Sept 2026 has 5 Wednesdays and 4 Mondays
    peaks = {(p["day"], p["hour"]): p["avg_per_day"] for p in out["peaks"]}
    assert peaks[("Wed", 10)] == 0.2 and peaks[("Mon", 19)] == 0.25
    hour10 = next(h for h in out["frt_median_by_creation_hour"] if h["hour"] == 10)
    assert hour10 == {"hour": 10, "frt_median_min": 20.0, "tickets": 2}


async def test_top_issues(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("top_issues", {"period": "2026-09", "anonymize_orgs": False}))
    assert out["problems_with_most_incidents"] == [{"problem_ticket_id": 900, "linked_incidents": 2,
                                                    "subject": "Login outage", "status": "open",
                                                    "created_at": "2026-08-30T00:00:00Z"}]
    acme = out["top_organizations"][0]
    assert acme["organization"] == "Acme" and acme["tickets"] == 2 and acme["urgent_or_high"] == 1
    assert acme["csat_pct"] == 50.0
    assert out["highest_effort_tickets"][0]["id"] == 2 and out["highest_effort_tickets"][0]["replies"] == 4
    assert "assignee" not in out["highest_effort_tickets"][0]
    assert [t["id"] for t in out["most_reopened_tickets"]] == [2]
    async with connect() as cl:
        anon = data(await cl.call_tool("top_issues", {"period": "2026-09", "anonymize_orgs": True}))
    assert anon["top_organizations"][0]["organization"] == "Customer 1"


async def test_help_center_report(connect, fake):
    install(fake)
    arts = [
        {"id": 1, "title": "Refunds", "vote_count": 10, "vote_sum": 8, "edited_at": "2026-09-01T00:00:00Z"},
        {"id": 2, "title": "Reset password", "vote_count": 10, "vote_sum": -6, "edited_at": "2026-09-01T00:00:00Z"},
        {"id": 3, "title": "Draft", "draft": True, "vote_count": 0, "vote_sum": 0},
        {"id": 4, "title": "Old", "vote_count": 2, "vote_sum": 2, "edited_at": "2024-01-01T00:00:00Z",
         "outdated": True},
    ]
    fake.on("GET", "/api/v2/help_center/articles", {"articles": arts, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/help_center/articles/search",
            lambda r: {"results": [{"title": "Bug reports"}], "count": 1} if r.url.params["query"] == "Bug"
            else {"results": [], "count": 0})
    async with connect() as cl:
        out = data(await cl.call_tool("help_center_report", {"period": "2026-09", "gap_topics": 3}))
    assert out["articles"] == {"total": 4, "published": 3, "drafts": 1, "flagged_outdated": 1,
                               "not_edited_in_365_days": 1, "more_available": False}
    # up votes = (count + sum) / 2 -> 9/10 = 90%, 2/10 = 20%; article 4 has < 5 votes
    assert [(a["id"], a["helpful_pct"]) for a in out["most_helpful"]] == [(1, 90.0), (2, 20.0)]
    assert out["least_helpful"][0]["id"] == 2 and out["stalest"][0]["id"] == 4
    coverage = {g["topic"]: g for g in out["topic_coverage"]}
    assert coverage["Category: Bug"]["articles_found"] == 1
    assert {g["topic"] for g in out["gaps"]} == {"Category: Billing", "tag: vip"}


async def test_report_targets(connect, tmp_path):
    async with connect(file_root=str(tmp_path)) as cl:
        out = data(await cl.call_tool("report_targets", {"action": "set", "targets": {"csat_pct": 92,
                                                         "frt_median_min": {"goal": 60, "amber_pct": 20}}}))
        assert out["targets"]["frt_median_min"] == {"goal": 60.0, "amber_pct": 20.0}
        assert json.loads((tmp_path / "zendesk_targets.json").read_text())["csat_pct"]["goal"] == 92.0
        r = await cl.call_tool("report_targets", {"action": "set", "targets": {"tickets_created": 5}})
        assert r.is_error and "untargetable" in text(r)
        out = data(await cl.call_tool("report_targets", {"action": "clear", "metrics": ["csat_pct"]}))
    assert list(out["targets"]) == ["frt_median_min"]


async def test_support_report_bundle(connect, fake, tmp_path):
    install(fake)
    (tmp_path / "zendesk_targets.json").write_text(json.dumps({"csat_pct": {"goal": 92, "amber_pct": 10},
                                                               "frt_median_min": {"goal": 30, "amber_pct": 10}}))
    fake.on("GET", "/api/v2/incremental/ticket_metric_events", {"ticket_metric_events": [], "end_of_stream": True})
    fake.on("GET", "/api/v2/satisfaction_ratings", {"satisfaction_ratings": [
        {"id": 1, "score": "bad", "comment": "Alice Smith never replied", "ticket_id": 2, "assignee_id": 8,
         "group_id": 3, "created_at": "2026-09-05T00:00:00Z"}], "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/satisfaction_reasons", {"reasons": []})
    fake.on("GET", "/api/v2/help_center/articles", {"articles": [], "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/help_center/articles/search", {"results": [], "count": 0})
    async with connect(file_root=str(tmp_path)) as cl:
        out = data(await cl.call_tool("support_report", {"period": "2026-09", "time_zone": "America/Los_Angeles",
                                                         "csv": "files"}))
    assert "_truncated" not in out
    assert out["report"]["period"]["label"] == "2026-09" and out["report"]["previous_period"]["label"] == "2026-08"
    cards = {c["metric"]: c for c in out["headline"]}
    assert cards["tickets_created"]["current"] == 4 and cards["tickets_created"]["previous"] == 2
    assert cards["frt_median_min"]["current"] == 25.0 and cards["frt_median_min"]["trend"] == "improved"
    assert cards["frt_median_min"]["target"] == {"goal": 30.0, "status": "green"}
    assert cards["csat_pct"]["target"]["status"] == "red"  # 66.7 < 82.8
    assert cards["backlog_unsolved"]["current"] == 1
    assert "sla_achievement_pct" in cards  # present even with no SLA events (value None)
    for section in ("trends", "channels", "topics", "csat", "sla", "heatmap", "issues", "backlog", "help_center"):
        assert section in out, section
    assert out["csat"]["verbatims"]["bad"][0]["comment"] == "[name] never replied"
    assert all("assignee" not in t for t in out["issues"]["highest_effort_tickets"])
    assert out["help_center"]["gaps"]  # nothing in the KB matches any topic
    assert {d["metric"] for d in out["definitions"]} >= {"frt_median_min", "csat_pct"}
    slides = [s["slide"] for s in out["suggested_slides"]]
    assert slides[0] == "Title" and slides[-1] == "Appendix: metric definitions"
    names = sorted(p.split("/")[-1].split("-")[0] for p in out["csv_files"])
    assert {"headline", "trend", "topics", "heatmap", "channels"} <= set(names)
    assert all((tmp_path / "reports").joinpath(p.split("/")[-1]).exists() for p in out["csv_files"])
