"""Collect every event, baseline bar, path and chain the study needs, one coin at a time.

Nothing here aggregates across coins or venues; ``analysis`` does that, per venue only.
For each coin of one venue and architecture:

1. series from the retained snapshot (structure / event / confirmation / resolution);
2. the regime vocabulary on the structure series;
3. the baseline: every event-timeframe bar opening inside the event window, entered at its
   own open, for both directions and every horizon (same cost model as the events);
4. per level kind and variant: ``run_chain`` -> rung populations -> entries -> returns,
   with each event's vol bucket and regime label at its entry instant, and the excess
   over its matched baseline cell (same coin, direction, horizon, vol tercile);
5. for the central variant only: ``trade_path_v1`` (MFE/MAE, thresholds, R with 15m
   ambiguity resolution), the chain table and the failure-bar rejection metrics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.backtest.events import decluster
from market_signal.research.structure import events as sev
from market_signal.research.structure import path as tp
from market_signal.research.structure.chain import run_chain
from market_signal.research.structure.registry import TradePathParams
from market_signal.research.structure.series import NAT
from market_signal.research.structure.study import populations as pop
from market_signal.research.structure.study.data import CoinData
from market_signal.research.structure.study.spec import (
    DIRECTIONS,
    LEVEL_KINDS,
    R_RUNGS,
    STRETCH,
    Architecture,
    StudyDefinition,
)

VOL_NAMES = {0: "vol_low", 1: "vol_mid", 2: "vol_high"}


@dataclass
class Collected:
    venue: str
    arch: str
    events: list[pd.DataFrame] = field(default_factory=list)  # central, every horizon
    neighbours: list[pd.DataFrame] = field(default_factory=list)  # primary horizon, compact
    baseline: list[pd.DataFrame] = field(default_factory=list)
    paths: list[pd.DataFrame] = field(default_factory=list)
    thresholds: list[pd.DataFrame] = field(default_factory=list)
    chains: list[pd.DataFrame] = field(default_factory=list)
    bmetrics: list[pd.DataFrame] = field(default_factory=list)
    inputs: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    seconds: dict = field(default_factory=dict)

    def frame(self, name: str) -> pd.DataFrame:
        parts = [p for p in getattr(self, name) if len(p)]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _baseline(series, start, end, horizons, cost, cd: CoinData, reg) -> pd.DataFrame:
    j = np.flatnonzero((series.open_time >= start) & (series.open_time < end))
    rows = []
    for which in (1, -1):
        d = np.full(len(j), which)
        lab, vol = pop.labels_at(reg, series.open_time[j], d)
        for h in horizons:
            o = pop.outcomes(series, series.open_time[j], d, h)
            ok = np.isfinite(o.gross) & (vol >= 0)
            fund = pop.funding_paid(cd.funding_ns, cd.funding_rate, o.entry_ns, o.exit_ns, d)
            net = o.gross - 2 * cost.per_side
            rows.append(pd.DataFrame({"coin": cd.coin, "d": which, "horizon": h, "e_idx": j[ok],
                                      "net": net[ok], "net_f": (net - fund)[ok],
                                      "regime": lab[ok], "vol": vol[ok]}))  # fmt: skip
    return pd.concat(rows, ignore_index=True)


def _cell_means(base: pd.DataFrame, keys: list[str]) -> pd.Series:
    return base.groupby(keys)["net"].mean()


def _events(series, rungs: dict, start, end, horizons, cost, cd: CoinData, reg, means,
            means_reg) -> pd.DataFrame:  # fmt: skip
    """One row per (rung, direction, horizon, event) inside the event window."""
    out = []
    for rung, f in rungs.items():
        f = f[(f["breach_bar_ns"] >= start) & (f["breach_bar_ns"] < end)]
        if f.empty:
            continue
        for which in DIRECTIONS:
            d = pop.direction(f["side"].to_numpy(), which)
            after = f["available_ns"].to_numpy(np.int64)
            for h in horizons:
                o = pop.outcomes(series, after, d, h)
                lab, vol = pop.labels_at(reg, o.entry_ns, d)
                net = o.gross - 2 * cost.per_side
                fund = pop.funding_paid(cd.funding_ns, cd.funding_rate, o.entry_ns, o.exit_ns, d)
                t = pd.DataFrame({
                    "rung": rung, "direction": which, "horizon": h, "coin": cd.coin,
                    "key": f["key"].to_numpy(), "breach_id": f["breach_id"].to_numpy(),
                    "failed_id": f["failed_id"].to_numpy(), "side": f["side"].to_numpy(), "d": d,
                    "available_ns": after, "e_idx": o.e_idx, "entry_ns": o.entry_ns,
                    "entry_price": o.entry_price, "exit_ns": o.exit_ns, "gross": o.gross,
                    "net": net, "fund": fund, "net_f": net - fund, "regime": lab, "vol": vol,
                    "atr": f["atr"].to_numpy(float),
                    "invalidation": f["invalidation"].to_numpy(float),
                })  # fmt: skip
                key = pd.MultiIndex.from_arrays([t["d"], t["horizon"], t["vol"]])
                t["base"] = means.reindex(key).to_numpy()
                kreg = pd.MultiIndex.from_arrays([t["d"], t["horizon"], t["vol"], t["regime"]])
                t["base_reg"] = means_reg.reindex(kreg).to_numpy()
                t["excess"] = t["net"] - t["base"]
                t["excess_reg"] = t["net"] - t["base_reg"]
                t["evaluable"] = np.isfinite(t["excess"].to_numpy(float))
                # independent events: greedy, gap = horizon bars, per coin and direction sign
                t["independent"] = False
                for s in (1, -1):
                    m = t["evaluable"].to_numpy() & (t["d"].to_numpy() == s)
                    idx = np.flatnonzero(m)
                    if len(idx):
                        keep = set(decluster(t["e_idx"].to_numpy()[idx], h).tolist())
                        sel = np.array([e in keep for e in t["e_idx"].to_numpy()[idx]])
                        # several events can share an entry bar: keep the first per bar
                        first = ~pd.Series(t["e_idx"].to_numpy()[idx]).duplicated().to_numpy()
                        t.loc[t.index[idx[sel & first]], "independent"] = True
                out.append(t)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def _paths(series, child, ev: pd.DataFrame, h: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """trade_path_v1 at the primary horizon for central events (R only where the reversal
    direction has an objective invalidation)."""
    sub = ev[(ev["horizon"] == h)].drop_duplicates(["rung", "direction", "key"])
    if sub.empty:
        return pd.DataFrame(), pd.DataFrame()
    rev_r = (sub["direction"] == "reversal") & sub["rung"].isin(R_RUNGS)
    pid = sub["rung"] + "|" + sub["direction"] + "|" + sub["key"].astype(str)
    entries = pd.DataFrame({"event_id": pid.to_numpy(),
                            "entry_after_ns": sub["available_ns"].to_numpy(np.int64),
                            "direction": sub["d"].to_numpy(int), "atr": sub["atr"].to_numpy(float),
                            "invalidation": np.where(rev_r, sub["invalidation"], np.nan)})  # fmt: skip
    params = TradePathParams(horizons=(h,), pct_levels=(1.0,), atr_levels=(1.0,))
    paths, thr = tp.trade_paths(series, entries, params, resolve_with=child)
    return paths, thr


def collect(defn: StudyDefinition, arch: Architecture, venue: str,
            coins: dict[str, CoinData]) -> Collected:  # fmt: skip
    win = arch.window(venue)
    start = int(pd.Timestamp(win.event_start).value)
    end = int(pd.Timestamp(win.event_end).value)
    hp = arch.primary_horizon
    col = Collected(venue, arch.name)
    for coin, cd in coins.items():
        t0 = time.perf_counter()
        S = cd.series[arch.structure_tf]
        E = cd.series[arch.event_tf]
        C = cd.series[arch.confirm_tf]
        R = cd.series.get(arch.resolve_tf) if arch.resolve_tf else None
        cost = defn.cost(venue, coin)
        col.inputs[coin] = {tf: {"rows": len(s), "sha256": s.fingerprint(),
                                 "first_open": None if not len(s) else int(s.open_time[0]),
                                 "last_open": None if not len(s) else int(s.open_time[-1])}
                            for tf, s in cd.series.items()}  # fmt: skip
        col.inputs[coin]["funding_rows"] = len(cd.funding_ns)
        if not len(E) or not len(S):
            continue
        reg = pop.regime(S, arch.regime)
        base = _baseline(E, start, end, arch.horizons, cost, cd, reg)
        col.baseline.append(base)
        means = _cell_means(base, ["d", "horizon", "vol"])
        means_reg = _cell_means(base, ["d", "horizon", "vol", "regime"])
        metrics = sev.rejection_metrics(E)
        n_bars = int(((E.open_time >= start) & (E.open_time < end)).sum())
        col.counts.setdefault("event_bars_in_window", 0)
        col.counts["event_bars_in_window"] += n_bars
        if arch.stretch_control is not None:
            st = pop.stretch_events(E, arch.stretch_control)
            ev = _events(E, {STRETCH: st}, start, end, arch.horizons, cost, cd, reg, means,
                         means_reg)  # fmt: skip
            if len(ev):
                col.events.append(ev.assign(kind="none", variant="central"))
                p, thr = _paths(E, R, ev, hp)
                if len(p):
                    col.paths.append(p.assign(kind="none", coin=coin))
                    col.thresholds.append(thr.assign(kind="none", coin=coin))
        for kind in LEVEL_KINDS:
            for vkey, chain, moved in arch.variants(kind):
                spec = arch.chain_spec(venue, kind, chain)
                res = run_chain(spec, S, E, C)
                rungs = pop.rung_frames(res)
                central = vkey == "central"
                hs = arch.horizons if central else (hp,)
                ev = _events(E, rungs, start, end, hs, cost, cd, reg, means, means_reg)
                if ev.empty:
                    continue
                if not central:
                    keep = ev[ev["independent"]][["rung", "direction", "coin", "d", "excess",
                                                  "net"]]  # fmt: skip
                    col.neighbours.append(keep.assign(kind=kind, variant=vkey,
                                                      **{f"ax_{k}": v for k, v in moved.items()}))  # fmt: skip
                    continue
                col.events.append(ev.assign(kind=kind, variant="central"))
                p, thr = _paths(E, R, ev, hp)
                if len(p):
                    col.paths.append(p.assign(kind=kind, coin=coin))
                    col.thresholds.append(thr.assign(kind=kind, coin=coin))
                ch = res.chain.copy()
                if len(ch):
                    ch = ch[(ch["breach_bar_ns"] >= start) & (ch["breach_bar_ns"] < end)]
                    rt = res.retests
                    if rt is not None and len(rt):
                        ch = ch.merge(rt[["parent_id", "delay_hours", "delay_bars"]].rename(
                            columns={"parent_id": "shift_id", "delay_hours": "retest_wait_hours",
                                     "delay_bars": "retest_wait_bars"}), on="shift_id", how="left")  # fmt: skip
                    col.chains.append(ch.assign(kind=kind, coin=coin))
                f = res.failed
                if len(f):
                    f = f[(f["breach_bar_ns"] >= start) & (f["breach_bar_ns"] < end)]
                    i = f["bar_idx"].to_numpy(int)
                    side = f["side"].to_numpy()
                    hi = side == "high"
                    wr = np.where(hi, metrics["wick_range_high"].to_numpy()[i],
                                  metrics["wick_range_low"].to_numpy()[i])  # fmt: skip
                    ra = np.where(hi, metrics["reversal_atr_high"].to_numpy()[i],
                                  metrics["reversal_atr_low"].to_numpy()[i])  # fmt: skip
                    clv = metrics["clv"].to_numpy()[i]
                    col.bmetrics.append(pd.DataFrame({
                        "kind": kind, "coin": coin, "key": f["event_id"].to_numpy(),
                        "wick_range": wr, "reversal_atr": ra,
                        # +1 = closed at the extreme away from the swept side (full rejection)
                        "clv_reversal": np.where(hi, -clv, clv),
                        "same_bar": f["failure_delay_bars"].to_numpy(float) == 0,
                    }))  # fmt: skip
        col.seconds[coin] = round(time.perf_counter() - t0, 3)
    return col


__all__ = ["NAT", "VOL_NAMES", "Collected", "collect"]
