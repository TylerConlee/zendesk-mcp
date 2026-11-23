"""Follow-through: commitments agents made in replies (and whether they were kept), customers waiting on
us, and a one-call brief of a single ticket for escalations and handoffs."""

import asyncio
import re
from collections import Counter, defaultdict
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import Field

from ..audits import load_audits, parse_audits
from ..client import ZendeskError
from ..metrics import analyze_conversation, parse_ts, rate, summarize, utcnow
from ..privacy import Redactor, no_redact
from ..toolkit import Toolkit
from .analytics import _find_tickets, _gather
from .customers import score_message, tone
from .reporting import _tz, field_catalog

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

    @tk.tool("read", title="Ticket brief")
    async def ticket_brief(
        ticket_id: int,
        excerpt_chars: Annotated[int, Field(ge=80, le=2000, description="Characters kept from each message.")] = 300,
        include_internal: Annotated[bool, Field(description="Include internal notes in the timeline (they often hold "
                                                "what's been tried).")] = True,
        redact: Annotated[bool, Field(description="Strip people's names, emails, links and long numbers from "
                                      "text (the organization name stays).")] = True,
        timeline_limit: Annotated[int, Field(ge=10, le=500, description="Most recent timeline entries kept.")] = 60,
    ) -> str:
        """Everything about one ticket for an escalation or handoff, in one call: the headline (status, priority,
        age, replies, reopens, who it's waiting on and for how long), a merged timeline of messages, internal
        notes, status/group/assignee/priority changes and macros (with who or which trigger made each change),
        how long the customer waited at each step, the customer's tone message by message and whether it's
        getting worse, promises made and kept, custom field values, and related tickets — the problem it's
        linked to or its incidents, and the requester's and organization's other open tickets. Ready to turn
        into a Slack post for engineering or a handover note."""
        now = utcnow()
        try:
            tdata = await zd.get(f"tickets/{ticket_id}", include="users,groups,organizations")
        except ZendeskError as exc:
            raise ZendeskError(f"Ticket {ticket_id}: {exc}") from None
        t = tdata["ticket"]
        jobs: dict[str, Any] = {
            "comments": _comments(tk, ticket_id, limit=2000),
            "audits": load_audits(zd, ticket_id),
            "metrics": zd.get(f"tickets/{ticket_id}/metrics"),
            "catalog": field_catalog(tk),
        }
        if t.get("requester_id"):
            jobs["requester_open"] = _find_tickets(zd, f"requester:{t['requester_id']} status<solved", 50)
        if t.get("organization_id"):
            jobs["org_open"] = _find_tickets(zd, f"organization:{t['organization_id']} status<solved", 100)
        if t.get("problem_id"):
            jobs["problem"] = zd.get(f"tickets/{t['problem_id']}")
        if t.get("type") == "problem":
            jobs["incidents"] = zd.paginate(f"tickets/{ticket_id}/incidents", "tickets", limit=200)

        async def safe(c):
            try:
                return await c
            except ZendeskError:
                return None
        done = dict(zip(jobs, await asyncio.gather(*(safe(j) for j in jobs.values()))))
        comments, cusers = done["comments"] or ([], {})
        users = {u["id"]: u for u in tdata.get("users") or []} | cusers
        groups = {g["id"]: g.get("name") for g in tdata.get("groups") or []}
        orgs = {o["id"]: o.get("name") for o in tdata.get("organizations") or []}
        roles = {i: u.get("role") for i, u in users.items()}
        red = Redactor(u.get("name") for u in users.values() if u.get("name")) if redact else no_redact
        name = lambda uid: users.get(uid, {}).get("name") or (f"user {uid}" if uid else None)  # noqa: E731
        m = ((done["metrics"] or {}).get("ticket_metric")) or {}
        parsed = parse_audits(done["audits"] or [])

        def text_of(c: dict) -> str:
            return re.sub(r"\s+", " ", _QUOTED.sub("", c.get("plain_body") or c.get("body") or "")).strip()

        # ---- timeline
        timeline: list[dict[str, Any]] = []
        tones = []
        for c in comments:
            role = roles.get(c.get("author_id"))
            public = c.get("public", True)
            if not public and not include_internal:
                continue
            who = "customer" if role == "end-user" else "agent" if role in AGENT_ROLES else "system"
            entry: dict[str, Any] = {"at": c.get("created_at"),
                                     "event": "customer message" if who == "customer" else
                                     ("agent reply" if public else "internal note") if who == "agent" else "message",
                                     "text": red(text_of(c)[:excerpt_chars])}
            if who == "agent" or not redact:
                entry["by"] = name(c.get("author_id")) if who == "agent" else red(name(c.get("author_id")))
            if who == "customer" and public:
                s = score_message(c.get("plain_body") or c.get("body") or "")
                entry["tone"] = tone(s["negative"], s["positive"])
                tones.append((entry["tone"], s["negative"] - s["positive"]))
            if c.get("attachments"):
                entry["attachments"] = len(c["attachments"])
            timeline.append(entry)
        labels = {"group_id": lambda v: groups.get(v) or (f"group {v}" if v else "(none)"),
                  "assignee_id": lambda v: name(v) or "(unassigned)"}
        for ch in parsed["changes"]:
            f = ch["field"]
            show = labels.get(f, lambda v: v)
            by = ch["by"]
            who = by.get("title") and f"{by['type']} '{by['title']}'" or name(by.get("id")) or by["type"]
            timeline.append({"at": ch["at"], "event": f"{f.replace('_id', '')} changed",
                             "change": f"{show(ch['from'])} → {show(ch['to'])}", "by": who})
        for mc in parsed["macros"]:
            timeline.append({"at": mc["at"], "event": "macro applied", "change": mc.get("title"),
                             "by": name(mc["by"].get("id"))})
        timeline.sort(key=lambda e: e.get("at") or "")
        trimmed = len(timeline) - timeline_limit

        # ---- waits, tone trend, promises
        conv = analyze_conversation(comments, roles, status=t.get("status"), now=now)
        waits = [g["minutes"] for g in conv.agent_responses]
        state: dict[str, Any] = {}
        w = waiting_on_us(t, comments, roles, now)
        if w:
            state = {"waiting_on": "us", "since": w["waiting_since"], "hours": w["waiting_hours"]}
        elif t.get("status") in ("pending", "hold"):
            last = next((c for c in reversed(comments) if roles.get(c.get("author_id")) in AGENT_ROLES
                         and c.get("public", True)), None)
            since = parse_ts(last["created_at"]) if last else parse_ts(t.get("updated_at"))
            state = {"waiting_on": "customer" if t["status"] == "pending" else "on hold",
                     "since": since.isoformat() if since else None,
                     "hours": round((now - since).total_seconds() / 3600, 1) if since else None}
        trend = None
        if len(tones) >= 2:
            first, last_ = tones[0][1], tones[-1][1]
            trend = "worsening" if last_ - first >= 2 else "improving" if first - last_ >= 2 else "steady"
        promises = track_promises(t, comments, roles, ZoneInfo("UTC"), now, datetime.min.replace(tzinfo=timezone.utc))
        for p in promises:
            p["promise"] = red(p["promise"])
            p["by"] = name(p.pop("author_id"))

        created = parse_ts(t.get("created_at"))
        age_days = round((now - created).total_seconds() / 86400, 1) if created else None
        catalog = done["catalog"] or {}
        fields = {}
        for cf in t.get("custom_fields") or []:
            f = catalog.get(cf.get("id"))
            if f and cf.get("value") not in (None, "", [], False):
                v = cf["value"]
                fields[f["title"]] = [f["options"].get(x, x) for x in v] if isinstance(v, list) else f["options"].get(v, v)
        group_moves = sum(1 for c in parsed["changes"] if c["field"] == "group_id")
        assignee_moves = sum(1 for c in parsed["changes"] if c["field"] == "assignee_id")
        overdue = sum(1 for p in promises if p["status"] == "overdue")
        bits = [t.get("priority") or "no priority", t.get("status"), f"open {age_days} days" if t.get("status") not in
                ("solved", "closed") else f"{age_days} days old", f"{m.get('replies') or 0} agent replies"]
        if m.get("reopens"):
            bits.append(f"reopened {m['reopens']}×")
        if group_moves:
            bits.append(f"{group_moves} group transfer(s)")
        if state.get("waiting_on") == "us":
            bits.append(f"customer waiting {state['hours']}h")
        if overdue:
            bits.append(f"{overdue} overdue promise(s)")
        if trend == "worsening":
            bits.append("customer tone worsening")

        def brief(x: dict) -> dict[str, Any]:
            return {"id": x["id"], "subject": red(x.get("subject")), "status": x.get("status"),
                    "priority": x.get("priority"), "created_at": x.get("created_at")}

        related: dict[str, Any] = {}
        if done.get("problem"):
            related["problem"] = brief(done["problem"]["ticket"])
        if done.get("incidents"):
            inc = done["incidents"]["tickets"]
            related["incidents"] = {"count": len(inc), "open": sum(1 for x in inc if x.get("status") not in
                                                                   ("solved", "closed")),
                                    "organizations": len({x.get("organization_id") for x in inc if x.get("organization_id")}),
                                    "latest": [brief(x) for x in sorted(inc, key=lambda x: x.get("created_at") or "",
                                                                         reverse=True)[:5]]}
        if done.get("requester_open"):
            other = [x for x in done["requester_open"][0] if x["id"] != ticket_id]
            if other:
                related["requester_other_open"] = [brief(x) for x in other[:10]]
        if done.get("org_open"):
            other = [x for x in done["org_open"][0] if x["id"] != ticket_id]
            related["organization_other_open"] = {"count": len(other), "tickets": [brief(x) for x in other[:10]]}

        result: dict[str, Any] = {
            "headline": f"#{ticket_id} {red(t.get('subject'))} — " + ", ".join(b for b in bits if b),
            "ticket": {k: v for k, v in {
                "id": ticket_id, "subject": red(t.get("subject")), "status": t.get("status"),
                "priority": t.get("priority"), "type": t.get("type"), "channel": (t.get("via") or {}).get("channel"),
                "created_at": t.get("created_at"), "updated_at": t.get("updated_at"), "age_days": age_days,
                "organization": orgs.get(t.get("organization_id")),
                "requester": None if redact else name(t.get("requester_id")),
                "assignee": name(t.get("assignee_id")), "group": groups.get(t.get("group_id")),
                "tags": t.get("tags") or None, "fields": fields or None,
                "url": f"https://{tk.settings.host}/agent/tickets/{ticket_id}"}.items() if v is not None},
            "state_now": state or {"waiting_on": None, "note": "solved/closed" if t.get("status") in
                                   ("solved", "closed") else "agent replied last"},
            "numbers": {
                "agent_replies": m.get("replies"), "reopens": m.get("reopens"),
                "group_transfers": group_moves, "assignee_changes": assignee_moves,
                "customer_messages": conv.counted["customer_messages"],
                "internal_notes": sum(1 for c in comments if not c.get("public", True)),
                "first_reply_min": ((m.get("reply_time_in_minutes") or {}).get("calendar")),
                "customer_wait_each_time_min": summarize(waits) or {"n": 0},
                "total_customer_wait_hours": round(sum(waits) / 60, 1) if waits else 0,
            },
            "customer_tone": {"trend": trend, "messages": [x[0] for x in tones],
                              "note": "Keyword heuristic on the customer's own words; read the timeline."}
            if tones else None,
            "promises": promises or None,
            "related": related or None,
            "timeline": timeline[-timeline_limit:],
        }
        if trimmed > 0:
            result["timeline_note"] = f"Oldest {trimmed} entries left out (timeline_limit)."
        return tk.out({k: v for k, v in result.items() if v is not None})
