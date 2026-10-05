"""Minimal regime primitives for later conditioning; NOT a regime classifier.

Phase 17 may combine these into simple, frozen regime definitions. Here they are only
measured, causally, on any bar series:

- existing ``lab_features_v1`` tokens that are not daily-only (price vs EMA/SMA via
  ``dist_ema_n`` / ``dist_sma_n``, rolling return ``ret_n``, ``rvol_n``, ``atr_pct_n``,
  ``donchian_high_n`` / ``donchian_low_n``), computed by the Lab's own implementations
  and counted in BARS of the series' timeframe (``ret_24`` on 1h bars = 24 hours);
- ``donchian_pos_n``: (close - prior-n low) / (prior-n high - prior-n low), in [0, 1]
  inside the prior range, outside it on a breakout;
- ``ma_slope_n_k``: ``sma_n`` now / ``sma_n`` k bars ago - 1;
- ``efficiency_ratio_n``: |close_t - close_t-n| / sum(|close_i - close_i-1|) over the same
  n bars (1 = a straight line, near 0 = chop); NaN for a flat window.

The value at bar i uses bars <= i only and is known at ``ready_at[i]``.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from market_signal.models.domain import AssetClass
from market_signal.research.lab import features as lab_features
from market_signal.research.lab.vocabulary import parse_feature
from market_signal.research.structure.series import BarSeries, to_ts

_EXTRA = {
    "donchian_pos": re.compile(r"^donchian_pos_([1-9][0-9]*)$"),
    "ma_slope": re.compile(r"^ma_slope_([1-9][0-9]*)_([1-9][0-9]*)$"),
    "efficiency_ratio": re.compile(r"^efficiency_ratio_([1-9][0-9]*)$"),
}
MAX_N = 1000


def efficiency_ratio(close: pd.Series, n: int) -> pd.Series:
    net = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n, min_periods=n).sum()
    return (net / path).where(path > 0)


def _frame(series: BarSeries) -> pd.DataFrame:
    return pd.DataFrame({"open": series.o, "high": series.h, "low": series.l,
                         "close": series.c, "volume": series.v})  # fmt: skip


def regime_features(series: BarSeries, names: list[str]) -> pd.DataFrame:
    """Named regime measurements on ``series`` (index = bar close time)."""
    f = _frame(series)
    out = {}
    for name in names:
        extra = next(((k, m) for k, rx in _EXTRA.items() if (m := rx.fullmatch(name))), None)
        if extra is not None:
            kind, m = extra
            args = [int(g) for g in m.groups()]
            if any(a > MAX_N for a in args):
                raise ValueError(f"{name}: parameters must be <= {MAX_N}")
            if kind == "donchian_pos":
                hi = f["high"].rolling(args[0], min_periods=args[0]).max().shift(1)
                lo = f["low"].rolling(args[0], min_periods=args[0]).min().shift(1)
                out[name] = ((f["close"] - lo) / (hi - lo)).where(hi > lo)
            elif kind == "ma_slope":
                sma = f["close"].rolling(args[0], min_periods=args[0]).mean()
                out[name] = sma / sma.shift(args[1]) - 1
            else:
                out[name] = efficiency_ratio(f["close"], args[0])
            continue
        key = parse_feature(name)
        if key.spec.daily_only or key.spec.market == "perp":
            raise ValueError(
                f"{name} is daily-only or needs funding; not a bar-series regime input"
            )
        out[name] = lab_features.compute(key, f, AssetClass.CRYPTO)
    df = pd.DataFrame(out).astype(float)
    df = df.where(np.isfinite(df))
    df.index = to_ts(series.close_time).to_numpy()
    df.index.name = "close_time"
    return df
