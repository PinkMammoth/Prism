"""Pure daily feature calculations for vocabulary v1. No Store, DuckDB or clock access.

Every function maps a native-bar frame (``open, high, low, close, volume`` and, for perps,
``funding_day``) to one Series aligned to it, using only bars <= t for the value at t.
Formulas are the existing Prism implementations wherever one exists (``technical.py``,
``perps/strategies.py``, ``perps/backtest.py``); this module only names and parameterises
them. ``warmup`` is the number of bars (counting the current one) needed before a value
can exist on gap-free input; NaN inputs still yield NaN, which the compiler treats as
undefined rather than false.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.indicators import technical as ta
from market_signal.indicators.technical import BarsPerPeriod
from market_signal.models.domain import AssetClass
from market_signal.perps.backtest import daily_funding
from market_signal.perps.strategies import funding_percentile, sma_atr
from market_signal.research.lab.vocabulary import FAMILIES, FeatureKey

Calc = Callable[[pd.DataFrame, tuple[int, ...], AssetClass | None], pd.Series]


@dataclass(frozen=True)
class Implementation:
    calc: Calc
    warmup: Callable[[tuple[int, ...], AssetClass | None], int]


def _month(asset_class: AssetClass | None) -> int:
    if asset_class is None:
        raise ValueError("roc_1m needs an explicit asset class (class-dependent month)")
    return BarsPerPeriod.for_class(asset_class).month


def _prior_max(x: pd.Series, n: int) -> pd.Series:
    # The threshold at t is formed from bars t-n..t-1: today's bar never sets its own level.
    return x.rolling(n, min_periods=n).max().shift(1)


def _prior_min(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).min().shift(1)


def _vol_z(v: pd.Series, n: int) -> pd.Series:
    prior = v.shift(1).rolling(n, min_periods=n)
    std = prior.std(ddof=1)
    # Constant prior volume has no dispersion: undefined, not +/-inf.
    return ((v - prior.mean()) / std).where(std > 0)


def _funding_frame(f: pd.DataFrame) -> pd.DataFrame:
    if "funding_day" not in f:
        raise ValueError("funding features require a perp_funding series in the dataset")
    return f


def _const(k: int):
    return lambda p, a: k


IMPLEMENTATIONS: dict[str, Implementation] = {
    "open": Implementation(lambda f, p, a: f["open"], _const(1)),
    "high": Implementation(lambda f, p, a: f["high"], _const(1)),
    "low": Implementation(lambda f, p, a: f["low"], _const(1)),
    "close": Implementation(lambda f, p, a: f["close"], _const(1)),
    "volume": Implementation(lambda f, p, a: f["volume"], _const(1)),
    "ret": Implementation(lambda f, p, a: ta.roc(f["close"], p[0]), lambda p, a: p[0] + 1),
    "roc_1m": Implementation(
        lambda f, p, a: ta.roc(f["close"], _month(a)), lambda p, a: _month(a) + 1
    ),
    "range_pct": Implementation(lambda f, p, a: (f["high"] - f["low"]) / f["close"], _const(1)),
    "sma": Implementation(lambda f, p, a: ta.sma(f["close"], p[0]), lambda p, a: p[0]),
    "ema": Implementation(lambda f, p, a: ta.ema(f["close"], p[0]), lambda p, a: p[0]),
    "dist_sma": Implementation(
        lambda f, p, a: f["close"] / ta.sma(f["close"], p[0]) - 1, lambda p, a: p[0]
    ),
    "dist_ema": Implementation(
        lambda f, p, a: f["close"] / ta.ema(f["close"], p[0]) - 1, lambda p, a: p[0]
    ),
    "rsi": Implementation(lambda f, p, a: ta.rsi(f["close"], p[0]), lambda p, a: p[0] + 1),
    # technical.atr seeds from bar 1 (the first true range needs a previous close)
    "atr": Implementation(
        lambda f, p, a: ta.atr(f["high"], f["low"], f["close"], p[0]), lambda p, a: p[0] + 1
    ),
    "atr_sma": Implementation(lambda f, p, a: sma_atr(f, p[0]), lambda p, a: p[0]),
    "atr_pct": Implementation(
        lambda f, p, a: ta.atr(f["high"], f["low"], f["close"], p[0]) / f["close"],
        lambda p, a: p[0] + 1,
    ),
    "rvol": Implementation(
        lambda f, p, a: ta.realised_vol(f["close"], p[0], bars_per_year=1), lambda p, a: p[0] + 1
    ),
    "donchian_high": Implementation(
        lambda f, p, a: _prior_max(f["high"], p[0]), lambda p, a: p[0] + 1
    ),
    "donchian_low": Implementation(
        lambda f, p, a: _prior_min(f["low"], p[0]), lambda p, a: p[0] + 1
    ),
    "close_high": Implementation(
        lambda f, p, a: _prior_max(f["close"], p[0]), lambda p, a: p[0] + 1
    ),
    "close_low": Implementation(
        lambda f, p, a: _prior_min(f["close"], p[0]), lambda p, a: p[0] + 1
    ),
    "dist_donchian_high": Implementation(
        lambda f, p, a: f["close"] / _prior_max(f["high"], p[0]) - 1, lambda p, a: p[0] + 1
    ),
    "dist_donchian_low": Implementation(
        lambda f, p, a: f["close"] / _prior_min(f["low"], p[0]) - 1, lambda p, a: p[0] + 1
    ),
    "vol_sma": Implementation(lambda f, p, a: ta.sma(f["volume"], p[0]), lambda p, a: p[0]),
    "rel_volume": Implementation(
        lambda f, p, a: f["volume"] / ta.sma(f["volume"], p[0]), lambda p, a: p[0]
    ),
    "vol_z": Implementation(lambda f, p, a: _vol_z(f["volume"], p[0]), lambda p, a: p[0] + 1),
    "funding_day": Implementation(lambda f, p, a: _funding_frame(f)["funding_day"], _const(1)),
    "funding_sum": Implementation(
        lambda f, p, a: _funding_frame(f)["funding_day"].rolling(p[0], min_periods=p[0]).sum(),
        lambda p, a: p[0],
    ),
    "funding_mean": Implementation(
        lambda f, p, a: _funding_frame(f)["funding_day"].rolling(p[0], min_periods=p[0]).mean(),
        lambda p, a: p[0],
    ),
    "funding_pct": Implementation(
        lambda f, p, a: funding_percentile(_funding_frame(f), p[0], p[1], min_history=p[1]),
        lambda p, a: p[0] + p[1] - 1,
    ),
}

assert IMPLEMENTATIONS.keys() == FAMILIES.keys(), "vocabulary and implementations diverged"


def compute(key: FeatureKey, frame: pd.DataFrame, asset_class: AssetClass | None) -> pd.Series:
    out = IMPLEMENTATIONS[key.family].calc(frame, key.params, asset_class)
    return out.astype(float).rename(key.name)


def warmup(key: FeatureKey, asset_class: AssetClass | None) -> int:
    return IMPLEMENTATIONS[key.family].warmup(key.params, asset_class)


# --------------------------------------------------------------------------- funding input

CADENCE_WINDOW = pd.Timedelta(days=7)


def causal_settlements_per_day(bar_end: np.ndarray, settled: np.ndarray) -> np.ndarray:
    """Expected settlements per day for each bar from the settlements in the trailing 7 days
    ending at that bar's end (ns timestamps, both sorted). NaN with fewer than two.

    Prism's ``daily_funding`` infers one cadence from the median spacing of the WHOLE
    series, so later observations can change earlier coverage. This estimate uses only
    settlements already made, and adapts if a venue changes its funding interval.
    """
    out = np.full(len(bar_end), np.nan)
    lo = np.searchsorted(settled, bar_end - CADENCE_WINDOW.value, side="right")
    hi = np.searchsorted(settled, bar_end, side="right")
    for i, (a, b) in enumerate(zip(lo, hi, strict=True)):
        if b - a >= 2:
            spacing = float(np.median(np.diff(settled[a:b]))) / 1e9
            if spacing > 0:
                out[i] = max(round(86400 / spacing), 1)
    return out


def lab_funding_day(
    bars: pd.DataFrame, funding: pd.DataFrame, min_coverage: float = 0.8
) -> np.ndarray:
    """``daily_funding`` with a causal cadence and an availability check.

    ``bars``: ts, close_time (UTC). ``funding``: time, funding_rate, available_at (UTC).
    Settlement times snap to the nearest minute exactly as in ``daily_funding``; a bar is
    missing (NaN) if any settlement it sums was not available (to the minute) by the bar's
    close, so a later-published rate can never inform an earlier decision.
    """
    out = np.full(len(bars), np.nan)
    if funding.empty or bars.empty:
        return out
    f = funding.dropna(subset=["funding_rate"]).sort_values("time")
    if f.empty:
        return out
    t = pd.DatetimeIndex(f["time"]).round("min").as_unit("ns").asi8
    starts = pd.DatetimeIndex(bars["ts"]).as_unit("ns").asi8
    ends = starts + pd.Timedelta(days=1).value
    per_day = causal_settlements_per_day(ends, t)
    series = pd.Series(f["funding_rate"].to_numpy(float), index=pd.DatetimeIndex(f["time"]))
    out = daily_funding(bars, series, min_coverage, per_day=per_day)
    # Latest (minute-snapped) availability among each bar's summed settlements.
    avail = pd.DatetimeIndex(f["available_at"]).round("min").as_unit("ns").asi8
    lo = np.searchsorted(t, starts, side="right")
    hi = np.searchsorted(t, ends, side="right")
    close = pd.DatetimeIndex(bars["close_time"]).as_unit("ns").asi8
    for i, (a, b) in enumerate(zip(lo, hi, strict=True)):
        if b > a and avail[a:b].max() > close[i]:
            out[i] = np.nan
    return out
