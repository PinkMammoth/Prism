"""Pre-registered perp strategies (Phase 3). Hypotheses to test, not recommendations.

Each strategy turns a perp frame (daily bars + ``funding_day``) into long/short signals and
stop prices using only data available at each bar's close. Defaults are fixed here BEFORE
any real-data result. Sensitivity axes and walk-forward grids are declared up front, so
the research can't quietly tune its way to a good result.

  trend_ls      Trend-following, long and short: a new N-day high in an uptrend (or low in
                a downtrend). Hypothesis: crypto trends persist for weeks.
  funding_fade  Fade crowded positioning: short when funding is extreme-high for this coin
                after a run-up, long when extreme-low after a sell-off. Hypothesis:
                one-sided leverage unwinds.
  breakout_ls   Range breakout, both sides: the close leaves a tight N-day range.
                Hypothesis: volatility expansion out of compression continues.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from market_signal.setups.base import edge_trigger


@dataclass
class Signals:
    long: pd.Series
    short: pd.Series
    long_stop: pd.Series
    short_stop: pd.Series


@dataclass
class PerpStrategy:
    name: str
    title: str
    hypothesis: str
    primary_horizon: str
    defaults: dict[str, Any]
    sensitivity: dict[str, list]  # ≤ 2 signal parameters, default value included
    walk_forward: dict[str, list]  # ≤ 2 parameters the walk-forward may choose between
    fn: Callable[[pd.DataFrame, dict[str, Any]], Signals] = field(repr=False)

    def signals(self, frame: pd.DataFrame, params: dict[str, Any] | None = None) -> Signals:
        return self.fn(frame.reset_index(drop=True), {**self.defaults, **(params or {})})


def _atr(f: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = f["close"].shift(1)
    tr = pd.concat(
        [f["high"] - f["low"], (f["high"] - prev).abs(), (f["low"] - prev).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def _stops(f: pd.DataFrame, k: float) -> tuple[pd.Series, pd.Series]:
    atr = _atr(f)
    return f["close"] - k * atr, f["close"] + k * atr


# --------------------------------------------------------------------------- trend_ls


def _trend(f: pd.DataFrame, p: dict) -> Signals:
    c = f["close"]
    fast, slow = c.rolling(int(p["fast"])).mean(), c.rolling(int(p["slow"])).mean()
    prior_hi = c.rolling(int(p["breakout"])).max().shift(1)
    prior_lo = c.rolling(int(p["breakout"])).min().shift(1)
    up = (c > slow) & (fast > slow) & (c > prior_hi)
    down = (c < slow) & (fast < slow) & (c < prior_lo)
    ls, ss = _stops(f, float(p["stop_atr"]))
    cd = int(p["cooldown"])
    return Signals(edge_trigger(up, cd), edge_trigger(down, cd), ls, ss)


# --------------------------------------------------------------------------- funding_fade


def funding_percentile(
    f: pd.DataFrame, avg_days: int, lookback: int, min_history: int
) -> pd.Series:
    """Where today's ``avg_days`` mean funding sits among this coin's own trailing
    ``lookback`` days (inclusive of today; causal). NaN until ``min_history`` days exist."""
    avg = f["funding_day"].rolling(avg_days, min_periods=avg_days).mean()
    return avg.rolling(lookback, min_periods=min_history).rank(pct=True)


def _funding(f: pd.DataFrame, p: dict) -> Signals:
    pct = funding_percentile(f, int(p["avg_days"]), int(p["lookback"]), int(p["min_history"]))
    roc = f["close"] / f["close"].shift(int(p["roc_days"])) - 1
    hi, lo = float(p["pct_hi"]), 1 - float(p["pct_hi"])
    crowded_long = (pct >= hi) & (roc > 0)
    crowded_short = (pct <= lo) & (roc < 0)
    ls, ss = _stops(f, float(p["stop_atr"]))
    cd = int(p["cooldown"])
    return Signals(edge_trigger(crowded_short, cd), edge_trigger(crowded_long, cd), ls, ss)


# --------------------------------------------------------------------------- breakout_ls


def _breakout(f: pd.DataFrame, p: dict) -> Signals:
    n = int(p["base"])
    hi = f["high"].rolling(n).max().shift(1)
    lo = f["low"].rolling(n).min().shift(1)
    atr = _atr(f).shift(1)
    tight = (hi - lo) <= float(p["max_width_atr"]) * atr
    c = f["close"]
    ls, ss = _stops(f, float(p["stop_atr"]))
    cd = int(p["cooldown"])
    return Signals(edge_trigger(tight & (c > hi), cd), edge_trigger(tight & (c < lo), cd), ls, ss)


STRATEGIES: dict[str, PerpStrategy] = {
    s.name: s
    for s in (
        PerpStrategy(
            "trend_ls",
            "Trend following, long & short",
            "A new 20-day closing high in an established uptrend (close and 50-day average above the "
            "150-day) tends to keep rising for weeks; the mirror image for shorts.",
            "1m",
            {
                "fast": 50,
                "slow": 150,
                "breakout": 20,
                "stop_atr": 3.0,
                "cooldown": 10,
                "max_hold": 30,
            },
            {"breakout": [10, 20, 30, 40], "slow": [100, 150, 200]},
            {"breakout": [10, 20, 40]},
            _trend,
        ),
        PerpStrategy(
            "funding_fade",
            "Fade crowded funding",
            "When a coin's 7-day funding is in the top 5% of its own past year after a run-up, longs "
            "are crowded and the move tends to unwind (go short); the mirror image for extreme "
            "negative funding after a sell-off (go long).",
            "2w",
            {
                "avg_days": 7,
                "lookback": 365,
                "min_history": 120,
                "pct_hi": 0.95,
                "roc_days": 14,
                "stop_atr": 2.5,
                "cooldown": 10,
                "max_hold": 14,
            },
            {"pct_hi": [0.85, 0.90, 0.95, 0.98], "avg_days": [3, 7, 14]},
            {"pct_hi": [0.90, 0.95]},
            _funding,
        ),
        PerpStrategy(
            "breakout_ls",
            "Range breakout, long & short",
            "A close outside a tight 30-day range (range <= 8 ATR) starts a move that continues; "
            "up-breaks go long, down-breaks go short.",
            "1m",
            {"base": 30, "max_width_atr": 8.0, "stop_atr": 2.0, "cooldown": 10, "max_hold": 21},
            {"base": [20, 30, 40, 55], "max_width_atr": [6.0, 8.0, 10.0, 12.0]},
            {"base": [20, 30, 40]},
            _breakout,
        ),
    )
}


def get_strategy(name: str) -> PerpStrategy:
    if name not in STRATEGIES:
        raise KeyError(f"unknown perp strategy {name!r}; known: {sorted(STRATEGIES)}")
    return STRATEGIES[name]


def latest_signals(frame: pd.DataFrame, strategy: PerpStrategy) -> dict[str, Any]:
    """What the strategy says on the most recent closed bar (for display)."""
    if frame is None or frame.empty:
        return {"side": None}
    s = strategy.signals(frame)
    i = len(frame) - 1
    side = "long" if bool(s.long.iloc[i]) else "short" if bool(s.short.iloc[i]) else None
    stop = (
        s.long_stop.iloc[i]
        if side == "long"
        else s.short_stop.iloc[i]
        if side == "short"
        else np.nan
    )
    return {"side": side, "close": float(frame["close"].iloc[i]), "stop": float(stop) if np.isfinite(stop) else None,
            "as_of": frame["close_time"].iloc[i]}  # fmt: skip
