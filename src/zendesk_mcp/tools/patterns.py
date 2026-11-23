"""Patterns across tickets: issues spreading across customers right now, and repetitive work that a macro,
article or trigger could take off the team."""

import asyncio
import math
import random
import re
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field

from ..client import ZendeskError
from ..metrics import rate, summarize, utcnow
from ..periods import PERIOD_HELP, Period, resolve_period
from ..textmine import clean, cluster, cosine, key_terms, name_stopwords, tfidf, ticket_text, tokens, top_terms
from ..toolkit import Toolkit
from .analytics import _gather, load_dataset
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


def _tf(toks: list[str]) -> dict[str, float]:
    c = Counter(toks)
    norm = math.sqrt(sum(v * v for v in c.values())) or 1.0
    return {k: v / norm for k, v in c.items()}


def answer_similarity(replies: list[str]) -> tuple[float | None, int | None]:
    """Mean pairwise cosine of agent replies, and the index of the most typical one (highest mean similarity)."""
    if len(replies) < 2:
        return None, 0 if replies else None
    vecs = [_tf(tokens(r)) for r in replies]  # plain term overlap: identical replies must score 1, not 0 (as IDF would)
    n = len(vecs)
    sims = [[cosine(vecs[i], vecs[j]) if i != j else 0.0 for j in range(n)] for i in range(n)]
    pairs = [sims[i][j] for i in range(n) for j in range(i + 1, n)]
    typical = max(range(n), key=lambda i: sum(sims[i]))
    return round(sum(pairs) / len(pairs), 2), typical


def suggest(same: bool | None, article: dict | None, macros: list[str], one_touch: float | None,
            channels: Counter, replies_avg: float | None) -> list[str]:
    out = []
    if same:
        if macros:
            out.append(f"A matching macro exists ({macros[0]!r}) — check it's being used (macro_report) and kept "
                       "up to date.")
        else:
            out.append("Agents give the same answer each time: turn the typical reply into a macro.")
        if article:
            out.append(f"Link the existing article {article['title']!r} in that reply, and in an auto-reply "
                       "trigger or AI agent answer so customers can self-serve.")
        else:
            out.append("No Help Center article covers it: publish the typical reply as an article, then link it from "
                       "the macro and an auto-reply.")
    elif same is False:
        out.append("Answers vary: write an internal troubleshooting guide, and add a ticket form field that collects "
                   "the details agents keep asking for.")
    if (one_touch or 0) >= 60:
        out.append(f"{one_touch}% are solved in one reply: a strong candidate for an automatic answer.")
    if (replies_avg or 0) >= 3:
        out.append(f"Averages {replies_avg} agent replies per ticket: collecting the right information up front "
                   "(form fields, a checklist in the auto-reply) would cut the back-and-forth.")
    web = channels.get("web", 0)
    if web and web >= 0.6 * sum(channels.values()):
        out.append("Mostly from the web form: add a dropdown for this topic so a trigger can route and auto-answer it.")
    return out


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

    @tk.tool("read", title="Deflection finder")
    async def deflection_finder(
        period: Annotated[str | None, Field(description=PERIOD_HELP + " Default: last_90_days.")] = None,
        start: Annotated[str | None, Field(description="Start date YYYY-MM-DD (inclusive). Overrides period.")] = None,
        end: Annotated[str | None, Field(description="End date YYYY-MM-DD (exclusive). Overrides period.")] = None,
        filter: Annotated[str | None, Field(description="Extra Zendesk search terms, e.g. 'group:\"Tier 1\"'.")] = None,
        min_cluster: Annotated[int, Field(ge=3, le=500, description="Smallest group of similar tickets to report.")] = 5,
        similarity: Annotated[float, Field(ge=0.2, le=0.9, description="How alike tickets must be to group "
                                           "(cosine, 0–1). Lower finds broader groups.")] = 0.45,
        limit: Annotated[int, Field(ge=1, le=30)] = 10,
        sample_replies: Annotated[int, Field(ge=0, le=20, description="Tickets per group whose first agent reply is "
                                             "read to check whether the answer is always the same (1 call each).")] = 6,
        check_help_center: bool = True,
        check_macros: bool = True,
        redact: Annotated[bool, Field(description="Strip names, emails, links and long numbers from text.")] = True,
        max_tickets: Annotated[int, Field(ge=1, le=10000)] = 5000,
    ) -> str:
        """Find repetitive work worth automating: groups solved tickets that ask the same thing (similar subjects
        and opening messages), ranks the groups by agent effort (total replies), reads a sample of each group's
        first agent reply to see whether agents give the same answer, checks the Help Center and macros for
        existing coverage, and suggests what to build — a macro, an article, an auto-reply or AI agent answer,
        or a form field. Returns the typical reply (a macro or article draft to start from), example subjects and
        ticket IDs. Default: tickets solved in the last 90 days."""
        p = resolve_period(period, start, end, default="last_90_days")
        ds = await load_dataset(tk, p, "solved", filter, max_tickets)
        people = name_stopwords(u.get("name") for u in ds.users.values())
        tickets = [t for t in ds.tickets if (t.get("subject") or t.get("description"))]
        docs = [tokens(ticket_text(t), people) for t in tickets]
        vecs = tfidf(docs)
        groups = [g for g in cluster(vecs, similarity) if len(g) >= min_cluster]
        red = _redactor_for(ds.users, redact)
        m = ds.metrics

        rows = []
        for g in groups:
            ts = [tickets[i] for i in g]
            reps = [(m.get(t["id"]) or {}).get("replies") for t in ts]
            reps = [r for r in reps if r is not None]
            full = summarize((m.get(t["id"]) or {}).get("full_resolution_time_in_minutes", {}).get("calendar")
                             for t in ts)
            good = sum(1 for t in ts if (t.get("satisfaction_rating") or {}).get("score") == "good")
            bad = sum(1 for t in ts if (t.get("satisfaction_rating") or {}).get("score") == "bad")
            rows.append({"_tickets": ts, "terms": top_terms([vecs[i] for i in g], 5),
                         "tickets": len(ts), "share_pct": rate(len(ts), len(tickets)),
                         "agent_replies_total": sum(reps),
                         "replies_avg": round(sum(reps) / len(reps), 1) if reps else None,
                         "one_touch_pct": rate(sum(1 for r in reps if r == 1), len(reps)),
                         "resolution_median_hours": round(full["median"] / 60, 1) if full else None,
                         "csat_pct": rate(good, good + bad),
                         "channels": Counter((t.get("via") or {}).get("channel") or "unknown" for t in ts)})
        rows.sort(key=lambda r: (-r["agent_replies_total"], -r["tickets"]))
        rows = rows[:limit]

        async def first_reply(t: dict) -> str | None:
            try:
                page = await zd.paginate(f"tickets/{t['id']}/comments", "comments", params={"include": "users"},
                                         limit=20)
            except ZendeskError:
                return None
            roles = {u["id"]: u.get("role") for u in page.get("users", [])}
            for c in page["comments"]:
                if roles.get(c.get("author_id")) in ("agent", "admin") and c.get("public", True):
                    return clean(c.get("plain_body") or c.get("body") or "").strip()
            return None

        async def hc(terms: list[str]):
            if not check_help_center:
                return None
            try:
                res = await zd.get("help_center/articles/search", query=" ".join(terms[:3]), per_page=3)
            except ZendeskError:
                return None
            hits = res.get("results") or []
            return {"title": hits[0].get("title"), "url": hits[0].get("html_url"),
                    "matches": res.get("count", len(hits))} if hits else None

        async def macros(terms: list[str]):
            if not check_macros:
                return []
            try:
                res = await zd.get("macros/search", query=terms[0], active="true")
            except ZendeskError:
                return []
            return [x.get("title") for x in res.get("macros") or []][:3]

        async def enrich(r: dict):
            sample = random.Random(0).sample(r["_tickets"], min(sample_replies, len(r["_tickets"])))
            replies = [x for x in await _gather([(lambda t=t: first_reply(t)) for t in sample]) if x]
            sim, typical = answer_similarity(replies)
            art, mac = await asyncio.gather(hc(r["terms"]), macros(r["terms"]))
            return replies, sim, typical, art, mac

        extra = await asyncio.gather(*(enrich(r) for r in rows))
        out_rows = []
        for r, (replies, sim, typical, art, mac) in zip(rows, extra):
            ts = r.pop("_tickets")
            same = None if sim is None else sim >= 0.5
            channels = r.pop("channels")
            row = {
                "topic": ", ".join(r["terms"][:3]), **{k: v for k, v in r.items() if k != "terms"},
                "key_terms": r["terms"],
                "channels": dict(channels.most_common()),
                "answer_consistency": {"replies_sampled": len(replies), "similarity": sim,
                                       "same_answer": same},
                "typical_reply": red(re.sub(r"\s+", " ", replies[typical])[:700]) if replies and typical is not None
                else None,
                "help_center_match": art, "matching_macros": mac or None,
                "suggestions": suggest(same, art, mac, r["one_touch_pct"], channels, r["replies_avg"]),
                "examples": [{"id": t["id"], "subject": red(t.get("subject"))} for t in ts[:5]],
                "ticket_ids": [t["id"] for t in ts][:40],
            }
            out_rows.append({k: v for k, v in row.items() if v is not None})
        grouped = sum(r["tickets"] for r in out_rows)
        return tk.out({
            "scope": {**ds.scope(), "tickets_compared": len(tickets), "similarity": similarity,
                      "min_cluster": min_cluster},
            "summary": {"groups_found": len(groups), "tickets_in_shown_groups": grouped,
                        "share_of_solved_pct": rate(grouped, len(tickets)),
                        "agent_replies_in_shown_groups": sum(r["agent_replies_total"] for r in out_rows)},
            "groups": out_rows,
            "how_it_works": "Tickets are compared on the words in their subject (weighted double) and opening "
                            "message, after removing names, greetings and boilerplate. Groups are ranked by total "
                            "agent replies, a proxy for effort. same_answer means the sampled first replies are at "
                            "least 0.5 similar on average. Treat groups as leads: skim the examples before building.",
        })
