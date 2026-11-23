"""How the Zendesk setup is working: routing (where tickets land vs where they're solved), macros (which get
used and whether they help), one agent's work for 1:1s, and whether the team will keep up with demand."""

import asyncio
import json
import math
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import Field

from ..audits import audits_for, group_path, minutes_between
from ..client import ZendeskError
from ..metrics import parse_ts, rate, summarize, utcnow
from ..periods import PERIOD_HELP, Period, resolve_period
from ..privacy import Redactor, no_redact
from ..targets import METRICS, compare, definitions
from ..toolkit import Toolkit
from .analytics import _compact, _gather, _kpis, compute_backlog, load_dataset
from .customers import difficulty, score_message, tone
from .reporting import compute_heatmap
from .followups import AGENT_ROLES, track_promises

PeriodArg = Annotated[str | None, Field(description=PERIOD_HELP + " Default: last_30_days.")]
Start = Annotated[str | None, Field(description="Start date YYYY-MM-DD (inclusive). Overrides period.")]
End = Annotated[str | None, Field(description="End date YYYY-MM-DD (exclusive). Overrides period.")]
Filter = Annotated[str | None, Field(description="Extra Zendesk search terms, e.g. 'brand:acme' or 'group:\"Tier 2\"'.")]
Audited = Annotated[int, Field(ge=10, le=3000, description="Tickets whose change history is read (1 API call each, "
                                                         "random sample if more solved). Cached 10 minutes.")]


async def _groups(tk: Toolkit) -> dict[int, str]:
    page = await tk.zd.paginate("groups", "groups", limit=2000)
    return {g["id"]: g.get("name") for g in page["groups"]}


async def _forms(tk: Toolkit) -> dict[int, str]:
    try:
        page = await tk.zd.paginate("ticket_forms", "ticket_forms", limit=500, cursor=False)
        return {f["id"]: f.get("name") for f in page["ticket_forms"]}
    except ZendeskError:
        return {}


def _sample(tickets: list[dict], n: int) -> list[dict]:
    return tickets if len(tickets) <= n else random.Random(0).sample(tickets, n)


def _med(values) -> float | None:
    s = summarize(values)
    return s["median"] if s else None


def _mover(by: dict, users: dict[int, dict], team_level: bool) -> str:
    kind = by.get("type")
    if kind in ("trigger", "automation"):
        return f"{kind}: {by.get('title') or by.get('id')}"
    if kind == "user":
        return "agent" if team_level else f"agent: {users.get(by.get('id'), {}).get('name') or by.get('id')}"
    return kind or "unknown"


EMPATHY = re.compile(r"\b(?:sorry|apolog\w*|i understand|understandabl\w*|appreciate your patience|thank(?:s| you) for "
                     r"(?:your )?(?:patience|waiting|bearing)|frustrat\w*)", re.I)
CARD = ["tickets_solved", "frt_median_min", "frt_median_business_min", "full_resolution_median_min",
        "requester_wait_median_min", "one_touch_pct", "reopened_pct", "csat_pct"]


async def resolve_agent(zd, ref: str) -> dict[str, Any]:
    ref = (ref or "").strip()
    if not ref:
        raise ZendeskError("Provide an agent name, email or ID.")
    if ref.isdigit():
        u = (await zd.get(f"users/{ref}"))["user"]
    else:
        res = (await zd.get("users/search", query=ref)).get("users") or []
        staff = [u for u in res if u.get("role") in AGENT_ROLES]
        exact = [u for u in staff if ref.casefold() in ((u.get("name") or "").casefold(), (u.get("email") or "").casefold())]
        pool = exact or staff
        if not pool:
            raise ZendeskError(f"No agent matches {ref!r}.")
        if len(pool) > 1:
            listed = "; ".join(f"{u.get('name')} (id {u['id']})" for u in pool[:10])
            raise ZendeskError(f"{ref!r} matches {len(pool)} agents: {listed}. Call again with the ID.")
        u = pool[0]
    if u.get("role") not in AGENT_ROLES:
        raise ZendeskError(f"{u.get('name')} is an {u.get('role')}, not an agent.")
    return u


def forecast(weekly: list[float], horizon: int) -> list[dict[str, float]]:
    """Recent level (mean of the last 4 weeks) plus the history's linear trend, with an 80% range from how far
    weeks scattered around that trend. Simple on purpose: easy to explain, hard to overfit."""
    n = len(weekly)
    xs = range(n)
    mx, my = (n - 1) / 2, sum(weekly) / n
    sxx = sum((x - mx) ** 2 for x in xs) or 1.0
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, weekly)) / sxx
    recent = weekly[-4:]
    level = sum(recent) / len(recent)
    slope = max(min(slope, 0.1 * level), -0.1 * level)  # cap the trend at ±10% of the level per week
    resid = [y - (my + slope * (x - mx)) for x, y in zip(xs, weekly)]
    sd = math.sqrt(sum(r * r for r in resid) / max(n - 2, 1))
    centre = (len(recent) - 1) / 2  # the level sits mid-way through the recent weeks
    out = []
    for h in range(1, horizon + 1):
        mid = max(level + slope * (len(recent) - 1 - centre + h), 0)
        out.append({"expected": round(mid, 1), "low": round(max(mid - 1.28 * sd, 0), 1),
                    "high": round(mid + 1.28 * sd, 1)})
    return out


def register(tk: Toolkit) -> None:
    @tk.tool("read", title="Routing report")
    async def routing_report(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        filter: Filter = None,
        max_tickets_audited: Audited = 300,
        team_level: Annotated[bool, Field(description="Don't name the agents who moved tickets.")] = True,
        max_tickets: Annotated[int, Field(ge=1, le=50000)] = 20000,
    ) -> str:
        """Are tickets landing in the right place? For tickets solved in the period, compares the group each one
        first landed in with the group that solved it, and returns: how often the first group was right, the
        common transfer paths ('Tier 1 → Tier 2'), groups that receive tickets they pass on, ping-pong (a ticket
        returning to a group it left), who moves tickets (which triggers, agents or Zendesk itself), by ticket form
        and channel, and what transfers cost — first reply, resolution and CSAT for transferred vs not, and how long
        tickets took to reach their final group. Reads each sampled ticket's change history."""
        p = resolve_period(period, start, end)
        ds, groups, forms = await asyncio.gather(load_dataset(tk, p, "solved", filter, max_tickets), _groups(tk),
                                                 _forms(tk))
        sample = _sample(ds.tickets, max_tickets_audited)
        parsed = await audits_for(tk, sample)
        gname = lambda g: groups.get(g) or (f"group {g}" if g else "(no group)")  # noqa: E731

        rows = []
        for t in sample:
            pa = parsed.get(t["id"])
            if pa is None:
                continue
            path = group_path(pa, t.get("group_id"))
            gids = [x["group_id"] for x in path]
            arrived = path[-1]["at"] if path else None
            rows.append({"t": t, "path": path, "first": gids[0] if gids else None, "final": t.get("group_id"),
                         "hops": max(len(gids) - 1, 0), "pingpong": len(gids) != len(set(gids)),
                         "unrouted_at_creation": not pa["initial"].get("group_id"),
                         "to_final_min": minutes_between(t.get("created_at"), arrived) if arrived else 0.0})
        n = len(rows)
        moved = [r for r in rows if r["hops"]]
        stayed = [r for r in rows if not r["hops"]]

        paths = Counter(" → ".join(gname(x["group_id"]) for x in r["path"]) for r in moved)
        movers: Counter = Counter()
        for r in moved:
            for step in r["path"][1:]:
                movers[_mover(step["by"], ds.users, team_level)] += 1
        by_first: dict[Any, list[dict]] = defaultdict(list)
        for r in rows:
            by_first[r["first"]].append(r)
        first_rows = []
        for g, rs in sorted(by_first.items(), key=lambda kv: -len(kv[1])):
            dest = Counter(gname(r["final"]) for r in rs if r["hops"])
            first_rows.append({"first_group": gname(g), "tickets": len(rs),
                               "solved_there_pct": rate(sum(1 for r in rs if not r["hops"]), len(rs)),
                               "passed_on_to": [{"group": k, "tickets": v} for k, v in dest.most_common(5)]})

        def slice_(key) -> list[dict]:
            d: dict[str, list[dict]] = defaultdict(list)
            for r in rows:
                d[key(r["t"])].append(r)
            return sorted(({"value": k, "tickets": len(rs), "transferred_pct": rate(sum(1 for r in rs if r["hops"]),
                                                                                     len(rs))}
                           for k, rs in d.items()), key=lambda x: -x["tickets"])[:15]

        def cost(rs: list[dict]) -> dict[str, Any]:
            k = _compact(_kpis([r["t"] for r in rs], ds.metrics))
            return {"tickets": len(rs), "frt_median_min": k["frt_median_min"],
                    "full_resolution_median_min": k["full_resolution_median_min"], "csat_pct": k["csat_pct"],
                    "reopened_pct": k["reopened_pct"]}

        worst = sorted(moved, key=lambda r: (-r["hops"], -(r["to_final_min"] or 0)))[:10]
        url = f"https://{tk.settings.host}/agent/tickets/"
        return tk.out({
            "scope": {**ds.scope(), "tickets_audited": n, "note": "Based on tickets solved in the period."},
            "summary": {
                "landed_in_final_group_first_pct": rate(len(stayed), n),
                "transferred_pct": rate(len(moved), n),
                "avg_transfers_when_moved": round(sum(r["hops"] for r in moved) / len(moved), 2) if moved else 0,
                "ping_pong_tickets": sum(1 for r in rows if r["pingpong"]),
                "created_without_group_pct": rate(sum(1 for r in rows if r["unrouted_at_creation"]), n),
                "time_to_final_group_median_min": _med(r["to_final_min"] for r in moved),
            },
            "cost_of_transfers": {"not_transferred": cost(stayed), "transferred": cost(moved)},
            "top_paths": [{"path": k, "tickets": v} for k, v in paths.most_common(10)],
            "by_first_group": first_rows,
            "who_moves_tickets": [{"moved_by": k, "transfers": v} for k, v in movers.most_common(15)],
            "by_form": slice_(lambda t: forms.get(t.get("ticket_form_id")) or "(no form)"),
            "by_channel": slice_(lambda t: (t.get("via") or {}).get("channel") or "unknown"),
            "most_transferred": [{"id": r["t"]["id"], "path": " → ".join(gname(x["group_id"]) for x in r["path"]),
                                  "hours_to_final_group": round((r["to_final_min"] or 0) / 60, 1),
                                  "url": url + str(r["t"]["id"])} for r in worst],
        })

    @tk.tool("read", title="Macro report")
    async def macro_report(
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        filter: Filter = None,
        max_tickets_audited: Audited = 300,
        stale_days: Annotated[int, Field(ge=30, le=1825, description="Active macros not edited in this many days "
                                         "are listed as stale.")] = 365,
        min_uses: Annotated[int, Field(ge=1, le=100, description="Uses in the sample before a macro's outcomes "
                                       "are shown.")] = 3,
        team_level: Annotated[bool, Field(description="Leave out per-agent macro usage.")] = True,
        limit: Annotated[int, Field(ge=1, le=200)] = 20,
        max_tickets: Annotated[int, Field(ge=1, le=50000)] = 20000,
    ) -> str:
        """Which macros pull their weight: most used (Zendesk's 7- and 30-day counts), and for a sample of tickets
        solved in the period, how tickets where each macro was used turned out — CSAT, reopens, agent replies,
        one-touch, resolution time — compared with tickets where no macro was used. Also lists active macros
        unused in 30 days, macros not edited in a year, likely duplicates (same actions or near-identical
        titles), personal vs shared, and (unless team_level) who uses macros. Use it to prune, fix or promote
        macros; pair with deflection_finder to find macros worth writing."""
        p = resolve_period(period, start, end)
        page, ds = await asyncio.gather(
            tk.zd.paginate("macros", "macros", params={"include": "usage_7d,usage_30d"}, limit=5000),
            load_dataset(tk, p, "solved", filter, max_tickets))
        macros = page["macros"]
        active = [m for m in macros if m.get("active", True)]
        sample = _sample(ds.tickets, max_tickets_audited)
        parsed = await audits_for(tk, sample)
        now = utcnow()

        used_on: dict[Any, list[dict]] = defaultdict(list)
        by_agent: Counter = Counter()
        with_macro, without = [], []
        titles = {m["id"]: m.get("title") for m in macros}
        for t in sample:
            pa = parsed.get(t["id"])
            if pa is None:
                continue
            ids = {mc["macro_id"] for mc in pa["macros"]}
            for mc in pa["macros"]:
                titles.setdefault(mc["macro_id"], mc.get("title"))
                by_agent[ds.users.get(mc["by"].get("id"), {}).get("name") or "(unknown)"] += 1
            for mid in ids:
                used_on[mid].append(t)
            (with_macro if ids else without).append(t)

        def outcome(ts: list[dict]) -> dict[str, Any]:
            k = _compact(_kpis(ts, ds.metrics))
            reps = [(ds.metrics.get(t["id"]) or {}).get("replies") for t in ts]
            reps = [r for r in reps if r is not None]
            return {"tickets": len(ts), "csat_pct": k["csat_pct"], "csat_ratings": k["csat_ratings"],
                    "reopened_pct": k["reopened_pct"], "one_touch_pct": k["one_touch_pct"],
                    "replies_avg": round(sum(reps) / len(reps), 1) if reps else None,
                    "full_resolution_median_min": k["full_resolution_median_min"]}

        base = outcome(without)
        perf = []
        for mid, ts in sorted(used_on.items(), key=lambda kv: -len(kv[1])):
            if len(ts) < min_uses:
                continue
            o = outcome(ts)
            flags = []
            if o["csat_pct"] is not None and base["csat_pct"] is not None and o["csat_ratings"] >= 3 \
                    and o["csat_pct"] <= base["csat_pct"] - 10:
                flags.append("CSAT 10+ pts below tickets without macros")
            if (o["reopened_pct"] or 0) >= max(15, (base["reopened_pct"] or 0) + 10):
                flags.append("reopened more often")
            if (o["replies_avg"] or 0) >= (base["replies_avg"] or 0) + 1:
                flags.append("more back-and-forth than usual")
            perf.append({"macro": titles.get(mid) or f"macro {mid}", "id": mid, **o, **({"flags": flags} if flags else {})})

        cutoff = now - timedelta(days=stale_days)
        unused = sorted((m for m in active if not m.get("usage_30d")), key=lambda m: m.get("updated_at") or "")
        stale = sorted((m for m in active if (parse_ts(m.get("updated_at")) or now) < cutoff),
                       key=lambda m: m.get("updated_at") or "")

        def norm_title(s: str | None) -> str:
            s = re.sub(r"\((?:copy|old|new|v\d+)\)|\bcopy( of)?\b|\bv\d+\b", " ", (s or "").lower())
            return re.sub(r"[^a-z0-9]+", " ", s).strip()

        def actions_key(m: dict) -> str:
            acts = sorted((a.get("field") or "", json.dumps(a.get("value"), sort_keys=True)) for a in m.get("actions") or [])
            return json.dumps(acts)

        dupes = []
        for key, label in ((actions_key, "same actions"), (lambda m: norm_title(m.get("title")), "near-identical titles")):
            d: dict[str, list[dict]] = defaultdict(list)
            for m in active:
                k = key(m)
                if k and k != "[]":
                    d[k].append(m)
            for ms in d.values():
                if len(ms) > 1:
                    dupes.append({"why": label, "macros": [{"id": m["id"], "title": m.get("title"),
                                                             "usage_30d": m.get("usage_30d") or 0} for m in ms]})
        brief = lambda m: {"id": m["id"], "title": m.get("title"), "updated_at": m.get("updated_at"),  # noqa: E731
                           "personal": m.get("restriction") is not None and m["restriction"].get("type") == "User"}
        n_audited = len(with_macro) + len(without)
        result: dict[str, Any] = {
            "scope": {**ds.scope(), "tickets_audited": n_audited, "macros_read": len(macros),
                      "more_macros": page["has_more"]},
            "summary": {
                "active": len(active), "inactive": len(macros) - len(active),
                "personal": sum(1 for m in active if (m.get("restriction") or {}).get("type") == "User"),
                "unused_30d": len(unused), f"not_edited_in_{stale_days}_days": len(stale),
                "duplicate_groups": len(dupes),
                "tickets_with_a_macro_pct": rate(len(with_macro), n_audited),
            },
            "most_used_30d": [{"title": m.get("title"), "id": m["id"], "usage_30d": m.get("usage_30d") or 0,
                               "usage_7d": m.get("usage_7d") or 0}
                              for m in sorted(active, key=lambda m: -(m.get("usage_30d") or 0))[:limit]
                              if m.get("usage_30d")],
            "outcomes": {"tickets_without_macros": base, "by_macro": perf[:limit],
                         "note": "Outcomes are associations, not causes: macros are used on different kinds of "
                                 "tickets. Flags point at macros worth re-reading."},
            "unused_30d": [brief(m) for m in unused[:limit]],
            "stale": [brief(m) for m in stale[:limit]],
            "likely_duplicates": dupes[:limit],
        }
        if not team_level:
            result["by_agent"] = [{"agent": a, "macro_uses": n} for a, n in by_agent.most_common(25)]
        return tk.out(result)

    @tk.tool("read", title="Agent profile")
    async def agent_profile(
        agent: Annotated[str, Field(description="Agent name, email or user ID.")],
        period: PeriodArg = None,
        start: Start = None,
        end: End = None,
        date_field: Annotated[str, Field(pattern="^(solved|created|updated)$", description="Count tickets the agent "
                                         "solved (default), or that were created/updated, in the period.")] = "solved",
        compare_previous: bool = True,
        benchmark: Annotated[bool, Field(description="Compare with the whole team's tickets in the same period.")] = True,
        challenging: Annotated[int, Field(ge=0, le=20)] = 5,
        reply_samples: Annotated[int, Field(ge=0, le=30, description="Tickets whose conversation is read to quote "
                                            "the agent's replies (1 API call each).")] = 6,
        redact: Annotated[bool, Field(description="Strip customer names, emails, links and long numbers from text.")] = True,
        max_tickets: Annotated[int, Field(ge=1, le=50000)] = 5000,
    ) -> str:
        """One agent's work for a 1:1 or coaching conversation: tickets and share of the team's volume, a
        scorecard (first reply, resolution, customer wait, one-touch, reopens, CSAT) vs their previous period and
        vs the team, strengths and focus areas, their most challenging tickets, CSAT comments, their open queue
        (stale and oldest), groups, and a sample of their actual replies with reply length, empathy language and
        promises kept. Meant for the agent's manager — not for company-wide sharing."""
        u = await resolve_agent(tk.zd, agent)
        uid = u["id"]
        p = resolve_period(period, start, end)
        scope = f"assignee:{uid}"
        jobs: dict[str, Any] = {"ds": load_dataset(tk, p, date_field, scope, max_tickets),
                                "backlog": compute_backlog(tk.zd, scope, "none", 7, 2000),
                                "memberships": tk.zd.paginate(f"users/{uid}/group_memberships", "group_memberships",
                                                              limit=200),
                                "groups": _groups(tk)}
        if compare_previous:
            jobs["prev"] = load_dataset(tk, p.previous(), date_field, scope, max_tickets)
        if benchmark:
            jobs["team"] = load_dataset(tk, p, date_field, None, 20000)
        done = dict(zip(jobs, await asyncio.gather(*jobs.values())))
        ds = done["ds"]
        mine = [t for t in ds.tickets if t.get("assignee_id") == uid] or ds.tickets
        prev = done.get("prev")
        team = done.get("team")
        now = utcnow()
        names = [x.get("name") for x in ds.users.values() if x.get("role") == "end-user"]
        red = Redactor(n for n in names if n) if redact else no_redact

        def vals(ts: list[dict], metrics: dict) -> dict[str, Any]:
            k = _kpis(ts, metrics)
            c = _compact(k)
            return {"tickets_solved": c["solved"], "frt_median_min": c["frt_median_min"],
                    "frt_median_business_min": c["frt_median_business_min"],
                    "full_resolution_median_min": c["full_resolution_median_min"],
                    "requester_wait_median_min": k["requester_wait_time_min"]["calendar"].get("median"),
                    "one_touch_pct": c["one_touch_pct"], "reopened_pct": c["reopened_pct"], "csat_pct": c["csat_pct"]}

        cur = vals(mine, ds.metrics)
        pv = vals([t for t in prev.tickets if t.get("assignee_id") == uid] or prev.tickets, prev.metrics) if prev else {}
        tv = vals(team.tickets, team.metrics) if team else {}
        card = []
        strengths, focus = [], []
        for key in CARD:
            row = {"metric": key, "label": METRICS[key]["label"], "unit": METRICS[key]["unit"],
                   **compare(key, cur[key], pv.get(key))}
            if team and key != "tickets_solved":
                row["team"] = tv.get(key)
                vs = compare(key, cur[key], tv.get(key))
                if vs.get("trend") in ("improved", "worsened") and abs(vs.get("change_pct") or 0) >= 10:
                    row["vs_team"] = "better" if vs["trend"] == "improved" else "worse"
                    (strengths if vs["trend"] == "improved" else focus).append(
                        f"{METRICS[key]['label']}: {cur[key]} vs team {tv.get(key)}")
            card.append(row)

        hard = sorted(((difficulty(t, ds.metrics.get(t["id"]), now), t) for t in mine), key=lambda x: -x[0][0])
        hard = [(s, why, t) for (s, why), t in hard if s > 0][:challenging]
        url = f"https://{tk.settings.host}/agent/tickets/"
        rated = [t for t in sorted(mine, key=lambda t: t.get("updated_at") or "", reverse=True)
                 if (t.get("satisfaction_rating") or {}).get("comment")]

        pick, seen = [], set()
        for t in [t for _, _, t in hard] + sorted(mine, key=lambda t: t.get("updated_at") or "", reverse=True):
            if t["id"] not in seen and len(pick) < reply_samples:
                seen.add(t["id"])
                pick.append(t)

        async def convo(t: dict):
            try:
                page = await tk.zd.paginate(f"tickets/{t['id']}/comments", "comments", params={"include": "users"},
                                            limit=500)
            except ZendeskError:
                return t, [], {}
            return t, page["comments"], {x["id"]: x.get("role") for x in page.get("users", [])}

        convs = await _gather([(lambda t=t: convo(t)) for t in pick])
        lengths, empathic, replies_n, quotes, promises, cust_tone = [], 0, 0, [], [], Counter()
        for t, comments, roles in convs:
            own = [c for c in comments if c.get("author_id") == uid and c.get("public", True)]
            for c in own:
                text = (c.get("plain_body") or c.get("body") or "").strip()
                replies_n += 1
                lengths.append(len(text.split()))
                empathic += bool(EMPATHY.search(text))
            if own:
                text = re.sub(r"\s+", " ", own[-1].get("plain_body") or own[-1].get("body") or "")
                quotes.append({"ticket_id": t["id"], "subject": red(t.get("subject")), "reply": red(text[:500])})
            promises += track_promises(t, comments, roles, ZoneInfo("UTC"), now,
                                       datetime.combine(p.start, datetime.min.time(), timezone.utc))
            for c in comments:
                if roles.get(c.get("author_id")) == "end-user" and c.get("public", True):
                    s = score_message(c.get("plain_body") or c.get("body") or "")
                    cust_tone[tone(s["negative"], s["positive"])] += 1
        mine_promises = [x for x in promises if x.get("author_id") == uid]
        kept = sum(1 for x in mine_promises if x["status"] == "kept")
        late = sum(1 for x in mine_promises if x["status"] == "late")
        bl = done["backlog"]
        gmap = done["groups"]
        result: dict[str, Any] = {
            "agent": {"id": uid, "name": u.get("name"), "role": u.get("role"),
                      "groups": [gmap.get(m.get("group_id")) for m in done["memberships"]["group_memberships"]],
                      "last_login_at": u.get("last_login_at")},
            "scope": {**ds.scope(), "previous_period": prev.period.as_dict() if prev else None,
                      "generated_at": now.isoformat(), "units": "minutes"},
            "volume": {"tickets": len(mine), "previous": len([t for t in prev.tickets if t.get("assignee_id") == uid])
                       if prev else None, "share_of_team_pct": rate(len(mine), len(team.tickets)) if team else None,
                       "priority": dict(Counter(t.get("priority") or "(none)" for t in mine).most_common()),
                       "channels": dict(Counter((t.get("via") or {}).get("channel") or "unknown"
                                                for t in mine).most_common())},
            "scorecard": card,
            "strengths": strengths, "focus_areas": focus,
            "open_queue": {k: bl[k] for k in ("unsolved_total", "by_status", "age_since_created", "stale") if k in bl}
            | {"oldest": [{**r, "subject": red(r.get("subject"))} for r in bl.get("oldest", [])[:5]]},
            "challenging_tickets": [{"id": t["id"], "subject": red(t.get("subject")), "status": t.get("status"),
                                     "difficulty": s, "why": why, "url": url + str(t["id"])} for s, why, t in hard],
            "csat_comments": [{"ticket_id": t["id"], "score": t["satisfaction_rating"]["score"],
                               "comment": red(t["satisfaction_rating"]["comment"])} for t in rated[:8]],
            "replies": {"tickets_read": len(convs), "replies_read": replies_n,
                        "words_median": summarize(lengths)["median"] if lengths else None,
                        "empathy_language_pct": rate(empathic, replies_n),
                        "promises": {"made": len(mine_promises), "kept_on_time": kept, "late": late,
                                     "overdue": sum(1 for x in mine_promises if x["status"] == "overdue")},
                        "customer_tone_in_these_tickets": dict(cust_tone),
                        "samples": quotes},
            "definitions": definitions(CARD),
        }
        return tk.out(result)


    @tk.tool("read", title="Capacity forecast")
    async def capacity_forecast(
        history_weeks: Annotated[int, Field(ge=6, le=52, description="Complete past weeks used for the forecast.")] = 16,
        horizon_weeks: Annotated[int, Field(ge=1, le=12, description="Weeks ahead to forecast, starting with this "
                                            "one.")] = 6,
        filter: Filter = None,
        per_agent: Annotated[bool, Field(description="Also show each agent's recent solves per week (1 API call "
                                         "per agent).")] = True,
        heatmap_weeks: Annotated[int, Field(ge=0, le=12, description="Recent weeks used for the busiest day/hour "
                                            "slots (0 to skip).")] = 4,
        time_zone: Annotated[str, Field(description="IANA time zone for day/hour slots.")] = "UTC",
    ) -> str:
        """Will the team keep up? Uses weekly created and solved counts for the past `history_weeks` to forecast
        new tickets for the coming weeks (recent level + trend, with an 80% range), compares that with how many
        tickets the team has actually been solving per week, and projects the open backlog week by week — flagging
        the weeks where demand is likely to outrun capacity. Also shows each agent's recent solves per week and the
        busiest day/hour slots with their first reply times, for scheduling cover."""
        now = utcnow()
        today = now.date()
        this_monday = today - timedelta(days=today.weekday())
        weeks = [this_monday - timedelta(weeks=k) for k in range(history_weeks, 0, -1)]
        extra = f" {filter.strip()}" if filter else ""

        async def count(q: str) -> int:
            return (await tk.zd.get("search/count", query=f"type:ticket {q}{extra}")).get("count") or 0

        calls = []
        for w in weeks:
            a, b = w.isoformat(), (w + timedelta(days=7)).isoformat()
            calls += [(lambda a=a, b=b: count(f"created>={a} created<{b}")),
                      (lambda a=a, b=b: count(f"solved>={a} solved<{b}"))]
        calls.append(lambda: count("status<solved"))
        counts = await _gather(calls)
        backlog_now = counts[-1]
        created = [float(c) for c in counts[0:-1:2]]
        solved = [float(c) for c in counts[1:-1:2]]
        fc = forecast(created, horizon_weeks)
        recent_solved = solved[-4:]
        capacity = round(sum(recent_solved) / len(recent_solved), 1)
        peak = max(solved) if solved else 0

        proj, backlog, backlog_high, tight = [], float(backlog_now), float(backlog_now), []
        for h, f in enumerate(fc):
            ws = this_monday + timedelta(weeks=h)
            backlog = max(backlog + f["expected"] - capacity, 0)
            backlog_high = max(backlog_high + f["high"] - capacity, 0)
            row = {"week_starting": ws.isoformat(), "new_tickets": f, "capacity": capacity,
                   "utilisation_pct": rate(f["expected"], capacity) if capacity else None,
                   "backlog_end_expected": round(backlog), "backlog_end_if_busy": round(backlog_high)}
            if f["expected"] > capacity:
                row["flag"] = "demand above capacity"
                tight.append(ws.isoformat())
            elif f["high"] > capacity:
                row["flag"] = "at risk if busy"
            proj.append(row)

        result: dict[str, Any] = {
            "as_of": now.isoformat(), "filter": filter,
            "summary": {
                "new_tickets_per_week_recent": round(sum(created[-4:]) / len(created[-4:]), 1),
                "solved_per_week_recent": capacity, "solved_per_week_peak": peak,
                "trend": "rising" if fc[-1]["expected"] > fc[0]["expected"] * 1.05 else
                         "falling" if fc[-1]["expected"] < fc[0]["expected"] * 0.95 else "flat",
                "backlog_now": backlog_now, "backlog_in_%d_weeks" % horizon_weeks: proj[-1]["backlog_end_expected"],
                "weeks_demand_above_capacity": tight,
            },
            "history": [{"week_starting": w.isoformat(), "created": int(c), "solved": int(s), "net": int(c - s)}
                        for w, c, s in zip(weeks, created, solved)],
            "forecast": proj,
            "method": "Expected new tickets = mean of the last 4 complete weeks + the linear trend over the history "
                      "(capped at ±10% a week); the range is ±1.28 standard deviations of past weeks around that trend "
                      "(about 80%). Capacity = tickets solved per week over the last 4 weeks. Holidays, launches and "
                      "seasonality aren't modelled — adjust for known events.",
        }
        jobs: dict[str, Any] = {}
        if per_agent:
            jobs["agents"] = tk.zd.paginate("users", "users", params={"role[]": ["agent", "admin"]}, limit=200)
        if heatmap_weeks:
            hs = this_monday - timedelta(weeks=heatmap_weeks)
            jobs["ds"] = load_dataset(tk, Period(hs, this_monday, f"last {heatmap_weeks} weeks"), "created", filter,
                                      20000)
        done = dict(zip(jobs, await asyncio.gather(*jobs.values())))
        if "agents" in done:
            agents = [a for a in done["agents"]["users"] if a.get("active", True) and not a.get("suspended")]
            since = (this_monday - timedelta(weeks=4)).isoformat()
            solves = await _gather([(lambda a=a: count(f"assignee:{a['id']} solved>={since} solved<"
                                                       f"{this_monday.isoformat()}")) for a in agents[:25]])
            rows = sorted(({"agent": a.get("name"), "solved_per_week": round(n / 4, 1)}
                           for a, n in zip(agents, solves) if n), key=lambda r: -r["solved_per_week"])
            result["agents"] = {"active_solvers": len(rows), "per_agent": rows,
                                "note": "Solves per week over the last 4 weeks, by current assignee."}
            if rows and capacity:
                result["summary"]["tickets_per_agent_per_week_needed"] = round(
                    max(f["expected"] for f in fc) / len(rows), 1)
        if "ds" in done and done["ds"].tickets:
            hm = compute_heatmap(done["ds"], time_zone, "created")
            hm.pop("_rows", None)
            result["busiest_slots"] = {"time_zone": time_zone, "weeks": heatmap_weeks, "peaks": hm["peaks"],
                                       "by_day": hm["by_day"],
                                       "frt_median_by_hour": [r for r in hm.get("frt_median_by_creation_hour", [])
                                                              if r["tickets"]]}
        return tk.out(result)
