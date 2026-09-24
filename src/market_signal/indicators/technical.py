"""Technical indicators: pure, causal functions of past bars.

Rules:
- Every value at bar t uses only bars <= t (tested by truncation invariance).
- Rolling windows require a full window (``min_periods = window``): early values are NaN,
  never partially computed or back-filled.
- Windows are in *bars*. Calendar-equivalent windows differ by asset class (crypto trades
  365 days/year, NYSE ~252), see ``BarsPerPeriod``.

Each feature has a research purpose (``FEATURE_PURPOSE``). Indicators without a purpose are
not added.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.models.domain import AssetClass


@dataclass(frozen=True)
class BarsPerPeriod:
    week: int
    month: int
    quarter: int
    half: int
    year: int

    @staticmethod
    def for_class(asset_class: AssetClass) -> BarsPerPeriod:
        if asset_class == AssetClass.CRYPTO:
            return BarsPerPeriod(week=7, month=30, quarter=91, half=182, year=365)
        return BarsPerPeriod(week=5, month=21, quarter=63, half=126, year=252)


FEATURE_PURPOSE: dict[str, str] = {
    "sma_20": "short-term mean; pullback reference for fast trends",
    "sma_50": "medium-term trend; primary pullback support in Setup A",
    "sma_100": "intermediate support; crypto breadth measure",
    "sma_200": "long-term trend filter (above = constructive)",
    "ema_21": "responsive trend reference used in entry zones",
    "rsi_14": "pullback depth / exhaustion (Setup A entry band)",
    "atr_14": "volatility unit: normalises distances, sizes stops",
    "atr_pct": "volatility as % of price (risk quality)",
    "roc_1m": "1-month momentum (entry context, forward-return conditioning)",
    "roc_3m": "3-month momentum (medium-term trend confirmation)",
    "roc_6m": "6-month momentum (trend quality)",
    "roc_12m": "12-month momentum (long-term trend; needs a full year)",
    "dist_sma_50": "extension from 50DMA (avoid chasing)",
    "dist_sma_200": "extension from 200DMA (trend maturity)",
    "dist_sma_50_atr": "distance to 50DMA in ATR units (asset-agnostic proximity)",
    "high_52w": "52-week high (breakout / drawdown reference)",
    "dist_52w_high": "drawdown from 52-week high",
    "pullback_63_atr": "depth of pullback from 3-month high in ATR units (Setup A)",
    "pullback_from_20d_high": "fractional pullback from the 20-bar high (experiment DSL)",
    "swing_low_20": "recent swing low (technical invalidation)",
    "rvol_20": "20-bar annualised realised volatility (regime/vol filter)",
    "rvol_pct": "rvol_20 percentile vs trailing 3 years (vol extremes filter)",
    "vol_sma_20": "average volume (expansion detection)",
    "rel_volume": "volume / 20-bar average (breakout expansion confirmation)",
    "dollar_vol_20": "average traded value (liquidity quality)",
    "sma_200_slope": "20-bar change of the 200DMA (trend direction, regime)",
}


def sma(x: pd.Series, n: int) -> pd.Series:
    return x.rolling(n, min_periods=n).mean()


def ema(x: pd.Series, n: int) -> pd.Series:
    """Recursive EMA (adjust=False) seeded at the first value; NaN until n bars exist."""
    out = x.ewm(span=n, adjust=False, min_periods=n).mean()
    return out


def wilder(x: pd.Series, n: int) -> pd.Series:
    """Wilder smoothing (RMA): alpha = 1/n, seeded with the simple mean of the first n."""
    values = x.to_numpy(dtype=float)
    out = np.full(len(values), np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < n:
        return pd.Series(out, index=x.index)
    start = valid[0]
    # require n consecutive valid values from the first valid one
    seed_end = start + n
    if np.isnan(values[start:seed_end]).any():
        return pd.Series(out, index=x.index)
    prev = values[start:seed_end].mean()
    out[seed_end - 1] = prev
    for i in range(seed_end, len(values)):
        v = values[i]
        if np.isnan(v):
            out[i] = np.nan  # a gap breaks the recursion: missing stays missing
            prev = np.nan
            continue
        if np.isnan(prev):
            continue
        prev = prev + (v - prev) / n
        out[i] = prev
    return pd.Series(out, index=x.index)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = wilder(delta.clip(lower=0), n)
    loss = wilder((-delta).clip(lower=0), n)
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    out[(loss == 0) & gain.notna()] = 100.0
    return out


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0] if len(tr) else np.nan
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    # the first TR has no previous close; start the seed from bar 1 for a consistent definition
    tr = true_range(high, low, close)
    tr.iloc[:1] = np.nan
    return wilder(tr, n)


def roc(close: pd.Series, n: int) -> pd.Series:
    return close / close.shift(n) - 1.0


def realised_vol(close: pd.Series, n: int, bars_per_year: int) -> pd.Series:
    lr = np.log(close).diff()
    return lr.rolling(n, min_periods=n).std(ddof=1) * np.sqrt(bars_per_year)


def rolling_percentile(x: pd.Series, window: int, min_periods: int) -> pd.Series:
    """Percentile rank of x[t] within x[t-window+1..t] (inclusive). Causal."""

    def rank_last(a: np.ndarray) -> float:
        last = a[-1]
        a = a[~np.isnan(a)]
        if np.isnan(last) or len(a) == 0:
            return np.nan
        return float((a <= last).sum() - 1) / max(len(a) - 1, 1)

    return x.rolling(window, min_periods=min_periods).apply(rank_last, raw=True)


def compute_features(bars: pd.DataFrame, asset_class: AssetClass) -> pd.DataFrame:
    """Full daily feature frame. Input: bars (split-adjusted for equities) with ts,
    close_time, open, high, low, close, volume. Output keeps those columns plus features."""
    p = BarsPerPeriod.for_class(asset_class)
    df = bars.copy().reset_index(drop=True)
    c, h, lo, v = df["close"], df["high"], df["low"], df["volume"]
    for n in (20, 50, 100, 200):
        df[f"sma_{n}"] = sma(c, n)
    df["ema_21"] = ema(c, 21)
    df["rsi_14"] = rsi(c, 14)
    df["atr_14"] = atr(h, lo, c, 14)
    df["atr_pct"] = df["atr_14"] / c
    df["roc_1m"] = roc(c, p.month)
    df["roc_3m"] = roc(c, p.quarter)
    df["roc_6m"] = roc(c, p.half)
    df["roc_12m"] = roc(c, p.year)
    df["dist_sma_50"] = c / df["sma_50"] - 1
    df["dist_sma_200"] = c / df["sma_200"] - 1
    df["dist_sma_50_atr"] = (c - df["sma_50"]) / df["atr_14"]
    df["high_52w"] = h.rolling(p.year, min_periods=p.year).max()
    df["dist_52w_high"] = c / df["high_52w"] - 1
    high_63 = h.rolling(p.quarter, min_periods=p.quarter).max()
    df["high_63"] = high_63
    df["pullback_63_atr"] = (high_63 - c) / df["atr_14"]
    df["pullback_from_20d_high"] = 1 - c / h.rolling(20, min_periods=20).max()
    df["swing_low_20"] = lo.rolling(20, min_periods=20).min()
    df["rvol_20"] = realised_vol(c, 20, p.year)
    df["rvol_pct"] = rolling_percentile(df["rvol_20"], window=3 * p.year, min_periods=p.year)
    df["vol_sma_20"] = sma(v, 20)
    df["rel_volume"] = v / df["vol_sma_20"]
    df["dollar_vol_20"] = sma(v * c, 20)
    df["sma_200_slope"] = df["sma_200"] / df["sma_200"].shift(20) - 1
    return df
