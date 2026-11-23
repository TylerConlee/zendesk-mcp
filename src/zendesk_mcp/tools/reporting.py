"""Presentation-ready reporting: trends with period comparisons, contact drivers, CSAT verbatims,
volume heatmap, biggest issues, Help Center health, a one-call report bundle, and targets.

Every compute_* function returns plain dicts so the bundle and the individual tools share logic.
Tables meant for charts are tidy rows (one record per row) and can be exported as CSV."""

import asyncio
import random
import re
import time
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field

from ..client import ZendeskError
from ..formatting import to_csv
from ..metrics import analyze_conversation, parse_ts, rate, resolve_tz, summarize, utcnow
from ..periods import PERIOD_HELP, Period, auto_interval, bucket_start, buckets, resolve_period
from ..privacy import Redactor, no_redact
from ..targets import (METRICS, compare, definitions, load_targets, normalize_targets, save_targets, status,
                       targets_path)
from ..toolkit import Toolkit
from .advanced import _confine
from .analytics import (CACHE_TTL, DateField, Dataset, Filter, GroupBy, _compact, _gather, _group_key, _kpis,
                        _names, compute_backlog, compute_sla, load_dataset)

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

PeriodArg = Annotated[str | None, Field(description=PERIOD_HELP)]
Start = Annotated[str | None, Field(description="Start date YYYY-MM-DD (inclusive). Overrides period.")]
End = Annotated[str | None, Field(description="End date YYYY-MM-DD (exclusive). Overrides period.")]
MaxTickets = Annotated[int, Field(ge=1, le=50000, description="Upper bound on tickets analyzed (≈1 API call per 100).")]
Redact = Annotated[bool, Field(description="Strip customer/agent names, emails, links and long numbers from text.")]
CsvMode = Annotated[Literal["none", "inline", "files"],
                    Field(description="Also return chart tables as CSV: 'inline' in the response, or 'files' written "
                                      "under ZENDESK_FILE_ROOT/csv_dir.")]
CsvDir = Annotated[str, Field(description="Folder (inside ZENDESK_FILE_ROOT) for csv='files'.")]
TZ = Annotated[str, Field(description="Time zone for bucketing by day/hour (IANA, e.g. 'America/Los_Angeles').")]


# ----------------------------------------------------------------------------- shared helpers

def _tz(name: str) -> ZoneInfo:
    try:
        return resolve_tz(name)
    except ValueError as exc:
        raise ZendeskError(str(exc)) from exc


def _event_time(t: dict, metrics: dict[int, dict], date_field: str) -> datetime | None:
    if date_field == "solved":
        return parse_ts((metrics.get(t["id"]) or {}).get("solved_at")) or parse_ts(t.get("updated_at"))
    return parse_ts(t.get("updated_at" if date_field == "updated" else "created_at"))


def _unix(d: date) -> int:
    return int(datetime.combine(d, dtime(0), timezone.utc).timestamp())


def emit_tables(tk: Toolkit, result: dict[str, Any], tables: dict[str, list[dict]], csv: str, csv_dir: str) -> None:
    tables = {k: v for k, v in tables.items() if v}
    if csv == "inline":
        result["csv"] = {k: to_csv(v) for k, v in tables.items()}
    elif csv == "files":
        folder = _confine(tk.settings.file_root, csv_dir)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        paths = []
        for k, v in tables.items():
            path = folder / f"{k}-{stamp}.csv"
            path.write_text(to_csv(v))
            paths.append(str(path))
        result["csv_files"] = paths


def _redactor_for(ds_users: dict[int, dict], enabled: bool, extra_names: list[str] | None = None):
    if not enabled:
        return no_redact
    names = [u.get("name") for u in ds_users.values()] + (extra_names or [])
    return Redactor(n for n in names if n)


def headline_values(ds: Dataset) -> dict[str, float | None]:
    k = _kpis(ds.tickets, ds.metrics)
    c = _compact(k)
    count_key = "tickets_created" if ds.date_field == "created" else f"tickets_{ds.date_field}"
    return {
        count_key: len(ds.tickets),
        "tickets_solved": c["solved"],
        "frt_median_min": c["frt_median_min"],
        "frt_median_business_min": c["frt_median_business_min"],
        "frt_p90_min": c["frt_p90_min"],
        "full_resolution_median_min": c["full_resolution_median_min"],
        "full_resolution_median_business_min": c["full_resolution_median_business_min"],
        "requester_wait_median_min": k["requester_wait_time_min"]["calendar"].get("median"),
        "one_touch_pct": c["one_touch_pct"],
        "reopened_pct": c["reopened_pct"],
        "csat_pct": c["csat_pct"],
        "csat_response_rate_pct": k["csat"]["response_rate_pct"],
    }


# ----------------------------------------------------------------------------- trends

async def compute_trends(tk: Toolkit, p: Period, interval: str, date_field: str, filter: str | None,
                         split_by: str, max_tickets: int, tz_name: str, compare_previous: bool):
    tz = _tz(tz_name)
    interval = auto_interval(p) if interval == "auto" else interval
    ds = await load_dataset(tk, p, date_field, filter, max_tickets)
    by_bucket: dict[date, list[dict]] = defaultdict(list)
    for t in ds.tickets:
        at = _event_time(t, ds.metrics, date_field)
        if at:
            by_bucket[bucket_start(at.astimezone(tz).date(), interval)].append(t)
    bks = buckets(p, interval)
    trend = [{"period_start": b.isoformat(), **_compact(_kpis(by_bucket.get(b, []), ds.metrics))} for b in bks]

    result: dict[str, Any] = {"scope": {**ds.scope(), "interval": interval, "time_zone": tz_name,
                                        "units": "minutes"}}
    cur = headline_values(ds)
    if compare_previous:
        prev_p = p.previous()
        prev = headline_values(await load_dataset(tk, prev_p, date_field, filter, max_tickets))
        result["previous_period"] = prev_p.as_dict()
        result["vs_previous"] = {k: compare(k, cur[k], prev.get(k)) for k in cur}
    else:
        result["totals"] = cur
    tables = {"trend": trend}
    if split_by != "none":
        counts: dict[date, Counter] = defaultdict(Counter)
        overall: Counter = Counter()
        for b, ts in by_bucket.items():
            for t in ts:
                for key in _group_key(t, split_by, ds.users, ds.groups):
                    counts[b][key] += 1
                    overall[key] += 1
        top = [k for k, _ in overall.most_common(8)]
        rows = []
        for b in bks:
            other = sum(v for k, v in counts[b].items() if k not in top)
            rows.extend({"period_start": b.isoformat(), split_by: k, "tickets": counts[b].get(k, 0)} for k in top)
            if other:
                rows.append({"period_start": b.isoformat(), split_by: "Other", "tickets": other})
        tables[f"volume_by_{split_by}"] = rows
    result.update(tables)
    return result, tables, ds


# ----------------------------------------------------------------------------- topics

async def field_catalog(tk: Toolkit) -> dict[int, dict[str, Any]]:
    key = ("ticket_fields",)
    hit = tk.cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    page = await tk.zd.paginate("ticket_fields", "ticket_fields", limit=2000)
    cat = {}
    for f in page["ticket_fields"]:
        cat[f["id"]] = {
            "title": f.get("title"), "type": f.get("type"), "active": f.get("active", True),
            "options": {o.get("value"): o.get("name") for o in f.get("custom_field_options") or []},
        }
    tk.cache[key] = (time.monotonic(), cat)
    return cat


def topic_index(ds: Dataset, catalog: dict[int, dict], sources: list[str], field_ids: list[int] | None,
                exclude_tags: list[str] | None) -> dict[str, list[dict]]:
    """topic label -> tickets. Dropdown values also appear as tags in Zendesk, so when fields are
    counted, those tags are skipped to avoid double counting."""
    if field_ids:
        fields = {i: catalog[i] for i in field_ids if i in catalog}
    else:
        fields = {i: f for i, f in catalog.items() if f["active"] and f["type"] in ("tagger", "multiselect")}
    option_tags = {v for f in fields.values() for v in f["options"]} if "fields" in sources else set()
    skip = set(exclude_tags or []) | option_tags
    idx: dict[str, list[dict]] = defaultdict(list)
    for t in ds.tickets:
        if "fields" in sources:
            for cf in t.get("custom_fields") or []:
                f = fields.get(cf.get("id"))
                val = cf.get("value")
                if not f or val in (None, "", [], False):
                    continue
                for v in (val if isinstance(val, list) else [val]):
                    idx[f"{f['title']}: {f['options'].get(v, v)}"].append(t)
        if "tags" in sources:
            for tag in t.get("tags") or []:
                if tag not in skip:
                    idx[f"tag: {tag}"].append(t)
    return idx


def compute_topics(ds: Dataset, prev: Dataset | None, catalog: dict[int, dict], sources: list[str],
                   field_ids: list[int] | None, exclude_tags: list[str] | None, top: int) -> dict[str, Any]:
    cur_idx = topic_index(ds, catalog, sources, field_ids, exclude_tags)
    prev_idx = topic_index(prev, catalog, sources, field_ids, exclude_tags) if prev else {}
    total = len(ds.tickets) or 1
    rows = []
    for label, ts in cur_idx.items():
        k = _compact(_kpis(ts, ds.metrics))
        row = {"topic": label, "tickets": len(ts), "share_pct": rate(len(ts), total),
               "csat_pct": k["csat_pct"], "frt_median_min": k["frt_median_min"],
               "full_resolution_median_min": k["full_resolution_median_min"]}
        if prev is not None:
            pc = len(prev_idx.get(label, []))
            row.update(previous=pc, change=len(ts) - pc, change_pct=rate(len(ts) - pc, pc))
        rows.append(row)
    if prev is not None:  # topics that vanished entirely
        for label, ts in prev_idx.items():
            if label not in cur_idx:
                rows.append({"topic": label, "tickets": 0, "share_pct": 0.0, "previous": len(ts),
                             "change": -len(ts), "change_pct": -100.0})
    rows.sort(key=lambda r: -r["tickets"])
    result: dict[str, Any] = {"topics": rows[:top]}
    if prev is not None:
        movers = [r for r in rows if max(r["tickets"], r["previous"]) >= 3]
        result["rising"] = sorted((r for r in movers if r["change"] > 0), key=lambda r: -r["change"])[:5]
        result["falling"] = sorted((r for r in movers if r["change"] < 0), key=lambda r: r["change"])[:5]
    covered = {t["id"] for ts in cur_idx.values() for t in ts}
    result["note"] = ("A ticket can carry several topics, so shares can sum past 100%. Tickets with no topic: "
                      f"{sum(1 for t in ds.tickets if t['id'] not in covered)}.")
    return result


# ----------------------------------------------------------------------------- CSAT

async def compute_csat(tk: Toolkit, p: Period, verbatims: int, comments_only: bool, redact: bool,
                       team_level: bool, max_ratings: int) -> dict[str, Any]:
    zd = tk.zd
    page = await zd.paginate("satisfaction_ratings", "satisfaction_ratings",
                             params={"score": "received", "start_time": _unix(p.start), "end_time": _unix(p.end)},
                             limit=max_ratings)
    ratings = [r for r in page["satisfaction_ratings"] if r.get("score") in ("good", "bad")]
    try:
        reasons_page = await zd.paginate("satisfaction_reasons", "reasons", limit=500, cursor=False)
        reasons = {r["id"]: r.get("value") or r.get("raw_value") for r in reasons_page["reasons"]}
    except ZendeskError:  # reasons need a plan that supports them
        reasons = {}
    users, groups = await _names(zd, {r.get("assignee_id") for r in ratings} | {r.get("requester_id") for r in ratings},
                                 {r.get("group_id") for r in ratings if r.get("group_id")})
    red = _redactor_for(users, redact)

    def reason(r: dict) -> str:
        return reasons.get(r.get("reason_id")) or r.get("reason") or "No reason given"

    good = [r for r in ratings if r["score"] == "good"]
    bad = [r for r in ratings if r["score"] == "bad"]
    bad_reasons = Counter(reason(r) for r in bad)
    by_group: dict[str, Counter] = defaultdict(Counter)
    by_agent: dict[str, Counter] = defaultdict(Counter)
    for r in ratings:
        g = groups.get(r.get("group_id"), {}).get("name") or "(no group)"
        by_group[g][r["score"]] += 1
        by_agent[users.get(r.get("assignee_id"), {}).get("name") or "(unassigned)"][r["score"]] += 1

    def table(d: dict[str, Counter], label: str) -> list[dict]:
        rows = [{label: k, "good": c["good"], "bad": c["bad"], "ratings": c["good"] + c["bad"],
                 "csat_pct": rate(c["good"], c["good"] + c["bad"])} for k, c in d.items()]
        return sorted(rows, key=lambda r: -r["ratings"])[:50]

    def quotes(pool: list[dict]) -> list[dict]:
        pool = [r for r in pool if r.get("comment")] if comments_only else pool
        pool = sorted(pool, key=lambda r: r.get("created_at") or "", reverse=True)[:verbatims]
        out = []
        for r in pool:
            q = {"comment": red(r.get("comment")), "ticket_id": r.get("ticket_id"), "created_at": r.get("created_at"),
                 "group": groups.get(r.get("group_id"), {}).get("name")}
            if r["score"] == "bad":
                q["reason"] = reason(r)
            if not team_level:
                q["agent"] = users.get(r.get("assignee_id"), {}).get("name")
            out.append(q)
        return out

    result = {
        "basis": "Ratings received in the period (by rating date), from the satisfaction ratings API.",
        "totals": {"good": len(good), "bad": len(bad), "csat_pct": rate(len(good), len(ratings)),
                   "with_comment": sum(1 for r in ratings if r.get("comment")),
                   "ratings_read": len(ratings), "more_available": page["has_more"]},
        "bad_reasons": [{"reason": k, "count": v, "share_pct": rate(v, len(bad))} for k, v in bad_reasons.most_common()],
        "by_group": table(by_group, "group"),
        "verbatims": {"good": quotes(good), "bad": quotes(bad)},
    }
    if not team_level:
        result["by_agent"] = table(by_agent, "agent")
    return result


# ----------------------------------------------------------------------------- heatmap

def compute_heatmap(ds: Dataset, tz_name: str, event: str) -> dict[str, Any]:
    tz = _tz(tz_name)
    grid = [[0] * 24 for _ in range(7)]
    frt_by_hour: dict[int, list[float]] = defaultdict(list)
    for t in ds.tickets:
        at = _event_time(t, ds.metrics, "solved" if event == "solved" else "created")
        if not at:
            continue
        local = at.astimezone(tz)
        grid[local.weekday()][local.hour] += 1
        if event == "created":
            frt = ((ds.metrics.get(t["id"]) or {}).get("reply_time_in_minutes") or {}).get("calendar")
            if frt is not None:
                frt_by_hour[local.hour].append(frt)
    occurrences = Counter((ds.period.start + timedelta(days=i)).weekday() for i in range(ds.period.days))
    rows = [{"day": DAYS[d], "hour": h, "tickets": grid[d][h],
             "avg_per_day": round(grid[d][h] / occurrences[d], 2) if occurrences[d] else None}
            for d in range(7) for h in range(24)]
    by_day = [{"day": DAYS[d], "tickets": sum(grid[d]),
               "avg_per_day": round(sum(grid[d]) / occurrences[d], 1) if occurrences[d] else None} for d in range(7)]
    by_hour = [{"hour": h, "tickets": sum(grid[d][h] for d in range(7))} for h in range(24)]
    result: dict[str, Any] = {
        "time_zone": tz_name, "event": event,
        "matrix": {"rows": DAYS, "columns": "hour 0-23", "values": grid},
        "peaks": sorted(rows, key=lambda r: -r["tickets"])[:5],
        "by_day": by_day, "by_hour": by_hour,
    }
    if frt_by_hour:
        result["frt_median_by_creation_hour"] = [
            {"hour": h, "frt_median_min": (summarize(frt_by_hour[h]) or {}).get("median"), "tickets": len(frt_by_hour[h])}
            for h in range(24)]
    result["_rows"] = rows
    return result


# ----------------------------------------------------------------------------- issues

async def compute_issues(tk: Toolkit, ds: Dataset, limit: int, redact: bool, team_level: bool,
                         anonymize_orgs: bool) -> dict[str, Any]:
    zd = tk.zd
    red = _redactor_for(ds.users, redact)
    m = ds.metrics
    gname = lambda t: ds.groups.get(t.get("group_id"), {}).get("name")  # noqa: E731

    incidents = Counter(t["problem_id"] for t in ds.tickets if t.get("problem_id"))
    top_problems = incidents.most_common(limit)
    problems = []
    if top_problems:
        ids = [pid for pid, _ in top_problems]
        found = {t["id"]: t for t in (await zd.get("tickets/show_many", ids=",".join(map(str, ids)))).get("tickets", [])}
        for pid, n in top_problems:
            pt = found.get(pid, {})
            problems.append({"problem_ticket_id": pid, "linked_incidents": n, "subject": red(pt.get("subject")),
                             "status": pt.get("status"), "created_at": pt.get("created_at")})

    by_org: dict[int, list[dict]] = defaultdict(list)
    for t in ds.tickets:
        if t.get("organization_id"):
            by_org[t["organization_id"]].append(t)
    org_ids = sorted(by_org, key=lambda o: -len(by_org[o]))[:200]
    names: dict[int, str] = {}
    pages = await _gather([(lambda b=org_ids[i:i + 100]: zd.get("organizations/show_many", ids=",".join(map(str, b))))
                           for i in range(0, len(org_ids), 100)])
    for pg in pages:
        names.update({o["id"]: o.get("name") for o in pg.get("organizations", [])})
    org_rows = []
    for rank, oid in enumerate(org_ids, 1):
        ts = by_org[oid]
        k = _compact(_kpis(ts, m))
        org_rows.append({"organization": f"Customer {rank}" if anonymize_orgs else names.get(oid, f"org {oid}"),
                         "tickets": len(ts), "share_pct": rate(len(ts), len(ds.tickets)),
                         "urgent_or_high": sum(1 for t in ts if t.get("priority") in ("urgent", "high")),
                         "csat_pct": k["csat_pct"], "csat_ratings": k["csat_ratings"],
                         "frt_median_min": k["frt_median_min"], "reopened_pct": k["reopened_pct"]})
    worst = sorted((r for r in org_rows if (r["csat_ratings"] or 0) >= 3), key=lambda r: r["csat_pct"])[:5]

    def brief(t: dict) -> dict[str, Any]:
        mm = m.get(t["id"]) or {}
        row = {"id": t["id"], "subject": red(t.get("subject")), "status": t.get("status"), "group": gname(t),
               "replies": mm.get("replies"), "reopens": mm.get("reopens"),
               "full_resolution_min": ((mm.get("full_resolution_time_in_minutes") or {}).get("calendar"))}
        if not team_level:
            row["assignee"] = ds.users.get(t.get("assignee_id"), {}).get("name")
        return row

    effort = sorted((t for t in ds.tickets if (m.get(t["id"]) or {}).get("replies")),
                    key=lambda t: -(m[t["id"]]["replies"]))[:limit]
    reopened = sorted((t for t in ds.tickets if (m.get(t["id"]) or {}).get("reopens")),
                      key=lambda t: -(m[t["id"]]["reopens"]))[:limit]
    prio = Counter(t.get("priority") or "(none)" for t in ds.tickets)
    return {
        "problems_with_most_incidents": problems,
        "top_organizations": org_rows[:limit],
        "lowest_csat_organizations": worst,
        "priority_mix": [{"priority": k, "tickets": v, "share_pct": rate(v, len(ds.tickets))} for k, v in prio.most_common()],
        "highest_effort_tickets": [brief(t) for t in effort],
        "most_reopened_tickets": [brief(t) for t in reopened],
    }


# ----------------------------------------------------------------------------- Help Center

async def compute_help_center(tk: Toolkit, ds: Dataset | None, top: int, stale_days: int, min_votes: int,
                              gap_topics: int, max_articles: int) -> dict[str, Any]:
    zd = tk.zd
    page = await zd.paginate("help_center/articles", "articles", limit=max_articles)
    arts = page["articles"]
    now = utcnow()

    def helpful(a: dict) -> float | None:
        n = a.get("vote_count") or 0
        return round(100 * (n + (a.get("vote_sum") or 0)) / 2 / n, 1) if n else None

    def row(a: dict) -> dict[str, Any]:
        return {"id": a["id"], "title": a.get("title"), "helpful_pct": helpful(a), "votes": a.get("vote_count") or 0,
                "last_edited": a.get("edited_at") or a.get("updated_at"), "url": a.get("html_url")}

    published = [a for a in arts if not a.get("draft")]
    voted = [a for a in published if (a.get("vote_count") or 0) >= min_votes]
    cutoff = now - timedelta(days=stale_days)
    stale = [a for a in published if (parse_ts(a.get("edited_at") or a.get("updated_at")) or now) < cutoff]
    result: dict[str, Any] = {
        "articles": {"total": len(arts), "published": len(published), "drafts": len(arts) - len(published),
                     "flagged_outdated": sum(1 for a in arts if a.get("outdated")),
                     f"not_edited_in_{stale_days}_days": len(stale), "more_available": page["has_more"]},
        "most_helpful": [row(a) for a in sorted(voted, key=lambda a: (-helpful(a), -a["vote_count"]))[:top]],
        "least_helpful": [row(a) for a in sorted(voted, key=lambda a: (helpful(a), -a["vote_count"]))[:top]],
        "most_voted": [row(a) for a in sorted(published, key=lambda a: -(a.get("vote_count") or 0))[:top]],
        "stalest": [row(a) for a in sorted(stale, key=lambda a: a.get("edited_at") or a.get("updated_at") or "")[:top]],
        "note": "Article view counts aren't exposed by the Help Center API (they live in Explore); "
                "helpfulness comes from up/down votes.",
    }
    if ds is not None and gap_topics:
        catalog = await field_catalog(tk)
        idx = topic_index(ds, catalog, ["fields", "tags"], None, None)
        topics = sorted(idx.items(), key=lambda kv: -len(kv[1]))[:gap_topics]

        async def probe(label: str) -> dict[str, Any]:
            q = re.sub(r"[_\-:]+", " ", label.split(": ", 1)[-1]).strip()
            res = await zd.get("help_center/articles/search", query=q, per_page=3)
            hits = res.get("results") or []
            return {"query": q, "articles_found": res.get("count", len(hits)),
                    "top_match": hits[0].get("title") if hits else None}

        probes = await _gather([(lambda lb=lb: probe(lb)) for lb, _ in topics])
        gaps = [{"topic": lb, "tickets": len(ts), **pr} for (lb, ts), pr in zip(topics, probes)]
        result["topic_coverage"] = gaps
        result["gaps"] = [g for g in gaps if not g["articles_found"]]
    return result


# ----------------------------------------------------------------------------- reply-gap sample (bundle)

async def compute_reply_gap_sample(tk: Toolkit, ds: Dataset, n: int) -> dict[str, Any]:
    pool = [t for t in ds.tickets if (ds.metrics.get(t["id"]) or {}).get("replies")]
    sample = random.Random(0).sample(pool, min(n, len(pool)))

    async def one(t: dict):
        page = await tk.zd.paginate(f"tickets/{t['id']}/comments", "comments", params={"include": "users"}, limit=2000)
        roles = {u["id"]: u.get("role") for u in page.get("users", [])}
        return analyze_conversation(page["comments"], roles, status=t.get("status"))

    convs = await _gather([(lambda t=t: one(t)) for t in sample])
    agent = [r["minutes"] for c in convs for r in c.agent_responses]
    cust = [r["minutes"] for c in convs for r in c.customer_responses]
    return {"tickets_sampled": len(sample), "agent_response_min": summarize(agent) or {"n": 0},
            "customer_response_min": summarize(cust) or {"n": 0},
            "definition": METRICS["agent_response_median_min"]["definition"]}


# ----------------------------------------------------------------------------- tools

ALL_SECTIONS = ["kpis", "trends", "channels", "topics", "csat", "sla", "heatmap", "issues", "backlog", "help_center"]
Section = Literal["kpis", "trends", "channels", "topics", "csat", "sla", "heatmap", "issues", "backlog", "help_center"]


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="KPI trends")
    async def kpi_trends(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        interval: Literal["auto", "day", "week", "month"] = "auto",
        date_field: DateField = "created",
        filter: Filter = None,
        split_by: Annotated[GroupBy, Field(description="Also return ticket volume per period split by this "
                                           "(top 8 + Other), e.g. channel for a stacked chart.")] = "none",
        compare_previous: Annotated[bool, Field(description="Compare totals with the previous equivalent period.")] = True,
        time_zone: TZ = "UTC",
        max_tickets: MaxTickets = 20000,
        csv: CsvMode = "none",
        csv_dir: CsvDir = "reports",
    ) -> str:
        """KPIs over time for charts: one row per day/week/month with volume, solved, first reply and resolution
        medians (calendar + business), one-touch %, reopen %, CSAT — plus period-over-period deltas marked
        improved/worsened (e.g. Q3 vs Q2). Default period: last 30 days."""
        result, tables, _ = await compute_trends(tk, resolve_period(period, start, end), interval, date_field, filter,
                                                 split_by, max_tickets, time_zone, compare_previous)
        emit_tables(tk, result, tables, csv, csv_dir)
        return tk.out(result)

    @tk.tool("read", title="Ticket topics / contact drivers")
    async def ticket_topics(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        date_field: DateField = "created",
        filter: Filter = None,
        sources: Annotated[list[Literal["fields", "tags"]], Field(description="Count dropdown/multiselect field "
                                                                   "values, tags, or both.")] = ["fields", "tags"],
        field_ids: Annotated[list[int] | None, Field(description="Only these ticket fields (default: all active "
                                                     "dropdown and multiselect fields).")] = None,
        exclude_tags: list[str] | None = None,
        top: Annotated[int, Field(ge=1, le=200)] = 25,
        compare_previous: bool = True,
        max_tickets: MaxTickets = 20000,
        csv: CsvMode = "none",
        csv_dir: CsvDir = "reports",
    ) -> str:
        """What customers contact you about: ticket volume by category field values and tags, share of total,
        growth vs the previous period, rising/falling topics, and CSAT/first reply/resolution per topic.
        For free-text themes not captured by fields or tags, use sample_ticket_text."""
        p = resolve_period(period, start, end)
        ds = await load_dataset(tk, p, date_field, filter, max_tickets)
        prev = await load_dataset(tk, p.previous(), date_field, filter, max_tickets) if compare_previous else None
        result = {"scope": ds.scope(), **compute_topics(ds, prev, await field_catalog(tk), sources, field_ids,
                                                        exclude_tags, top)}
        if prev:
            result["previous_period"] = prev.period.as_dict()
        emit_tables(tk, result, {"topics": result["topics"]}, csv, csv_dir)
        return tk.out(result)

    @tk.tool("read", title="Sample ticket text for themes")
    async def sample_ticket_text(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        date_field: DateField = "created",
        filter: Filter = None,
        sample_size: Annotated[int, Field(ge=1, le=500)] = 150,
        excerpt_chars: Annotated[int, Field(ge=50, le=2000)] = 300,
        redact: Redact = True,
        seed: Annotated[int, Field(description="Same seed = same sample, so results are reproducible.")] = 0,
        max_tickets: MaxTickets = 20000,
    ) -> str:
        """A reproducible random sample of ticket subjects and opening messages, so you can identify themes in
        free text. Estimate a theme's volume as (share of sample) × total_in_scope, and present it as an estimate."""
        ds = await load_dataset(tk, resolve_period(period, start, end), date_field, filter, max_tickets)
        sample = random.Random(seed).sample(ds.tickets, min(sample_size, len(ds.tickets)))
        red = _redactor_for(ds.users, redact)
        rows = []
        for t in sorted(sample, key=lambda t: t["id"]):
            desc = re.sub(r"\s+", " ", t.get("description") or "").strip()
            rows.append({"id": t["id"], "subject": red(t.get("subject")), "excerpt": red(desc[:excerpt_chars]),
                         "tags": t.get("tags"), "channel": (t.get("via") or {}).get("channel"),
                         "csat": (t.get("satisfaction_rating") or {}).get("score")})
        return tk.out({"scope": ds.scope(), "total_in_scope": len(ds.tickets), "sample_size": len(rows),
                       "how_to_use": "Group into themes; estimated volume = share of sample × total_in_scope "
                                     f"(±~{round(100 / max(len(rows), 1) ** 0.5)} pts at this sample size).",
                       "tickets": rows})

    @tk.tool("read", title="CSAT feedback & verbatims")
    async def csat_feedback(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        verbatims: Annotated[int, Field(ge=0, le=200, description="Quotes to return per score (newest first).")] = 10,
        comments_only: bool = True,
        redact: Redact = True,
        team_level: Annotated[bool, Field(description="Hide per-agent breakdowns and agent names on quotes.")] = True,
        max_ratings: Annotated[int, Field(ge=1, le=50000)] = 10000,
        csv: CsvMode = "none",
        csv_dir: CsvDir = "reports",
    ) -> str:
        """Customer satisfaction detail: good/bad totals, reasons given for bad ratings, CSAT by group (or agent),
        and customer comments for quote slides — redacted by default."""
        result = await compute_csat(tk, resolve_period(period, start, end), verbatims, comments_only, redact,
                                    team_level, max_ratings)
        emit_tables(tk, result, {"csat_by_group": result["by_group"], "bad_reasons": result["bad_reasons"]},
                    csv, csv_dir)
        return tk.out(result)

    @tk.tool("read", title="Volume heatmap")
    async def volume_heatmap(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        filter: Filter = None,
        event: Annotated[Literal["created", "solved"], Field(description="Count new tickets or solves.")] = "created",
        time_zone: TZ = "UTC",
        max_tickets: MaxTickets = 20000,
        csv: CsvMode = "none",
        csv_dir: CsvDir = "reports",
    ) -> str:
        """When customers contact you: a day-of-week × hour-of-day matrix (counts and average per day), peak hours,
        totals by day and hour, and median first reply time by the hour tickets arrive — for staffing slides."""
        p = resolve_period(period, start, end)
        ds = await load_dataset(tk, p, "solved" if event == "solved" else "created", filter, max_tickets)
        result = {"scope": ds.scope(), **compute_heatmap(ds, time_zone, event)}
        rows = result.pop("_rows")
        emit_tables(tk, result, {"heatmap": rows}, csv, csv_dir)
        return tk.out(result)

    @tk.tool("read", title="Biggest issues")
    async def top_issues(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        date_field: DateField = "created",
        filter: Filter = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        redact: Redact = True,
        team_level: bool = True,
        anonymize_orgs: Annotated[bool, Field(description="Show organizations as 'Customer 1', 'Customer 2'...")] = False,
        max_tickets: MaxTickets = 20000,
    ) -> str:
        """Biggest issues in a period: problem tickets with the most linked incidents, organizations with the most
        tickets and lowest CSAT, priority mix, highest-effort tickets (most replies) and most-reopened tickets."""
        ds = await load_dataset(tk, resolve_period(period, start, end), date_field, filter, max_tickets)
        return tk.out({"scope": ds.scope(), **await compute_issues(tk, ds, limit, redact, team_level, anonymize_orgs)})

    @tk.tool("read", title="Help Center report")
    async def help_center_report(
        period: Annotated[str | None, Field(description="Period whose ticket topics are checked for article "
                                            "coverage. " + PERIOD_HELP)] = None,
        start: Start = None,
        end: End = None,
        top: Annotated[int, Field(ge=1, le=50)] = 10,
        stale_days: int = 365,
        min_votes: Annotated[int, Field(ge=1, description="Minimum votes for helpfulness rankings.")] = 5,
        gap_topics: Annotated[int, Field(ge=0, le=30, description="How many top ticket topics to check for matching "
                                         "articles (0 = skip).")] = 10,
        max_articles: Annotated[int, Field(ge=1, le=10000)] = 3000,
    ) -> str:
        """Self-service health: article counts, most/least helpful (by votes), stale articles, and the top ticket
        topics that have no matching Help Center article — the content gaps."""
        ds = None
        if gap_topics:
            ds = await load_dataset(tk, resolve_period(period, start, end), "created", None, 20000)
        return tk.out(await compute_help_center(tk, ds, top, stale_days, min_votes, gap_topics, max_articles))

    @tk.tool("read", title="Report targets", read_only_hint=False)
    async def report_targets(
        action: Literal["get", "set", "clear"] = "get",
        targets: Annotated[dict[str, Any] | None, Field(description="For 'set': {metric: goal} or {metric: {'goal': n, "
                                                        "'amber_pct': 10}}, e.g. {'csat_pct': 92, "
                                                        "'frt_median_business_min': 60}. Merged with existing.")] = None,
        metrics: Annotated[list[str] | None, Field(description="For 'clear': metrics to remove (omit to clear all).")] = None,
    ) -> str:
        """Goals used to colour report metrics green/amber/red. Stored in a local JSON file (not in Zendesk).
        Amber = within amber_pct of the goal on the wrong side."""
        root = tk.settings.file_root
        current = load_targets(root)
        if action == "set":
            if not targets:
                raise ZendeskError("Pass `targets` to set.")
            try:
                current.update(normalize_targets(targets))
            except ValueError as exc:
                raise ZendeskError(str(exc)) from exc
            save_targets(root, current)
        elif action == "clear":
            for k in (metrics or list(current)):
                current.pop(k, None)
            save_targets(root, current)
        return tk.out({"targets": current, "stored_at": str(targets_path(root)),
                       "targetable_metrics": {k: f"{v['label']} ({v['unit']}, {v['better']} is better)"
                                              for k, v in METRICS.items() if v["better"] != "neutral"}})

    @tk.tool("read", title="Support report (deck bundle)")
    async def support_report(
        period: Annotated[str | None, Field(description=PERIOD_HELP + " Default: last_quarter.")] = None,
        start: Start = None,
        end: End = None,
        filter: Filter = None,
        sections: Annotated[list[Section] | None, Field(description="Sections to include (default all).")] = None,
        business_hours: Annotated[bool, Field(description="Use business-hours medians for headline speed metrics.")] = False,
        interval: Literal["auto", "day", "week", "month"] = "auto",
        time_zone: TZ = "UTC",
        team_level: Annotated[bool, Field(description="Company-audience mode: no per-agent figures or agent names.")] = True,
        redact: Redact = True,
        verbatims: Annotated[int, Field(ge=0, le=50, description="Customer quotes per score.")] = 5,
        reply_gap_sample: Annotated[int, Field(ge=0, le=500, description="Also sample N tickets to measure time "
                                               "between every customer and agent reply (1 API call per ticket).")] = 0,
        anonymize_orgs: bool = False,
        max_tickets: MaxTickets = 20000,
        csv: CsvMode = "none",
        csv_dir: CsvDir = "reports",
    ) -> str:
        """Everything for a support review deck in one call: a headline scorecard (vs previous period and vs
        targets, with green/amber/red), trends, channel mix, contact drivers, CSAT with quotes, SLA, staffing
        heatmap, biggest issues, backlog and Help Center gaps — plus metric definitions and a suggested slide
        outline. Defaults are safe for a company-wide audience (team-level, redacted)."""
        p = resolve_period(period, start, end, default="last_quarter")
        prev_p = p.previous()
        want = set(sections or ALL_SECTIONS)
        ds, prev = await asyncio.gather(load_dataset(tk, p, "created", filter, max_tickets),
                                        load_dataset(tk, prev_p, "created", filter, max_tickets))
        targets = load_targets(tk.settings.file_root)
        notes: list[str] = []
        for d in (ds, prev):
            if d.total is not None and d.total > len(d.tickets):
                notes.append(f"{d.period.label}: analyzed {len(d.tickets)} of {d.total} tickets (max_tickets).")

        # Independent API-backed sections run concurrently.
        jobs: dict[str, Any] = {}
        if "sla" in want:
            jobs["sla"] = compute_sla(zd, p)
            jobs["sla_prev"] = compute_sla(zd, prev_p)
        if "csat" in want:
            jobs["csat"] = compute_csat(tk, p, verbatims, True, redact, team_level, 10000)
        if "backlog" in want or "kpis" in want:
            jobs["backlog"] = compute_backlog(zd, filter, "group", 7, 20000)
        if "issues" in want:
            jobs["issues"] = compute_issues(tk, ds, 5, redact, team_level, anonymize_orgs)
        if "help_center" in want:
            jobs["help_center"] = compute_help_center(tk, ds, 5, 365, 5, 8, 3000)
        if "topics" in want:
            jobs["catalog"] = field_catalog(tk)
        if reply_gap_sample:
            jobs["reply_gaps"] = compute_reply_gap_sample(tk, ds, reply_gap_sample)
        done = dict(zip(jobs, await asyncio.gather(*jobs.values())))

        cur_v, prev_v = headline_values(ds), headline_values(prev)
        if "sla" in done:
            cur_v["sla_achievement_pct"] = done["sla"]["overall"]["achievement_pct"]
            prev_v["sla_achievement_pct"] = done["sla_prev"]["overall"]["achievement_pct"]
        if "backlog" in done:
            cur_v["backlog_unsolved"] = done["backlog"]["unsolved_total"]
        if "reply_gaps" in done:
            cur_v["agent_response_median_min"] = done["reply_gaps"]["agent_response_min"].get("median")
        speed = ["frt_median_business_min", "full_resolution_median_business_min"] if business_hours else \
            ["frt_median_min", "full_resolution_median_min"]
        card_keys = ["tickets_created", "tickets_solved", *speed, "requester_wait_median_min", "one_touch_pct",
                     "reopened_pct", "csat_pct", "sla_achievement_pct", "backlog_unsolved", "agent_response_median_min"]
        cards = []
        for k in card_keys:
            if k not in cur_v:
                continue
            card = {"metric": k, "label": METRICS[k]["label"], "unit": METRICS[k]["unit"],
                    **compare(k, cur_v[k], prev_v.get(k))}
            st = status(k, cur_v[k], targets)
            if st:
                card["target"] = st
            cards.append(card)

        result: dict[str, Any] = {
            "report": {"period": p.as_dict(), "previous_period": prev_p.as_dict(),
                       "generated_at": utcnow().isoformat(), "instance": tk.settings.host, "filter": filter,
                       "tickets_in_period": len(ds.tickets), "privacy": {"team_level": team_level, "redacted": redact},
                       "targets_set": bool(targets)},
            "headline": cards,
        }
        tables: dict[str, list[dict]] = {"headline": cards}

        if "trends" in want or "kpis" in want:
            tr, ttables, _ = await compute_trends(tk, p, interval, "created", filter, "none", max_tickets,
                                                  time_zone, False)
            result["trends"] = {"interval": tr["scope"]["interval"], "rows": tr["trend"]}
            tables["trend"] = tr["trend"]
        if "kpis" in want:
            k = _kpis(ds.tickets, ds.metrics)
            result["kpi_detail"] = {key: k[key] for key in ("first_reply_time_min", "full_resolution_time_min",
                                                            "requester_wait_time_min", "efficiency", "csat")}
        if "channels" in want:
            cc = Counter((t.get("via") or {}).get("channel") or "unknown" for t in ds.tickets)
            pc = Counter((t.get("via") or {}).get("channel") or "unknown" for t in prev.tickets)
            rows = [{"channel": ch, "tickets": cc[ch], "share_pct": rate(cc[ch], len(ds.tickets)),
                     "previous": pc[ch], "previous_share_pct": rate(pc[ch], len(prev.tickets)),
                     "change_pct": rate(cc[ch] - pc[ch], pc[ch])}
                    for ch in sorted(set(cc) | set(pc), key=lambda c: -cc[c])]
            result["channels"] = rows
            tables["channels"] = rows
        if "topics" in want:
            tp = compute_topics(ds, prev, done["catalog"], ["fields", "tags"], None, None, 15)
            result["topics"] = tp
            tables["topics"] = tp["topics"]
        if "csat" in want:
            result["csat"] = done["csat"]
            tables["csat_by_group"] = done["csat"]["by_group"]
        if "sla" in want:
            result["sla"] = {k: done["sla"][k] for k in ("overall", "by_policy_and_metric")}
            result["sla"]["previous_overall"] = done["sla_prev"]["overall"]
            result["sla"]["currently_breached_unfulfilled"] = done["sla"]["currently_breached_unfulfilled"]["count"]
            tables["sla"] = done["sla"]["by_policy_and_metric"]
            if not done["sla"]["scope"]["complete"]:
                notes.append("SLA event export hit its page limit; SLA figures may be incomplete.")
        if "heatmap" in want:
            hm = compute_heatmap(ds, time_zone, "created")
            tables["heatmap"] = hm.pop("_rows")
            result["heatmap"] = hm
        if "issues" in want:
            result["issues"] = done["issues"]
            tables["top_organizations"] = done["issues"]["top_organizations"]
        if "backlog" in want:
            bl = done["backlog"]
            result["backlog"] = {k: bl[k] for k in ("unsolved_total", "by_status", "age_since_created", "stale",
                                                    "unassigned", "by_group")}
        if "help_center" in want:
            result["help_center"] = done["help_center"]
        if "reply_gaps" in done:
            result["reply_gaps"] = done["reply_gaps"]

        result["definitions"] = definitions([c["metric"] for c in cards])
        result["suggested_slides"] = _outline(want, bool(done.get("reply_gaps")))
        notes.append("CSAT in the headline is ticket-based (ratings on tickets created in the period); the CSAT "
                     "section counts ratings received in the period, so the two can differ slightly.")
        result["data_notes"] = notes
        emit_tables(tk, result, tables, csv, csv_dir)
        return tk.out(result)


def _outline(want: set[str], reply_gaps: bool) -> list[dict[str, str]]:
    slides = [
        ("Title", "report.period", True),
        ("Executive summary: 3 wins, 3 watch-outs", "headline (trend + target status)", True),
        ("Scorecard", "headline", True),
        ("Volume & channel mix", "trends.rows, channels", "trends" in want or "channels" in want),
        ("Speed: first reply & resolution over time", "trends.rows, kpi_detail", "trends" in want),
        ("Time between customer and agent replies", "reply_gaps", reply_gaps),
        ("Customer satisfaction", "csat.totals, csat.by_group, csat.bad_reasons", "csat" in want),
        ("Voice of the customer", "csat.verbatims", "csat" in want),
        ("SLA performance", "sla", "sla" in want),
        ("What customers contacted us about", "topics.topics", "topics" in want),
        ("Rising & falling topics", "topics.rising, topics.falling", "topics" in want),
        ("When customers reach us", "heatmap", "heatmap" in want),
        ("Biggest issues", "issues", "issues" in want),
        ("Backlog health", "backlog", "backlog" in want),
        ("Self-service opportunities", "help_center.gaps, help_center.least_helpful", "help_center" in want),
        ("Next quarter priorities", "synthesise from the sections above", True),
        ("Appendix: metric definitions", "definitions", True),
    ]
    return [{"slide": s, "data": d} for s, d, ok in slides if ok]
