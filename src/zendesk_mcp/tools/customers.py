"""Customer profiles: everything about one organization's support relationship in a single call —
who they are, how much they contact us, how fast and well we respond, their hardest tickets, and how
they feel about us (CSAT, any AI sentiment field, and the tone of their own messages).

Built on the same dataset, KPI, topic, backlog and SLA code as the reporting tools, so the numbers agree."""

import asyncio
import re
from collections import Counter, defaultdict
from datetime import date
from typing import Annotated, Any

from pydantic import Field

from ..client import ZendeskClient, ZendeskError
from ..metrics import analyze_conversation, parse_ts, rate, summarize, utcnow
from ..periods import PERIOD_HELP, bucket_start, buckets, resolve_period
from ..targets import METRICS, compare, definitions
from ..toolkit import Toolkit
from .analytics import Dataset, _compact, _gather, _kpis, compute_backlog, compute_sla, load_dataset
from .reporting import _redactor_for, _tz, compute_topics, field_catalog, headline_values

SCORECARD = ["tickets_created", "tickets_solved", "frt_median_min", "frt_median_business_min",
             "full_resolution_median_min", "requester_wait_median_min", "one_touch_pct", "reopened_pct", "csat_pct",
             "csat_response_rate_pct"]

# ----------------------------------------------------------------------------- tone lexicon
# A transparent heuristic, not a model: cues are counted in the customer's own public messages and the
# matching excerpts are returned so the LLM (or a person) can judge the actual tone.

# (pattern, weight, label shown in top_cues)
NEGATIVE_CUES: list[tuple[str, float, str]] = [
    (r"frustrat\w*", 2, "frustrated"), (r"disappoint\w*", 2, "disappointed"),
    (r"unacceptable|not acceptable", 3, "unacceptable"), (r"ridiculous", 2.5, "ridiculous"),
    (r"terrible|awful|horrible|worst", 2.5, "terrible/awful"), (r"angry|furious|upset", 2.5, "angry/upset"),
    (r"annoy\w*|fed up", 2, "annoyed"),
    (r"still (?:not|no|broken|waiting|having|seeing|getting)", 2, "still not fixed/waiting"),
    (r"no (?:response|reply|update)s?", 2, "no response"),
    (r"(?:haven't|have not|never) (?:heard|received|gotten)", 2, "haven't heard back"),
    (r"any updates?\b", 1, "any update?"), (r"following up|follow(?:ing)?-up", 1, "following up"),
    (r"escalat\w*", 2, "escalate"), (r"cancel\w*|churn\w*|switch(?:ing)? (?:to|vendors?|providers?)", 3,
                                     "cancel/switch"),
    (r"refund", 1.5, "refund"), (r"waste of|wasting", 2, "waste of time"),
    (r"useless|pointless", 2.5, "useless"), (r"(?:second|third|fourth|multiple|several) times?", 1.5,
                                              "asked multiple times"),
    (r"for (?:days|weeks|months)", 1.5, "for days/weeks"), (r"blocker|blocking us|blocked", 1.5, "blocked"),
    (r"urgent(?:ly)?|asap|critical", 1, "urgent/asap"),
    (r"(?:no ?one|nobody) (?:has|is|seems)", 2, "nobody helping"),
    (r"losing (?:money|customers|data|business)", 3, "losing money/data"),
]
POSITIVE_CUES: list[tuple[str, float, str]] = [
    (r"thank you so much|thanks so much|many thanks|really appreciate", 2, "big thanks"),
    (r"appreciate\w*", 1.5, "appreciate"),
    (r"great|awesome|excellent|fantastic|amazing|brilliant|wonderful", 1.5, "praise"),
    (r"perfect(?:ly)?", 1.5, "perfect"), (r"helpful", 1, "helpful"), (r"love", 1.5, "love"),
    (r"(?:works|working) (?:now|great|perfectly)", 2, "works now"),
    (r"that (?:worked|fixed|did) it|problem solved|all good now", 2, "that fixed it"),
    (r"thanks?\b|thank you", 0.5, "thanks"),
]
_NEG = [(re.compile(rf"\b(?:{p})", re.I), w, label) for p, w, label in NEGATIVE_CUES]
_POS = [(re.compile(rf"\b(?:{p})", re.I), w, label) for p, w, label in POSITIVE_CUES]
_QUOTED = re.compile(r"\n(?:On .{5,200}wrote:|-{2,} ?Original Message|From: .+\nSent: ).*", re.S | re.I)
_SHOUT = re.compile(r"\b[A-Z]{4,}\b")


def score_message(text: str) -> dict[str, Any]:
    """Score one customer message: negative and positive cue weights, plus intensity (!!, shouting)."""
    text = _QUOTED.sub("", text or "")
    neg, pos, cues = 0.0, 0.0, Counter()
    for rx, w, label in _NEG:
        n = len(rx.findall(text))
        if n:
            neg += w * min(n, 2)
            cues[label] += n
    for rx, w, label in _POS:
        n = len(rx.findall(text))
        if n:
            pos += w * min(n, 2)
            cues["+" + label] += n
    shouting = len(_SHOUT.findall(text)) >= 3
    if "!!" in text or shouting:
        neg += 1
        cues["!! / caps"] += 1
    return {"negative": neg, "positive": pos, "cues": cues, "text": text}


def tone(neg: float, pos: float) -> str:
    if neg >= 2 and neg > pos:
        return "negative"
    if pos >= 1.5 and pos > neg:
        return "positive"
    return "neutral"


# ----------------------------------------------------------------------------- lookups

async def resolve_org(zd: ZendeskClient, ref: str) -> dict[str, Any]:
    """Organization by ID, exact name, unique name prefix, or unique search hit. Ambiguity is an error
    that lists the candidates, so the caller can pick one by ID."""
    ref = (ref or "").strip()
    if not ref:
        raise ZendeskError("Provide an organization name or ID.")
    if ref.isdigit():
        return (await zd.get(f"organizations/{ref}"))["organization"]
    cands: list[dict] = []
    if len(ref) >= 2:
        page = await zd.paginate("organizations/autocomplete", "organizations", params={"name": ref}, limit=50,
                                 cursor=False)
        cands = page["organizations"]
    exact = [o for o in cands if (o.get("name") or "").casefold() == ref.casefold()]
    if len(exact) == 1:
        return exact[0]
    if not cands:  # autocomplete is prefix-only; fall back to full-text search ("Acme" finds "The Acme Co")
        res = await zd.get("search", query=f'type:organization "{ref}"', per_page=50)
        cands = [o for o in res.get("results") or [] if o.get("result_type", "organization") == "organization"]
        exact = [o for o in cands if (o.get("name") or "").casefold() == ref.casefold()]
        if len(exact) == 1:
            return exact[0]
    if len(cands) == 1:
        return cands[0]
    if not cands:
        raise ZendeskError(f"No organization matches {ref!r}. Try search_organizations or a different spelling.")
    listed = "; ".join(f"{o.get('name')} (id {o['id']})" for o in (exact or cands)[:15])
    raise ZendeskError(f"{ref!r} matches {len(exact or cands)} organizations: {listed}. "
                       "Call again with the organization ID.")


async def _org_context(zd: ZendeskClient, org: dict) -> dict[str, Any]:
    """Account facts: custom organization fields (labelled), tags, domains, related counts."""
    async def fields():
        try:
            return (await zd.paginate("organization_fields", "organization_fields", limit=500))["organization_fields"]
        except ZendeskError:
            return []

    async def related():
        try:
            return (await zd.get(f"organizations/{org['id']}/related")).get("organization_related") or {}
        except ZendeskError:
            return {}

    defs, rel = await asyncio.gather(fields(), related())
    labels = {f.get("key"): f.get("title") for f in defs}
    options = {f.get("key"): {o.get("value"): o.get("name") for o in f.get("custom_field_options") or []}
               for f in defs}
    values = {}
    for key, val in (org.get("organization_fields") or {}).items():
        if val in (None, "", [], False):
            continue
        values[labels.get(key) or key] = options.get(key, {}).get(val, val)
    out = {
        "id": org["id"], "name": org.get("name"), "external_id": org.get("external_id"),
        "customer_since": org.get("created_at"), "tags": org.get("tags") or None,
        "domains": org.get("domain_names") or None, "details": org.get("details") or None,
        "notes": org.get("notes") or None, "fields": values or None,
        "users": rel.get("users_count"), "tickets_all_time": rel.get("tickets_count"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _only_org(ds: Dataset, org_id: int) -> Dataset:
    """Keep this organization's tickets. Search scoping does the real filtering; this guards against a search
    term matching more than intended."""
    keep = [t for t in ds.tickets if t.get("organization_id") == org_id]
    if not keep and not any(t.get("organization_id") for t in ds.tickets):
        keep = ds.tickets  # tickets came back without organization_id; trust the search scope
    return Dataset(ds.period, ds.date_field, ds.filter, keep, ds.metrics, ds.users, ds.groups, ds.total,
                   ds.max_tickets)


def _mix(tickets: list[dict], key) -> list[dict[str, Any]]:
    c = Counter(key(t) for t in tickets)
    return [{"value": k, "tickets": v, "share_pct": rate(v, len(tickets))} for k, v in c.most_common()]


# ----------------------------------------------------------------------------- challenging tickets

def difficulty(t: dict, m: dict | None, now) -> tuple[float, list[str]]:
    """Score how hard a ticket was for the customer, with the reasons in words."""
    m = m or {}
    score, why = 0.0, []
    replies = m.get("replies") or 0
    if replies >= 4:
        score += replies
        why.append(f"{replies} agent replies")
    reopens = m.get("reopens") or 0
    if reopens:
        score += 4 * reopens
        why.append(f"reopened {reopens}×")
    groups = (m.get("group_stations") or 1) - 1
    if groups > 0:
        score += 2 * groups
        why.append(f"moved between groups {groups}×")
    if (t.get("satisfaction_rating") or {}).get("score") == "bad":
        score += 6
        why.append("rated bad")
    if t.get("priority") in ("urgent", "high"):
        score += 4 if t["priority"] == "urgent" else 2
        why.append(f"{t['priority']} priority")
    full = ((m.get("full_resolution_time_in_minutes") or {}).get("calendar"))
    if full and full >= 7 * 1440:
        score += min(full / 1440, 30) / 3
        why.append(f"took {round(full / 1440)} days to resolve")
    if t.get("status") in ("new", "open", "pending", "hold"):
        age = (now - parse_ts(t["created_at"])).days if t.get("created_at") else 0
        if age >= 7:
            score += 3 + min(age, 60) / 10
            why.append(f"unsolved for {age} days")
    if t.get("problem_id"):
        score += 1
        why.append(f"linked to problem #{t['problem_id']}")
    return round(score, 1), why


# ----------------------------------------------------------------------------- sentiment

async def _read_conversations(tk: Toolkit, tickets: list[dict]) -> list[tuple[dict, list[dict], dict[int, str]]]:
    async def one(t: dict):
        page = await tk.zd.paginate(f"tickets/{t['id']}/comments", "comments", params={"include": "users"},
                                    limit=500)
        return t, page["comments"], {u["id"]: u.get("role") for u in page.get("users", [])}
    return await _gather([(lambda t=t: one(t)) for t in tickets])


def _sentiment(ds: Dataset, prev: Dataset | None, catalog: dict[int, dict],
               convs: list[tuple[dict, list[dict], dict[int, str]]], red, excerpt_chars: int) -> dict[str, Any]:
    k = _kpis(ds.tickets, ds.metrics)["csat"]
    csat: dict[str, Any] = {"good": k["good"], "bad": k["bad"], "score_pct": k["score_pct"],
                            "response_rate_pct": k["response_rate_pct"]}
    if prev is not None:
        csat["previous_score_pct"] = _kpis(prev.tickets, prev.metrics)["csat"]["score_pct"]
    rated = [t for t in ds.tickets if (t.get("satisfaction_rating") or {}).get("score") in ("good", "bad")]
    reasons = Counter((t["satisfaction_rating"].get("reason") or "No reason given") for t in rated
                      if t["satisfaction_rating"]["score"] == "bad")
    if reasons:
        csat["bad_reasons"] = [{"reason": r, "count": n} for r, n in reasons.most_common()]
    comments = [t for t in sorted(rated, key=lambda t: t.get("updated_at") or "", reverse=True)
                if t["satisfaction_rating"].get("comment")]
    if comments:
        csat["comments"] = [{"ticket_id": t["id"], "score": t["satisfaction_rating"]["score"],
                             "comment": red(t["satisfaction_rating"]["comment"])} for t in comments[:8]]
    out: dict[str, Any] = {"csat": csat}

    # AI sentiment already in Zendesk (intelligent triage or an app) — a field or tags named *sentiment*.
    sfields = {i: f for i, f in catalog.items() if "sentiment" in (f.get("title") or "").lower()}
    if sfields:
        dist: Counter = Counter()
        for t in ds.tickets:
            for cf in t.get("custom_fields") or []:
                f = sfields.get(cf.get("id"))
                if f and cf.get("value") not in (None, ""):
                    dist[f"{f['title']}: {f['options'].get(cf['value'], cf['value'])}"] += 1
        if dist:
            out["sentiment_field"] = [{"value": v, "tickets": n, "share_pct": rate(n, len(ds.tickets))}
                                      for v, n in dist.most_common()]
    stags = Counter(tag for t in ds.tickets for tag in t.get("tags") or [] if "sentiment" in tag.lower())
    if stags:
        out["sentiment_tags"] = [{"tag": g, "tickets": n} for g, n in stags.most_common(10)]

    if not convs:
        return out
    per_ticket, cues, evidence = [], Counter(), []
    agent_gaps, cust_gaps = [], []
    for t, comments_, roles in convs:
        conv = analyze_conversation(comments_, roles, status=t.get("status"))
        agent_gaps += [g["minutes"] for g in conv.agent_responses]
        cust_gaps += [g["minutes"] for g in conv.customer_responses]
        neg = pos = 0.0
        worst = None
        for c in comments_:
            if roles.get(c.get("author_id")) != "end-user" or not c.get("public", True):
                continue
            s = score_message(c.get("plain_body") or c.get("body") or "")
            neg += s["negative"]
            pos += s["positive"]
            cues.update(s["cues"])
            if s["negative"] or s["positive"]:
                evidence.append({"ticket_id": t["id"], "at": c.get("created_at"), "net": s["positive"] - s["negative"],
                                 "excerpt": red(re.sub(r"\s+", " ", s["text"]).strip()[:excerpt_chars])})
            if worst is None or s["negative"] > worst:
                worst = s["negative"]
        per_ticket.append({"ticket_id": t["id"], "tone": tone(neg, pos), "negative": neg, "positive": pos})
    tones = Counter(r["tone"] for r in per_ticket)
    evidence.sort(key=lambda e: e["net"])
    out["conversation_tone"] = {
        "tickets_read": len(per_ticket),
        "tickets_by_tone": {k: tones.get(k, 0) for k in ("negative", "neutral", "positive")},
        "negative_share_pct": rate(tones.get("negative", 0), len(per_ticket)),
        "top_cues": [{"cue": c, "count": n} for c, n in cues.most_common(12)],
        "most_negative_excerpts": [e for e in evidence if e["net"] < 0][:6],
        "most_positive_excerpts": [e for e in reversed(evidence) if e["net"] > 0][:4],
        "by_ticket": sorted(per_ticket, key=lambda r: r["positive"] - r["negative"]),
        "method": "Keyword/phrase cues in the customer's own public messages (quoted email history removed): "
                  "frustration, repeated chasing, escalation or churn language, '!!'/shouting vs thanks and praise. "
                  "It's a heuristic — read the excerpts before characterising the relationship.",
    }
    out["reply_pace_in_sampled_tickets"] = {
        "our_response_min": summarize(agent_gaps) or {"n": 0},
        "their_response_min": summarize(cust_gaps) or {"n": 0},
        "definition": METRICS["agent_response_median_min"]["definition"],
    }
    return out


# ----------------------------------------------------------------------------- signals

def _signals(card: dict[str, dict], volume: dict, sentiment: dict, backlog: dict | None, sla: dict | None,
             open_hot: int) -> list[dict[str, str]]:
    sig: list[dict[str, str]] = []
    add = lambda kind, text: sig.append({"type": kind, "signal": text})  # noqa: E731
    v = volume["vs_previous"]
    if v.get("change_pct") is not None and max(v["current"] or 0, v["previous"] or 0) >= 5:
        if v["change_pct"] >= 25:
            add("watch", f"Ticket volume up {v['change_pct']}% vs the previous period ({v['previous']} → {v['current']}).")
        elif v["change_pct"] <= -25:
            add("positive", f"Ticket volume down {abs(v['change_pct'])}% vs the previous period.")
    c = card.get("csat_pct", {})
    ratings = (sentiment.get("csat") or {})
    n_rated = (ratings.get("good") or 0) + (ratings.get("bad") or 0)
    if c.get("current") is not None and n_rated >= 3:
        if c.get("previous") is not None and c["current"] - c["previous"] <= -10:
            add("risk", f"CSAT fell {round(c['previous'] - c['current'], 1)} pts to {c['current']}%.")
        if c.get("all_customers") is not None and c["current"] - c["all_customers"] <= -10:
            add("risk", f"CSAT {c['current']}% is {round(c['all_customers'] - c['current'], 1)} pts below the "
                        "all-customer average.")
        if c["current"] >= 90:
            add("positive", f"CSAT is strong at {c['current']}% ({n_rated} ratings).")
    if ratings.get("bad"):
        add("watch", f"{ratings['bad']} bad CSAT rating(s) in the period.")
    f = card.get("frt_median_min", {})
    if f.get("trend") == "worsened" and (f.get("change_pct") or 0) >= 25:
        add("watch", f"First reply median slowed {f['change_pct']}% vs the previous period.")
    if f.get("all_customers") and f.get("current") and f["current"] >= 1.25 * f["all_customers"]:
        add("watch", "First replies to this customer are slower than the all-customer median.")
    r = card.get("reopened_pct", {})
    solved = (card.get("tickets_solved") or {}).get("current") or 0
    if (r.get("current") or 0) >= 15 and solved >= 5:  # small samples make rates noisy
        add("risk", f"{r['current']}% of solved tickets were reopened.")
    if open_hot:
        add("risk", f"{open_hot} urgent/high-priority ticket(s) are open right now.")
    if backlog and backlog.get("stale", {}).get("count"):
        add("watch", f"{backlog['stale']['count']} unsolved ticket(s) without an update in "
                     f"{backlog['stale']['days_without_update']}+ days.")
    tone_ = sentiment.get("conversation_tone")
    if tone_ and tone_["tickets_read"] >= 5 and (tone_["negative_share_pct"] or 0) >= 30:
        add("risk", f"Frustration language in {tone_['negative_share_pct']}% of the conversations read.")
    if sla and sla["overall"]["achievement_pct"] is not None and sla["overall"]["achieved"] + sla["overall"]["breached"] >= 3:
        pct = sla["overall"]["achievement_pct"]
        if pct < 90:
            add("risk", f"SLA achievement on their tickets is {pct}%.")
        elif pct >= 98:
            add("positive", f"SLA achievement on their tickets is {pct}%.")
    order = {"risk": 0, "watch": 1, "positive": 2}
    return sorted(sig, key=lambda s: order[s["type"]])


# ----------------------------------------------------------------------------- tool

def register(tk: Toolkit) -> None:
    zd = tk.zd

    @tk.tool("read", title="Customer profile")
    async def customer_profile(
        organization: Annotated[str, Field(description="Organization name (exact, prefix or keywords) or ID.")],
        period: Annotated[str | None, Field(description=PERIOD_HELP + " Default: last_90_days.")] = None,
        start: Annotated[str | None, Field(description="Start date YYYY-MM-DD (inclusive). Overrides period.")] = None,
        end: Annotated[str | None, Field(description="End date YYYY-MM-DD (exclusive). Overrides period.")] = None,
        compare_previous: Annotated[bool, Field(description="Compare with the previous equivalent period.")] = True,
        benchmark: Annotated[bool, Field(description="Also compute all-customer figures for the period, to show how "
                                         "this customer compares (≈1 API call per 100 tickets account-wide; "
                                         "cached for 10 minutes).")] = True,
        challenging: Annotated[int, Field(ge=0, le=25, description="How many of their hardest tickets to list.")] = 5,
        conversations: Annotated[int, Field(ge=0, le=100, description="Tickets whose full conversation is read for "
                                            "tone and reply pace (1 API call each): their hardest tickets first, "
                                            "then the most recent.")] = 15,
        include_sla: Annotated[bool, Field(description="SLA achievement on their tickets (reads the account's SLA "
                                           "events for the period).")] = True,
        redact: Annotated[bool, Field(description="Strip names, emails, links and long numbers from ticket text and "
                                      "quotes, and list contacts as 'Contact 1, 2…'.")] = True,
        team_level: Annotated[bool, Field(description="Leave out agent names.")] = True,
        excerpt_chars: Annotated[int, Field(ge=80, le=1000)] = 280,
        time_zone: Annotated[str, Field(description="IANA time zone for the volume trend buckets.")] = "UTC",
        max_tickets: Annotated[int, Field(ge=1, le=50000)] = 5000,
    ) -> str:
        """Profile one customer (Zendesk organization): account facts and custom org fields, ticket volume and
        trend vs the previous period, channel/priority/topic mix, main contacts, our response and resolution
        performance (vs the previous period and vs all customers), open backlog, SLA achievement, their most
        challenging tickets with the reasons, and sentiment — CSAT and comments, any AI sentiment field or tags,
        and the tone of their own messages in a sample of conversations, with excerpts as evidence. `signals`
        lists the risks and positives worth leading with. Times are in minutes.
        Good for account reviews, QBR prep, escalations and renewal risk checks. If the name matches several
        organizations, the error lists them with IDs."""
        org = await resolve_org(zd, organization)
        oid = org["id"]
        p = resolve_period(period, start, end, default="last_90_days")
        scope = f"organization:{oid}"
        now = utcnow()

        jobs: dict[str, Any] = {
            "ds": load_dataset(tk, p, "created", scope, max_tickets),
            "context": _org_context(zd, org),
            "catalog": field_catalog(tk),
            "backlog": compute_backlog(zd, scope, "none", 7, 2000),
            "all_count": zd.get("search/count", query=f"type:ticket created>={p.start.isoformat()} "
                                                       f"created<{p.end.isoformat()}"),
        }
        if compare_previous:
            jobs["prev"] = load_dataset(tk, p.previous(), "created", scope, max_tickets)
        if benchmark:
            jobs["all"] = load_dataset(tk, p, "created", None, 20000)
        done = dict(zip(jobs, await asyncio.gather(*jobs.values())))
        ds = _only_org(done["ds"], oid)
        prev = _only_org(done["prev"], oid) if compare_previous else None
        red = _redactor_for(ds.users, redact)
        sla = await compute_sla(zd, p, ticket_ids={t["id"] for t in ds.tickets}) \
            if include_sla and ds.tickets else None
        notes: list[str] = []
        if ds.total is not None and ds.total > len(ds.tickets):
            notes.append(f"Analyzed {len(ds.tickets)} of {ds.total} tickets (max_tickets).")

        # ---- volume
        all_count = done["all_count"].get("count")
        interval = "week" if p.days <= 120 else "month"
        tz = _tz(time_zone)
        by_bucket: dict[date, list[dict]] = defaultdict(list)
        for t in ds.tickets:
            at = parse_ts(t.get("created_at"))
            if at:
                by_bucket[bucket_start(at.astimezone(tz).date(), interval)].append(t)
        trend = []
        for b in buckets(p, interval):
            c = _compact(_kpis(by_bucket.get(b, []), ds.metrics))
            trend.append({"period_start": b.isoformat(), "tickets": c["tickets"], "solved": c["solved"],
                          "frt_median_min": c["frt_median_min"], "csat_pct": c["csat_pct"]})
        requesters = Counter(t.get("requester_id") for t in ds.tickets if t.get("requester_id"))
        contacts = []
        for rank, (uid, n) in enumerate(requesters.most_common(8), 1):
            u = ds.users.get(uid, {})
            row = {"contact": f"Contact {rank}" if redact else (u.get("name") or f"user {uid}"), "tickets": n}
            if not redact and u.get("email"):
                row["email"] = u["email"]
            contacts.append(row)
        volume: dict[str, Any] = {
            "tickets": len(ds.tickets),
            "vs_previous": compare("tickets_created", len(ds.tickets), len(prev.tickets) if prev else None),
            "share_of_all_tickets_pct": rate(len(ds.tickets), all_count) if all_count else None,
            "per_week": round(len(ds.tickets) / max(p.days / 7, 1), 1),
            "trend": {"interval": interval, "rows": trend},
            "channels": _mix(ds.tickets, lambda t: (t.get("via") or {}).get("channel") or "unknown"),
            "priority": _mix(ds.tickets, lambda t: t.get("priority") or "(none)"),
            "type": _mix(ds.tickets, lambda t: t.get("type") or "(none)"),
            "status_now": _mix(ds.tickets, lambda t: t.get("status") or "unknown"),
            "top_contacts": contacts,
            "distinct_requesters": len(requesters),
        }
        if ds.tickets:
            volume["topics"] = compute_topics(ds, prev, done["catalog"], ["fields", "tags"], None, None, 10)

        # ---- performance
        cur_v = headline_values(ds)
        prev_v = headline_values(prev) if prev else {}
        all_v = headline_values(done["all"]) if benchmark else {}
        card: dict[str, dict] = {}
        for key in SCORECARD:
            if key not in cur_v:
                continue
            row = {"label": METRICS[key]["label"], "unit": METRICS[key]["unit"],
                   **compare(key, cur_v[key], prev_v.get(key))}
            if benchmark and key not in ("tickets_created", "tickets_solved"):
                row["all_customers"] = all_v.get(key)
                vs = compare(key, cur_v[key], all_v.get(key))
                if "trend" in vs:
                    row["vs_all_customers"] = {"improved": "better", "worsened": "worse"}.get(vs["trend"], vs["trend"])
            card[key] = row
        k = _kpis(ds.tickets, ds.metrics)
        performance: dict[str, Any] = {
            "scorecard": [{"metric": key, **row} for key, row in card.items()],
            "detail": {key: k[key] for key in ("first_reply_time_min", "full_resolution_time_min",
                                               "requester_wait_time_min", "efficiency")},
        }
        if sla:
            performance["sla"] = {"overall": sla["overall"], "by_policy_and_metric": sla["by_policy_and_metric"],
                                  "currently_breached_unfulfilled": sla["currently_breached_unfulfilled"]["count"]}
            if not sla["scope"]["complete"]:
                notes.append("SLA event export hit its page limit; SLA figures may be incomplete.")

        # ---- backlog now
        bl = done["backlog"]
        oldest = []
        for row in bl.get("oldest", []):
            row = {**row, "subject": red(row.get("subject"))}
            if team_level:
                row.pop("assignee", None)
            oldest.append(row)
        backlog = {key: bl[key] for key in ("unsolved_total", "by_status", "age_since_created", "stale", "unassigned")
                   if key in bl}
        backlog["oldest"] = oldest

        # ---- challenging tickets
        pool = {t["id"]: t for t in ds.tickets}
        scored = sorted(((difficulty(t, ds.metrics.get(t["id"]), now), t) for t in pool.values()),
                        key=lambda x: -x[0][0])
        hard = [(s, why, t) for (s, why), t in scored if s > 0][:challenging]
        agent_url = f"https://{tk.settings.host}/agent/tickets/"
        hard_rows = []
        for s, why, t in hard:
            mm = ds.metrics.get(t["id"]) or {}
            row = {"id": t["id"], "subject": red(t.get("subject")), "status": t.get("status"),
                   "priority": t.get("priority"), "created_at": t.get("created_at"),
                   "group": ds.groups.get(t.get("group_id"), {}).get("name"),
                   "replies": mm.get("replies"), "reopens": mm.get("reopens"),
                   "csat": (t.get("satisfaction_rating") or {}).get("score"),
                   "difficulty": s, "why": why, "url": agent_url + str(t["id"])}
            if not team_level:
                row["assignee"] = ds.users.get(t.get("assignee_id"), {}).get("name")
            hard_rows.append({k2: v for k2, v in row.items() if v is not None})
        problems = Counter(t["problem_id"] for t in ds.tickets if t.get("problem_id"))

        # ---- sentiment (reads conversations of the hardest, then most recent, tickets)
        pick: list[dict] = []
        if conversations:
            seen: set[int] = set()
            recent = sorted(ds.tickets, key=lambda t: t.get("created_at") or "", reverse=True)
            for t in [t for _, _, t in hard] + recent:
                if t["id"] not in seen and len(pick) < conversations:
                    seen.add(t["id"])
                    pick.append(t)
        convs = await _read_conversations(tk, pick) if pick else []
        sentiment = _sentiment(ds, prev, done["catalog"], convs, red, excerpt_chars)

        open_hot = sum(1 for t in ds.tickets if t.get("priority") in ("urgent", "high")
                       and t.get("status") in ("new", "open", "pending", "hold"))
        result: dict[str, Any] = {
            "customer": done["context"],
            "scope": {**ds.scope(), "previous_period": prev.period.as_dict() if prev else None,
                      "generated_at": now.isoformat(), "units": "minutes",
                      "privacy": {"redacted": redact, "team_level": team_level}},
            "signals": _signals(card, volume, sentiment, backlog, sla, open_hot),
            "volume": volume,
            "performance": performance,
            "backlog_now": backlog,
            "challenging_tickets": {
                "tickets": hard_rows,
                "how_ranked": "Difficulty adds up: replies (4+), reopens, group transfers, bad CSAT, urgent/high "
                              "priority, resolution over 7 days, unsolved over 7 days, links to a problem ticket.",
                "linked_problems": [{"problem_ticket_id": pid, "their_incidents": n} for pid, n in
                                    problems.most_common(5)],
            },
            "sentiment": sentiment,
            "definitions": definitions(list(card)),
        }
        if not ds.tickets:
            notes.append("No tickets from this organization in the period; try a longer period (e.g. last_year).")
        if notes:
            result["data_notes"] = notes
        return tk.out(result)
