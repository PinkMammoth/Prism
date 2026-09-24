"""Deterministic price views derived from stored RAW bars + corporate actions.

Price bases:
  ``raw``          as traded (what the provider reported, unadjusted).
  ``split``        split-adjusted: continuous across splits. Used for signals/technicals.
  ``total_return`` split + dividend adjusted (dividends reinvested at the ex-date prior
                   close). Used for P&L and forward returns.

For crypto there are no corporate actions and all three bases are identical.
Returned frames are indexed 0..n-1 with columns ts, close_time, open, high, low, close,
volume, and are sorted by ts.
"""

from __future__ import annotations

import os
from enum import StrEnum

import numpy as np
import pandas as pd

from market_signal.data.store import Store
from market_signal.models.domain import Asset, Calendar, Timeframe


class PriceBasis(StrEnum):
    RAW = "raw"
    SPLIT = "split"
    TOTAL_RETURN = "total_return"


def adjustment_factors(
    bars: pd.DataFrame, actions: pd.DataFrame, calendar: Calendar
) -> pd.DataFrame:
    """Per-bar multiplicative factors: ``split_mult`` and ``tr_mult`` (price multipliers).

    A split with factor f effective on date d divides all prices before d by f.
    A dividend D with ex-date d multiplies all prices before d by (1 - D / C), where C is
    the split-adjusted close of the bar before d.
    """
    n = len(bars)
    out = pd.DataFrame({"split_mult": np.ones(n), "tr_mult": np.ones(n)}, index=bars.index)
    if actions is None or actions.empty or n == 0:
        return out
    if calendar == Calendar.NYSE:
        bar_dates = bars["ts"].dt.tz_convert("America/New_York").dt.date.to_numpy()
    else:
        bar_dates = bars["ts"].dt.date.to_numpy()

    split_mult = np.ones(n)
    for _, a in actions.sort_values("date").iterrows():
        f = float(a["split_factor"])
        if np.isfinite(f) and f > 0 and f != 1.0:
            split_mult[bar_dates < a["date"]] /= f
    split_close = bars["close"].to_numpy() * split_mult

    tr_mult = split_mult.copy()
    for _, a in actions.sort_values("date").iterrows():
        d = float(a["dividend"])
        if not (np.isfinite(d) and d > 0):
            continue
        prior = np.flatnonzero(bar_dates < a["date"])
        if len(prior) == 0:
            continue
        # dividend is in ex-date share terms; compare with the prior close in the same terms
        ex_idx = np.flatnonzero(bar_dates >= a["date"])
        post_split = split_mult[ex_idx[0]] if len(ex_idx) else 1.0
        c_prev = split_close[prior[-1]] / post_split
        if c_prev > 0 and d < c_prev:
            tr_mult[prior] *= 1.0 - d / c_prev
    out["split_mult"] = split_mult
    out["tr_mult"] = tr_mult
    return out


def apply_basis(bars: pd.DataFrame, factors: pd.DataFrame, basis: PriceBasis) -> pd.DataFrame:
    if basis == PriceBasis.RAW:
        return bars.copy()
    mult = factors["split_mult"] if basis == PriceBasis.SPLIT else factors["tr_mult"]
    out = bars.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = bars[c] * mult
    # volume in share terms scales inversely with splits only
    out["volume"] = bars["volume"] / factors["split_mult"]
    return out


def load_bars(
    store: Store,
    asset: Asset,
    timeframe: Timeframe = Timeframe.D1,
    basis: PriceBasis = PriceBasis.SPLIT,
    source: str | None = None,
) -> pd.DataFrame:
    """Load one configured series in the requested price basis."""
    from market_signal.data.registry import source_label

    if timeframe == Timeframe.W1:
        daily = load_bars(store, asset, Timeframe.D1, basis, source)
        return weekly_from_daily(daily, asset.calendar)
    if source is None:
        spec = asset.series_for(timeframe)
        if spec is None:
            raise ValueError(f"{asset.symbol}: no {timeframe} series configured")
        # an explicit override (demo mode) replaces the configured source wholesale
        source = os.environ.get("PRISM_SOURCE_OVERRIDE") or source_label(spec.provider, timeframe)
    bars = store.get_bars(asset.symbol, timeframe, source)
    if bars.empty or timeframe != Timeframe.D1:
        return bars
    actions = store.get_actions(asset.symbol, source)
    return apply_basis(bars, adjustment_factors(bars, actions, asset.calendar), basis)


def weekly_from_daily(daily: pd.DataFrame, calendar: Calendar) -> pd.DataFrame:
    """Weekly bars from daily bars. Weeks end Sunday (crypto) or Friday (NYSE).

    The last week may be incomplete; its ``complete`` flag is False. Consumers doing
    historical research must use ``weekly_asof`` rather than this frame directly.
    """
    if daily.empty:
        return daily.assign(complete=pd.Series(dtype=bool))
    rule = "W-SUN" if calendar == Calendar.CRYPTO_24_7 else "W-FRI"
    local = daily.set_index(
        daily["ts"].dt.tz_convert("UTC" if calendar == Calendar.CRYPTO_24_7 else "America/New_York")
    )
    g = local.resample(rule)
    w = pd.DataFrame(
        {
            "ts": g["ts"].first(),
            "close_time": g["close_time"].last(),
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(min_count=1),
            "n_days": g["close"].count(),
        }
    ).dropna(subset=["close"])
    week_end = w.index  # label = period end date (local)
    last_daily_close = daily["close_time"].max()
    if calendar == Calendar.CRYPTO_24_7:
        nominal_end = week_end.tz_convert("UTC").normalize() + pd.Timedelta(days=1)
    else:
        nominal_end = pd.DatetimeIndex(week_end.date).tz_localize(
            "America/New_York"
        ) + pd.Timedelta(hours=16)
    w["complete"] = np.asarray(nominal_end <= last_daily_close) | (np.arange(len(w)) < len(w) - 1)
    return w.reset_index(drop=True)
