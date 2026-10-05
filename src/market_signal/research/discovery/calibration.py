"""Null calibration and planted temporary-edge power of the Phase 22 harness
(``discovery_calibration_v1``). Reads no market data.

The harness runs end to end (the real manifest's windows, the frozen catalogue, the frozen
statistics and verdict rules) on synthetic markets (``synthetic``):

| Scenario | Truth |
|---|---|
| ``null_costfree`` | no edge, zero costs, zero-mean funding: calibrates the test (raw p < 0.05 share ~ 5%, BH discoveries rare) |
| ``null_costed`` | no edge, realistic per-coin costs: what an honest catalogue looks like after costs |
| ``momentum_1h_60d`` | 1H momentum (n=4, k=2) pays +0.8% over 4h in the LAST 60 days only, both sides (an emerging edge with a null history) |
| ``momentum_1h_60d_strong`` | the same 60-day edge at +1.5% per event |
| ``breakout_15m_30d`` | 15m breakout (n=16) pays +0.5% over 1h in the last 30 days only, both sides |
| ``short_only_120d`` | 1H breakdown (breakout n=24 SHORT) pays +1.2% over 4h in the last 120 days; the long side gets nothing |
| ``compression_vol`` | after every 1H compression onset (n=24, pct 0.2) volatility is x2.5 for 8h, no direction |
| ``dead_recent`` | 1H momentum (n=4, k=2) pays +0.8% only in the FIRST year: strong contemporary history, dead recently |

Costed scenarios use Binance-like costs (5 bps fee + the coin's frozen slippage per side).
Every run is seeded; a scenario's report aggregates its seeds.
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from market_signal.research.discovery import catalogue as cat
from market_signal.research.discovery import synthetic as sy
from market_signal.research.discovery.collect import CoinInputs, VenueData, strategy_events
from market_signal.research.discovery.run import evaluate_manifest
from market_signal.research.discovery.spec import CANDIDATE_VERDICTS, DiscoveryManifest

CALIBRATION_VERSION = "discovery_calibration_v1"
SLIPPAGE_BPS = {"BTC": 2, "ETH": 2, "SOL": 4, "HYPE": 6, "LINK": 6, "AAVE": 8}
FEE_BPS = 5.0
DAY = pd.Timedelta(days=1)

SCENARIOS = {
    "null_costfree": {"targets": [], "costs": False},
    "null_costed": {"targets": [], "costs": True},
    "momentum_1h_60d": {"targets": ["momentum_z[n=4,k=2.0]:1h:long", "momentum_z[n=4,k=2.0]:1h:short"],
                        "mu": 0.008, "bars": 16, "last_days": 60, "costs": True},
    "momentum_1h_60d_strong": {"targets": ["momentum_z[n=4,k=2.0]:1h:long", "momentum_z[n=4,k=2.0]:1h:short"],
                               "mu": 0.015, "bars": 16, "last_days": 60, "costs": True},
    "breakout_15m_30d": {"targets": ["breakout[n=16]:15m:long", "breakout[n=16]:15m:short"],
                         "mu": 0.005, "bars": 4, "last_days": 30, "costs": True},
    "short_only_120d": {"targets": ["breakout[n=24]:1h:short"], "mu": 0.012, "bars": 16,
                        "last_days": 120, "costs": True},
    "compression_vol": {"targets": [], "vol_rule": "compression_state[n=24,pct=0.2]:1h",
                        "factor": 2.5, "bars": 32, "costs": True},
    "dead_recent": {"targets": ["momentum_z[n=4,k=2.0]:1h:long", "momentum_z[n=4,k=2.0]:1h:short"],
                    "mu": 0.008, "bars": 16, "first_days": 365, "costs": True},
}  # fmt: skip


def _costs(man: DiscoveryManifest, costed: bool) -> dict:
    return {(v.venue, c): ((FEE_BPS + SLIPPAGE_BPS[c]) / 1e4 if costed else 0.0)
            for v in man.venues for c in v.coins}  # fmt: skip


def _starts(man: DiscoveryManifest) -> dict:
    v = man.discovery
    s = pd.Timestamp(v.event_start) - pd.Timedelta(days=150)
    return {"15m": v.data_start.m15, "1h": s, "4h": s}


def _venue(data: dict, per_side: dict) -> VenueData:
    return VenueData("binance", {c: CoinInputs(c, d.series, d.funding_ns, d.funding_rate, per_side[c])
                                 for c, d in data.items()})  # fmt: skip


def market(man: DiscoveryManifest, scenario: str, seed: int) -> dict:
    """Synthetic discovery-venue CoinData for one scenario and seed."""
    sc = SCENARIOS[scenario]
    v = man.discovery
    starts = _starts(man)
    raw = sy.generate(starts["1h"], v.event_end, seed,
                      funding_mean=1e-4 if sc["costs"] else 0.0)  # fmt: skip
    data = {c: sy.coin_data(r, "binance", starts) for c, r in raw.items()}
    smap = {s.key: s for s in cat.strategies()}
    end = pd.Timestamp(v.event_end)
    if sc.get("targets"):
        if "last_days" in sc:
            a, b = end - sc["last_days"] * DAY, end
        else:
            a = pd.Timestamp(v.event_start)
            b = a + sc["first_days"] * DAY
        vd = _venue(data, {c: 0.0 for c in data})
        entries: dict[str, list] = {}
        for key in sc["targets"]:
            ev = strategy_events(vd, smap[key], a.value, b.value)
            for coin, g in ev.groupby("coin"):
                rc = raw[coin]
                e = ((g["entry_ns"].to_numpy(np.int64) - rc.open_ns[0]) // sy.M15).astype(int)
                ok = (e >= 0) & (e < len(rc.r))
                entries.setdefault(coin, []).extend(
                    zip(e[ok].tolist(), g["d"].to_numpy()[ok].tolist(), strict=True)
                )
        sy.plant_drift(raw, entries, sc["mu"], sc["bars"])
        data = {c: sy.coin_data(r, "binance", starts) for c, r in raw.items()}
    if sc.get("vol_rule"):
        from market_signal.research.discovery.collect import execute
        from market_signal.research.discovery.signals import vol_mask

        vt = next(x for x in cat.vol_tests() if x.key == sc["vol_rule"])
        vd = _venue(data, {c: 0.0 for c in data})
        onsets = {}
        for coin in data:
            ctx = vd.context(coin, vt.timeframe)
            idx = np.flatnonzero(vol_mask(ctx, vt))
            ex = execute(
                data[coin].series["15m"], data[coin].series[vt.timeframe].ready_at[idx], 15
            )
            rc = raw[coin]
            e = ((ex.entry_ns - rc.open_ns[0]) // sy.M15).astype(int)
            onsets[coin] = [int(x) for x in e if 0 <= x < len(rc.r)]
        sy.plant_volatility(raw, onsets, sc["factor"], sc["bars"])
        data = {c: sy.coin_data(r, "binance", starts) for c, r in raw.items()}
    return data


def run_one(args) -> dict:
    man, scenario, seed = args
    t0 = time.perf_counter()
    sc = SCENARIOS[scenario]
    data = market(man, scenario, seed)
    payload, meta = evaluate_manifest(man, _costs(man, sc["costs"]), lambda v, c: data[c],
                                      long_context=False, replication=False)  # fmt: skip
    rows = {s["key"]: s for s in payload["strategies"]}
    targets = set(sc.get("targets", []))
    others = [r for k, r in rows.items() if k not in targets]
    testable = [r for r in others if r.get("testable")]
    p = np.array([r["contemporary"]["p_one_sided"] for r in testable], float)
    tgt = {k: {"verdict": rows[k]["verdict"], "q": rows[k].get("q_value"),
               "route": rows[k].get("route"), "t_contemporary": rows[k]["contemporary"].get("t"),
               "t_recent_180": rows[k]["recent_180"].get("t"),
               "net_recent_180": rows[k]["recent_180"].get("net_mean"),
               "t_recent_90": rows[k]["recent_90"].get("t"), "n_recent_90": rows[k]["recent_90"].get("n"),
               "net_recent_90": rows[k]["recent_90"].get("net_mean"),
               "t_recent_30": rows[k]["recent_30"].get("t"),
               "edge_state": rows[k]["edge_state"].get("state"),
               "pattern": rows[k]["pattern"]["label"], "reasons": rows[k]["verdict_reasons"]}
           for k in targets}  # fmt: skip
    # opportunity-rate consistency: raw >= independent >= evaluable independent events
    chk = all(r["frequency"]["raw"] >= r["frequency"]["independent"] >= r["contemporary"]["n"]
              for r in rows.values())  # fmt: skip
    vol = payload["volatility_forecast"].get(sc.get("vol_rule", ""), None)
    return {
        "scenario": scenario, "seed": seed, "seconds": round(time.perf_counter() - t0, 1),
        "peak_rss_mb": meta["peak_rss_mb"],
        "testable_non_targets": len(testable),
        "raw_p_lt_005_share": float((p < 0.05).mean()) if len(p) else None,
        "raw_p_lt_010_share": float((p < 0.10).mean()) if len(p) else None,
        "bh_discoveries_non_target": sum(1 for r in others if (r.get("q_value") or 1) <= 0.10),
        "families_with_false_discovery": sorted({r["bh_family"] for r in others if (r.get("q_value") or 1) <= 0.10}),
        "false_candidates": sum(1 for r in others if r["verdict"] in CANDIDATE_VERDICTS),
        "false_strong": sum(1 for r in others if r["verdict"] == "STRONG_INCUBATION_CANDIDATE"),
        "false_interesting": sum(1 for r in others if r["verdict"] == "INTERESTING"),
        "verdict_counts": payload["verdict_counts"],
        "targets": tgt,
        "vol_target": vol,
        # the null market has volatility clustering, so volume spikes / large bars truly
        # precede larger moves and compression truly precedes smaller ones: reported, not "false"
        "vol_tests": {k: {"t": r.get("t"), "q": r.get("q_value"), "answer": r.get("answer")}
                      for k, r in payload["volatility_forecast"].items()},
        "frequency_selfcheck": bool(chk),
        "shortlist": payload["shortlist"],
        "ensemble_shortlist": payload["ensemble"]["shortlist"]["contemporary"],
    }  # fmt: skip


def calibrate(
    man: DiscoveryManifest, seeds: dict[str, int] | None = None, workers: int = 1
) -> dict:
    seeds = seeds or {"null_costfree": 4, "null_costed": 3, "momentum_1h_60d": 2,
                      "momentum_1h_60d_strong": 2, "breakout_15m_30d": 2,
                      "short_only_120d": 2, "compression_vol": 2, "dead_recent": 2}  # fmt: skip
    jobs = [(man, sc, 1000 + i) for sc, n in seeds.items() for i in range(n)]
    t0 = time.perf_counter()
    if workers > 1:
        with ProcessPoolExecutor(workers) as ex:
            runs = list(ex.map(run_one, jobs))
    else:
        runs = [run_one(j) for j in jobs]
    return {"version": CALIBRATION_VERSION, "null_model": sy.NULL_VERSION,
            "catalogue": cat.catalogue_id(), "scenarios": SCENARIOS, "runs": runs,
            "summary": summarise(runs), "wall_seconds": round(time.perf_counter() - t0, 1)}  # fmt: skip


def summarise(runs: list[dict]) -> dict:
    out: dict = {}
    for sc in dict.fromkeys(r["scenario"] for r in runs):
        rs = [r for r in runs if r["scenario"] == sc]
        tv: dict = {}
        for r in rs:
            for k, t in r["targets"].items():
                tv.setdefault(k, []).append(t["verdict"])
        out[sc] = {
            "seeds": len(rs),
            "raw_p_lt_005_share_mean": _mean([r["raw_p_lt_005_share"] for r in rs]),
            "bh_false_discoveries_per_run": _mean([r["bh_discoveries_non_target"] for r in rs]),
            "false_candidates_per_run": _mean([r["false_candidates"] for r in rs]),
            "false_strong_per_run": _mean([r["false_strong"] for r in rs]),
            "false_interesting_per_run": _mean([r["false_interesting"] for r in rs]),
            "target_verdicts": tv,
            "vol_target_t": [(r["vol_target"] or {}).get("t") for r in rs],
            "vol_tests_t": {k: [r["vol_tests"][k]["t"] for r in rs] for k in rs[0]["vol_tests"]},
            "frequency_selfcheck": all(r["frequency_selfcheck"] for r in rs),
        }  # fmt: skip
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else None


__all__ = ["CALIBRATION_VERSION", "SCENARIOS", "calibrate", "market", "run_one", "summarise"]
