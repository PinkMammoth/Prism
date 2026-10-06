"""Descriptive smoke run: do the detectors behave on real bars? Counts only.

No returns are aggregated, nothing is ranked, and no parameter is compared to another: a
fixed default parameterisation runs per venue/coin/timeframe and the output is how many
of each object were detected, how many trade paths were complete, and how often OHLC
could not order a path. The run reads the store and writes nothing.
"""

from __future__ import annotations

import resource
import time

import numpy as np
import pandas as pd

from market_signal.research.structure import levels as lv
from market_signal.research.structure import path as tp
from market_signal.research.structure import registry as reg
from market_signal.research.structure.chain import run_chain
from market_signal.research.structure.series import BarSeries, assumed

FASTER = {"1h": "15m", "4h": "1h"}

SAME_TF = {
    "rejection": reg.RejectionParams(),
    "structure_shift": reg.StructureShiftParams(),
    "retest": reg.RetestParams(),
}
PATHS = reg.TradePathParams(horizons=(4, 16), pct_levels=(1.0,), atr_levels=(1.0,))


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux: KiB


def _paths(res, series: BarSeries, child: BarSeries | None):
    f = res.failed
    if f.empty:
        return pd.DataFrame(), pd.DataFrame()
    entries = pd.DataFrame({
        "event_id": f["event_id"], "entry_after_ns": f["available_ns"],
        # the reversal direction is the one with a natural invalidation (the swept extreme)
        "direction": np.where(f["side"] == "low", 1, -1),
        "atr": f["atr"], "invalidation": f["extreme"],
    })  # fmt: skip
    return tp.trade_paths(series, entries, PATHS, resolve_with=child)


def series_counts(res, series: BarSeries, child: BarSeries | None) -> dict:
    """Descriptive counts for one same-timeframe chain run."""
    swings = sum(len(lv.swings(series, reg.SwingParams(), side)) for side in ("high", "low"))
    clusters = sum(len(lv.clusters(series, reg.ClusterParams(), side)) for side in ("high", "low"))
    paths, thr = _paths(res, series, child)
    amb = thr[thr["criterion"] == "r_1"] if len(thr) else thr
    return {
        "bars": len(series),
        "swings": swings,
        "cluster_snapshots": clusters,
        "levels": len(res.levels),
        "breaches": len(res.breaches),
        "close_beyond": int((res.breaches["breach_kind"] == "CLOSE_BEYOND").sum()),
        "held_breakouts": len(res.held),
        "failed_breakouts": len(res.failed),
        "same_bar_failures": int((res.failed["failure_delay_bars"] == 0).sum()),
        "rejections": int((res.rejections["status"] == "REJECTION").sum()),
        "structure_shifts": int((res.shifts["status"] == "SHIFT").sum()),
        "retests": int((res.retests["status"] == "RETEST").sum()),
        "paths": len(paths),
        "paths_complete": int(paths["complete"].sum()) if len(paths) else 0,
        "r_available": int((paths["risk_status"] == "OK").sum()) if len(paths) else 0,
        "r1_ambiguous_raw": int(
            ((amb["order"] == tp.AMBIGUOUS) | (amb["resolution"].notna())).sum()
        )
        if len(amb)
        else 0,
        "r1_resolved_ltf": int((amb["resolution"] == "RESOLVED").sum()) if len(amb) else 0,
        "r1_still_ambiguous": int((amb["order"] == tp.AMBIGUOUS).sum()) if len(amb) else 0,
    }


def run_smoke(store, venues, coins, tfs, start=None, end=None, latency_s: float = 60.0):
    """One row per venue/coin/timeframe (same-timeframe chain) plus one 4h/1h/15m chain
    per venue/coin. Assumed-latency availability (backfilled history)."""
    av = assumed(latency_s)
    rows = []
    for venue in venues:
        for coin in coins:
            loaded = {}
            for tf in ("15m", "1h", "4h"):
                if tf in tfs or (tf in ("4h", "1h", "15m") and "mtf" in tfs):
                    s = BarSeries.from_store(store, venue, coin, tf, start, end, availability=av)
                    if len(s):
                        loaded[tf] = s
            for tf in [t for t in ("15m", "1h", "4h") if t in tfs and t in loaded]:
                s = loaded[tf]
                t0 = time.perf_counter()
                spec = reg.ChainSpec(
                    venue=venue, structure_tf=tf, event_tf=tf, confirm_tf=tf, **SAME_TF
                )
                res = run_chain(spec, s, s)
                counts = series_counts(res, s, loaded.get(FASTER.get(tf, "")))
                rows.append({"venue": venue, "coin": coin, "chain": f"{tf}/{tf}/{tf}", **counts,
                             "seconds": round(time.perf_counter() - t0, 3)})  # fmt: skip
            if "mtf" in tfs and all(t in loaded for t in ("4h", "1h", "15m")):
                t0 = time.perf_counter()
                spec = reg.ChainSpec(
                    venue=venue, structure_tf="4h", event_tf="1h", confirm_tf="15m", **SAME_TF
                )
                res = run_chain(spec, loaded["4h"], loaded["1h"], loaded["15m"])
                rows.append({
                    "venue": venue, "coin": coin, "chain": "4h/1h/15m", "bars": len(loaded["1h"]),
                    "levels": len(res.levels), "breaches": len(res.breaches),
                    "held_breakouts": len(res.held), "failed_breakouts": len(res.failed),
                    "structure_shifts": int((res.shifts["status"] == "SHIFT").sum()),
                    "shift_no_data": int((res.shifts["status"] == "NO_DATA").sum()),
                    "retests": int((res.retests["status"] == "RETEST").sum()),
                    "seconds": round(time.perf_counter() - t0, 3),
                })  # fmt: skip
    return pd.DataFrame(rows)
