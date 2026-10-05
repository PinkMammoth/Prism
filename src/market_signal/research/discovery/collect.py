"""Collect every strategy's signals and fixed-horizon outcomes on one venue.

Per coin: ``Frame`` primitives are computed ONCE per timeframe and shared by every variant
(vectorised masks; no per-variant recomputation). Per strategy and coin:

1. signal bars: ``signals.signal_mask`` inside the event window (signal bar CLOSE time);
2. availability: the bar's ``ready_at`` (close + the assumed 60 s latency);
3. **entry** (``next_15m_open_after_availability_v1``): the open of the first execution bar
   opening at or after availability. The execution grid is 15m (the long-context block uses
   1h, labelled). No same-bar hindsight: a 1H signal closing at 10:00 enters at the 10:15
   open; a 15m signal closing at 10:15 enters at 10:30. The signal-to-entry delay is recorded;
4. **exit** (``fixed_horizon_close_v1``): the close of the execution bar ending
   ``horizon`` after the entry bar opened. Incomplete paths (data end, a gap) are NaN, never
   truncated. No stops, no targets;
5. gross = d x (exit / entry - 1); cost = 2 x (fee + slippage) per side, frozen per venue/coin;
   funding = settlements in (entry, exit], longs pay positive rates; net = gross - cost -
   funding (NaN funding -> not evaluable, never zero-filled);
6. independence: greedy per coin, gap = the primary horizon in signal bars;
7. excess: net minus the matched baseline (every signal-timeframe bar of the same coin in the
   window entered by the same rule, same side, horizon and Family-J volatility tercile).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.backtest.events import decluster
from market_signal.research.discovery import signals as sg
from market_signal.research.discovery.catalogue import IntradayStrategy, VolTest
from market_signal.research.discovery.primitives import Frame
from market_signal.research.structure.series import NAT, BarSeries
from market_signal.research.structure.study.populations import funding_paid

MIN_NS = 60 * 10**9


@dataclass(eq=False)
class CoinInputs:
    coin: str
    series: dict[str, BarSeries]
    funding_ns: np.ndarray
    funding_rate: np.ndarray
    per_side: float


@dataclass(eq=False)
class VenueData:
    venue: str
    coins: dict[str, CoinInputs]
    reference: str = "BTC"
    contexts: dict = field(default_factory=dict)  # (coin, tf) -> SignalContext
    vol: dict = field(default_factory=dict)  # (coin, tf) -> vol tercile per bar
    _baseline: dict = field(default_factory=dict)

    def context(self, coin: str, tf: str) -> sg.SignalContext | None:
        key = (coin, tf)
        if key in self.contexts:
            return self.contexts[key]
        ci = self.coins[coin]
        s = ci.series.get(tf)
        if s is None or not len(s):
            self.contexts[key] = None
            return None
        f = Frame.from_series(s)
        htf_tf = "4h" if tf == "1h" else "1h"
        htf_s = ci.series.get(htf_tf)
        htf = Frame.from_series(htf_s) if htf_s is not None and len(htf_s) else None
        htf_idx = sg.align(htf_s.ready_at, s.ready_at) if htf is not None else None
        btc, btc_idx = None, None
        if coin != self.reference and self.reference in self.coins:
            bctx = self.context(self.reference, tf)
            if bctx is not None:
                btc = bctx.f
                btc_idx = sg.align(bctx.ready_at, s.ready_at)
        lvl = sg.funding_level(ci.funding_ns, ci.funding_rate, s.ready_at)
        ctx = sg.SignalContext(tf=tf, f=f, ready_at=s.ready_at, htf=htf, htf_idx=htf_idx,
                               btc=btc, btc_idx=btc_idx, funding_pct=sg.funding_pct(lvl, tf))  # fmt: skip
        self.contexts[key] = ctx
        self.vol[key] = sg.vol_bucket(ctx)
        return ctx


@dataclass(frozen=True)
class Exec:
    e: np.ndarray  # entry bar index on the execution series
    entry_ns: np.ndarray
    entry_px: np.ndarray
    exit_ns: np.ndarray
    last: np.ndarray
    gross_long: np.ndarray  # +1 orientation; NaN if incomplete


def execute(x: BarSeries, after_ns: np.ndarray, minutes: int) -> Exec:
    """Entry at the open of the first execution bar opening at/after ``after_ns``; exit at the
    close of the bar ``minutes`` later (complete, gap-free paths only)."""
    n = len(x)
    step = int(x.step)
    hb = max(int(minutes * MIN_NS // step), 1)
    e = np.searchsorted(x.open_time, np.asarray(after_ns, dtype=np.int64), side="left")
    last = e + hb - 1
    ok = (last < n) & (e < n)
    ec, lc = np.minimum(e, max(n - 1, 0)), np.minimum(last, max(n - 1, 0))
    if n:
        ok &= x.segment[ec] == x.segment[lc]
        entry = np.where(e < n, x.o[ec], np.nan)
        gross = np.where(ok, x.c[lc] / entry - 1.0, np.nan)
        return Exec(e, np.where(e < n, x.open_time[ec], NAT), entry,
                    np.where(ok, x.close_time[lc], NAT), last, gross)  # fmt: skip
    z = np.full(len(e), np.nan)
    return Exec(e, np.full(len(e), NAT), z, np.full(len(e), NAT), last, z)


def path_stats(x: BarSeries, ex: Exec, d: np.ndarray, threshold: np.ndarray) -> pd.DataFrame:
    """MFE / MAE (fractions, side-oriented), time to each (minutes from entry) and whether
    the favourable threshold was touched before the adverse one (same bar = ambiguous)."""
    rows = []
    step_min = x.step / MIN_NS
    for k in range(len(ex.e)):
        e, last = int(ex.e[k]), int(ex.last[k])
        if not np.isfinite(ex.gross_long[k]):
            rows.append((np.nan,) * 5 + (None,))
            continue
        px = ex.entry_px[k]
        hi, lo = x.h[e : last + 1] / px - 1.0, x.l[e : last + 1] / px - 1.0
        fav, adv = (hi, -lo) if d[k] > 0 else (-lo, hi)
        i_f, i_a = int(np.argmax(fav)), int(np.argmax(adv))
        th = threshold[k]
        hit_f = np.flatnonzero(fav >= th)
        hit_a = np.flatnonzero(adv >= th)
        f0 = hit_f[0] if len(hit_f) else np.inf
        a0 = hit_a[0] if len(hit_a) else np.inf
        first = ("none" if f0 == a0 == np.inf else "ambiguous" if f0 == a0
                 else "favourable" if f0 < a0 else "adverse")  # fmt: skip
        rows.append((float(fav.max()), float(-adv.max()), i_f * step_min, i_a * step_min,
                     th, first))  # fmt: skip
    return pd.DataFrame(rows, columns=["mfe", "mae", "t_mfe_min", "t_mae_min", "threshold",
                                       "first_touch"])  # fmt: skip


def _in_window(series: BarSeries, start_ns: int, end_ns: int) -> np.ndarray:
    return (series.close_time >= start_ns) & (series.close_time < end_ns)


def baseline(vd: VenueData, coin: str, tf: str, minutes: int, exec_tf: str, start_ns: int,
             end_ns: int) -> pd.DataFrame:  # fmt: skip
    """Every signal-timeframe bar of the coin in the window, entered by the same rule:
    mean long gross and funding by vol tercile (short = the negation)."""
    key = (coin, tf, minutes, exec_tf, start_ns, end_ns)
    if key in vd._baseline:
        return vd._baseline[key]
    ctx = vd.context(coin, tf)
    ci = vd.coins[coin]
    s = ci.series[tf]
    idx = np.flatnonzero(_in_window(s, start_ns, end_ns))
    ex = execute(ci.series[exec_tf], s.ready_at[idx], minutes)
    fl = funding_paid(ci.funding_ns, ci.funding_rate, ex.entry_ns, ex.exit_ns, np.ones(len(idx)))
    vol = vd.vol[(coin, tf)][idx] if ctx is not None else np.full(len(idx), -1)
    df = pd.DataFrame({"vol": vol, "g": ex.gross_long, "f": fl}).dropna()
    out = df.groupby("vol")[["g", "f"]].mean()
    vd._baseline[key] = out
    return out


def strategy_events(vd: VenueData, s: IntradayStrategy, start_ns: int, end_ns: int, *,
                    exec_tf: str = "15m") -> pd.DataFrame:  # fmt: skip
    """One row per signal (all coins), wide over the strategy's horizons."""
    parts = []
    d = 1 if s.side == "long" else -1
    for coin, ci in vd.coins.items():
        if s.universe == "alts" and coin == vd.reference:
            continue
        ctx = vd.context(coin, s.timeframe)
        if ctx is None or exec_tf not in ci.series or not len(ci.series[exec_tf]):
            continue
        ser = ci.series[s.timeframe]
        mask = sg.signal_mask(ctx, s) & _in_window(ser, start_ns, end_ns)
        idx = np.flatnonzero(mask)
        if not len(idx):
            continue
        keep = set(decluster(idx, s.primary_horizon).tolist())
        ready = ser.ready_at[idx]
        row = {"coin": coin, "i": idx, "d": d, "signal_close_ns": ser.close_time[idx],
               "signal_ns": ready, "independent": np.array([i in keep for i in idx]),
               "vol": vd.vol[(coin, s.timeframe)][idx],
               "atr_frac": ctx.f.atr[idx]}  # log ATR ~ a fraction of price  # fmt: skip
        for h in s.horizons:
            m = s.horizon_minutes(h)
            ex = execute(ci.series[exec_tf], ready, m)
            fund = funding_paid(ci.funding_ns, ci.funding_rate, ex.entry_ns, ex.exit_ns,
                                np.full(len(idx), d))  # fmt: skip
            gross = d * ex.gross_long
            net = gross - 2 * ci.per_side - fund
            base = baseline(vd, coin, s.timeframe, m, exec_tf, start_ns, end_ns)
            bg = (
                base["g"].reindex(row["vol"]).to_numpy() if len(base) else np.full(len(idx), np.nan)
            )
            bf = (
                base["f"].reindex(row["vol"]).to_numpy() if len(base) else np.full(len(idx), np.nan)
            )
            base_net = d * bg - 2 * ci.per_side - d * bf
            row[f"gross_{h}"] = gross
            row[f"fund_{h}"] = fund
            row[f"net_{h}"] = net
            row[f"excess_{h}"] = net - base_net
            if h == s.primary_horizon:
                row["entry_ns"] = ex.entry_ns
                row["exit_ns"] = ex.exit_ns
                row["delay_min"] = np.where(ex.entry_ns != NAT,
                                            (ex.entry_ns - ser.close_time[idx]) / MIN_NS, np.nan)  # fmt: skip
        parts.append(pd.DataFrame(row))
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    df["cost"] = df["coin"].map({c: 2 * vd.coins[c].per_side for c in vd.coins})
    return df.sort_values(["signal_ns", "coin"], kind="mergesort").reset_index(drop=True)


def strategy_paths(vd: VenueData, s: IntradayStrategy, ev: pd.DataFrame,
                   exec_tf: str = "15m") -> pd.DataFrame:  # fmt: skip
    """MFE/MAE at the primary horizon for the given (independent) events; the threshold for
    favourable-before-adverse is half the signal-timeframe ATR (as a fraction of price)."""
    out = []
    for coin, g in ev.groupby("coin", sort=True):
        ci = vd.coins[coin]
        ex = execute(ci.series[exec_tf], g["signal_ns"].to_numpy(np.int64), s.primary_minutes)
        p = path_stats(ci.series[exec_tf], ex, g["d"].to_numpy(), 0.5 * g["atr_frac"].to_numpy())
        p.index = g.index
        out.append(p)
    return pd.concat(out).sort_index() if out else pd.DataFrame()


def vol_events(vd: VenueData, v: VolTest, start_ns: int, end_ns: int,
               exec_tf: str = "15m") -> pd.DataFrame:  # fmt: skip
    """Non-directional: the absolute move |exit/entry - 1| after each state onset vs every
    bar of the coin (baseline), at each horizon, plus the move relative to the current ATR."""
    parts = []
    for coin, ci in vd.coins.items():
        ctx = vd.context(coin, v.timeframe)
        if ctx is None or exec_tf not in ci.series or not len(ci.series[exec_tf]):
            continue
        ser = ci.series[v.timeframe]
        inw = _in_window(ser, start_ns, end_ns)
        mask = sg.vol_mask(ctx, v) & inw
        idx = np.flatnonzero(mask)
        if not len(idx):
            continue
        keep = set(decluster(idx, v.primary_horizon).tolist())
        allx = np.flatnonzero(inw)
        row = {"coin": coin, "i": idx, "signal_ns": ser.ready_at[idx],
               "signal_close_ns": ser.close_time[idx],
               "independent": np.array([i in keep for i in idx])}  # fmt: skip
        atr = ctx.f.atr  # log ATR ~ a fraction of price
        for h in v.horizons:
            m = h * (15 if v.timeframe == "15m" else 60)
            ex = execute(ci.series[exec_tf], ser.ready_at[idx], m)
            exb = execute(ci.series[exec_tf], ser.ready_at[allx], m)
            base = np.nanmean(np.abs(exb.gross_long)) if len(allx) else np.nan
            base_rel = np.nanmean(np.abs(exb.gross_long) / atr[allx]) if len(allx) else np.nan
            row[f"abs_{h}"] = np.abs(ex.gross_long)
            row[f"absx_{h}"] = np.abs(ex.gross_long) - base
            row[f"relx_{h}"] = np.abs(ex.gross_long) / atr[idx] - base_rel
        parts.append(pd.DataFrame(row))
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).sort_values(["signal_ns", "coin"], kind="mergesort")


def raw_signal_counts(vd: VenueData, s: IntradayStrategy, start_ns: int, end_ns: int) -> int:
    n = 0
    for coin in vd.coins:
        if s.universe == "alts" and coin == vd.reference:
            continue
        ctx = vd.context(coin, s.timeframe)
        if ctx is None:
            continue
        n += int((sg.signal_mask(ctx, s) & _in_window(vd.coins[coin].series[s.timeframe],
                                                       start_ns, end_ns)).sum())  # fmt: skip
    return n


__all__ = ["CoinInputs", "VenueData", "baseline", "execute", "path_stats", "strategy_events",
           "strategy_paths", "vol_events"]  # fmt: skip
