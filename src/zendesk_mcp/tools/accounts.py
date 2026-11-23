"""Account-level views across customers: a health ranking of every organization (with renewal and
segment filters) and a chronological history of one customer's support relationship."""

import asyncio
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from pydantic import Field

from ..client import ZendeskClient, ZendeskError
from ..metrics import parse_ts, rate, summarize, utcnow
from ..periods import PERIOD_HELP, resolve_period
from ..toolkit import Toolkit
from .analytics import _compact, _find_tickets, _gather, _kpis, load_dataset
from .customers import _only_org, _org_context, resolve_org
from .reporting import _redactor_for, field_catalog

# Org fields worth showing next to a health score when the caller doesn't name any.
INTERESTING_FIELD = re.compile(r"renew|arr\b|mrr|plan|tier|segment|csm|success.?manager|health|strategic|"
                               r"account.?status|contract", re.I)
OPEN = ("new", "open", "pending", "hold")


# ----------------------------------------------------------------------------- org fields

async def org_field_defs(zd: ZendeskClient) -> list[dict]:
    try:
        return (await zd.paginate("organization_fields", "organization_fields", limit=500))["organization_fields"]
    except ZendeskError:
        return []


def field_resolver(defs: list[dict]):
    """Returns (label_of(key), display(key, value)) using the account's organization field definitions."""
    titles = {f.get("key"): f.get("title") or f.get("key") for f in defs}
    options = {f.get("key"): {o.get("value"): o.get("name") for o in f.get("custom_field_options") or []}
               for f in defs}

    def display(key: str, value: Any) -> Any:
        return options.get(key, {}).get(value, value)

    return (lambda k: titles.get(k, k)), display


def _match_key(defs: list[dict], name: str) -> str | None:
    """Field key from a key or title (case-insensitive)."""
    n = name.strip().casefold()
    for f in defs:
        if n in ((f.get("key") or "").casefold(), (f.get("title") or "").casefold()):
            return f.get("key")
    return None


def _renewal_key(defs: list[dict]) -> str | None:
    dated = [f for f in defs if f.get("type") == "date"]
    for f in dated:
        if re.search(r"renew", f"{f.get('key')} {f.get('title')}", re.I):
            return f.get("key")
    return None


async def _all_orgs(zd: ZendeskClient, max_orgs: int) -> tuple[list[dict], bool]:
    page = await zd.paginate("organizations", "organizations", limit=max_orgs)
    return page["organizations"], page["has_more"]


async def _orgs_by_id(zd: ZendeskClient, ids: list[int]) -> dict[int, dict]:
    pages = await _gather([(lambda b=ids[i:i + 100]: zd.get("organizations/show_many", ids=",".join(map(str, b))))
                           for i in range(0, len(ids), 100)])
    return {o["id"]: o for p in pages for o in p.get("organizations", [])}


# ----------------------------------------------------------------------------- scoring

def health(m: dict[str, Any], baseline_frt: float | None) -> tuple[int, list[str]]:
    """Start at 100 and subtract for each warning sign. Every deduction comes with its reason."""
    score, why = 100.0, []

    def hit(points: float, reason: str):
        nonlocal score
        score -= points
        why.append(reason)

    rated = m["csat_good"] + m["csat_bad"]
    if rated >= 3 and m["csat_pct"] is not None and m["csat_pct"] < 80:
        hit(min((80 - m["csat_pct"]) / 2, 20), f"CSAT {m['csat_pct']}% ({rated} ratings)")
    if m["csat_bad"]:
        hit(min(5 * m["csat_bad"], 15), f"{m['csat_bad']} bad rating(s)")
    if m["urgent_high_open"]:
        hit(min(8 * m["urgent_high_open"], 16), f"{m['urgent_high_open']} urgent/high ticket(s) open")
    if m["stale_open"]:
        hit(min(4 * m["stale_open"], 12), f"{m['stale_open']} open ticket(s) idle 7+ days")
    ch = m.get("change_pct")
    if ch is not None and ch >= 50 and m["tickets"] >= 5:
        hit(10, f"volume up {ch}%")
    if m["solved"] >= 5 and (m["reopened_pct"] or 0) >= 15:
        hit(10, f"{m['reopened_pct']}% reopened")
    if baseline_frt and m["tickets"] >= 3 and m["frt_median_min"] and m["frt_median_min"] >= 1.5 * baseline_frt:
        hit(10, f"first reply median {m['frt_median_min']} min vs {baseline_frt} for all customers")
    if m["tickets"] >= 3 and (m.get("negative_sentiment_pct") or 0) >= 30:
        hit(10, f"negative sentiment on {m['negative_sentiment_pct']}% of tickets")
    return max(0, round(score)), why


def band(score: int | None) -> str:
    if score is None:
        return "quiet"
    return "healthy" if score >= 80 else "watch" if score >= 60 else "at_risk"


def _negative_sentiment(t: dict, sentiment_fields: set[int]) -> bool:
    for cf in t.get("custom_fields") or []:
        if cf.get("id") in sentiment_fields and re.search(r"neg|frustrat|angry|upset", str(cf.get("value") or ""), re.I):
            return True
    return any("sentiment" in g and re.search(r"neg|frustrat|angry", g) for g in (t.get("tags") or []))


# ----------------------------------------------------------------------------- tools

def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Account health (all customers)")
    async def account_health(
        period: Annotated[str | None, Field(description=PERIOD_HELP + " Default: last_90_days.")] = None,
        start: Annotated[str | None, Field(description="Start date YYYY-MM-DD (inclusive). Overrides period.")] = None,
        end: Annotated[str | None, Field(description="End date YYYY-MM-DD (exclusive). Overrides period.")] = None,
        renewing_within_days: Annotated[int | None, Field(ge=1, le=730, description=(
            "Only customers whose renewal date is within this many days (needs a date-type organization field "
            "with 'renew' in its name, or renewal_field)."))] = None,
        renewal_field: Annotated[str | None, Field(description="Organization field (key or title) holding the "
                                                   "renewal date, if auto-detection picks the wrong one.")] = None,
        org_tags: Annotated[list[str] | None, Field(description="Only organizations with any of these tags.")] = None,
        field_filters: Annotated[dict[str, str] | None, Field(description=(
            "Only organizations whose fields match, e.g. {'Plan type': 'Enterprise', 'strategic_account': 'true'}. "
            "Keys are field keys or titles; values match the option name or stored value, case-insensitive."))] = None,
        show_fields: Annotated[list[str] | None, Field(description="Organization fields to show per customer "
                                                       "(default: renewal/ARR/plan/tier/CSM-like fields).")] = None,
        min_tickets: Annotated[int, Field(ge=0, description="Hide customers with fewer tickets in the period.")] = 0,
        include_quiet: Annotated[bool, Field(description="When filtering by renewal, tags or fields, also list "
                                             "matching customers with no tickets in the period.")] = True,
        limit: Annotated[int, Field(ge=1, le=500)] = 25,
        anonymize_orgs: Annotated[bool, Field(description="Show customers as 'Customer 1, 2…'.")] = False,
        max_tickets: Annotated[int, Field(ge=1, le=50000)] = 20000,
        max_orgs: Annotated[int, Field(ge=1, le=50000, description="Organizations read when filtering.")] = 5000,
    ) -> str:
        """Rank every customer (organization) by support health, lowest first, with the reasons: CSAT and bad
        ratings, open urgent/high tickets, tickets idle 7+ days, volume spikes, reopen rate, first replies slower
        than for other customers, and negative AI sentiment. Shows renewal date, plan, ARR, CSM-style organization
        fields alongside. Filter to customers renewing soon, a segment (tags or field values), or both — e.g.
        "which enterprise customers renewing in the next 90 days look unhealthy?". Bands: healthy ≥80, watch
        60–79, at_risk <60, quiet = no tickets in the period. Drill into one with customer_profile."""
        p = resolve_period(period, start, end, default="last_90_days")
        now = utcnow()
        filtering = bool(renewing_within_days or org_tags or field_filters)
        jobs: dict[str, Any] = {
            "ds": load_dataset(tk, p, "created", None, max_tickets),
            "prev": load_dataset(tk, p.previous(), "created", None, max_tickets),
            "open": _find_tickets(zd, "status<solved", 20000),
            "defs": org_field_defs(zd),
        }
        if filtering:
            jobs["orgs"] = _all_orgs(zd, max_orgs)
        done = dict(zip(jobs, await asyncio.gather(*jobs.values())))
        ds, prev, defs = done["ds"], done["prev"], done["defs"]
        open_tickets, _ = done["open"]
        label_of, display = field_resolver(defs)
        notes: list[str] = []
        if ds.total is not None and ds.total > len(ds.tickets):
            notes.append(f"Analyzed {len(ds.tickets)} of {ds.total} tickets in the period (max_tickets).")

        # ---- which organizations
        rkey = (_match_key(defs, renewal_field) if renewal_field else None) or _renewal_key(defs)
        if renewal_field and not _match_key(defs, renewal_field):
            raise ZendeskError(f"No organization field called {renewal_field!r}.")
        if renewing_within_days and not rkey:
            raise ZendeskError("No date-type organization field with 'renew' in its name; pass renewal_field.")
        by_org: dict[int, list[dict]] = defaultdict(list)
        for t in ds.tickets:
            if t.get("organization_id"):
                by_org[t["organization_id"]].append(t)
        prev_count = Counter(t.get("organization_id") for t in prev.tickets if t.get("organization_id"))
        open_by_org: dict[int, list[dict]] = defaultdict(list)
        for t in open_tickets:
            if t.get("organization_id"):
                open_by_org[t["organization_id"]].append(t)

        if filtering:
            orgs_list, more = done["orgs"]
            if more:
                notes.append(f"Only the first {len(orgs_list)} organizations were checked against the filters "
                             "(max_orgs).")
            orgs = {o["id"]: o for o in orgs_list}
            wanted_tags = {g.casefold() for g in org_tags or []}
            ff = {}
            for name, val in (field_filters or {}).items():
                key = _match_key(defs, name) or name
                ff[key] = str(val).casefold()
            horizon = now.date() + timedelta(days=renewing_within_days or 0)

            def keep(o: dict) -> bool:
                if wanted_tags and not wanted_tags & {g.casefold() for g in o.get("tags") or []}:
                    return False
                fields = o.get("organization_fields") or {}
                for key, val in ff.items():
                    raw = fields.get(key)
                    if val not in (str(raw).casefold(), str(display(key, raw)).casefold()):
                        return False
                if renewing_within_days:
                    try:
                        d = date.fromisoformat(str(fields.get(rkey))[:10])
                    except ValueError:
                        return False
                    if not (now.date() <= d <= horizon):
                        return False
                return True

            candidates = [oid for oid, o in orgs.items() if keep(o)]
            if not include_quiet:
                candidates = [oid for oid in candidates if by_org.get(oid) or open_by_org.get(oid)]
        else:
            candidates = sorted(set(by_org) | set(open_by_org))
            orgs = await _orgs_by_id(zd, candidates)

        # ---- per-org metrics
        all_frt = _compact(_kpis(ds.tickets, ds.metrics))["frt_median_min"]
        catalog = await field_catalog(tk)
        sentiment_fields = {i for i, f in catalog.items() if "sentiment" in (f.get("title") or "").lower()}
        shown = [(_match_key(defs, f) or f) for f in show_fields] if show_fields else \
            [f.get("key") for f in defs if INTERESTING_FIELD.search(f"{f.get('key')} {f.get('title')}")][:6]
        if rkey and rkey not in shown:
            shown.insert(0, rkey)

        rows = []
        for oid in candidates:
            ts = by_org.get(oid, [])
            if len(ts) < min_tickets:
                continue
            o = orgs.get(oid, {})
            k = _compact(_kpis(ts, ds.metrics)) if ts else None
            op = open_by_org.get(oid, [])
            idle = [t for t in op if (now - parse_ts(t["updated_at"])).days >= 7] if op else []
            m = {
                "tickets": len(ts), "previous_tickets": prev_count.get(oid, 0),
                "change_pct": rate(len(ts) - prev_count[oid], prev_count[oid]) if prev_count.get(oid) else None,
                "solved": k["solved"] if k else 0,
                "csat_good": sum(1 for t in ts if (t.get("satisfaction_rating") or {}).get("score") == "good"),
                "csat_bad": sum(1 for t in ts if (t.get("satisfaction_rating") or {}).get("score") == "bad"),
                "csat_pct": k["csat_pct"] if k else None,
                "frt_median_min": k["frt_median_min"] if k else None,
                "reopened_pct": k["reopened_pct"] if k else None,
                "open_now": len(op), "urgent_high_open": sum(1 for t in op if t.get("priority") in ("urgent", "high")),
                "stale_open": len(idle),
                "negative_sentiment_pct": rate(sum(1 for t in ts if _negative_sentiment(t, sentiment_fields)), len(ts))
                if ts else None,
            }
            if ts or op:
                score, why = health(m, all_frt)
            else:
                score, why = None, ["no tickets in the period and none open"]
            fields = o.get("organization_fields") or {}
            row: dict[str, Any] = {"organization": o.get("name") or f"org {oid}", "id": oid, "score": score,
                                   "band": band(score), "reasons": why, **m}
            vals = {label_of(key): display(key, fields.get(key)) for key in shown
                    if fields.get(key) not in (None, "", [], False)}
            if vals:
                row["fields"] = vals
            if rkey and fields.get(rkey):
                try:
                    row["days_to_renewal"] = (date.fromisoformat(str(fields[rkey])[:10]) - now.date()).days
                except ValueError:
                    pass
            last = max((t.get("created_at") for t in ts + op if t.get("created_at")), default=None)
            if last:
                row["last_ticket_at"] = last
            rows.append({k2: v for k2, v in row.items() if v is not None or k2 == "score"})

        rows.sort(key=lambda r: (r["score"] if r["score"] is not None else 101, -r["tickets"]))
        if anonymize_orgs:
            for i, r in enumerate(rows, 1):
                r["organization"] = f"Customer {i}"
                r.pop("id", None)
        bands = Counter(r["band"] for r in rows)
        result = {
            "scope": {**p.as_dict(), "previous_period": p.previous().as_dict(), "generated_at": now.isoformat(),
                      "customers_scored": len(rows), "filters": {"renewing_within_days": renewing_within_days,
                                                                 "renewal_field": label_of(rkey) if rkey else None,
                                                                 "org_tags": org_tags, "field_filters": field_filters,
                                                                 "min_tickets": min_tickets} if filtering or min_tickets
                      else None},
            "summary": {b: bands.get(b, 0) for b in ("at_risk", "watch", "healthy", "quiet")},
            "all_customer_frt_median_min": all_frt,
            "customers": rows[:limit],
            "how_scored": "Score starts at 100. Deductions: CSAT under 80% with 3+ ratings (up to 20), 5 per bad "
                          "rating (up to 15), 8 per open urgent/high ticket (up to 16), 4 per open ticket idle 7+ days "
                          "(up to 12), 10 for volume up 50%+ (5+ tickets), 10 for 15%+ reopened (5+ solved), 10 for "
                          "first-reply median 1.5× the all-customer median (3+ tickets), 10 for negative AI sentiment "
                          "on 30%+ of tickets. Bands: healthy 80+, watch 60–79, at_risk under 60.",
        }
        if len(rows) > limit:
            result["more"] = f"{len(rows) - limit} more customers not shown (raise limit)."
        if notes:
            result["data_notes"] = notes
        return tk.out(result)

    @tk.tool("read", title="Customer timeline")
    async def customer_timeline(
        organization: Annotated[str, Field(description="Organization name (exact, prefix or keywords) or ID.")],
        period: Annotated[str | None, Field(description=PERIOD_HELP + " Default: last_365_days.")] = None,
        start: Annotated[str | None, Field(description="Start date YYYY-MM-DD (inclusive). Overrides period.")] = None,
        end: Annotated[str | None, Field(description="End date YYYY-MM-DD (exclusive). Overrides period.")] = None,
        quiet_gap_days: Annotated[int, Field(ge=7, le=365, description="Call out gaps with no new tickets at "
                                             "least this long.")] = 30,
        include_routine: Annotated[bool, Field(description="List every ticket. Off: only notable ones (urgent/high, "
                                               "rated, reopened, linked to a problem, slow, still open) — counts "
                                               "still cover everything.")] = False,
        redact: Annotated[bool, Field(description="Strip names, emails, links and long numbers from text.")] = True,
        limit: Annotated[int, Field(ge=10, le=1000, description="Most recent events kept.")] = 150,
        max_tickets: Annotated[int, Field(ge=1, le=50000)] = 5000,
    ) -> str:
        """One customer's support history in date order, for QBRs, account handovers and renewal prep: when each
        notable ticket arrived and was solved, urgent and high-priority issues, CSAT ratings with comments,
        reopens, links to problem tickets, quiet stretches with no tickets, and what's still open — plus a
        month-by-month rollup (tickets, solved, urgent/high, CSAT, first reply and resolution medians) and
        account facts. Default: the last 365 days."""
        org = await resolve_org(zd, organization)
        oid = org["id"]
        p = resolve_period(period, start, end, default="last_365_days")
        now = utcnow()
        raw, context = await asyncio.gather(load_dataset(tk, p, "created", f"organization:{oid}", max_tickets),
                                            _org_context_safe(zd, org))
        ds = _only_org(raw, oid)
        red = _redactor_for(ds.users, redact)
        tickets = sorted(ds.tickets, key=lambda t: t.get("created_at") or "")
        url = f"https://{tk.settings.host}/agent/tickets/"
        events: list[dict[str, Any]] = []
        for t in tickets:
            m = ds.metrics.get(t["id"]) or {}
            rating = t.get("satisfaction_rating") or {}
            full = (m.get("full_resolution_time_in_minutes") or {}).get("calendar")
            open_ = t.get("status") in OPEN
            notable = (t.get("priority") in ("urgent", "high") or rating.get("score") in ("good", "bad")
                       or m.get("reopens") or t.get("problem_id") or open_ or (full or 0) >= 7 * 1440)
            if not (notable or include_routine):
                continue
            e: dict[str, Any] = {"at": t.get("created_at"), "event": "ticket opened", "ticket_id": t["id"],
                                 "subject": red(t.get("subject")), "priority": t.get("priority"),
                                 "channel": (t.get("via") or {}).get("channel")}
            if t.get("problem_id"):
                e["linked_problem"] = t["problem_id"]
            if open_:
                e["still_open"] = True
            events.append(e)
            solved_at = m.get("solved_at")
            if solved_at and not open_:
                s: dict[str, Any] = {"at": solved_at, "event": "solved", "ticket_id": t["id"]}
                if full is not None:
                    s["took_days"] = round(full / 1440, 1)
                if m.get("reopens"):
                    s["reopened"] = m["reopens"]
                events.append(s)
                if rating.get("score") in ("good", "bad"):
                    r = {"at": solved_at, "event": f"rated {rating['score']}", "ticket_id": t["id"]}
                    if rating.get("comment"):
                        r["comment"] = red(rating["comment"])
                    if rating.get("reason"):
                        r["reason"] = rating["reason"]
                    events.append(r)
        created = [parse_ts(t["created_at"]) for t in tickets if t.get("created_at")]
        stops = [datetime_of(p.start)] + created + [min(now, datetime_of(p.end))]
        for a, b in zip(stops, stops[1:]):
            gap = (b - a).days
            if gap >= quiet_gap_days:
                events.append({"at": a.isoformat(), "event": f"quiet for {gap} days", "until": b.isoformat()})
        events.sort(key=lambda e: e["at"] or "")
        for e in events:
            if "ticket_id" in e and e["event"] == "ticket opened":
                e["url"] = url + str(e["ticket_id"])

        by_month: dict[str, list[dict]] = defaultdict(list)
        for t in tickets:
            by_month[(t.get("created_at") or "")[:7]].append(t)
        months = []
        cur = p.start.replace(day=1)
        while cur < p.end:
            key = cur.strftime("%Y-%m")
            ts = by_month.get(key, [])
            k = _compact(_kpis(ts, ds.metrics))
            months.append({"month": key, "tickets": len(ts), "solved": k["solved"],
                           "urgent_or_high": sum(1 for t in ts if t.get("priority") in ("urgent", "high")),
                           "csat_good": sum(1 for t in ts if (t.get("satisfaction_rating") or {}).get("score") == "good"),
                           "csat_bad": sum(1 for t in ts if (t.get("satisfaction_rating") or {}).get("score") == "bad"),
                           "frt_median_min": k["frt_median_min"],
                           "full_resolution_median_min": k["full_resolution_median_min"]})
            cur = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        busiest = max(months, key=lambda r: r["tickets"]) if months and any(r["tickets"] for r in months) else None
        longest = max(tickets, key=lambda t: (ds.metrics.get(t["id"]) or {}).get("full_resolution_time_in_minutes",
                                                                                 {}).get("calendar") or 0, default=None)
        lm = (ds.metrics.get(longest["id"]) or {}) if longest else {}
        k_all = _kpis(tickets, ds.metrics)
        result: dict[str, Any] = {
            "customer": context,
            "scope": {**ds.scope(), "generated_at": now.isoformat(), "events_shown": min(len(events), limit),
                      "events_total": len(events), "all_tickets_listed": include_routine},
            "summary": {
                "tickets": len(tickets), "still_open": sum(1 for t in tickets if t.get("status") in OPEN),
                "urgent_or_high": sum(1 for t in tickets if t.get("priority") in ("urgent", "high")),
                "csat": {"good": k_all["csat"]["good"], "bad": k_all["csat"]["bad"], "score_pct": k_all["csat"]["score_pct"]},
                "first_ticket": tickets[0].get("created_at") if tickets else None,
                "last_ticket": tickets[-1].get("created_at") if tickets else None,
                "busiest_month": {"month": busiest["month"], "tickets": busiest["tickets"]} if busiest else None,
                "longest_to_resolve": {"ticket_id": longest["id"], "subject": red(longest.get("subject")),
                                       "days": round(lm["full_resolution_time_in_minutes"]["calendar"] / 1440, 1)}
                if longest and (lm.get("full_resolution_time_in_minutes") or {}).get("calendar") else None,
                "resolution_median_days": round(summarize(
                    [(ds.metrics.get(t["id"]) or {}).get("full_resolution_time_in_minutes", {}).get("calendar")
                     for t in tickets])["median"] / 1440, 1) if any(
                    (ds.metrics.get(t["id"]) or {}).get("full_resolution_time_in_minutes", {}).get("calendar")
                    for t in tickets) else None,
            },
            "monthly": months,
            "timeline": events[-limit:],
        }
        if len(events) > limit:
            result["timeline_note"] = f"Oldest {len(events) - limit} events left out (limit)."
        return tk.out(result)


def datetime_of(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), timezone.utc)


async def _org_context_safe(zd: ZendeskClient, org: dict) -> dict[str, Any]:
    try:
        return await _org_context(zd, org)
    except ZendeskError:
        return {"id": org["id"], "name": org.get("name")}
