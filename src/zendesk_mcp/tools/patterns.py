"""Patterns across tickets: issues spreading across customers right now, and repetitive work that a macro,
article or trigger could take off the team."""

import asyncio
import math
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field

from ..metrics import utcnow
from ..periods import Period
from ..textmine import key_terms, name_stopwords, ticket_text
from ..toolkit import Toolkit
from .analytics import load_dataset
from .reporting import _redactor_for, field_catalog, topic_index


def _who(t: dict) -> str:
    return f"org:{t['organization_id']}" if t.get("organization_id") else f"user:{t.get('requester_id')}"


def spikes(recent: list[dict], baseline: list[dict], recent_days: int, baseline_days: int,
           terms_of, min_tickets: int, min_customers: int) -> list[dict[str, Any]]:
    """Terms appearing in more recent tickets than the baseline rate predicts, across enough customers."""
    r_idx: dict[str, list[dict]] = defaultdict(list)
    for t in recent:
        for term in terms_of(t):
            r_idx[term].append(t)
    b_count: Counter = Counter()
    for t in baseline:
        b_count.update(terms_of(t))
    out = []
    for term, ts in r_idx.items():
        n = len(ts)
        if n < min_tickets:
            continue
        customers = {_who(t) for t in ts}
        if len(customers) < min_customers:
            continue
        expected = b_count[term] * recent_days / baseline_days
        if n < 2 * expected:
            continue
        z = (n - expected) / math.sqrt(expected + 1)
        out.append({"term": term, "tickets": ts, "recent": n, "baseline": b_count[term], "expected": expected,
                    "customers": customers, "z": z})
    return sorted(out, key=lambda s: -s["z"])


def merge_spikes(cands: list[dict], overlap: float = 0.5) -> list[dict]:
    """Terms that mostly flag the same tickets ("sso", "sso login", "saml") become one issue."""
    issues: list[dict] = []
    for c in cands:
        ids = {t["id"] for t in c["tickets"]}
        for iss in issues:
            inter = len(ids & iss["ids"])
            if inter and inter / min(len(ids), len(iss["ids"])) >= overlap:
                iss["terms"].append(c["term"])
                iss["ids"] |= ids
                iss["tickets"].update({t["id"]: t for t in c["tickets"]})
                iss["baseline"] = max(iss["baseline"], c["baseline"])
                iss["z"] = max(iss["z"], c["z"])
                break
        else:
            issues.append({"terms": [c["term"]], "ids": ids, "tickets": {t["id"]: t for t in c["tickets"]},
                           "baseline": c["baseline"], "z": c["z"]})
    return issues


def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Emerging issues")
    async def emerging_issues(
        days: Annotated[int, Field(ge=1, le=30, description="Recent window to check, ending today.")] = 7,
        baseline_days: Annotated[int, Field(ge=7, le=180, description="Comparison window just before it.")] = 28,
        filter: Annotated[str | None, Field(description="Extra Zendesk search terms, e.g. 'brand:acme' or "
                                            "'-tags:spam'.")] = None,
        min_tickets: Annotated[int, Field(ge=2, le=100, description="Minimum recent tickets for an issue.")] = 3,
        min_customers: Annotated[int, Field(ge=1, le=50, description="Minimum distinct customers (organizations, "
                                            "or requesters without one).")] = 2,
        include_fields_and_tags: Annotated[bool, Field(description="Also look for spikes in dropdown field values "
                                                       "and tags, not just ticket text.")] = True,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        redact: Annotated[bool, Field(description="Strip names, emails, links and long numbers from subjects.")] = True,
        anonymize_orgs: bool = False,
        max_tickets: Annotated[int, Field(ge=1, le=50000)] = 20000,
    ) -> str:
        """Spot problems spreading across customers before they become a pile of escalations: compares the words
        customers use (subject + opening message) and dropdown/tag values in the last `days` with the
        `baseline_days` before, and returns terms that jumped well above their normal rate across several
        customers — grouped into issues, with the customers affected, first seen, daily counts, priority mix,
        example subjects, ticket IDs and any linked problem tickets. Use after a release or when a few similar
        tickets arrive close together; follow up with search or sample_ticket_text."""
        now = utcnow()
        today = now.date()
        end = today + timedelta(days=1)
        rp = Period(end - timedelta(days=days), end, f"last {days} days")
        bp = Period(rp.start - timedelta(days=baseline_days), rp.start, f"previous {baseline_days} days")
        recent, base, catalog = await asyncio.gather(load_dataset(tk, rp, "created", filter, max_tickets),
                                                     load_dataset(tk, bp, "created", filter, max_tickets),
                                                     field_catalog(tk))
        people = name_stopwords([u.get("name") for u in {**base.users, **recent.users}.values()])
        structured: dict[int, set[str]] = defaultdict(set)
        if include_fields_and_tags:
            for ds in (recent, base):
                for label, ts in topic_index(ds, catalog, ["fields", "tags"], None, None).items():
                    for t in ts:
                        structured[t["id"]].add(label)

        cache: dict[int, set[str]] = {}

        def terms_of(t: dict) -> set[str]:
            if t["id"] not in cache:
                cache[t["id"]] = key_terms(ticket_text(t), people) | structured.get(t["id"], set())
            return cache[t["id"]]

        cands = spikes(recent.tickets, base.tickets, rp.days, bp.days, terms_of, min_tickets, min_customers)
        issues = merge_spikes(cands[:80])

        org_ids = {t["organization_id"] for i in issues for t in i["tickets"].values() if t.get("organization_id")}
        names: dict[int, str] = {}
        if org_ids and not anonymize_orgs:
            ids = sorted(org_ids)
            pages = await asyncio.gather(*[zd.get("organizations/show_many", ids=",".join(map(str, ids[i:i + 100])))
                                           for i in range(0, len(ids), 100)])
            names = {o["id"]: o.get("name") for p in pages for o in p.get("organizations", [])}
        red = _redactor_for(recent.users, redact)
        alias: dict[str, str] = {}
        rows = []
        for iss in sorted(issues, key=lambda i: -i["z"])[:limit]:
            ts = sorted(iss["tickets"].values(), key=lambda t: t.get("created_at") or "")
            per_day = Counter((t.get("created_at") or "")[:10] for t in ts)
            customers = Counter(_who(t) for t in ts)
            listed = []
            for who, n in customers.most_common(10):
                if who.startswith("org:"):
                    oid = int(who[4:])
                    label = alias.setdefault(who, f"Customer {len(alias) + 1}") if anonymize_orgs \
                        else names.get(oid, f"org {oid}")
                else:
                    label = "(no organization)"
                listed.append({"customer": label, "tickets": n})
            problems = Counter(t["problem_id"] for t in ts if t.get("problem_id"))
            structured_terms = [x for x in iss["terms"] if ": " in x]
            rows.append({
                "issue": ", ".join(iss["terms"][:4]),
                "kind": "field/tag" if structured_terms and len(structured_terms) == len(iss["terms"]) else "text",
                "recent_tickets": len(ts), "baseline_tickets": iss["baseline"],
                "per_day_now": round(len(ts) / rp.days, 2), "per_day_before": round(iss["baseline"] / bp.days, 2),
                "customers_affected": len(customers), "customers": listed,
                "first_seen": ts[0].get("created_at"), "last_seen": ts[-1].get("created_at"),
                "daily": [{"date": (rp.start + timedelta(days=i)).isoformat(),
                           "tickets": per_day.get((rp.start + timedelta(days=i)).isoformat(), 0)}
                          for i in range(rp.days)],
                "priority": dict(Counter(t.get("priority") or "(none)" for t in ts).most_common()),
                "still_open": sum(1 for t in ts if t.get("status") in ("new", "open", "pending", "hold")),
                "linked_problems": [{"problem_ticket_id": p, "incidents": n} for p, n in problems.most_common(3)],
                "examples": [{"id": t["id"], "subject": red(t.get("subject"))} for t in ts[-5:]],
                "ticket_ids": [t["id"] for t in ts][:30],
                "matched_terms": iss["terms"][:10],
            })
        return tk.out({
            "scope": {"recent": rp.as_dict(), "baseline": bp.as_dict(), "filter": filter,
                      "recent_tickets": len(recent.tickets), "baseline_tickets": len(base.tickets),
                      "as_of": now.isoformat()},
            "issues": rows,
            "how_it_works": "Each ticket's subject (weighted double) and the start of its first message are reduced to "
                            "key words and two-word phrases (names, greetings and boilerplate removed), plus its "
                            "dropdown values and tags. A term is flagged when its recent count is at least twice what "
                            "the baseline rate predicts, with enough tickets and customers; terms flagging mostly the "
                            "same tickets are merged into one issue, strongest spike first.",
            **({"note": "Nothing is spiking above the baseline right now."} if not rows else {}),
        })
