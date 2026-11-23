"""How the Zendesk setup is working: routing (where tickets land vs where they're solved), macros (which get
used and whether they help), one agent's work for 1:1s, and whether the team will keep up with demand."""

import asyncio
import json
import random
import re
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field

from ..audits import audits_for, group_path, minutes_between
from ..client import ZendeskError
from ..metrics import parse_ts, rate, summarize, utcnow
from ..periods import PERIOD_HELP, resolve_period
from ..toolkit import Toolkit
from .analytics import _compact, _kpis, load_dataset

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
