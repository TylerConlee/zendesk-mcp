"""Turning API payloads into compact, size-bounded text for the model."""

from __future__ import annotations

import json
from typing import Any, Iterable


def strip_nulls(value: Any) -> Any:
    """Drop None values recursively; Zendesk objects are full of them."""
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value]
    return value


def project(items: Iterable[dict[str, Any]], fields: list[str] | None) -> list[dict[str, Any]]:
    """Keep only the requested top-level fields (dot paths allowed, e.g. 'via.channel')."""
    items = list(items)
    if not fields:
        return items
    out = []
    for item in items:
        row: dict[str, Any] = {}
        for f in fields:
            cur: Any = item
            for part in f.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is not None:
                row[f] = cur
        out.append(row)
    return out


def to_csv(rows: list[dict[str, Any]]) -> str:
    """Tidy rows -> CSV text, columns in first-seen order."""
    import csv
    import io

    cols: list[str] = []
    for r in rows:
        cols.extend(k for k in r if k not in cols)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
    return buf.getvalue()


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


def _largest_list(data: Any) -> tuple[dict | None, str | None]:
    """Find the dict + key holding the largest list (by serialized size), searching two levels deep."""
    best: tuple[dict | None, str | None, int] = (None, None, 0)
    candidates: list[dict] = []
    if isinstance(data, dict):
        candidates.append(data)
        candidates.extend(v for v in data.values() if isinstance(v, dict))
    for d in candidates:
        for k, v in d.items():
            if isinstance(v, list) and len(v) > 1:
                size = len(_dumps(v))
                if size > best[2]:
                    best = (d, k, size)
    return best[0], best[1]


def render(data: Any, max_chars: int) -> str:
    """Serialize to compact JSON. If it exceeds max_chars, trim the largest list
    (keeping whole items) and annotate what was dropped, so output stays valid JSON."""
    data = strip_nulls(data)
    text = _dumps(data)
    if len(text) <= max_chars:
        return text

    notes: list[str] = []
    for _ in range(12):
        parent, key = _largest_list(data)
        if parent is None or key is None:
            break
        lst = parent[key]
        lo, hi = 1, len(lst) - 1
        # binary search for how many items fit
        while lo < hi:
            mid = (lo + hi + 1) // 2
            parent[key] = lst[:mid]
            if len(_dumps(data)) <= max_chars - 300:
                lo = mid
            else:
                hi = mid - 1
        parent[key] = lst[:lo]
        notes.append(f"'{key}' trimmed from {len(lst)} to {lo} items")
        text = _dumps(data)
        if len(text) <= max_chars - 300:
            break

    if notes:
        if isinstance(data, dict):
            data["_truncated"] = notes + ["Narrow the request (smaller limit, `fields`, filters) to see more."]
            text = _dumps(data)
    if len(text) > max_chars:
        text = text[: max_chars - 120] + f'... [TRUNCATED: response was {len(text)} chars; narrow the request]'
    return text
