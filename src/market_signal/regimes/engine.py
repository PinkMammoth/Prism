"""Deterministic market-regime classifier.

Two regimes are computed on their own clocks:
  crypto — on BTC daily closes (00:00 UTC), from BTC trend/vol/drawdown + crypto breadth;
  macro  — on SPY daily closes (16:00 NY), from SPY/QQQ trend + PIT FRED series.
``regime_asof`` maps either onto any timestamps with a backward as-of join, so a crypto
bar closing at 00:00 UTC sees the macro regime of the *previous* US close — never a
future one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from market_signal.data.pit import asof_values, load_macro
from market_signal.data.store import Store
from market_signal.models.domain import Regime


def _vote(cond_good: pd.Series | None, cond_bad: pd.Series | None, valid: pd.Series) -> pd.Series:
    v = pd.Series(0.0, index=valid.index)
    if cond_good is not None:
        v = v.mask(cond_good.fillna(False).astype(bool), 1.0)
    if cond_bad is not None:
        v = v.mask(cond_bad.fillna(False).astype(bool), -1.0)
    return v.where(valid, np.nan)


def _combine(
    votes: pd.DataFrame, weights: dict[str, float], thresholds: dict[str, float], min_cov: float
) -> pd.DataFrame:
    w = pd.Series(weights)[votes.columns]
    available = votes.notna()
    wsum = available.mul(w, axis=1).sum(axis=1)
    score = votes.fillna(0).mul(w, axis=1).sum(axis=1) / wsum.replace(0, np.nan)
    coverage = wsum / w.sum()
    regime = pd.Series(Regime.NEUTRAL.value, index=votes.index)
    regime[score >= thresholds["risk_on"]] = Regime.RISK_ON.value
    regime[score <= thresholds["risk_off"]] = Regime.RISK_OFF.value
    regime[(coverage < min_cov) | score.isna()] = Regime.UNKNOWN.value
    out = votes.add_prefix("vote_")
    out["score"] = score
    out["coverage"] = coverage
    out["regime"] = regime
    return out


def classify_crypto(
    btc: pd.DataFrame, breadth_members: dict[str, pd.DataFrame], cfg: dict[str, Any]
) -> pd.DataFrame:
    """btc: feature frame (compute_features). breadth_members: symbol -> feature frame."""
    f = cfg["factors"]
    idx = pd.DatetimeIndex(btc["close_time"])
    b = btc.set_index(idx)
    votes = pd.DataFrame(index=idx)
    votes["above_sma200"] = _vote(b.close > b.sma_200, b.close <= b.sma_200, b.sma_200.notna())
    votes["sma50_above_sma200"] = _vote(
        b.sma_50 > b.sma_200, b.sma_50 <= b.sma_200, b.sma_200.notna()
    )
    votes["sma200_rising"] = _vote(
        b.sma_200_slope > 0, b.sma_200_slope <= 0, b.sma_200_slope.notna()
    )
    dd = f["drawdown"]
    votes["drawdown"] = _vote(
        b.dist_52w_high > dd["ok_above"], b.dist_52w_high < dd["bad_below"], b.dist_52w_high.notna()
    )
    votes["vol_extreme"] = _vote(
        None, b.rvol_pct > f["vol_extreme"]["pct_above"], b.rvol_pct.notna()
    )

    br = f["breadth"]
    flags = []
    for sym, feat in breadth_members.items():
        s = pd.Series(
            np.where(
                feat["sma_100"].notna(), (feat["close"] > feat["sma_100"]).astype(float), np.nan
            ),
            index=pd.DatetimeIndex(feat["close_time"]),
            name=sym,
        )
        flags.append(s.reindex(idx))  # same 24/7 clock: exact alignment, no fill
    if flags:
        m = pd.concat(flags, axis=1)
        n = m.notna().sum(axis=1)
        share = m.sum(axis=1) / n.replace(0, np.nan)
        valid = n >= br["min_assets"]
        votes["breadth"] = _vote(share > br["good_above"], share < br["bad_below"], valid)
        breadth_share = share.where(valid)
    else:
        votes["breadth"] = np.nan
        breadth_share = pd.Series(np.nan, index=idx)
    out = _combine(
        votes, {k: float(v["weight"]) for k, v in f.items()}, cfg["thresholds"], cfg["min_coverage"]
    )
    out["breadth_share"] = breadth_share
    return out


def derived_macro_rows(rows: pd.DataFrame, kind: str, lookback: int = 0) -> pd.DataFrame:
    """Derive a PIT-safe transform of a *single-vintage* series on its own obs clock.

    The derived value at obs o uses obs <= o only, and inherits o's available_at.
    Vintage (multi-row) series are refused: a rolling stat would need per-date histories.
    """
    if rows.empty:
        return rows
    if rows["obs_date"].duplicated().any():
        raise ValueError("derived_macro_rows requires single-vintage rows")
    r = rows.sort_values("obs_date").reset_index(drop=True).copy()
    s = r["value"]
    if kind == "diff":
        r["value"] = s - s.shift(lookback)  # NaNs in the source propagate
    elif kind == "pct":
        r["value"] = s / s.shift(lookback) - 1
    elif kind != "level":
        raise ValueError(kind)
    return r


def classify_macro(
    spy: pd.DataFrame | None,
    qqq: pd.DataFrame | None,
    macro_rows: dict[str, pd.DataFrame],
    cfg: dict[str, Any],
) -> pd.DataFrame:
    f = cfg["factors"]
    if spy is None or spy.empty:
        raise ValueError("macro regime needs SPY features")
    idx = pd.DatetimeIndex(spy["close_time"])
    s = spy.set_index(idx)
    votes = pd.DataFrame(index=idx)
    votes["spy_above_sma200"] = _vote(s.close > s.sma_200, s.close <= s.sma_200, s.sma_200.notna())
    votes["spy_sma50_above_sma200"] = _vote(
        s.sma_50 > s.sma_200, s.sma_50 <= s.sma_200, s.sma_200.notna()
    )
    if qqq is not None and not qqq.empty:
        q = qqq.set_index(pd.DatetimeIndex(qqq["close_time"])).reindex(idx)
        votes["qqq_above_sma200"] = _vote(
            q.close > q.sma_200, q.close <= q.sma_200, q.sma_200.notna()
        )
    else:
        votes["qqq_above_sma200"] = np.nan

    def pit(series: str, kind: str = "level", lookback: int = 0) -> pd.Series:
        rows = macro_rows.get(series)
        if rows is None or rows.empty:
            return pd.Series(np.nan, index=idx)
        return pd.Series(
            asof_values(derived_macro_rows(rows, kind, lookback), idx)["value"].to_numpy(),
            index=idx,
        )

    vix = pit(f["vix"]["series"])
    votes["vix"] = _vote(vix < f["vix"]["good_below"], vix > f["vix"]["bad_above"], vix.notna())
    cw = f["credit_widening"]
    credit = pit(cw["series"], "diff", cw["lookback_obs"])
    votes["credit_widening"] = _vote(
        credit < cw["good_below"], credit > cw["bad_above"], credit.notna()
    )
    curve = pit(f["curve_inverted"]["series"])
    votes["curve_inverted"] = _vote(None, curve < 0, curve.notna())
    us = f["usd_surge"]
    usd = pit(us["series"], "pct", us["lookback_obs"])
    votes["usd_surge"] = _vote(None, usd > us["bad_above"], usd.notna())
    out = _combine(
        votes, {k: float(v["weight"]) for k, v in f.items()}, cfg["thresholds"], cfg["min_coverage"]
    )
    out["vix"], out["credit_chg"], out["curve"], out["usd_chg"] = vix, credit, curve, usd
    return out


def regime_asof(regime: pd.DataFrame, times: pd.Series | pd.DatetimeIndex) -> pd.DataFrame:
    """Backward as-of join of a regime frame (indexed by close_time) onto ``times``."""
    t = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
    left = pd.DataFrame({"t": t, "_pos": np.arange(len(t))}).sort_values("t", kind="stable")
    r = regime.copy()
    r.index.name = "close_time"
    r = r.reset_index().sort_values("close_time")
    r["close_time"] = r["close_time"].astype(left["t"].dtype)
    merged = pd.merge_asof(left, r, left_on="t", right_on="close_time", direction="backward")
    merged = merged.sort_values("_pos").drop(columns="_pos")
    merged["regime"] = merged["regime"].fillna(Regime.UNKNOWN.value)
    return merged.set_index("t")


@dataclass
class RegimeBundle:
    crypto: pd.DataFrame | None
    macro: pd.DataFrame | None
    policy: dict[str, Any]

    def for_class(self, asset_class: str, times) -> pd.Series:
        which = self.policy["governing"].get(asset_class, "macro")
        frame = self.crypto if which == "crypto" else self.macro
        if frame is None or frame.empty:
            return pd.Series(
                Regime.UNKNOWN.value, index=pd.DatetimeIndex(pd.to_datetime(times, utc=True))
            )
        return regime_asof(frame, times)["regime"]


def build_regimes(store: Store, settings, feature_loader) -> RegimeBundle:
    """Compute both regimes from stored data. ``feature_loader(symbol) -> features|None``."""
    cfg = settings.yaml("regimes.yaml")
    crypto = macro = None
    ccfg = cfg["crypto"]
    btc = feature_loader(ccfg["benchmark"])
    if btc is not None and not btc.empty:
        members = {s: feature_loader(s) for s in ccfg["breadth_universe"]}
        members = {k: v for k, v in members.items() if v is not None and not v.empty}
        crypto = classify_crypto(btc, members, ccfg)
    spy = feature_loader("SPY")
    if spy is not None and not spy.empty:
        mcfg = cfg["macro"]
        series = {v["series"] for v in mcfg["factors"].values() if "series" in v}
        rows = {sid: load_macro(store, sid, research=True) for sid in series}
        macro = classify_macro(spy, feature_loader("QQQ"), rows, mcfg)
    return RegimeBundle(crypto, macro, cfg["policy"])
