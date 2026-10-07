"""Opportunity (activity) state: "is market activity elevated?" — never a direction.

Built from the only Phase 22 findings that held up: abnormal range expansion and volume
spikes precede *larger absolute* moves (volatility, not direction). Definitions are the
Phase 22 primitives themselves (``intraday_features_v1``), on closed 1h bars Prism had
observed by ``t`` (``load_bars(..., known_at=t)``, revisions undone):

* ``range_expansion``: log true range / prior ATR >= 2.5 on the newest closed bar
  (``expansion_state[k=2.5]``);
* ``volume_spike``: volume / prior 20-bar mean >= 3 (``volume_spike_state[r=3]``);
* ``rv_state``: realised-vol(24) percentile over 30 days in frozen terciles low/normal/high.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from market_signal.data.store import Store

OPPORTUNITY_VERSION = "opportunity_state_v1"
EXPANSION_K, SPIKE_R, PCT_W = 2.5, 3.0, 720
STALE = pd.Timedelta(hours=3)


def _segments(open_time: pd.Series) -> np.ndarray:
    gaps = open_time.diff() != pd.Timedelta(hours=1)
    return np.cumsum(gaps.to_numpy()).astype(np.int64)


def opportunity_state(store: Store, coin: str, t: datetime, source: str = "hyperliquid") -> dict:
    from market_signal.intraday.bars import load_bars
    from market_signal.research.discovery.primitives import Frame

    t = pd.Timestamp(t).tz_convert("UTC")
    out = {"version": OPPORTUNITY_VERSION, "coin": coin.upper(), "source": source,
           "as_of": t.isoformat(), "state": "unknown", "range_expansion": None,
           "volume_spike": None, "rv_state": None, "tr_atr": None, "rel_volume": None,
           "rv_pct_30d": None, "bar_close": None, "stale": None,
           "note": "activity/volatility state only; not directional"}  # fmt: skip
    try:
        bars = load_bars(
            store, source, coin.upper(), "1h", start=t - pd.Timedelta(days=40), known_at=t
        )
    except Exception:
        return out
    if len(bars) < 50:
        return out
    f = Frame(bars["open"].to_numpy(float), bars["high"].to_numpy(float), bars["low"].to_numpy(float),
              bars["close"].to_numpy(float), bars["volume"].to_numpy(float), _segments(bars["open_time"]))  # fmt: skip
    tra, rv = f.tr_atr[-1], f.rel_volume(20)[-1]
    rvp = f.rv_pct(24, PCT_W)[-1]
    close = pd.Timestamp(bars["close_time"].iloc[-1])
    stale = t - close > STALE
    rv_state = None if not np.isfinite(rvp) else ("low", "normal", "high")[min(int(rvp * 3), 2)]
    exp = None if not np.isfinite(tra) else bool(tra >= EXPANSION_K)
    spike = None if not np.isfinite(rv) else bool(rv >= SPIKE_R)
    elevated = bool(exp) or bool(spike) or rv_state == "high"
    known = [x is not None for x in (exp, spike, rv_state)]
    out.update(range_expansion=exp, volume_spike=spike, rv_state=rv_state,
               tr_atr=None if not np.isfinite(tra) else float(tra),
               rel_volume=None if not np.isfinite(rv) else float(rv),
               rv_pct_30d=None if not np.isfinite(rvp) else float(rvp),
               bar_close=close.isoformat(), stale=bool(stale),
               state="unknown" if stale or not any(known) else "elevated" if elevated
               else "quiet" if rv_state == "low" else "normal")  # fmt: skip
    return out
