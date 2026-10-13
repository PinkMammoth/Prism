"""Calendar-day stationary block bootstrap of the frozen trade stream.

Trades are resampled by whole UTC days (Politis-Romano stationary bootstrap, geometric block
length), so intra-day concurrency and capital contention are preserved exactly and multi-day
regime clustering is preserved within blocks. ``mean_block_days=1`` gives the IID-day
comparison; the gap between the two shows how much serial dependence matters.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np

from .metrics import drawdowns
from .replay import _ts, sequential

DAY = 86400.0


def _shift(x: str, seconds: float) -> str:
    return (datetime.fromisoformat(x) + timedelta(seconds=seconds)).isoformat()


def resample(trades, rng: np.random.Generator, mean_block_days: float = 5.0):
    if not trades:
        return []
    day0 = math.floor(min(_ts(t.decision_at) for t in trades) / DAY)
    by_day: dict[int, list] = {}
    for t in trades:
        by_day.setdefault(int(_ts(t.decision_at) // DAY) - day0, []).append(t)
    days = max(by_day) + 1
    seq = []
    d = int(rng.integers(days))
    for _ in range(days):
        seq.append(d)
        d = int(rng.integers(days)) if rng.random() < 1 / mean_block_days else (d + 1) % days
    out = []
    for new, src in enumerate(seq):
        shift = (new - src) * DAY
        for t in by_day.get(src, ()):
            out.append(
                replace(
                    t,
                    trade_id=f"{t.trade_id}#b{new}",
                    signal_at=_shift(t.signal_at, shift),
                    decision_at=_shift(t.decision_at, shift),
                    entry_at=_shift(t.entry_at, shift),
                    exit_at=_shift(t.exit_at, shift),
                )
            )
    return out


def run(trades, policy, equity, *, n=200, seed=0, mean_block_days=5.0, evidence=None) -> dict:
    """Deterministic for a given (trades, policy, equity, n, seed, block length)."""
    rng = np.random.default_rng(seed)
    term, mdd, mineq, longest, halted = [], [], [], [], 0
    for _ in range(n):
        sample = resample(trades, rng, mean_block_days)
        res = sequential(sample, policy, equity, evidence=evidence)
        dd = drawdowns(res.curve)
        term.append(res.curve[-1][1])
        mdd.append(dd["max_drawdown"])
        mineq.append(dd["min_equity"])
        longest.append(dd["longest_drawdown_days"])
        halted += res.halted_at is not None
    term, mdd, mineq = (np.array(x, dtype=float) for x in (term, mdd, mineq))

    def q(a):
        return {str(p): float(np.quantile(a, p)) for p in (0.05, 0.25, 0.5, 0.75, 0.95)}

    loss = 1 - mineq / equity
    return {
        "method": "calendar_day_stationary_block_bootstrap",
        "mean_block_days": mean_block_days,
        "samples": n,
        "seed": seed,
        "terminal_equity": q(term),
        "mean_terminal_equity": float(term.mean()),
        "max_drawdown": q(mdd),
        "longest_drawdown_days": q(np.array(longest, dtype=float)),
        "p_terminal_below_start": float((term < equity).mean()),
        "p_path_loss_10": float((loss >= 0.10).mean()),
        "p_path_loss_20": float((loss >= 0.20).mean()),
        "p_path_loss_30": float((loss >= 0.30).mean()),
        "p_path_loss_50": float((loss >= 0.50).mean()),
        "p_ruin": float((loss >= 0.50).mean()),
        "ruin_definition": "path equity at or below 50% of start (experiment cannot continue)",
        "p_halted": halted / n,
    }
