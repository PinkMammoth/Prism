"""Rung populations, executable entries, returns, costs, funding and regime labels.

The ladder (one ``run_chain`` per level kind and parameter variant, shared detectors only):

- ``A_breach``: every qualifying breach, known at the breach bar (``available_ns``);
- ``B_failed``: breaches whose breakout FAILED, known at the failure bar;
- ``C_rejection``: B with ``rejection_v1`` (within_bars = 0, so known exactly when B is);
- ``D_shift``: C followed by ``structure_shift_v1``, known at the shift bar;
- ``E_retest``: D followed by ``retest_v1`` of the shift level, known at the retest bar;
- ``H_held``: the same breaches whose breakout HELD (the first-class comparator);
- ``M_stretch``: a plain N-bar price-extension control without any level narrative.

Every rung row keeps ``breach_id`` (and ``failed_id`` for B..E), so C/D/E are verifiably
subsets of their predecessors. A rung's availability is the max of its own stage's and
every earlier included stage's availability: confirmation is never back-dated.

**Entry.** The open of the first event-timeframe bar opening at or after the rung's
availability (``trade_path_v1``'s rule: an achievable action). Under the assumed latency a
bar closing at T is available at T + latency, so the entry is the open of the bar AFTER
the next one. A rung never inherits an earlier rung's entry price.

**Direction.** Reversal = against the breached side (short after a high-side event, long
after a low-side one); continuation = with it. One sign convention: ``d`` = +1 long, -1
short, and every return is ``d x (exit / entry - 1)``.

**Returns.** Gross: entry open to the close of the ``H``-th bar (entry bar included), the
same path ``trade_path_v1`` measures. Net: gross minus two sides of (fee + slippage), the
venue/coin costs frozen at registration from Prism's perp cost machinery. Funding: the sum
of settlements in (entry, exit], paid by longs when positive, reported separately (NaN
where the retained funding series does not span the holding window).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from market_signal.research.structure.chain import ChainResult
from market_signal.research.structure.regime import regime_features
from market_signal.research.structure.series import NAT, TF_SECONDS, BarSeries
from market_signal.research.structure.study.spec import RegimeSpec, StretchControl

SIDE_SIGN = {"high": 1, "low": -1}


def direction(side: np.ndarray, which: str) -> np.ndarray:
    """+1 long / -1 short. Reversal fades the breached side; continuation follows it."""
    s = np.where(np.asarray(side) == "high", 1, -1)
    return -s if which == "reversal" else s


# --------------------------------------------------------------------------- rungs


def _avail(*cols: np.ndarray) -> np.ndarray:
    """Max availability over stages; NAT if any included stage did not occur."""
    m = np.vstack([np.asarray(c, dtype=np.int64) for c in cols])
    return np.where((m != NAT).all(axis=0), m.max(axis=0), NAT)


def rung_frames(res: ChainResult) -> dict[str, pd.DataFrame]:
    """Rung tables from one chain result: key, breach_id, failed_id, side, breach_bar_ns,
    available_ns, atr (event ATR at the breach), invalidation (reversal direction)."""
    br = res.breaches
    s = br["side"].map(SIDE_SIGN).to_numpy(float) if len(br) else np.array([])
    # only the breach bar's own extreme is known at the breach
    inval = br["level_price"].to_numpy(float) + s * br["overshoot"].to_numpy(float)
    a = pd.DataFrame({
        "key": br["event_id"], "breach_id": br["event_id"], "failed_id": None,
        "side": br["side"], "breach_bar_ns": br["bar_ns"].astype(np.int64),
        "available_ns": br["available_ns"].astype(np.int64), "atr": br["atr"],
        "invalidation": inval,
    })  # fmt: skip
    out = {"A_breach": a}
    for rung, t in (("B_failed", res.failed), ("H_held", res.held)):
        out[rung] = pd.DataFrame({
            "key": t["event_id"], "breach_id": t["parent_id"],
            "failed_id": t["event_id"] if rung == "B_failed" else None, "side": t["side"],
            "breach_bar_ns": t["breach_bar_ns"].astype(np.int64) if len(t) else [],
            "available_ns": t["available_ns"].astype(np.int64) if len(t) else [],
            "atr": t["atr"], "invalidation": t["extreme"],
        })  # fmt: skip
    ch = res.chain
    if ch.empty:
        for rung in ("C_rejection", "D_shift", "E_retest"):
            out[rung] = out["B_failed"].iloc[:0].copy()
        return out
    f = ch["failed_available_ns"].to_numpy(np.int64)
    c_av = _avail(f, ch["rejection_available_ns"].to_numpy(np.int64))
    d_av = _avail(c_av, ch["shift_available_ns"].to_numpy(np.int64))
    e_av = _avail(d_av, ch["retest_available_ns"].to_numpy(np.int64))
    for rung, key, av in (("C_rejection", "rejection_id", c_av), ("D_shift", "shift_id", d_av),
                          ("E_retest", "retest_id", e_av)):  # fmt: skip
        keep = av != NAT
        sub = ch[keep]
        out[rung] = pd.DataFrame({
            "key": sub[key].to_numpy(), "breach_id": sub["breach_id"].to_numpy(),
            "failed_id": sub["failed_id"].to_numpy(), "side": sub["side"].to_numpy(),
            "breach_bar_ns": sub["breach_bar_ns"].to_numpy(np.int64),
            "available_ns": av[keep], "atr": sub["atr"].to_numpy(float),
            "invalidation": sub["extreme"].to_numpy(float),
        })  # fmt: skip
    return out


def stretch_events(series: BarSeries, ctl: StretchControl) -> pd.DataFrame:
    """``M_stretch``: the first bar whose ``lookback``-bar close change is at least
    ``threshold_atr`` x prior ATR up (side 'high') or down (side 'low'); the bar before must
    not already qualify. Known at the bar's ``ready_at``. No level and no invalidation."""
    n, k = len(series), ctl.lookback_bars
    z = np.full(n, np.nan)
    if n > k:
        same = series.segment[k:] == series.segment[:-k]
        z[k:] = np.where(same, (series.c[k:] - series.c[:-k]) / series.atr_prior[k:], np.nan)
    rows = []
    for side, sgn in (("high", 1), ("low", -1)):
        hit = sgn * z >= ctl.threshold_atr
        prev = np.concatenate([[False], hit[:-1]])
        for i in np.flatnonzero(hit & ~prev):
            rows.append({"key": f"stretch:{series.prov.coin}:{side}:{int(series.open_time[i])}",
                         "breach_id": None, "failed_id": None, "side": side,
                         "breach_bar_ns": int(series.open_time[i]),
                         "available_ns": int(series.ready_at[i]),
                         "atr": float(series.atr_prior[i]), "invalidation": np.nan})  # fmt: skip
    cols = ["key", "breach_id", "failed_id", "side", "breach_bar_ns", "available_ns", "atr",
            "invalidation"]  # fmt: skip
    return pd.DataFrame(rows, columns=cols).sort_values("breach_bar_ns", kind="stable")


# --------------------------------------------------------------------------- returns


@dataclass(frozen=True)
class Outcome:
    e_idx: np.ndarray  # entry bar index (len(series) if none)
    entry_ns: np.ndarray
    entry_price: np.ndarray
    exit_ns: np.ndarray
    gross: np.ndarray  # direction-adjusted, NaN if the path is incomplete


def outcomes(series: BarSeries, after_ns: np.ndarray, d: np.ndarray, h: int) -> Outcome:
    """Entry at the open of the first bar opening at/after ``after_ns``; exit at the close of
    bar ``e + h - 1``. Incomplete (past the data or across a gap) -> NaN, never truncated."""
    n = len(series)
    e = np.searchsorted(series.open_time, np.asarray(after_ns, dtype=np.int64), side="left")
    last = e + h - 1
    ok = last < n
    ok[ok] &= series.segment[e[ok]] == series.segment[last[ok]]
    ec, lc = np.minimum(e, n - 1), np.minimum(last, n - 1)
    entry = np.where(e < n, series.o[ec], np.nan)
    gross = np.where(ok, d * (series.c[lc] / entry - 1.0), np.nan)
    return Outcome(
        e_idx=e,
        entry_ns=np.where(e < n, series.open_time[ec], NAT),
        entry_price=entry,
        exit_ns=np.where(ok, series.close_time[lc], NAT),
        gross=gross,
    )


def funding_paid(t_ns: np.ndarray, rate: np.ndarray, entry_ns, exit_ns, d) -> np.ndarray:
    """Funding paid (fraction of notional) over settlements in (entry, exit]; positive rate
    means longs pay. NaN where the series does not span the window (never assumed zero)."""
    entry_ns = np.asarray(entry_ns, dtype=np.int64)
    exit_ns = np.asarray(exit_ns, dtype=np.int64)
    out = np.full(len(entry_ns), np.nan)
    if not len(t_ns):
        return out
    cs = np.concatenate([[0.0], np.cumsum(rate)])
    a = np.searchsorted(t_ns, entry_ns, side="right")
    b = np.searchsorted(t_ns, exit_ns, side="right")
    ok = (exit_ns != NAT) & (t_ns[0] <= entry_ns) & (t_ns[-1] >= exit_ns)
    out[ok] = (np.asarray(d) * (cs[b] - cs[a]))[ok]
    return out


# --------------------------------------------------------------------------- regime


@dataclass(frozen=True)
class Regime:
    """Per structure bar: trend direction (+1/-1/0), range flag, vol tercile (0/1/2, -1
    undefined); all known at that bar's ``ready_at``."""

    ready_at: np.ndarray
    trend: np.ndarray
    is_range: np.ndarray
    vol: np.ndarray


def regime(series: BarSeries, spec: RegimeSpec) -> Regime:
    f = regime_features(series, [f"efficiency_ratio_{spec.er_n}"])
    er = f[f"efficiency_ratio_{spec.er_n}"].to_numpy(float)
    ema = pd.Series(series.c).ewm(span=spec.trend_ema, adjust=False,
                                  min_periods=spec.trend_ema).mean().to_numpy()  # fmt: skip
    trend = np.where(np.isfinite(ema), np.sign(series.c - ema), 0).astype(int)
    is_range = np.where(np.isfinite(er), er < 1.0 / np.sqrt(spec.er_n), False)
    defined = np.isfinite(er) & np.isfinite(ema)
    w = int(spec.vol_rank_days * 86400 // TF_SECONDS[series.prov.timeframe])
    x = series.atr / series.c
    rank = np.full(len(x), np.nan)
    if len(x) >= w:
        win = sliding_window_view(x, w)
        seg = sliding_window_view(series.segment, w)
        r = (win <= win[:, -1:]).mean(axis=1)
        good = np.isfinite(win).all(axis=1) & (seg[:, 0] == seg[:, -1])
        rank[w - 1 :] = np.where(good, r, np.nan)
    # terciles of the causal rank: [0, 1/3) low, [1/3, 2/3) mid, [2/3, 1] high
    tercile = np.minimum(np.floor(np.nan_to_num(rank) * 3), 2)
    vol = np.where(np.isfinite(rank), tercile, -1).astype(int)
    trend = np.where(defined, trend, 0)
    return Regime(series.ready_at, trend, is_range & defined, vol)


def labels_at(reg: Regime, t_ns: np.ndarray, d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(regime label, vol bucket) at instants ``t_ns`` for directions ``d``, from the newest
    structure bar available by then. Label: range / with_trend / counter_trend / unknown."""
    i = np.searchsorted(reg.ready_at, np.asarray(t_ns, dtype=np.int64), side="right") - 1
    ok = i >= 0
    ic = np.maximum(i, 0)
    tr = np.where(ok, reg.trend[ic], 0)
    lab = np.where(tr == 0, "unknown", np.where(tr == d, "with_trend", "counter_trend"))
    lab = np.where(ok & reg.is_range[ic], "range", lab)
    vol = np.where(ok, reg.vol[ic], -1)
    return lab.astype(object), vol
