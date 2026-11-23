"""How the Zendesk setup is working: routing (where tickets land vs where they're solved), macros (which get
used and whether they help), one agent's work for 1:1s, and whether the team will keep up with demand."""

import asyncio
import random
from collections import Counter, defaultdict
from typing import Annotated, Any

from pydantic import Field

from ..audits import audits_for, group_path, minutes_between
from ..client import ZendeskError
from ..metrics import rate, summarize
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
