"""Follow-through: commitments agents made in replies (and whether they were kept), customers waiting on
us, and a one-call brief of a single ticket for escalations and handoffs."""

import re
from collections import Counter, defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import Field

from ..metrics import analyze_conversation, parse_ts, rate, summarize, utcnow
from ..privacy import Redactor, no_redact
from ..toolkit import Toolkit
from .analytics import _find_tickets, _gather
from .customers import score_message, tone
from .reporting import _tz

AGENT_ROLES = ("agent", "admin")

# ----------------------------------------------------------------------------- promise detection

_ACTIONS = (r"get back|follow(?:[\s-]?up)|update|circle back|reach out|send|check|look into|investigate|confirm|"
            r"let you know|keep you (?:posted|updated|informed)|share|provide|escalate|loop|ping|come back|"
            r"have (?:a|an|the|this|it|that|more|news|something)|report back|respond|reply|call|schedule|fix|deploy|"
            r"release|test|review|dig|talk to|speak (?:to|with)|hear back")
PROMISE = re.compile(
    rf"\b(?:I(?:'ll| will| am going to|'m going to)|we(?:'ll| will| are going to|'re going to)|let me)\s+"
    rf"(?:\w+\s+){{0,3}}?(?:{_ACTIONS})\b"
    rf"|\byou(?:'ll| will| should) (?:hear (?:back )?from (?:me|us)|get an update|receive)"
    rf"|\bexpect (?:an update|a fix|to hear|a reply|a response)", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "couple of": 2, "few": 3, "a few": 3}
_QUOTED = re.compile(r"\n(?:On .{5,200}wrote:|-{2,} ?Original Message|From: .+\nSent: ).*", re.S | re.I)


def _eod(d) -> datetime:
    return datetime.combine(d, dtime(23, 59))


def _add_business_days(d, n: int):
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def due_from_text(sentence: str, said_at: datetime, tz: ZoneInfo) -> tuple[datetime, str]:
    """When the promise falls due, and how specific it was ('dated' or 'vague')."""
    s = sentence.lower()
    local = said_at.astimezone(tz)
    day = local.date()

    def at(d) -> datetime:
        return _eod(d).replace(tzinfo=tz)

    m = re.search(r"\b(?:in|within) (\d+|an?|one|two|three|four|five|a few|a couple of|few|couple of) "
                  r"(hour|business day|working day|day|week)s?\b", s)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM.get(m.group(1), 1)
        unit = m.group(2)
        if unit == "hour":
            return local + timedelta(hours=n), "dated"
        if unit in ("business day", "working day"):
            return at(_add_business_days(day, n)), "dated"
        return at(day + timedelta(days=n * (7 if unit == "week" else 1))), "dated"
    if re.search(r"\b(?:the )?next (?:few|couple of) hours\b|\bin a bit\b|\bshortly\b", s):
        return local + timedelta(hours=4), "dated"
    if re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening)|later today|end of (?:the )?day|eod|cob|"
                 r"close of business)\b", s):
        return at(day), "dated"
    if re.search(r"\btomorrow\b", s):
        return at(day + timedelta(days=1)), "dated"
    m = re.search(r"\b(?:(next) )?(" + "|".join(WEEKDAYS) + r")\b", s)
    if m:
        target = WEEKDAYS.index(m.group(2))
        ahead = (target - day.weekday()) % 7 or 7  # the next one; 'next Friday' is read the same way
        return at(day + timedelta(days=ahead)), "dated"
    m = re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.? (\d{1,2})(?:st|nd|rd|th)?\b", s)
    if m:
        mo, dd = MONTHS.index(m.group(1)) + 1, int(m.group(2))
        try:
            d = day.replace(month=mo, day=dd)
            if d < day:
                d = d.replace(year=d.year + 1)
            return at(d), "dated"
        except ValueError:
            pass
    if re.search(r"\b(?:this week|end of (?:the )?week|eow)\b", s):
        return at(day + timedelta(days=(4 - day.weekday()) % 7)), "dated"  # this Friday (next, if said at a weekend)
    if re.search(r"\bnext week\b", s):
        return at(day - timedelta(days=day.weekday()) + timedelta(days=11)), "dated"  # Friday of next week
    if re.search(r"\b(?:asap|as soon as (?:possible|i can|we can|i have|we have)|soon)\b", s):
        return at(_add_business_days(day, 1)), "vague"
    return at(_add_business_days(day, 2)), "vague"


def find_promises(text: str) -> list[str]:
    text = _QUOTED.sub("", text or "")
    return [re.sub(r"\s+", " ", s).strip() for s in _SENT.split(text) if PROMISE.search(s or "")]


def _who(c: dict, roles: dict[int, str]) -> str | None:
    role = roles.get(c.get("author_id"))
    if role == "end-user":
        return "customer"
    if role in AGENT_ROLES and c.get("public", True):
        return "agent"
    return None


def track_promises(t: dict, comments: list[dict], roles: dict[int, str], tz: ZoneInfo, now: datetime,
                   since: datetime) -> list[dict[str, Any]]:
    out = []
    public = [(c, _who(c, roles)) for c in comments]
    for i, (c, who) in enumerate(public):
        said = parse_ts(c.get("created_at"))
        if who != "agent" or not said or said < since:
            continue
        for sentence in find_promises(c.get("plain_body") or c.get("body") or ""):
            due, kind = due_from_text(sentence, said, tz)
            follow = next((parse_ts(c2.get("created_at")) for c2, w2 in public[i + 1:] if w2 == "agent"), None)
            row: dict[str, Any] = {"ticket_id": t["id"], "said_at": said.isoformat(), "due": due.isoformat(),
                                   "deadline": kind, "promise": sentence, "author_id": c.get("author_id")}
            if follow:
                late = (follow - due).total_seconds() / 3600
                row["status"] = "kept" if late <= 0 else "late"
                row["followed_up_at"] = follow.isoformat()
                if late > 0:
                    row["late_by_hours"] = round(late, 1)
            elif t.get("status") in ("solved", "closed"):
                row["status"] = "solved_without_follow_up"
            elif due < now:
                row["status"] = "overdue"
                row["overdue_by_hours"] = round((now - due).total_seconds() / 3600, 1)
            else:
                row["status"] = "pending"
                row["due_in_hours"] = round((due - now).total_seconds() / 3600, 1)
            out.append(row)
    return out


def waiting_on_us(t: dict, comments: list[dict], roles: dict[int, str], now: datetime) -> dict[str, Any] | None:
    """The customer spoke last (publicly) and the ticket isn't solved: how long have they been waiting?"""
    if t.get("status") in ("solved", "closed"):
        return None
    last = None
    for c in comments:
        who = _who(c, roles)
        if who:
            last = (c, who)
    if not last or last[1] != "customer":
        return None
    since, first_unanswered = parse_ts(last[0]["created_at"]), last[0]
    for c in reversed(comments):  # the clock starts at the first of a run of unanswered customer messages
        who = _who(c, roles)
        if who == "agent":
            break
        if who == "customer":
            since, first_unanswered = parse_ts(c["created_at"]), c
    return {"ticket_id": t["id"], "status": t.get("status"), "waiting_since": since.isoformat(),
            "waiting_hours": round((now - since).total_seconds() / 3600, 1), "_comment": first_unanswered}


async def _comments(tk: Toolkit, ticket_id: int, limit: int = 500) -> tuple[list[dict], dict[int, dict]]:
    page = await tk.zd.paginate(f"tickets/{ticket_id}/comments", "comments", params={"include": "users"}, limit=limit)
    return page["comments"], {u["id"]: u for u in page.get("users", [])}


# ----------------------------------------------------------------------------- tools

def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Promise tracker")
    async def promise_tracker(
        days: Annotated[int, Field(ge=1, le=90, description="Look at promises made in the last N days.")] = 14,
        filter: Annotated[str | None, Field(description="Extra Zendesk search terms, e.g. 'group:\"Tier 2\"' or "
                                            "'assignee:jane@acme.com' or 'organization:123'.")] = None,
        time_zone: Annotated[str, Field(description="IANA time zone used to read 'today', 'tomorrow', 'Friday'… "
                                        "(usually your team's).")] = "UTC",
        include_pending: Annotated[bool, Field(description="Also list promises not yet due.")] = True,
        include_kept: Annotated[bool, Field(description="Also list promises that were kept on time.")] = False,
        redact: Annotated[bool, Field(description="Strip names, emails, links and long numbers from excerpts.")] = True,
        team_level: Annotated[bool, Field(description="Leave out agent names (no per-agent breakdown).")] = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 30,
        max_tickets: Annotated[int, Field(ge=1, le=2000, description="Tickets read (1 API call each), most "
                                          "recently updated first.")] = 300,
    ) -> str:
        """Catch dropped balls: finds commitments agents made in public replies ("I'll follow up tomorrow", "we'll
        have a fix by Friday", "let me check with engineering") and works out when each fell due from the wording
        — today, tomorrow, a weekday, a date, 'in 2 days', 'this/next week'; vague ones ('soon', no time) get 2
        business days. Each is kept (an agent replied before the deadline), late, overdue (no reply yet and past
        due), pending, or solved without a follow-up. Also lists customers waiting on us right now — the customer
        wrote last and the ticket isn't solved — longest wait first."""
        now = utcnow()
        tz = _tz(time_zone)
        since = now - timedelta(days=days)
        query = f"updated>={since.date().isoformat()} status<closed {filter or ''}".strip()
        found, total = await _find_tickets(zd, query, max_tickets * 3)
        found = sorted(found, key=lambda t: t.get("updated_at") or "", reverse=True)[:max_tickets]
        pages = await _gather([(lambda t=t: _comments(tk, t["id"])) for t in found])
        users: dict[int, dict] = {}
        for _, us in pages:
            users.update(us)
        red = Redactor(u.get("name") for u in users.values() if u.get("name")) if redact else no_redact
        promises, waiting = [], []
        for t, (comments, us) in zip(found, pages):
            roles = {i: u.get("role") for i, u in us.items()}
            for p in track_promises(t, comments, roles, tz, now, since):
                p["subject"] = red(t.get("subject"))
                p["promise"] = red(p["promise"])
                agent = users.get(p.pop("author_id"), {}).get("name")
                if not team_level:
                    p["agent"] = agent
                promises.append(p)
            w = waiting_on_us(t, comments, roles, now)
            if w:
                c = w.pop("_comment")
                w.update(subject=red(t.get("subject")), priority=t.get("priority"),
                         last_message=red(re.sub(r"\s+", " ", _QUOTED.sub("", c.get("plain_body") or ""))[:240]))
                waiting.append(w)

        counts = Counter(p["status"] for p in promises)
        finished = counts["kept"] + counts["late"]
        url = f"https://{tk.settings.host}/agent/tickets/"
        order = {"overdue": 0, "solved_without_follow_up": 1, "pending": 2, "late": 3, "kept": 4}
        shown = [p for p in promises if (p["status"] != "pending" or include_pending)
                 and (p["status"] != "kept" or include_kept)]
        shown.sort(key=lambda p: (order[p["status"]], -(p.get("overdue_by_hours") or 0), p.get("due_in_hours") or 0))
        for p in shown:
            p["url"] = url + str(p["ticket_id"])
        result: dict[str, Any] = {
            "scope": {"days": days, "filter": filter, "time_zone": time_zone, "tickets_read": len(found),
                      "tickets_matched": total, "as_of": now.isoformat()},
            "summary": {"promises": len(promises), **{k: counts.get(k, 0) for k in order},
                        "kept_on_time_pct": rate(counts["kept"], finished),
                        "customers_waiting_on_us": len(waiting),
                        "longest_wait_hours": max((w["waiting_hours"] for w in waiting), default=None)},
            "promises": shown[:limit],
            "waiting_on_us": sorted(waiting, key=lambda w: -w["waiting_hours"])[:limit],
            "how_it_works": "A promise is a sentence in a public agent reply that commits to a follow-up. Its deadline "
                            "comes from the wording in time_zone; 'vague' deadlines default to 2 business days "
                            "(1 for 'soon'/'ASAP'). Any later public agent reply on the ticket counts as the "
                            "follow-up. Read the promise text: phrase matching can misfire.",
        }
        if not team_level and promises:
            per: dict[str, Counter] = defaultdict(Counter)
            for p in promises:
                per[p.get("agent") or "(unknown)"][p["status"]] += 1
            result["by_agent"] = sorted(
                ({"agent": a, "promises": sum(c.values()), "overdue": c["overdue"], "late": c["late"],
                  "kept_on_time_pct": rate(c["kept"], c["kept"] + c["late"])} for a, c in per.items()),
                key=lambda r: (-r["overdue"], -r["promises"]))
        if total and total > len(found):
            result["scope"]["note"] = f"Read the {len(found)} most recently updated of {total} tickets (max_tickets)."
        return tk.out(result)
