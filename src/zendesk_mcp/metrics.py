"""Pure calculation helpers for analytics: summary stats, business-hours time,
and reply-gap analysis of a ticket conversation. No I/O here, so it is easy to test."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ----------------------------------------------------------------------------- time parsing


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------------------- stats


def percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile (same method as numpy's default / Excel PERCENTILE.INC)."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def summarize(values: Iterable[float | None]) -> dict[str, Any] | None:
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    return {
        "n": len(vals),
        "mean": round(sum(vals) / len(vals), 1),
        "median": round(percentile(vals, 50), 1),
        "p90": round(percentile(vals, 90), 1),
        "min": round(vals[0], 1),
        "max": round(vals[-1], 1),
    }


def rate(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


# ----------------------------------------------------------------------------- business hours

# Zendesk schedules report Rails time zone names ("Pacific Time (US & Canada)").
# IANA names are tried first; this maps the common Rails names. Anything else can be
# supplied explicitly via the tools' `time_zone` parameter.
RAILS_TZ = {
    "Pacific Time (US & Canada)": "America/Los_Angeles",
    "Mountain Time (US & Canada)": "America/Denver",
    "Central Time (US & Canada)": "America/Chicago",
    "Eastern Time (US & Canada)": "America/New_York",
    "Arizona": "America/Phoenix",
    "Alaska": "America/Juneau",
    "Hawaii": "Pacific/Honolulu",
    "Atlantic Time (Canada)": "America/Halifax",
    "Newfoundland": "America/St_Johns",
    "Indiana (East)": "America/Indiana/Indianapolis",
    "Mexico City": "America/Mexico_City",
    "Bogota": "America/Bogota",
    "Lima": "America/Lima",
    "Santiago": "America/Santiago",
    "Buenos Aires": "America/Argentina/Buenos_Aires",
    "Brasilia": "America/Sao_Paulo",
    "UTC": "Etc/UTC",
    "London": "Europe/London",
    "Edinburgh": "Europe/London",
    "Dublin": "Europe/Dublin",
    "Lisbon": "Europe/Lisbon",
    "Amsterdam": "Europe/Amsterdam",
    "Berlin": "Europe/Berlin",
    "Brussels": "Europe/Brussels",
    "Copenhagen": "Europe/Copenhagen",
    "Madrid": "Europe/Madrid",
    "Paris": "Europe/Paris",
    "Prague": "Europe/Prague",
    "Rome": "Europe/Rome",
    "Stockholm": "Europe/Stockholm",
    "Vienna": "Europe/Vienna",
    "Warsaw": "Europe/Warsaw",
    "Zurich": "Europe/Zurich",
    "Athens": "Europe/Athens",
    "Helsinki": "Europe/Helsinki",
    "Istanbul": "Europe/Istanbul",
    "Moscow": "Europe/Moscow",
    "Jerusalem": "Asia/Jerusalem",
    "Cairo": "Africa/Cairo",
    "Johannesburg": "Africa/Johannesburg",
    "Nairobi": "Africa/Nairobi",
    "Riyadh": "Asia/Riyadh",
    "Tehran": "Asia/Tehran",
    "Karachi": "Asia/Karachi",
    "Mumbai": "Asia/Kolkata",
    "New Delhi": "Asia/Kolkata",
    "Chennai": "Asia/Kolkata",
    "Kolkata": "Asia/Kolkata",
    "Kathmandu": "Asia/Kathmandu",
    "Dhaka": "Asia/Dhaka",
    "Bangkok": "Asia/Bangkok",
    "Jakarta": "Asia/Jakarta",
    "Kuala Lumpur": "Asia/Kuala_Lumpur",
    "Singapore": "Asia/Singapore",
    "Manila": "Asia/Manila",
    "Hong Kong": "Asia/Hong_Kong",
    "Beijing": "Asia/Shanghai",
    "Taipei": "Asia/Taipei",
    "Seoul": "Asia/Seoul",
    "Tokyo": "Asia/Tokyo",
    "Perth": "Australia/Perth",
    "Adelaide": "Australia/Adelaide",
    "Brisbane": "Australia/Brisbane",
    "Melbourne": "Australia/Melbourne",
    "Sydney": "Australia/Sydney",
    "Auckland": "Pacific/Auckland",
    "Wellington": "Pacific/Auckland",
}


def resolve_tz(name: str) -> ZoneInfo:
    for candidate in (name, RAILS_TZ.get(name)):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    raise ValueError(f"Unknown time zone {name!r}; pass an IANA name like 'America/Los_Angeles' via time_zone.")


@dataclass
class BusinessHours:
    """A Zendesk schedule: weekly intervals (minutes since Sunday 00:00 local) + holiday dates."""

    tz: ZoneInfo
    intervals: list[tuple[int, int]]
    holidays: set[date] = field(default_factory=set)
    name: str = ""

    @classmethod
    def from_zendesk(cls, schedule: dict[str, Any], holidays: list[dict[str, Any]] | None = None,
                     time_zone: str | None = None) -> "BusinessHours":
        days: set[date] = set()
        for h in holidays or []:
            d0 = date.fromisoformat(h["start_date"])
            d1 = date.fromisoformat(h.get("end_date") or h["start_date"])
            while d0 <= d1:
                days.add(d0)
                d0 += timedelta(days=1)
        return cls(
            tz=resolve_tz(time_zone or schedule.get("time_zone") or "UTC"),
            intervals=[(int(i["start_time"]), int(i["end_time"])) for i in schedule.get("intervals", [])],
            holidays=days,
            name=schedule.get("name", ""),
        )

    def _day_windows(self, d: date) -> list[tuple[datetime, datetime]]:
        """Business windows for one local calendar day, as aware datetimes (DST-safe)."""
        if d in self.holidays:
            return []
        w = (d.weekday() + 1) % 7  # Zendesk weeks start on Sunday
        day_lo, day_hi = w * 1440, (w + 1) * 1440
        out = []
        for start, end in self.intervals:
            a, b = max(start, day_lo), min(end, day_hi)
            if a < b:
                base = datetime.combine(d, time(0))
                out.append(((base + timedelta(minutes=a - day_lo)).replace(tzinfo=self.tz),
                            (base + timedelta(minutes=b - day_lo)).replace(tzinfo=self.tz)))
        return out

    def minutes_between(self, start: datetime, end: datetime) -> float:
        if end <= start:
            return 0.0
        d = start.astimezone(self.tz).date() - timedelta(days=1)
        last = end.astimezone(self.tz).date() + timedelta(days=1)
        total = 0.0
        while d <= last:
            for ws, we in self._day_windows(d):
                lo, hi = max(ws, start), min(we, end)
                if lo < hi:
                    total += (hi - lo).total_seconds()
            d += timedelta(days=1)
        return total / 60


# ----------------------------------------------------------------------------- conversation analysis

AGENT_ROLES = {"agent", "admin"}
AUTOMATED_CHANNELS = {"rule", "system"}
AUTOMATED_RELS = {"trigger", "automation"}


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60


@dataclass
class Conversation:
    agent_responses: list[dict[str, Any]]  # customer waiting -> agent reply
    customer_responses: list[dict[str, Any]]  # agent reply -> customer reply
    first_response: dict[str, Any] | None
    awaiting_agent: dict[str, Any] | None
    awaiting_customer: dict[str, Any] | None
    counted: dict[str, int]


def classify(comment: dict[str, Any], roles: dict[int, str], include_internal: bool,
             include_automated: bool) -> str | None:
    """Return 'customer', 'agent' or None (ignored) for a comment."""
    role = roles.get(comment.get("author_id"))
    via = comment.get("via") or {}
    automated = via.get("channel") in AUTOMATED_CHANNELS or (via.get("source") or {}).get("rel") in AUTOMATED_RELS
    if role == "end-user":
        return "customer"
    if role in AGENT_ROLES:
        if automated and not include_automated:
            return None
        if not comment.get("public", True) and not include_internal:
            return None
        return "agent"
    return None


def analyze_conversation(
    comments: list[dict[str, Any]],
    roles: dict[int, str],
    *,
    status: str | None = None,
    hours: BusinessHours | None = None,
    include_internal: bool = False,
    include_automated: bool = False,
    now: datetime | None = None,
) -> Conversation:
    """Walk a ticket's comments in order and measure the gaps between sides.

    Agent response time: from the FIRST customer message that has not yet been answered to the next agent
    reply (consecutive customer messages don't reset the clock — the customer has been waiting since the first).
    Customer response time: from the LAST agent message to the next customer message.
    Internal notes and trigger/automation comments don't count as replies unless included."""
    now = now or utcnow()
    counted = {"customer_messages": 0, "agent_messages": 0, "ignored": 0}
    agent_resp: list[dict[str, Any]] = []
    cust_resp: list[dict[str, Any]] = []
    waiting_since: datetime | None = None
    waiting_comment: int | None = None
    last_agent_at: datetime | None = None

    def gap(a: datetime, b: datetime) -> dict[str, Any]:
        g = {"minutes": round(_minutes(a, b), 1)}
        if hours:
            g["business_minutes"] = round(hours.minutes_between(a, b), 1)
        return g

    ordered = sorted(comments, key=lambda c: (c.get("created_at") or "", c.get("id") or 0))
    for c in ordered:
        side = classify(c, roles, include_internal, include_automated)
        at = parse_ts(c.get("created_at"))
        if side is None or at is None:
            counted["ignored"] += 1
            continue
        if side == "customer":
            counted["customer_messages"] += 1
            if last_agent_at is not None:
                cust_resp.append({"agent_at": last_agent_at.isoformat(), "customer_at": at.isoformat(),
                                  **gap(last_agent_at, at)})
                last_agent_at = None
            if waiting_since is None:
                waiting_since, waiting_comment = at, c.get("id")
        else:
            counted["agent_messages"] += 1
            if waiting_since is not None:
                agent_resp.append({"customer_at": waiting_since.isoformat(), "agent_at": at.isoformat(),
                                   "agent_id": c.get("author_id"), "public": c.get("public", True),
                                   "customer_comment_id": waiting_comment, "agent_comment_id": c.get("id"),
                                   **gap(waiting_since, at)})
                waiting_since = None
            last_agent_at = at

    awaiting_agent = awaiting_customer = None
    if waiting_since is not None and status in (None, "new", "open"):
        awaiting_agent = {"since": waiting_since.isoformat(), **gap(waiting_since, now)}
    if last_agent_at is not None and status in (None, "pending", "hold"):
        awaiting_customer = {"since": last_agent_at.isoformat(), **gap(last_agent_at, now)}
    return Conversation(agent_resp, cust_resp, agent_resp[0] if agent_resp else None,
                        awaiting_agent, awaiting_customer, counted)
