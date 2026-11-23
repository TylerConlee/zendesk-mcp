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
