"""Candidate opportunity rate (``incubation_opportunity_v1``): a diagnostic, never a target.

A perp trader whose whole ensemble produces one trade every few weeks may be clean but
operationally useless; one that takes 50 random trades a day is not better. Frequency is
always reported beside expectancy, uncertainty, drawdown, false activation and costs.

Over a span of calendar days, from independent signals (one row per strategy, asset and
signal; ``admitted`` = the candidate's level at the signal was EXPLORATORY/CONFIRMED_PAPER):

| Metric | Definition |
|---|---|
| candidate signals / day | every independent signal of the pool |
| paper-admissible signals / day | signals while the candidate was admitted (= shadow intents) |
| independent opportunities / day | distinct (asset, side, UTC signal day) among admissible signals: correlated variants firing together count once |
| median days between opportunities | median gap between consecutive distinct opportunity days |
| zero-opportunity day share | share of span days without any opportunity |
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _rate(n: int, days: float) -> float | None:
    return float(n / days) if days > 0 else None


def opportunity_rate(sig: pd.DataFrame, span_start: float, span_end: float) -> dict:
    """``sig`` columns: ``t_sig`` (float days), ``asset``, ``side``, ``admitted`` (bool)."""
    days = float(span_end - span_start)
    s = sig[(sig["t_sig"] >= span_start) & (sig["t_sig"] < span_end)]
    adm = s[s["admitted"].astype(bool)]
    day = np.floor(adm["t_sig"].to_numpy(float)).astype(np.int64)
    opp = pd.DataFrame({"asset": adm["asset"].to_numpy(), "side": adm["side"].to_numpy(),
                        "day": day}).drop_duplicates()  # fmt: skip
    opp_days = np.unique(opp["day"].to_numpy())
    gaps = np.diff(opp_days)
    n_days = max(int(np.ceil(days)), 0)

    def by_side(frame: pd.DataFrame) -> dict:
        return {side: int((frame["side"] == side).sum()) for side in ("long", "short")}

    return {
        "span_days": days,
        "candidate_signals": len(s),
        "candidate_signals_per_day": _rate(len(s), days),
        "admissible_signals": len(adm),
        "admissible_signals_per_day": _rate(len(adm), days),
        "independent_opportunities": len(opp),
        "independent_opportunities_per_day": _rate(len(opp), days),
        "median_days_between_opportunities": float(np.median(gaps)) if len(gaps) else None,
        "zero_opportunity_day_share": (1 - len(opp_days) / n_days) if n_days else None,
        "candidate_signals_by_side": by_side(s),
        "admissible_signals_by_side": by_side(adm),
        "opportunities_by_side": by_side(opp),
    }
