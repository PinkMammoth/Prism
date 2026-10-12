"""Resource admission is independent of trading score and account equity."""

from __future__ import annotations

import json
import os
from pathlib import Path

from market_signal.paper.v2.data import ts
from market_signal.universe import ledger
from market_signal.universe.policy import POLICY


def validate(now, *, path=None, rich_assets=None):
    """Require a measured deployment-volume budget, not a fabricated linear benchmark claim."""
    required_assets = rich_assets or 6 + POLICY.rich_satellite_cap
    selected = path or os.environ.get("PRISM_UNIVERSE_BUDGET")
    if not selected:
        raise ValueError("measured deployment storage/runtime budget required before warming")
    b = json.loads(Path(selected).read_text())
    if (
        b.get("policy_id") != POLICY.identity
        or b.get("validated") is not True
        or not 0
        <= int(ts(now).timestamp()) - b.get("measured_at", 0)
        <= POLICY.budget_max_age_seconds
        or b.get("rich_assets", 0) < required_assets
        or b.get("minimum_runway_days", 0) < POLICY.budget_minimum_runway_days
        or b.get("daily_growth_bytes", 0) <= 0
        or b.get("available_bytes", 0) / b["daily_growth_bytes"] < b["minimum_runway_days"]
        or b.get("worker_cpu_seconds_per_minute", 60) >= POLICY.budget_cpu_seconds_per_minute_max
    ):
        raise ValueError("unvalidated, stale or insufficient dynamic universe deployment budget")
    return b


def activation_ready(store, now):
    validate(now)
    ev = ledger.latest(store, at=int(ts(now).timestamp()))
    if (
        not ev
        or ev["stage"] != "RANKING + WARMING"
        or int(ts(now).timestamp()) - ev["at"] > 2 * POLICY.cadence_seconds
    ):
        raise ValueError(
            "fresh ranking and real prospective warming required before dynamic paper activation"
        )
    if not any(
        r["cohort"] == "SATELLITE"
        and r["entry_eligible"]
        and any(h["available"] for h in r.get("hypotheses", {}).get("hypotheses", []))
        for r in ev["rows"]
    ):
        raise ValueError("no genuinely warmed portable satellite hypothesis; continue observation")
    return ev
