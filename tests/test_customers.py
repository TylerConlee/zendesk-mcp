"""customer_profile tests. Org 50 "Acme Corp": Sept 2026 = current period, Aug 2026 = previous.
Org 51 is another customer, used for the all-customer benchmark. Expected values worked out in comments."""

from datetime import datetime, timezone

import httpx
import pytest

from conftest import data, make_settings, text
from zendesk_mcp.client import ZendeskClient
from zendesk_mcp.tools.customers import difficulty, resolve_org, score_message, tone

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    for mod in ("periods", "tools.analytics", "tools.reporting", "tools.customers"):
        monkeypatch.setattr(f"zendesk_mcp.{mod}.utcnow", lambda: NOW)


USERS = [{"id": 7, "name": "Jane Doe", "role": "end-user", "email": "jane@acme.com"},
         {"id": 8, "name": "Alice Smith", "role": "agent"}]
GROUPS = [{"id": 3, "name": "Tier 1"}, {"id": 4, "name": "Tier 2"}]


def T(tid, created, *, org=50, csat=None, priority="normal", status="solved", channel="email", sentiment=None):
    return {"id": tid, "subject": f"Ticket {tid} for Jane Doe", "status": status, "created_at": created,
            "updated_at": created, "requester_id": 7, "assignee_id": 8, "group_id": 3, "organization_id": org,
            "priority": priority, "type": "question", "via": {"channel": channel}, "tags": ["cat_billing"],
            "satisfaction_rating": csat, "description": "hi",
            "custom_fields": [{"id": 100, "value": "cat_billing"}, {"id": 101, "value": sentiment}]}


def M(tid, frt, replies=1, reopens=0, full=100, groups=1):
    return {"ticket_id": tid, "replies": replies, "reopens": reopens, "assignee_stations": 1, "group_stations": groups,
            "reply_time_in_minutes": {"calendar": frt, "business": frt},
            "full_resolution_time_in_minutes": {"calendar": full, "business": full},
            "first_resolution_time_in_minutes": {"calendar": full, "business": full},
            "requester_wait_time_in_minutes": {"calendar": 50, "business": 50}}


CUR = [
    T(1, "2026-09-02T17:00:00Z", csat={"score": "good", "comment": "Great help from Jane's agent, thanks!"}),
    T(2, "2026-09-03T17:30:00Z", priority="urgent", sentiment="sent_negative",
      csat={"score": "bad", "comment": "Took forever", "reason": "Issue not resolved"}),
    T(3, "2026-09-20T17:00:00Z", priority="high", status="open", channel="web"),
]
OTHER = [T(4, "2026-09-10T12:00:00Z", org=51, csat={"score": "good"})]
PREV = [T(11, "2026-08-05T12:00:00Z"), T(12, "2026-08-20T12:00:00Z", csat={"score": "good"})]
ALL = {t["id"]: t for t in CUR + OTHER + PREV}
# ticket 2: 5 replies, reopened, moved groups once, 10 days to resolve
METRICS_ = {1: M(1, 10), 2: M(2, 30, replies=5, reopens=1, full=14400, groups=2), 3: M(3, 60, replies=2),
            4: M(4, 20), 11: M(11, 40), 12: M(12, 40)}
FIELDS = [{"id": 100, "title": "Category", "type": "tagger", "active": True,
           "custom_field_options": [{"name": "Billing", "value": "cat_billing"}]},
          {"id": 101, "title": "Sentiment", "type": "tagger", "active": True,
           "custom_field_options": [{"name": "Negative", "value": "sent_negative"}]}]


def C(cid, author, body, at, public=True):
    return {"id": cid, "author_id": author, "plain_body": body, "public": public, "created_at": at, "via": {}}


COMMENTS = {
    1: [C(1, 7, "Can't log in.", "2026-09-02T17:00:00Z"), C(2, 8, "Fixed.", "2026-09-02T17:10:00Z"),
        C(3, 7, "Thank you so much, works now!", "2026-09-02T18:00:00Z")],
    2: [C(4, 7, "Billing is wrong.", "2026-09-03T17:30:00Z"), C(5, 8, "Looking.", "2026-09-03T18:00:00Z"),
        C(6, 7, "This is still not fixed!! Unacceptable. Call me on +1 503 555 0199.\n\nOn Tue, Alice wrote:\n"
                "> thanks, great", "2026-09-05T18:00:00Z"),
        C(7, 8, "Sorry, escalating.", "2026-09-05T20:00:00Z")],
    3: [C(8, 7, "How do I configure SSO for jane@acme.com?", "2026-09-20T17:00:00Z"),
        C(9, 8, "Internal: check config", "2026-09-20T17:05:00Z", public=False)],
}
SLA_EVENTS = [
    {"ticket_id": 1, "metric": "reply_time", "instance_id": 1, "type": "apply_sla", "time": "2026-09-02T17:00:00Z",
     "sla": {"target": 60, "business_hours": False, "policy": {"title": "Enterprise"}}},
    {"ticket_id": 1, "metric": "reply_time", "instance_id": 1, "type": "fulfill", "time": "2026-09-02T17:10:00Z"},
    {"ticket_id": 2, "metric": "reply_time", "instance_id": 1, "type": "apply_sla", "time": "2026-09-03T17:30:00Z",
     "sla": {"target": 15, "business_hours": False, "policy": {"title": "Enterprise"}}},
    {"ticket_id": 2, "metric": "reply_time", "instance_id": 1, "type": "breach", "time": "2026-09-03T17:45:00Z"},
    {"ticket_id": 2, "metric": "reply_time", "instance_id": 1, "type": "fulfill", "time": "2026-09-03T18:00:00Z"},
    # another customer's ticket — must not count
    {"ticket_id": 4, "metric": "reply_time", "instance_id": 1, "type": "apply_sla", "time": "2026-09-10T12:00:00Z",
     "sla": {"target": 60, "business_hours": False, "policy": {"title": "Enterprise"}}},
    {"ticket_id": 4, "metric": "reply_time", "instance_id": 1, "type": "fulfill", "time": "2026-09-10T12:05:00Z"},
]
ACME = {"id": 50, "name": "Acme Corp", "created_at": "2024-01-15T00:00:00Z", "tags": ["enterprise"],
        "domain_names": ["acme.com"], "organization_fields": {"plan": "ent", "csm": None}}


def install(fake):
    def pick(req):
        q = req.url.params["query"]
        if "status<solved" in q:
            return [CUR[2]] if "organization:50" in q else []
        sept, aug = "created>=2026-09-01" in q, "created>=2026-08-01" in q
        if "organization:50" in q:
            return CUR if sept else PREV if aug else []
        return CUR + OTHER if sept else []

    fake.on("GET", "/api/v2/search/count", lambda r: {"count": len(pick(r))})
    fake.on("GET", "/api/v2/search/export", lambda r: {"results": pick(r), "meta": {"has_more": False}})

    def show_many(req):
        ids = [int(x) for x in req.url.params["ids"].split(",")]
        return {"tickets": [ALL[i] for i in ids if i in ALL], "users": USERS, "groups": GROUPS,
                "metric_sets": [METRICS_[i] for i in ids if i in METRICS_]}

    fake.on("GET", "/api/v2/tickets/show_many", show_many)
    fake.on("GET", "/api/v2/ticket_fields", {"ticket_fields": FIELDS, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/users/show_many", {"users": USERS})
    fake.on("GET", "/api/v2/groups", {"groups": GROUPS, "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/organizations/autocomplete", lambda r: {"organizations": [
        o for o in [ACME, {"id": 52, "name": "Acme Labs"}] if o["name"].lower().startswith(r.url.params["name"].lower())
    ]})
    fake.on("GET", "/api/v2/search", lambda r: {"results": [{"id": 60, "name": "The Initech Co",
                                                             "result_type": "organization"}]
                                                if "Initech" in r.url.params["query"] else []})
    fake.on("GET", "/api/v2/organizations/50", {"organization": ACME})
    fake.on("GET", "/api/v2/organizations/50/related", {"organization_related": {"users_count": 12,
                                                                                 "tickets_count": 140}})
    fake.on("GET", "/api/v2/organization_fields", {"organization_fields": [
        {"key": "plan", "title": "Plan", "custom_field_options": [{"name": "Enterprise", "value": "ent"}]},
        {"key": "csm", "title": "CSM"}], "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/incremental/ticket_metric_events",
            {"ticket_metric_events": SLA_EVENTS, "end_of_stream": True})
    for tid, cs in COMMENTS.items():
        fake.on("GET", f"/api/v2/tickets/{tid}/comments", {"comments": cs, "users": USERS, "meta": {"has_more": False}})


# ----------------------------------------------------------------------------- pure helpers

def test_tone_scoring():
    s = score_message("Still waiting!! This is unacceptable.\n\nOn Mon, Bob wrote:\n> thanks so much, great")
    assert tone(s["negative"], s["positive"]) == "negative" and s["positive"] == 0  # quoted history ignored
    s = score_message("Thanks so much, that fixed it!")
    assert tone(s["negative"], s["positive"]) == "positive"
    s = score_message("How do I export a CSV? Thanks, Sam")  # a sign-off 'thanks' isn't praise
    assert tone(s["negative"], s["positive"]) == "neutral"
    s = score_message("Any update? Following up again — this has been blocking us for weeks.")
    assert tone(s["negative"], s["positive"]) == "negative"
    assert "following up" in s["cues"]


def test_difficulty_reasons():
    score, why = difficulty(CUR[1], METRICS_[2], NOW)
    # 5 replies + 4 reopen + 2 group move + 6 bad + 4 urgent + 10 days/3
    assert score == pytest.approx(24.3)
    assert why == ["5 agent replies", "reopened 1×", "moved between groups 1×", "rated bad", "urgent priority",
                   "took 10 days to resolve"]
    assert difficulty(CUR[0], METRICS_[1], NOW) == (0.0, [])


# ----------------------------------------------------------------------------- org lookup

async def test_ambiguous_name_lists_candidates(connect, fake):
    install(fake)
    async with connect() as cl:
        res = await cl.call_tool("customer_profile", {"organization": "Acme", "period": "2026-09"})
    assert res.is_error
    assert "Acme Corp (id 50)" in text(res) and "Acme Labs (id 52)" in text(res)


async def test_lookup_by_exact_name_id_and_search(fake):
    install(fake)
    zd = ZendeskClient(make_settings(), transport=httpx.MockTransport(fake.handle))
    assert (await resolve_org(zd, "acme corp"))["id"] == 50  # case-insensitive exact beats prefix matches
    assert (await resolve_org(zd, "50"))["id"] == 50
    assert (await resolve_org(zd, "Initech"))["id"] == 60  # not a prefix: falls back to search
    await zd.aclose()


# ----------------------------------------------------------------------------- full profile

async def test_customer_profile(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("customer_profile", {"organization": "Acme Corp", "period": "2026-09"}))

    c = out["customer"]
    assert c["name"] == "Acme Corp" and c["fields"] == {"Plan": "Enterprise"}  # labelled, empty CSM dropped
    assert c["users"] == 12 and c["tickets_all_time"] == 140

    v = out["volume"]
    assert v["tickets"] == 3 and v["vs_previous"]["change_pct"] == 50.0  # 3 vs 2
    assert v["share_of_all_tickets_pct"] == 75.0  # 3 of 4 account-wide
    assert v["top_contacts"] == [{"contact": "Contact 1", "tickets": 3}]  # redacted by default
    assert {r["value"]: r["tickets"] for r in v["channels"]} == {"email": 2, "web": 1}
    assert v["topics"]["topics"][0]["topic"] == "Category: Billing"

    card = {r["metric"]: r for r in out["performance"]["scorecard"]}
    assert card["frt_median_min"]["current"] == 30 and card["frt_median_min"]["previous"] == 40
    assert card["frt_median_min"]["all_customers"] == 25  # median of 10, 20, 30, 60
    assert card["frt_median_min"]["vs_all_customers"] == "worse"
    assert card["csat_pct"]["current"] == 50.0 and card["csat_pct"]["all_customers"] == 66.7
    sla = out["performance"]["sla"]["overall"]
    assert (sla["achieved"], sla["breached"]) == (1, 1)  # org 51's SLA event is excluded

    assert out["backlog_now"]["unsolved_total"] == 1
    assert "Jane" not in out["backlog_now"]["oldest"][0]["subject"]

    hard = out["challenging_tickets"]["tickets"]
    assert [t["id"] for t in hard] == [2, 3]
    assert "unsolved for 23 days" in hard[1]["why"] and "high priority" in hard[1]["why"]
    assert hard[0]["url"] == "https://acme.zendesk.com/agent/tickets/2" and "assignee" not in hard[0]

    s = out["sentiment"]
    assert s["csat"]["good"] == 1 and s["csat"]["bad"] == 1 and s["csat"]["previous_score_pct"] == 100.0
    assert s["csat"]["bad_reasons"] == [{"reason": "Issue not resolved", "count": 1}]
    assert "[name]" in s["csat"]["comments"][-1]["comment"]  # 'Jane' redacted from the CSAT comment
    assert s["sentiment_field"] == [{"value": "Sentiment: Negative", "tickets": 1, "share_pct": 33.3}]
    ct = s["conversation_tone"]
    assert ct["tickets_read"] == 3 and ct["tickets_by_tone"] == {"negative": 1, "neutral": 1, "positive": 1}
    worst = ct["most_negative_excerpts"][0]
    assert worst["ticket_id"] == 2 and "[number]" in worst["excerpt"] and "thanks, great" not in worst["excerpt"]
    assert s["reply_pace_in_sampled_tickets"]["our_response_min"]["n"] == 3  # #1 once, #2 twice; #3 only has an internal note

    signals = [x["signal"] for x in out["signals"]]
    assert out["signals"][0]["type"] == "risk"
    assert any("urgent/high-priority ticket(s) are open" in x for x in signals)
    assert any("1 bad CSAT" in x for x in signals)
    assert not any("reopened" in x for x in signals)  # 1 of 2 solved is too small a sample to flag


async def test_customer_profile_unredacted_with_agents(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("customer_profile", {
            "organization": "50", "period": "2026-09", "redact": False, "team_level": False, "benchmark": False,
            "include_sla": False, "conversations": 0}))
    assert out["volume"]["top_contacts"] == [{"contact": "Jane Doe", "tickets": 3, "email": "jane@acme.com"}]
    assert out["challenging_tickets"]["tickets"][0]["assignee"] == "Alice Smith"
    assert "all_customers" not in out["performance"]["scorecard"][2]
    assert "sla" not in out["performance"] and "conversation_tone" not in out["sentiment"]
    assert not any(c.url.path.endswith("/comments") for c in fake.calls)
