"""Support analytics: KPIs from Zendesk's ticket metrics, reply-gap analysis computed
from actual conversations, backlog health, and SLA achievement."""

import asyncio
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal

from pydantic import Field

from ..client import ZendeskClient, ZendeskError
from ..metrics import BusinessHours, analyze_conversation, parse_ts, rate, summarize, utcnow
from ..periods import PERIOD_HELP, Period, resolve_period
from ..toolkit import Toolkit

CONCURRENCY = 5
CACHE_TTL = 600  # seconds; report tools reuse ticket data fetched in the last 10 minutes

DateField = Literal["created", "solved", "updated"]
GroupBy = Literal["none", "assignee", "group", "channel", "priority", "type", "brand", "form", "organization", "tag"]

PeriodArg = Annotated[str | None, Field(description=PERIOD_HELP)]
Start = Annotated[str | None, Field(description="Start date YYYY-MM-DD (inclusive). Overrides period.")]
End = Annotated[str | None, Field(description="End date YYYY-MM-DD (exclusive). Overrides period.")]
Filter = Annotated[str | None, Field(description="Extra Zendesk search terms to narrow scope, e.g. 'group:\"Tier 2\" "
                                    "-tags:spam brand:acme'.")]
UseBusiness = Annotated[bool, Field(description="Also compute gaps in business hours using a Zendesk schedule.")]
ScheduleId = Annotated[int | None, Field(description="Schedule for business hours (default: the account's first schedule).")]
TimeZone = Annotated[str | None, Field(description="Override the schedule's time zone with an IANA name if it isn't recognised.")]


# ----------------------------------------------------------------------------- helpers

async def _gather(fns: list[Callable[[], Awaitable[Any]]], limit: int = CONCURRENCY) -> list[Any]:
    sem = asyncio.Semaphore(limit)

    async def run(fn: Callable[[], Awaitable[Any]]) -> Any:
        async with sem:
            return await fn()

    return await asyncio.gather(*(run(f) for f in fns))


def _scope(p: Period, date_field: str, extra: str | None):
    q = f"{date_field}>={p.start.isoformat()} {date_field}<{p.end.isoformat()}"
    if extra:
        q += f" {extra.strip()}"
    return q, {**p.as_dict(), "date_field": date_field, "filter": extra}


async def _find_tickets(zd: ZendeskClient, query: str, max_tickets: int) -> tuple[list[dict], int | None]:
    """Search-export tickets matching `query` (no 1000-result cap). Returns (tickets, total_matches)."""
    query = query.replace("type:ticket", "").strip()
    count = await zd.get("search/count", query=f"type:ticket {query}")
    page = await zd.paginate("search/export", "results", params={"query": query, "filter[type]": "ticket"},
                             limit=max_tickets)
    return page["results"], count.get("count")


async def _load_with_metrics(zd: ZendeskClient, ids: list[int]) -> tuple[list[dict], dict[int, dict], dict, dict]:
    """show_many with metric_sets/users/groups sideloads; falls back to per-ticket metric calls."""
    batches = [ids[i:i + 100] for i in range(0, len(ids), 100)]
    pages = await _gather([
        (lambda b=b: zd.get("tickets/show_many", ids=",".join(map(str, b)), include="metric_sets,users,groups"))
        for b in batches
    ])
    tickets: list[dict] = []
    metrics: dict[int, dict] = {}
    users: dict[int, dict] = {}
    groups: dict[int, dict] = {}
    for p in pages:
        for t in p.get("tickets", []):
            tickets.append(t)
            if isinstance(t.get("metric_set"), dict):
                metrics[t["id"]] = t["metric_set"]
        for m in p.get("metric_sets", []) or []:
            metrics[m["ticket_id"]] = m
        users.update({u["id"]: u for u in p.get("users", []) or []})
        groups.update({g["id"]: g for g in p.get("groups", []) or []})
    missing = [t["id"] for t in tickets if t["id"] not in metrics]
    if missing:
        fetched = await _gather([(lambda i=i: zd.get(f"tickets/{i}/metrics")) for i in missing])
        for i, m in zip(missing, fetched):
            if m.get("ticket_metric"):
                metrics[i] = m["ticket_metric"]
    return tickets, metrics, users, groups


@dataclass
class Dataset:
    """Tickets in a period, with Zendesk metric sets and sideloaded users/groups."""

    period: Period
    date_field: str
    filter: str | None
    tickets: list[dict]
    metrics: dict[int, dict]
    users: dict[int, dict]
    groups: dict[int, dict]
    total: int | None
    max_tickets: int

    def scope(self) -> dict[str, Any]:
        return {**self.period.as_dict(), "date_field": self.date_field, "filter": self.filter,
                **_scope_note(len(self.tickets), self.total, self.max_tickets)}


async def load_dataset(tk: Toolkit, p: Period, date_field: str, extra: str | None, max_tickets: int) -> Dataset:
    key = ("dataset", p.start, p.end, date_field, (extra or "").strip(), max_tickets)
    hit = tk.cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    query, _ = _scope(p, date_field, extra)
    found, total = await _find_tickets(tk.zd, query, max_tickets)
    tickets, metrics, users, groups = await _load_with_metrics(tk.zd, [t["id"] for t in found]) if found \
        else ([], {}, {}, {})
    ds = Dataset(p, date_field, extra, tickets, metrics, users, groups, total, max_tickets)
    tk.cache[key] = (time.monotonic(), ds)
    return ds


async def _hours(zd: ZendeskClient, enabled: bool, schedule_id: int | None, time_zone: str | None):
    if not enabled and schedule_id is None:
        return None
    if schedule_id is None:
        sch = (await zd.get("business_hours/schedules")).get("schedules") or []
        if not sch:
            raise ZendeskError("No business-hours schedules exist on this account.")
        schedule = sch[0]
    else:
        schedule = (await zd.get(f"business_hours/schedules/{schedule_id}"))["schedule"]
    holidays = (await zd.get(f"business_hours/schedules/{schedule['id']}/holidays")).get("holidays") or []
    try:
        return BusinessHours.from_zendesk(schedule, holidays, time_zone)
    except ValueError as exc:
        raise ZendeskError(str(exc)) from exc


def _s(values) -> dict[str, Any]:
    """summarize() for output: an explicit {'n': 0} instead of a value that gets dropped."""
    return summarize(values) or {"n": 0}


def _cal_bus(m: dict | None, key: str) -> tuple[float | None, float | None]:
    v = (m or {}).get(key)
    if isinstance(v, dict):
        return v.get("calendar"), v.get("business")
    return None, None


def _group_key(t: dict, group_by: str, users: dict, groups: dict) -> list[str]:
    if group_by == "assignee":
        a = t.get("assignee_id")
        return [users.get(a, {}).get("name") or (f"user {a}" if a else "(unassigned)")]
    if group_by == "group":
        g = t.get("group_id")
        return [groups.get(g, {}).get("name") or (f"group {g}" if g else "(no group)")]
    if group_by == "channel":
        return [(t.get("via") or {}).get("channel") or "unknown"]
    if group_by == "tag":
        return t.get("tags") or ["(no tags)"]
    field = {"priority": "priority", "type": "type", "brand": "brand_id", "form": "ticket_form_id",
             "organization": "organization_id"}[group_by]
    return [str(t.get(field)) if t.get(field) is not None else f"(no {group_by})"]


def _kpis(tickets: list[dict], metrics: dict[int, dict]) -> dict[str, Any]:
    solved = [t for t in tickets if t.get("status") in ("solved", "closed")]
    m = lambda t: metrics.get(t["id"])  # noqa: E731

    def both(key: str, pool: list[dict]) -> dict[str, Any]:
        cal = [_cal_bus(m(t), key)[0] for t in pool]
        bus = [_cal_bus(m(t), key)[1] for t in pool]
        return {"calendar": _s(cal), "business": _s(bus)}

    replies = [m(t).get("replies") for t in solved if m(t) and m(t).get("replies") is not None]
    scores = Counter((t.get("satisfaction_rating") or {}).get("score") for t in tickets)
    rated = scores["good"] + scores["bad"]
    offered = rated + scores["offered"]
    return {
        "tickets": len(tickets),
        "solved_or_closed": len(solved),
        "first_reply_time_min": both("reply_time_in_minutes", tickets),
        "first_resolution_time_min": both("first_resolution_time_in_minutes", solved),
        "full_resolution_time_min": both("full_resolution_time_in_minutes", solved),
        "requester_wait_time_min": both("requester_wait_time_in_minutes", solved),
        "agent_wait_time_min": both("agent_wait_time_in_minutes", solved),
        "on_hold_time_min": both("on_hold_time_in_minutes", solved),
        "efficiency": {
            "avg_replies_per_solved_ticket": round(sum(replies) / len(replies), 2) if replies else None,
            "one_touch_resolution_pct": rate(sum(1 for r in replies if r == 1), len(replies)),
            "zero_touch_resolution_pct": rate(sum(1 for r in replies if r == 0), len(replies)),
            "reopened_pct": rate(sum(1 for t in solved if ((m(t) or {}).get("reopens") or 0) > 0), len(solved)),
            "avg_assignee_changes": _avg(m(t).get("assignee_stations") for t in solved if m(t)),
            "avg_group_changes": _avg(m(t).get("group_stations") for t in solved if m(t)),
            "no_reply_yet": sum(1 for t in tickets if m(t) and not m(t).get("replies")
                                and t.get("status") in ("new", "open")),
        },
        "csat": {
            "good": scores["good"], "bad": scores["bad"], "offered_no_response": scores["offered"],
            "score_pct": rate(scores["good"], rated), "response_rate_pct": rate(rated, offered),
        },
    }


def _avg(vals) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _compact(k: dict[str, Any]) -> dict[str, Any]:
    """One-line version of _kpis for breakdown rows."""
    frt = k["first_reply_time_min"]
    full = k["full_resolution_time_min"]
    med = lambda s: s.get("median")  # noqa: E731
    return {
        "tickets": k["tickets"], "solved": k["solved_or_closed"],
        "frt_median_min": med(frt["calendar"]), "frt_median_business_min": med(frt["business"]),
        "frt_p90_min": frt["calendar"].get("p90"),
        "full_resolution_median_min": med(full["calendar"]),
        "full_resolution_median_business_min": med(full["business"]),
        "one_touch_pct": k["efficiency"]["one_touch_resolution_pct"],
        "reopened_pct": k["efficiency"]["reopened_pct"],
        "csat_pct": k["csat"]["score_pct"], "csat_ratings": k["csat"]["good"] + k["csat"]["bad"],
    }


def _scope_note(found: int, total: int | None, max_tickets: int) -> dict[str, Any]:
    note: dict[str, Any] = {"tickets_analyzed": found, "tickets_matched": total}
    if total is not None and total > found:
        note["limited"] = f"Only the first {found} of {total} tickets were analyzed (max_tickets={max_tickets})."
    return note


def _age_bucket(hours: float) -> str:
    for limit, label in ((24, "<1d"), (72, "1-3d"), (168, "3-7d"), (720, "7-30d")):
        if hours < limit:
            return label
    return ">30d"


async def _names(zd: ZendeskClient, user_ids: set[int], group_ids: set[int]) -> tuple[dict, dict]:
    users: dict[int, dict] = {}
    ids = [i for i in user_ids if i]
    pages = await _gather([(lambda b=ids[i:i + 100]: zd.get("users/show_many", ids=",".join(map(str, b))))
                           for i in range(0, len(ids), 100)])
    for p in pages:
        users.update({u["id"]: u for u in p.get("users", [])})
    groups: dict[int, dict] = {}
    if group_ids:
        g = await zd.paginate("groups", "groups", limit=1000)
        groups = {x["id"]: x for x in g["groups"]}
    return users, groups


async def compute_backlog(zd: ZendeskClient, filter: str | None, group_by: str, stale_days: int,
                          max_tickets: int) -> dict[str, Any]:
    found, total = await _find_tickets(zd, f"status<solved {filter or ''}", max_tickets)
    users, groups = await _names(zd, {t.get("assignee_id") for t in found},
                                 {t.get("group_id") for t in found if t.get("group_id")})
    now = utcnow()
    age_h = {t["id"]: (now - parse_ts(t["created_at"])).total_seconds() / 3600 for t in found}
    idle_h = {t["id"]: (now - parse_ts(t["updated_at"])).total_seconds() / 3600 for t in found}
    order = ["<1d", "1-3d", "3-7d", "7-30d", ">30d"]
    ages = Counter(_age_bucket(age_h[t["id"]]) for t in found)
    stale = [t for t in found if idle_h[t["id"]] >= stale_days * 24]
    result: dict[str, Any] = {
        "scope": {"filter": filter, **_scope_note(len(found), total, max_tickets), "as_of": now.isoformat()},
        "unsolved_total": total if total is not None else len(found),
        "by_status": dict(Counter(t.get("status") for t in found).most_common()),
        "age_since_created": {k: ages.get(k, 0) for k in order},
        "median_age_hours": summarize(age_h.values())["median"] if found else None,
        "stale": {"days_without_update": stale_days, "count": len(stale)},
        "unassigned": sum(1 for t in found if not t.get("assignee_id")),
    }
    if group_by != "none":
        rows: dict[str, Counter] = defaultdict(Counter)
        for t in found:
            for key in _group_key(t, group_by, users, groups):
                rows[key]["total"] += 1
                rows[key][t.get("status")] += 1
                if idle_h[t["id"]] >= stale_days * 24:
                    rows[key]["stale"] += 1
        result[f"by_{group_by}"] = [{group_by: k, **v} for k, v in
                                    sorted(rows.items(), key=lambda kv: -kv[1]["total"])[:50]]
    oldest = sorted(found, key=lambda t: t["created_at"])[:10]
    result["oldest"] = [{"id": t["id"], "subject": t.get("subject"), "status": t.get("status"),
                         "age_days": round(age_h[t["id"]] / 24, 1),
                         "days_since_update": round(idle_h[t["id"]] / 24, 1),
                         "assignee": users.get(t.get("assignee_id"), {}).get("name")} for t in oldest]
    return result


async def compute_sla(zd: ZendeskClient, p: Period, lookback_days: int = 14, max_pages: int = 30) -> dict[str, Any]:
    ws = datetime.combine(p.start, datetime.min.time(), timezone.utc)
    we = datetime.combine(p.end, datetime.min.time(), timezone.utc)
    cursor = int((ws - timedelta(days=lookback_days)).timestamp())

    events: list[dict] = []
    pages, complete = 0, False
    while pages < max_pages:
        data = await zd.get("incremental/ticket_metric_events", start_time=cursor)
        pages += 1
        batch = data.get("ticket_metric_events") or []
        events.extend(batch)
        end_time = data.get("end_time")
        if data.get("end_of_stream") or not batch or not end_time or end_time >= we.timestamp():
            complete = True
            break
        if end_time <= cursor:
            break
        cursor = end_time

    inst: dict[tuple, dict[str, Any]] = {}
    for ev in events:
        key = (ev.get("ticket_id"), ev.get("metric"), ev.get("instance_id"))
        st = inst.setdefault(key, {"policy": None, "target": None, "breach_at": None, "fulfill_at": None})
        at = parse_ts(ev.get("time"))
        kind = ev.get("type")
        if kind == "apply_sla" and ev.get("sla"):
            sla = ev["sla"]
            st["policy"] = (sla.get("policy") or {}).get("title") or "(unnamed policy)"
            st["target"] = sla.get("target")
            st["business_hours"] = sla.get("business_hours")
        elif kind == "breach" and not ev.get("deleted"):
            st["breach_at"] = st["breach_at"] or at
        elif kind == "fulfill":
            st["fulfill_at"] = at

    rows: dict[tuple[str, str], Counter] = defaultdict(Counter)
    active: list[dict] = []
    for (ticket_id, metric, _), st in inst.items():
        if not st["policy"]:
            continue
        row = rows[(st["policy"], metric)]
        fulfilled = st["fulfill_at"] is not None and ws <= st["fulfill_at"] < we
        breached = st["breach_at"] is not None and ws <= st["breach_at"] < we
        if fulfilled and st["breach_at"] is None:
            row["achieved"] += 1
        elif breached or (fulfilled and st["breach_at"] is not None):
            row["breached"] += 1
        if st["breach_at"] is not None and st["fulfill_at"] is None:
            active.append({"ticket_id": ticket_id, "metric": metric, "policy": st["policy"],
                           "breached_at": st["breach_at"].isoformat()})

    table = []
    for (policy, metric), c in sorted(rows.items()):
        done = c["achieved"] + c["breached"]
        if done:
            table.append({"policy": policy, "metric": metric, "achieved": c["achieved"], "breached": c["breached"],
                          "achievement_pct": rate(c["achieved"], done)})
    tot_a = sum(r["achieved"] for r in table)
    tot_b = sum(r["breached"] for r in table)
    result = {
        "scope": {**p.as_dict(), "events_read": len(events),
                  "complete": complete},
        "overall": {"achieved": tot_a, "breached": tot_b, "achievement_pct": rate(tot_a, tot_a + tot_b)},
        "by_policy_and_metric": table,
        "currently_breached_unfulfilled": {"count": len(active),
                                           "tickets": sorted(active, key=lambda a: a["breached_at"])[:25]},
    }
    if not complete:
        result["scope"]["note"] = "Stopped at max_pages before reaching the end date; raise max_pages for full coverage."
    return result


# ----------------------------------------------------------------------------- tools

def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Support KPIs")
    async def support_kpis(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        date_field: Annotated[DateField, Field(description="Which date the range applies to.")] = "created",
        filter: Filter = None,
        group_by: Annotated[GroupBy, Field(description="Add a per-agent/group/channel/etc. breakdown.")] = "none",
        max_tickets: Annotated[int, Field(ge=1, le=20000)] = 2000,
    ) -> str:
        """Key support KPIs for a date range, from Zendesk's own ticket metrics: volume, first reply time,
        first/full resolution time, requester wait, agent wait, on-hold time (all as count/mean/median/p90,
        calendar and business minutes), one-touch resolution, reopen rate, reassignments and CSAT.
        Optional breakdown by assignee, group, channel, priority, type, brand, form, organization or tag.
        Note: first reply time is Zendesk's definition (first public agent reply). For every reply gap
        in the conversation use reply_time_report."""
        ds = await load_dataset(tk, resolve_period(period, start, end), date_field, filter, max_tickets)
        tickets, metrics, users, groups = ds.tickets, ds.metrics, ds.users, ds.groups
        result: dict[str, Any] = {"scope": {**ds.scope(), "units": "minutes"}}
        result["volume"] = {
            "by_status": dict(Counter(t.get("status") for t in tickets).most_common()),
            "by_channel": dict(Counter((t.get("via") or {}).get("channel") for t in tickets).most_common()),
            "by_priority": dict(Counter(t.get("priority") or "(none)" for t in tickets).most_common()),
        }
        result.update(_kpis(tickets, metrics))
        if group_by != "none":
            buckets: dict[str, list[dict]] = defaultdict(list)
            for t in tickets:
                for key in _group_key(t, group_by, users, groups):
                    buckets[key].append(t)
            rows = sorted(((k, _compact(_kpis(v, metrics))) for k, v in buckets.items()),
                          key=lambda kv: -kv[1]["tickets"])
            result[f"by_{group_by}"] = [{group_by: k, **v} for k, v in rows[:50]]
            if len(rows) > 50:
                result[f"by_{group_by}_note"] = f"Showing top 50 of {len(rows)} by volume."
        return tk.out(result)

    @tk.tool("read", title="Backlog report")
    async def backlog_report(
        filter: Filter = None,
        group_by: GroupBy = "group",
        stale_days: Annotated[int, Field(ge=1, description="Flag unsolved tickets not updated in this many days.")] = 7,
        max_tickets: Annotated[int, Field(ge=1, le=20000)] = 5000,
    ) -> str:
        """Snapshot of all unsolved tickets right now: counts by status, age buckets, stale tickets,
        a breakdown (default by group), and the oldest tickets."""
        return tk.out(await compute_backlog(zd, filter, group_by, stale_days, max_tickets))

    async def _conversation(ticket: dict, hours: BusinessHours | None, include_internal: bool,
                            include_automated: bool, now: datetime):
        page = await zd.paginate(f"tickets/{ticket['id']}/comments", "comments",
                                 params={"include": "users"}, limit=5000)
        roles = {u["id"]: u.get("role") for u in page.get("users", [])}
        names = {u["id"]: u.get("name") for u in page.get("users", [])}
        conv = analyze_conversation(page["comments"], roles, status=ticket.get("status"), hours=hours,
                                    include_internal=include_internal, include_automated=include_automated, now=now)
        return conv, names

    @tk.tool("read", title="Ticket reply times")
    async def ticket_reply_times(
        ticket_id: int,
        business_hours: UseBusiness = False,
        schedule_id: ScheduleId = None,
        time_zone: TimeZone = None,
        include_internal_notes: Annotated[bool, Field(description="Count internal notes as agent responses.")] = False,
        include_automated: Annotated[bool, Field(description="Count trigger/automation comments as agent responses.")] = False,
    ) -> str:
        """Every back-and-forth on one ticket: how long the customer waited for each agent reply and how long
        the customer took to respond, plus Zendesk's own metrics for comparison. The clock for an agent
        response starts at the first unanswered customer message."""
        hours = await _hours(zd, business_hours, schedule_id, time_zone)
        t = (await zd.get(f"tickets/{ticket_id}"))["ticket"]
        now = utcnow()
        conv, names = await _conversation(t, hours, include_internal_notes, include_automated, now)
        zm = (await zd.get(f"tickets/{ticket_id}/metrics")).get("ticket_metric") or {}
        for r in conv.agent_responses:
            r["agent_name"] = names.get(r["agent_id"])
        has_bus = hours is not None
        result = {
            "ticket": {k: t.get(k) for k in ("id", "subject", "status", "created_at", "updated_at")},
            "business_hours_schedule": hours.name if hours else None,
            "summary": {
                "first_agent_response": conv.first_response,
                "agent_response_min": _s(r["minutes"] for r in conv.agent_responses),
                "agent_response_business_min": _s(r["business_minutes"] for r in conv.agent_responses) if has_bus else None,
                "customer_response_min": _s(r["minutes"] for r in conv.customer_responses),
                "customer_response_business_min": _s(r["business_minutes"] for r in conv.customer_responses) if has_bus else None,
                "exchanges": len(conv.agent_responses),
                "awaiting_agent_now": conv.awaiting_agent,
                "awaiting_customer_now": conv.awaiting_customer,
                "messages": conv.counted,
            },
            "zendesk_metrics": {k: zm.get(k) for k in (
                "reply_time_in_minutes", "first_resolution_time_in_minutes", "full_resolution_time_in_minutes",
                "requester_wait_time_in_minutes", "agent_wait_time_in_minutes", "on_hold_time_in_minutes",
                "replies", "reopens", "assignee_stations", "group_stations") if k in zm},
            "agent_responses": conv.agent_responses,
            "customer_responses": conv.customer_responses,
        }
        return tk.out(result)

    @tk.tool("read", title="Reply time report")
    async def reply_time_report(
        period: Annotated[str | None, Field(description=PERIOD_HELP + " Default: last_7_days.")] = None,
        start: Start = None,
        end: End = None,
        date_field: Annotated[DateField, Field(description="Which ticket date the range applies to.")] = "updated",
        filter: Filter = None,
        ticket_ids: Annotated[list[int] | None, Field(description="Analyze exactly these tickets instead of a date range.")] = None,
        business_hours: UseBusiness = False,
        schedule_id: ScheduleId = None,
        time_zone: TimeZone = None,
        include_internal_notes: bool = False,
        include_automated: bool = False,
        max_tickets: Annotated[int, Field(ge=1, le=2000, description="Each ticket costs one API call; keep modest.")] = 200,
    ) -> str:
        """Time between customer and agent replies across many tickets, computed from the actual conversations:
        agent response time for every customer message (not just the first), customer response time, per-agent
        and per-group stats, the slowest responses, and tickets currently waiting on an agent. Only replies
        that happened inside the date range are counted when a range is used."""
        hours = await _hours(zd, business_hours, schedule_id, time_zone)
        now = utcnow()
        window: tuple[datetime, datetime] | None = None
        if ticket_ids:
            ids, total, scope = ticket_ids[:max_tickets], len(ticket_ids), {"ticket_ids": len(ticket_ids)}
        else:
            p = resolve_period(period, start, end, default="last_7_days")
            query, scope = _scope(p, date_field, filter)
            found, total = await _find_tickets(zd, query, max_tickets)
            ids = [t["id"] for t in found]
            window = (datetime.combine(p.start, datetime.min.time(), timezone.utc),
                      datetime.combine(p.end, datetime.min.time(), timezone.utc))
        tickets, _metrics, users, groups = await _load_with_metrics(zd, ids) if ids else ([], {}, {}, {})
        convs = await _gather([(lambda t=t: _conversation(t, hours, include_internal_notes, include_automated, now))
                               for t in tickets])

        def in_window(r: dict, key: str) -> bool:
            if not window:
                return True
            at = parse_ts(r[key])
            return window[0] <= at < window[1]

        agent_rows, cust_rows, first_rows, waiting = [], [], [], []
        by_agent: dict[str, list[dict]] = defaultdict(list)
        by_group: dict[str, list[dict]] = defaultdict(list)
        all_names: dict[int, str] = {}
        for t, (conv, names) in zip(tickets, convs):
            all_names.update({k: v for k, v in names.items() if v})
            gname = groups.get(t.get("group_id"), {}).get("name") or (f"group {t['group_id']}" if t.get("group_id") else "(no group)")
            for r in conv.agent_responses:
                if not in_window(r, "agent_at"):
                    continue
                row = {**r, "ticket_id": t["id"]}
                agent_rows.append(row)
                by_agent[names.get(r["agent_id"]) or f"user {r['agent_id']}"].append(row)
                by_group[gname].append(row)
            cust_rows.extend(r for r in conv.customer_responses if in_window(r, "customer_at"))
            if conv.first_response and in_window(conv.first_response, "agent_at"):
                first_rows.append(conv.first_response)
            if conv.awaiting_agent:
                waiting.append({"ticket_id": t["id"], "subject": t.get("subject"), "status": t.get("status"),
                                "group": gname, **conv.awaiting_agent})

        def stats(rows: list[dict]) -> dict[str, Any]:
            out = {"calendar_min": _s(r["minutes"] for r in rows)}
            if hours:
                out["business_min"] = _s(r["business_minutes"] for r in rows)
            return out

        def table(groups_: dict[str, list[dict]], label: str) -> list[dict]:
            rows = []
            for k, v in sorted(groups_.items(), key=lambda kv: -len(kv[1])):
                cal = summarize(r["minutes"] for r in v)
                row = {label: k, "responses": len(v), "median_min": cal["median"], "p90_min": cal["p90"],
                       "mean_min": cal["mean"]}
                if hours:
                    bus = summarize(r["business_minutes"] for r in v)
                    row.update(median_business_min=bus["median"], p90_business_min=bus["p90"])
                rows.append(row)
            return rows[:50]

        slow_key = "business_minutes" if hours else "minutes"
        slowest = sorted(agent_rows, key=lambda r: -r[slow_key])[:10]
        result = {
            "scope": {**scope, **_scope_note(len(tickets), total, max_tickets), "units": "minutes",
                      "business_hours_schedule": hours.name if hours else None,
                      "definition": "agent response = first unanswered customer message -> next agent reply; "
                                    "customer response = last agent message -> next customer message"},
            "first_response": stats(first_rows),
            "agent_response_all_replies": stats(agent_rows),
            "customer_response": stats(cust_rows),
            "by_agent": table(by_agent, "agent"),
            "by_group": table(by_group, "group"),
            "slowest_agent_responses": [{"ticket_id": r["ticket_id"], "minutes": r["minutes"],
                                         "business_minutes": r.get("business_minutes"),
                                         "agent": all_names.get(r["agent_id"]), "customer_at": r["customer_at"]}
                                        for r in slowest],
            "awaiting_agent_now": {"count": len(waiting),
                                   "longest": sorted(waiting, key=lambda w: -w["minutes"])[:10]},
        }
        return tk.out(result)

    @tk.tool("read", title="SLA report")
    async def sla_report(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        lookback_days: Annotated[int, Field(ge=0, le=90, description="Also read events this many days before start, "
                                            "so SLAs applied earlier but completed in range are attributed correctly.")] = 14,
        max_pages: Annotated[int, Field(ge=1, le=200, description="Incremental export pages (up to 1000 events each).")] = 30,
    ) -> str:
        """SLA achievement by policy and metric (first reply, next reply, requester wait, agent work time,
        periodic update...) from ticket metric events. A target counts as achieved if it was fulfilled in the
        range without a breach; breached if it breached. Also lists targets currently in breach."""
        return tk.out(await compute_sla(zd, resolve_period(period, start, end), lookback_days, max_pages))
