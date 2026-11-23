"""Reporting periods: shorthands like 'last_quarter' or '2026-Q3', and the matching
previous period for comparisons. All ranges are [start, end) dates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .client import ZendeskError
from .metrics import utcnow

PERIOD_HELP = ("Reporting period: last_quarter, this_quarter, last_month, this_month, last_year, this_year, ytd, "
               "last_7_days, last_30_days, last_90_days, or a calendar unit like '2026-Q3', '2026-09', '2026'. "
               "Ignored if start/end are given.")


class PeriodError(ZendeskError):
    pass


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # exclusive
    label: str
    unit: str | None = None  # 'quarter' | 'month' | 'year' for calendar units
    partial: bool = False  # True for this_* (period still in progress)

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def previous(self) -> "Period":
        if self.unit:
            ps = _shift(self.start, self.unit, -1)
            if self.partial:
                pe = min(ps + (self.end - self.start), self.start)
                return Period(ps, pe, f"{_label(ps, self.unit)} (same {self.days} days)", self.unit, True)
            return Period(ps, self.start, _label(ps, self.unit), self.unit)
        return Period(self.start - (self.end - self.start), self.start, f"previous {self.days} days")

    def as_dict(self) -> dict[str, str]:
        return {"label": self.label, "start": self.start.isoformat(), "end_exclusive": self.end.isoformat()}


def _q_start(d: date) -> date:
    return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)


def _shift(d: date, unit: str, n: int) -> date:
    if unit == "year":
        return date(d.year + n, 1, 1)
    months = {"month": 1, "quarter": 3}[unit] * n
    idx = d.year * 12 + d.month - 1 + months
    return date(idx // 12, idx % 12 + 1, 1)


def _label(d: date, unit: str) -> str:
    if unit == "quarter":
        return f"{d.year}-Q{(d.month - 1) // 3 + 1}"
    if unit == "month":
        return f"{d.year}-{d.month:02d}"
    return str(d.year)


def resolve_period(period: str | None, start: str | None = None, end: str | None = None,
                   default: str = "last_30_days") -> Period:
    today = utcnow().date()
    tomorrow = today + timedelta(days=1)
    if start or end:
        try:
            s = date.fromisoformat(start) if start else today - timedelta(days=30)
            e = date.fromisoformat(end) if end else tomorrow
        except ValueError as exc:
            raise PeriodError(f"Dates must be YYYY-MM-DD: {exc}") from None
        if e <= s:
            raise PeriodError("end must be after start.")
        return Period(s, e, f"{s.isoformat()} to {(e - timedelta(days=1)).isoformat()}")

    p = (period or default).strip().lower()
    rolling = re.fullmatch(r"last_(\d+)_days", p)
    if rolling:
        n = int(rolling.group(1))
        return Period(tomorrow - timedelta(days=n), tomorrow, f"last {n} days")
    for unit in ("quarter", "month", "year"):
        cur = _q_start(today) if unit == "quarter" else (date(today.year, today.month, 1) if unit == "month"
                                                          else date(today.year, 1, 1))
        if p == f"last_{unit}":
            s = _shift(cur, unit, -1)
            return Period(s, cur, _label(s, unit), unit)
        if p == f"this_{unit}":
            return Period(cur, tomorrow, f"{_label(cur, unit)} to date", unit, partial=True)
    if p == "ytd":
        return Period(date(today.year, 1, 1), tomorrow, f"{today.year} YTD", "year", partial=True)
    m = re.fullmatch(r"(\d{4})-q([1-4])", p)
    if m:
        s = date(int(m.group(1)), (int(m.group(2)) - 1) * 3 + 1, 1)
        return _calendar(s, "quarter", tomorrow)
    m = re.fullmatch(r"(\d{4})-(\d{2})", p)
    if m:
        return _calendar(date(int(m.group(1)), int(m.group(2)), 1), "month", tomorrow)
    if re.fullmatch(r"\d{4}", p):
        return _calendar(date(int(p), 1, 1), "year", tomorrow)
    raise PeriodError(f"Unrecognised period {period!r}. {PERIOD_HELP}")


def _calendar(s: date, unit: str, tomorrow: date) -> Period:
    e = _shift(s, unit, 1)
    if s >= tomorrow:
        raise PeriodError(f"{_label(s, unit)} hasn't started yet.")
    if e > tomorrow:  # current, unfinished unit
        return Period(s, tomorrow, f"{_label(s, unit)} to date", unit, partial=True)
    return Period(s, e, _label(s, unit), unit)


def bucket_start(d: date, interval: str) -> date:
    if interval == "day":
        return d
    if interval == "week":
        return d - timedelta(days=d.weekday())  # ISO weeks start Monday
    return date(d.year, d.month, 1)


def auto_interval(p: Period) -> str:
    if p.days <= 14:
        return "day"
    if p.days <= 120:
        return "week"
    return "month"


def buckets(p: Period, interval: str) -> list[date]:
    out, cur = [], bucket_start(p.start, interval)
    while cur < p.end:
        out.append(cur)
        cur = cur + timedelta(days=1 if interval == "day" else 7) if interval != "month" else _shift(cur, "month", 1)
    return out
