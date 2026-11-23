"""promise_tracker and ticket_brief. 'Now' is Wed 2026-10-14 15:00Z (08:00 in Los Angeles)."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import data
from zendesk_mcp.tools.followups import due_from_text, find_promises

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
LA = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    for mod in ("periods", "tools.analytics", "tools.reporting", "tools.customers", "tools.followups"):
        monkeypatch.setattr(f"zendesk_mcp.{mod}.utcnow", lambda: NOW)


USERS = [{"id": 7, "name": "Jane Doe", "role": "end-user"}, {"id": 8, "name": "Alice Smith", "role": "agent"},
         {"id": 9, "name": "Bob Jones", "role": "agent"}]


def C(cid, author, body, at, public=True):
    return {"id": cid, "author_id": author, "plain_body": body, "public": public, "created_at": at, "via": {}}


def T(tid, status, updated, subject=None, priority="normal"):
    return {"id": tid, "subject": subject or f"Ticket {tid}", "status": status, "updated_at": updated,
            "created_at": "2026-10-01T00:00:00Z", "priority": priority, "requester_id": 7, "assignee_id": 8,
            "organization_id": 50, "group_id": 3}


TICKETS = [
    T(1, "open", "2026-10-13T18:00:00Z", subject="Export failing for Jane Doe"),
    T(2, "pending", "2026-10-12T17:00:00Z"),
    T(3, "solved", "2026-10-09T17:00:00Z"),
    T(4, "new", "2026-10-14T12:00:00Z", priority="urgent"),
    T(5, "solved", "2026-10-10T17:00:00Z"),
]
COMMENTS = {
    1: [C(1, 7, "Export fails.", "2026-10-12T16:00:00Z"),
        C(2, 8, "Thanks Jane. I'll follow up tomorrow with logs.", "2026-10-12T17:00:00Z"),  # Mon 10:00 PDT
        C(3, 9, "I'll look into the logs myself", "2026-10-12T17:30:00Z", public=False),  # internal: ignored
        C(4, 7, "Any update? Call me on +1 503 555 0199", "2026-10-13T18:00:00Z")],
    2: [C(5, 7, "Bug in API.", "2026-10-12T16:00:00Z"),
        C(6, 9, "Confirmed. We'll have a fix by Friday.", "2026-10-12T17:00:00Z")],
    3: [C(7, 8, "Old: I'll check tomorrow.", "2026-09-01T17:00:00Z"),  # before the window
        C(8, 7, "Still broken", "2026-10-05T16:00:00Z"),
        C(9, 8, "Let me check with engineering and get back to you.", "2026-10-05T17:00:00Z"),  # Mon -> due Wed
        C(10, 8, "Engineering fixed it.", "2026-10-09T17:00:00Z")],
    4: [C(11, 7, "Everything is down!", "2026-10-14T10:00:00Z"), C(12, 7, "Hello??", "2026-10-14T12:00:00Z")],
    5: [C(13, 7, "Question", "2026-10-10T16:00:00Z"), C(14, 8, "I'll update you today.", "2026-10-10T17:00:00Z")],
}


def install(fake):
    fake.on("GET", "/api/v2/search/count", {"count": len(TICKETS)})
    fake.on("GET", "/api/v2/search/export", {"results": TICKETS, "meta": {"has_more": False}})
    for tid, cs in COMMENTS.items():
        fake.on("GET", f"/api/v2/tickets/{tid}/comments", {"comments": cs, "users": USERS, "meta": {"has_more": False}})


# ----------------------------------------------------------------------------- promise detection

@pytest.mark.parametrize("sentence,due,kind", [
    ("I'll follow up tomorrow.", "2026-10-13T23:59", "dated"),
    ("We'll have a fix by Friday", "2026-10-16T23:59", "dated"),
    ("Let me check with engineering and get back to you.", "2026-10-14T23:59", "vague"),  # +2 business days
    ("I will update you in 2 business days", "2026-10-14T23:59", "dated"),
    ("You'll hear from us next week", "2026-10-23T23:59", "dated"),
    ("I'll send it over on Oct 20th", "2026-10-20T23:59", "dated"),
    ("We will get back to you within a few hours", "2026-10-12T13:00", "dated"),
    ("I'll circle back this week", "2026-10-16T23:59", "dated"),
    ("I'll check ASAP", "2026-10-13T23:59", "vague"),
])
def test_due_dates(sentence, due, kind):
    said = datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc)  # Monday 10:00 in Los Angeles
    d, k = due_from_text(sentence, said, LA)
    assert d.strftime("%Y-%m-%dT%H:%M") == due and k == kind


def test_find_promises():
    assert find_promises("Thanks. I'll follow up tomorrow with logs.\nJust info.\n\nOn Mon, X wrote:\n> I'll check") \
        == ["I'll follow up tomorrow with logs."]
    assert find_promises("Let me know if it works. We will be happy to help.") == []


async def test_promise_tracker(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("promise_tracker", {"time_zone": "America/Los_Angeles", "include_kept": True}))
    s = out["summary"]
    assert (s["promises"], s["overdue"], s["pending"], s["late"], s["kept"], s["solved_without_follow_up"]) == \
        (4, 1, 1, 1, 0, 1)
    assert s["kept_on_time_pct"] == 0.0 and s["customers_waiting_on_us"] == 2 and s["longest_wait_hours"] == 21.0
    first = out["promises"][0]
    # due Tue 23:59 PDT = Wed 06:59Z; now is 15:00Z
    assert (first["ticket_id"], first["status"], first["overdue_by_hours"]) == (1, "overdue", 8.0)
    assert first["agent"] == "Alice Smith" and first["promise"] == "I'll follow up tomorrow with logs."
    assert first["subject"] == "Export failing for [name]"
    assert [p["status"] for p in out["promises"]] == ["overdue", "solved_without_follow_up", "pending", "late"]
    late = out["promises"][3]
    assert late["deadline"] == "vague" and late["late_by_hours"] == 34.0  # due Wed 10/7 23:59 PDT (Thu 06:59Z), reply Fri 17:00Z
    w = out["waiting_on_us"]
    assert [(x["ticket_id"], x["waiting_hours"]) for x in w] == [(1, 21.0), (4, 5.0)]  # 4: clock from first message
    assert "[number]" in w[0]["last_message"]
    assert {r["agent"]: r["overdue"] for r in out["by_agent"]}["Alice Smith"] == 1


async def test_promise_tracker_team_level(connect, fake):
    install(fake)
    async with connect() as cl:
        out = data(await cl.call_tool("promise_tracker", {"team_level": True, "include_pending": False}))
    assert "by_agent" not in out and all("agent" not in p for p in out["promises"])
    assert all(p["status"] != "pending" for p in out["promises"])
