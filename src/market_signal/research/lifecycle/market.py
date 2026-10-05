"""Coarse causal market state (``market_state_v1``) and objective stress (``market_stress_v1``).

Market state, at each daily close T of the reference coin (BTC), from data closed by T:

- trend: BTC close vs its 100-day SMA, ``up`` above +3 %, ``down`` below -3 %, else
  ``neutral``;
- vol: BTC 30-day realised volatility, ranked within its own trailing 730 days (at least 365
  observations): ``low`` (bottom third), ``normal``, ``high`` (top third);
- breadth: share of the venue's coins closing above their own 50-day SMA (at least 3 coins
  defined): ``risk_on`` >= 2/3, ``risk_off`` <= 1/3, else ``mixed``;
- funding: cross-coin median of each coin's 7-day mean Lab causal funding, ranked within the
  trailing 365 days (at least 180 observations); ``unknown`` where funding is absent.

A small vocabulary on purpose. Similar-regime evidence is an exact match on coarse labels
(policy ``similarity``), never a fitted classifier.

Stress days, on BTC: |1-day return| >= 10 %, 7-day return <= -25 %, 7-day realised
volatility >= 3x its trailing 365-day median (prior days only), or a missing daily bar (a
data/market discontinuity). Prism holds no liquidation tape, so a liquidation proxy is
recorded as unavailable rather than invented. A stress episode is a run of stress days plus
the following 7 days. Stress labels are descriptive: they split reporting into all / normal
/ stress periods and are never used to delete outcomes or to drive lifecycle decisions.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.research.lifecycle.policy import MarketState, Stress


def _rank_last(window: np.ndarray) -> float:
    w = window[np.isfinite(window)]
    return float((w <= w[-1]).mean()) if len(w) and np.isfinite(window[-1]) else np.nan


def _tercile(x: pd.Series, lo: float, hi: float, labels: tuple[str, str, str]) -> pd.Series:
    out = pd.Series("unknown", index=x.index, dtype=object)
    out[x <= lo] = labels[0]
    out[(x > lo) & (x < hi)] = labels[1]
    out[x >= hi] = labels[2]
    out[x.isna()] = "unknown"
    return out


def market_state(frames: dict[str, pd.DataFrame], ref: str, cfg: MarketState) -> pd.DataFrame:
    """Daily labels indexed by the reference coin's ``close_time``.

    ``frames``: coin -> daily frame with ``close_time``, ``close`` and optional
    ``funding_day`` (the Lab's causal daily funding)."""
    btc = frames[ref].set_index(pd.to_datetime(frames[ref]["close_time"], utc=True))
    close = btc["close"].astype(float)
    idx = close.index
    sma = close.rolling(cfg.trend_sma, min_periods=cfg.trend_sma).mean()
    r = close / sma - 1
    trend = pd.Series("unknown", index=idx, dtype=object)
    trend[r >= cfg.trend_band] = "up"
    trend[r <= -cfg.trend_band] = "down"
    trend[(r > -cfg.trend_band) & (r < cfg.trend_band)] = "neutral"

    lr = np.log(close).diff()
    rv = lr.rolling(cfg.vol_days, min_periods=cfg.vol_days).std() * np.sqrt(365)
    vrank = rv.rolling(cfg.vol_rank_lookback_days, min_periods=cfg.vol_rank_min_obs).apply(
        _rank_last, raw=True
    )
    vol = _tercile(vrank, cfg.vol_low, cfg.vol_high, ("low", "normal", "high"))

    above, fund = [], []
    for coin, f in sorted(frames.items()):
        g = f.set_index(pd.to_datetime(f["close_time"], utc=True))
        c = g["close"].astype(float)
        s50 = c.rolling(cfg.breadth_sma, min_periods=cfg.breadth_sma).mean()
        above.append((c > s50).where(s50.notna()).reindex(idx).rename(coin))
        if "funding_day" in g:
            fm = g["funding_day"].astype(float).rolling(cfg.funding_mean_days,
                                                       min_periods=cfg.funding_mean_days).mean()  # fmt: skip
            fund.append(fm.reindex(idx).rename(coin))
    ab = pd.concat(above, axis=1).astype(float)
    defined = ab.notna().sum(axis=1)
    share = ab.mean(axis=1).where(defined >= cfg.breadth_min_coins)
    breadth = pd.Series("unknown", index=idx, dtype=object)
    breadth[share >= cfg.breadth_on] = "risk_on"
    breadth[share <= cfg.breadth_off] = "risk_off"
    breadth[(share > cfg.breadth_off) & (share < cfg.breadth_on)] = "mixed"

    if fund:
        med = pd.concat(fund, axis=1).median(axis=1, skipna=True)
        frank = med.rolling(cfg.funding_rank_lookback_days,
                            min_periods=cfg.funding_rank_min_obs).apply(_rank_last, raw=True)  # fmt: skip
        funding = _tercile(frank, cfg.funding_low, cfg.funding_high, ("low", "normal", "high"))
    else:
        funding = pd.Series("unknown", index=idx, dtype=object)
    return pd.DataFrame({"trend": trend, "vol": vol, "breadth": breadth, "funding": funding,
                         "trend_ratio": r, "vol_rank": vrank, "breadth_share": share})  # fmt: skip


def stress_days(ref: pd.DataFrame, cfg: Stress) -> pd.DataFrame:
    """Per reference-coin daily close: stress triggers (causal) and the episode flag."""
    g = ref.set_index(pd.to_datetime(ref["close_time"], utc=True))
    close = g["close"].astype(float)
    idx = close.index
    gap = pd.Series(idx, index=idx).diff() > pd.Timedelta(days=1)
    r1 = close.pct_change()
    r7 = close / close.shift(7) - 1
    lr = np.log(close).diff()
    v7 = lr.rolling(7, min_periods=7).std()
    base = v7.rolling(cfg.vol_baseline_days, min_periods=cfg.vol_baseline_min_obs).median().shift(1)
    trig = pd.DataFrame({
        "extreme_return": r1.abs() >= cfg.abs_return_1d,
        "drawdown_7d": r7 <= -cfg.drawdown_7d,
        "vol_spike": v7 >= cfg.vol_7d_multiple * base,
        "discontinuity": gap,
    }, index=idx).fillna(False)  # fmt: skip
    day = trig.any(axis=1)
    # episode: a stress day and the following ``episode_tail_days`` days (by elapsed time)
    stamps = idx[day.to_numpy()]
    episode = np.zeros(len(idx), dtype=bool)
    tail = pd.Timedelta(days=cfg.episode_tail_days)
    for s in stamps:
        episode |= (idx >= s) & (idx <= s + tail)
    out = trig.copy()
    out["stress_day"] = day
    out["episode"] = episode
    out["return_1d"] = r1
    out["return_7d"] = r7
    return out


def episodes(days: pd.DataFrame) -> list[dict]:
    """Merged stress episodes with their triggers and the worst reference-coin moves."""
    flag = days["episode"].to_numpy()
    idx = days.index
    out, i = [], 0
    while i < len(flag):
        if not flag[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(flag) and flag[j + 1]:
            j += 1
        sl = days.iloc[i : j + 1]
        trig = [c for c in ("extreme_return", "drawdown_7d", "vol_spike", "discontinuity")
                if sl[c].any()]  # fmt: skip
        out.append({"start": idx[i].isoformat(), "end": idx[j].isoformat(),
                    "days": int(j - i + 1), "triggers": trig,
                    "worst_return_1d": float(np.nanmin(sl["return_1d"])),
                    "worst_return_7d": float(np.nanmin(sl["return_7d"]))
                    if sl["return_7d"].notna().any() else None})  # fmt: skip
        i = j + 1
    return out


def label_events(events: pd.DataFrame, state: pd.DataFrame, days: pd.DataFrame) -> pd.DataFrame:
    """Attach the market state at each signal time (backward as-of: the latest reference
    close <= the signal close) and whether the holding window (signal, resolution] touched a
    stress episode."""
    out = events.copy()
    st = state.sort_index()
    sig = pd.to_datetime(out["signal_time"], utc=True)
    pos = st.index.searchsorted(sig, side="right") - 1
    for d in ("trend", "vol", "breadth", "funding"):
        vals = st[d].to_numpy()
        out[f"regime_{d}"] = [vals[p] if p >= 0 else "unknown" for p in pos]
    ep = days["episode"].astype(int)
    cum = np.concatenate([[0], np.cumsum(ep.to_numpy())])
    di = days.index
    a = di.searchsorted(sig, side="right")
    b = di.searchsorted(pd.to_datetime(out["resolved_at"], utc=True), side="right")
    out["stress"] = (cum[b] - cum[a]) > 0
    return out
