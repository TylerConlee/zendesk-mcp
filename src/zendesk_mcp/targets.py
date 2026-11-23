"""Metric definitions (for slide appendices), period-over-period deltas, and
targets with red/amber/green status. Targets persist in a small JSON file."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

METRICS: dict[str, dict[str, str]] = {
    "tickets_created": {"label": "Tickets created", "unit": "tickets", "better": "neutral",
                        "definition": "New tickets created in the period."},
    "tickets_solved": {"label": "Tickets solved", "unit": "tickets", "better": "higher",
                       "definition": "Tickets in scope that are now solved or closed."},
    "frt_median_min": {"label": "First reply time (median)", "unit": "minutes", "better": "lower",
                       "definition": "Median time from ticket creation to the first public agent reply, calendar "
                                     "hours (Zendesk's reply_time metric)."},
    "frt_median_business_min": {"label": "First reply time (median, business hrs)", "unit": "minutes",
                                "better": "lower",
                                "definition": "As first reply time, counting only business hours of the ticket's "
                                              "schedule."},
    "frt_p90_min": {"label": "First reply time (90th percentile)", "unit": "minutes", "better": "lower",
                    "definition": "90% of tickets got a first reply within this time (calendar hours)."},
    "full_resolution_median_min": {"label": "Resolution time (median)", "unit": "minutes", "better": "lower",
                                   "definition": "Median time from creation to final solve, for solved tickets."},
    "full_resolution_median_business_min": {"label": "Resolution time (median, business hrs)", "unit": "minutes",
                                            "better": "lower",
                                            "definition": "As resolution time, business hours only."},
    "requester_wait_median_min": {"label": "Customer wait time (median)", "unit": "minutes", "better": "lower",
                                  "definition": "Median total time solved tickets spent in New, Open or On-hold — "
                                                "time the customer was waiting on us."},
    "one_touch_pct": {"label": "One-touch resolution", "unit": "%", "better": "higher",
                      "definition": "Share of solved tickets resolved with a single public agent reply."},
    "reopened_pct": {"label": "Reopen rate", "unit": "%", "better": "lower",
                     "definition": "Share of solved tickets that were reopened at least once."},
    "csat_pct": {"label": "CSAT", "unit": "%", "better": "higher",
                 "definition": "Good ratings ÷ (good + bad) ratings on tickets in scope."},
    "csat_response_rate_pct": {"label": "CSAT response rate", "unit": "%", "better": "higher",
                               "definition": "Rated surveys ÷ surveys offered."},
    "sla_achievement_pct": {"label": "SLA achievement", "unit": "%", "better": "higher",
                            "definition": "SLA targets met ÷ targets completed in the period, across all policies "
                                          "and metrics (fulfilled late counts as breached)."},
    "backlog_unsolved": {"label": "Open backlog (now)", "unit": "tickets", "better": "lower",
                         "definition": "Unsolved tickets (new, open, pending, on-hold) at report time."},
    "agent_response_median_min": {"label": "Agent response time (median, all replies)", "unit": "minutes",
                                  "better": "lower",
                                  "definition": "Median time from a customer's first unanswered message to the next "
                                                "agent reply, across every exchange (not just the first)."},
}


def definitions(keys: list[str] | None = None) -> list[dict[str, str]]:
    keys = keys or list(METRICS)
    return [{"metric": k, "label": METRICS[k]["label"], "definition": METRICS[k]["definition"]}
            for k in keys if k in METRICS]


def compare(key: str, current: float | None, previous: float | None) -> dict[str, Any]:
    """Delta between periods plus whether it is an improvement for this metric."""
    out: dict[str, Any] = {"current": current, "previous": previous}
    if current is None or previous is None:
        return out
    change = round(current - previous, 1)
    out["change"] = change
    out["change_pct"] = round(100 * change / previous, 1) if previous else None
    better = METRICS.get(key, {}).get("better", "neutral")
    pct = out["change_pct"]
    if change == 0 or (pct is not None and abs(pct) < 1):
        out["trend"] = "flat"
    elif better == "neutral":
        out["trend"] = "up" if change > 0 else "down"
    else:
        out["trend"] = "improved" if (change < 0) == (better == "lower") else "worsened"
    return out


# ----------------------------------------------------------------------------- targets

def targets_path(file_root: str | None) -> Path:
    if os.getenv("ZENDESK_TARGETS_FILE"):
        return Path(os.path.expanduser(os.environ["ZENDESK_TARGETS_FILE"]))
    if file_root:
        return Path(file_root) / "zendesk_targets.json"
    return Path.home() / ".zendesk-mcp" / "targets.json"


def load_targets(file_root: str | None) -> dict[str, dict[str, Any]]:
    p = targets_path(file_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def normalize_targets(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for key, v in raw.items():
        if key not in METRICS or METRICS[key]["better"] == "neutral":
            valid = ", ".join(k for k, m in METRICS.items() if m["better"] != "neutral")
            raise ValueError(f"Unknown or untargetable metric {key!r}. Targetable: {valid}")
        spec = v if isinstance(v, dict) else {"goal": v}
        if not isinstance(spec.get("goal"), (int, float)):
            raise ValueError(f"Target for {key} needs a numeric goal.")
        out[key] = {"goal": float(spec["goal"]), "amber_pct": float(spec.get("amber_pct", 10))}
    return out


def save_targets(file_root: str | None, targets: dict[str, dict[str, Any]]) -> Path:
    p = targets_path(file_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(targets, indent=2))
    return p


def status(key: str, value: float | None, targets: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    t = targets.get(key)
    if not t or value is None:
        return None
    goal, amber = t["goal"], t["amber_pct"] / 100
    if METRICS[key]["better"] == "higher":
        rag = "green" if value >= goal else "amber" if value >= goal * (1 - amber) else "red"
    else:
        rag = "green" if value <= goal else "amber" if value <= goal * (1 + amber) else "red"
    return {"goal": goal, "status": rag}
