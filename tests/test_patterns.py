"""emerging_issues and deflection_finder. 'Now' is 2026-10-14: recent = Oct 8–14, baseline = Sep 10 – Oct 7."""

from datetime import datetime, timezone

import pytest

from conftest import data
from zendesk_mcp.textmine import cluster, key_terms, tfidf, tokens

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    for mod in ("periods", "tools.analytics", "tools.reporting", "tools.patterns"):
        monkeypatch.setattr(f"zendesk_mcp.{mod}.utcnow", lambda: NOW)


def T(tid, created, org, subject, desc="", priority="normal", status="open", problem=None, requester=7, tags=()):
    return {"id": tid, "subject": subject, "description": desc, "created_at": created, "updated_at": created,
            "organization_id": org, "requester_id": requester, "priority": priority, "status": status,
            "problem_id": problem, "tags": list(tags), "custom_fields": [], "via": {"channel": "email"},
            "assignee_id": 8, "group_id": 3}


RECENT = [
    T(1, "2026-10-09T10:00:00Z", 50, "SSO login fails with SAML error", "Hi team, Jane here. Our SSO login fails."),
    T(2, "2026-10-10T10:00:00Z", 51, "Cannot login via SSO", priority="urgent", problem=900),
    T(3, "2026-10-12T10:00:00Z", 52, "SAML SSO login broken after update"),
    T(4, "2026-10-13T10:00:00Z", 50, "SSO login loop", status="solved"),
    T(5, "2026-10-09T11:00:00Z", 53, "Invoice incorrect"), T(6, "2026-10-10T11:00:00Z", 53, "Invoice incorrect again"),
    T(7, "2026-10-11T11:00:00Z", 53, "Wrong invoice total"),
    T(8, "2026-10-11T12:00:00Z", 54, "Export CSV"), T(9, "2026-10-12T12:00:00Z", 55, "Export CSV broken"),
    T(10, "2026-10-12T13:00:00Z", 56, "Export CSV missing columns"),
]
BASE = [T(100 + i, f"2026-09-{12 + i:02d}T12:00:00Z", 54 + i % 3, "Export CSV question") for i in range(12)] + \
       [T(200, "2026-09-20T12:00:00Z", 50, "SSO setup question")]
USERS = [{"id": 7, "name": "Jane Doe", "role": "end-user"}]


def install(fake):
    def pick(req):
        q = req.url.params["query"]
        return RECENT if "created>=2026-10-08" in q else BASE if "created>=2026-09-10" in q else []

    fake.on("GET", "/api/v2/search/count", lambda r: {"count": len(pick(r))})
    fake.on("GET", "/api/v2/search/export", lambda r: {"results": pick(r), "meta": {"has_more": False}})
    ALL = {t["id"]: t for t in RECENT + BASE}
    fake.on("GET", "/api/v2/tickets/show_many", lambda r: {
        "tickets": [ALL[int(i)] for i in r.url.params["ids"].split(",")], "users": USERS, "groups": [],
        "metric_sets": [{"ticket_id": int(i), "replies": 1} for i in r.url.params["ids"].split(",")]})
    fake.on("GET", "/api/v2/ticket_fields", {"ticket_fields": [], "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/organizations/show_many", {"organizations": [
        {"id": 50, "name": "Acme"}, {"id": 51, "name": "Globex"}, {"id": 52, "name": "Initech"}]})


def test_text_tools():
    assert tokens("Hi team, our SSO login fails! See https://x.co jane@x.com 12345") == ["sso", "login", "fails"]
    assert key_terms("SSO login fails") == {"sso", "login", "fails", "sso login", "login fails"}
    vecs = tfidf([tokens(s) for s in ["sso login fails", "sso login error", "export csv", "export csv columns"]])
    groups = cluster(vecs, threshold=0.35)  # one-line docs score lower than real tickets
    assert sorted(map(sorted, groups)) == [[0, 1], [2, 3]]


async def test_emerging_issues(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("emerging_issues", {}))
    assert len(out["issues"]) == 1  # invoice: one customer only; CSV export: normal for the baseline
    iss = out["issues"][0]
    assert "sso" in iss["matched_terms"] and "login" in iss["matched_terms"]
    assert (iss["recent_tickets"], iss["baseline_tickets"], iss["customers_affected"]) == (4, 1, 3)
    assert iss["customers"][0] == {"customer": "Acme", "tickets": 2}
    assert iss["linked_problems"] == [{"problem_ticket_id": 900, "incidents": 1}]
    assert iss["priority"] == {"normal": 3, "urgent": 1} and iss["still_open"] == 3
    assert iss["first_seen"] == "2026-10-09T10:00:00Z" and len(iss["daily"]) == 7
    assert sum(d["tickets"] for d in iss["daily"]) == 4


async def test_emerging_issues_thresholds(connect, fake):
    install(fake)
    async with connect() as cl:
        solo = data(await cl.call_tool("emerging_issues", {"min_customers": 1, "anonymize_orgs": True}))
    issues = {i["issue"].split(",")[0] for i in solo["issues"]}
    assert "invoice" in issues  # single-customer spike shows when allowed
    assert all(c["customer"].startswith("Customer ") for i in solo["issues"] for c in i["customers"])


# ----------------------------------------------------------------------------- deflection_finder

from zendesk_mcp.tools.patterns import answer_similarity  # noqa: E402

PW = ["Reset password", "How to reset password", "Password reset not working", "Can't reset my password",
      "Reset password link", "Forgot password reset"]
CSV = ["Export to CSV fails", "CSV export error", "Export CSV broken", "Cannot export CSV file", "CSV export failing"]
OTHER = ["Billing invoice question", "Add new user seats", "API rate limit exceeded"]
SOLVED = (
    [T(300 + i, "2026-09-05T10:00:00Z", 50 + i, s, "Hi, I need to reset my password for my account. Thanks, Jane",
       status="solved") for i, s in enumerate(PW)]
    + [T(400 + i, "2026-09-06T10:00:00Z", 60 + i, s, "When I export the project to CSV it fails with an error.",
         status="solved") for i, s in enumerate(CSV)]
    + [T(500 + i, "2026-09-07T10:00:00Z", 70, s, "Quick question about our plan.", status="solved")
       for i, s in enumerate(OTHER)]
)
SOLVED[0]["via"] = {"channel": "web"}
PW_REPLY = "You can reset your password from Settings > Security > Reset password. Here's the link."
CSV_REPLIES = ["Could you send the browser console log?", "Which Label version are you on? Try a smaller project.",
               "This is a known bug in exports with images; engineering is on it.",
               "Please clear your cache and retry the download.", "Can you share the project ID and export format?"]


def install_solved(fake):
    ALL = {t["id"]: t for t in SOLVED}
    fake.on("GET", "/api/v2/search/count", {"count": len(SOLVED)})
    fake.on("GET", "/api/v2/search/export", lambda r: {"results": SOLVED if "solved>=2026-09-01" in
                                                       r.url.params["query"] else [], "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/tickets/show_many", lambda r: {
        "tickets": [ALL[int(i)] for i in r.url.params["ids"].split(",")], "users": USERS, "groups": [],
        "metric_sets": [{"ticket_id": int(i), "replies": 1 if int(i) < 400 else 3,
                         "full_resolution_time_in_minutes": {"calendar": 120}} for i in r.url.params["ids"].split(",")]})
    for t in SOLVED:
        reply = PW_REPLY if t["id"] < 400 else CSV_REPLIES[(t["id"] - 400) % 5] if t["id"] < 500 else "Sure."
        fake.on("GET", f"/api/v2/tickets/{t['id']}/comments", {"comments": [
            {"id": 1, "author_id": 7, "plain_body": t["description"], "public": True},
            {"id": 2, "author_id": 8, "plain_body": "Internal: easy one", "public": False},
            {"id": 3, "author_id": 8, "plain_body": reply + "\n\nBest,\nAlice", "public": True}],
            "users": USERS + [{"id": 8, "name": "Alice Smith", "role": "agent"}], "meta": {"has_more": False}})
    fake.on("GET", "/api/v2/help_center/articles/search", lambda r: {
        "results": [{"title": "Resetting your password", "html_url": "https://help.x/1"}], "count": 1}
        if "password" in r.url.params["query"] else {"results": [], "count": 0})
    fake.on("GET", "/api/v2/macros/search", lambda r: {"macros": [{"title": "CSV export troubleshooting"}]}
            if "csv" in r.url.params["query"].lower() or "export" in r.url.params["query"].lower() else {"macros": []})


def test_answer_similarity():
    sim, typical = answer_similarity([PW_REPLY, PW_REPLY + " Thanks!", "Totally different answer about billing"])
    assert 0.3 < sim < 1 and typical in (0, 1)
    assert answer_similarity(["one"]) == (None, 0) and answer_similarity([]) == (None, None)


async def test_deflection_finder(connect, fake):
    install_solved(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("deflection_finder", {"period": "2026-09"}))
    groups = out["groups"]
    assert [g["tickets"] for g in groups] == [5, 6]  # CSV first: 15 agent replies vs 6
    csv, pw = groups
    assert csv["agent_replies_total"] == 15 and csv["replies_avg"] == 3.0
    assert csv["answer_consistency"]["same_answer"] is False
    assert csv["matching_macros"] == ["CSV export troubleshooting"]
    assert any("Answers vary" in s for s in csv["suggestions"])
    assert any("3.0 agent replies" in s for s in csv["suggestions"])
    assert pw["one_touch_pct"] == 100.0 and pw["answer_consistency"]["same_answer"] is True
    assert pw["typical_reply"].startswith("You can reset your password from Settings")
    assert "Alice" not in pw["typical_reply"]  # sign-off stripped
    assert pw["help_center_match"]["title"] == "Resetting your password"
    assert pw["suggestions"][0].startswith("Agents give the same answer") and "Resetting your password" in \
        pw["suggestions"][1]
    assert "password" in pw["key_terms"] and "jane" not in pw["key_terms"]
    assert out["summary"]["groups_found"] == 2 and out["summary"]["tickets_in_shown_groups"] == 11
