"""Orchestrate one Phase 22 discovery evaluation from retained inputs to a deterministic payload.

``evaluate(defn, load)``: ``load(venue, coin)`` returns the ``CoinData`` of the frozen dataset
(the governance layer passes a ledger-backed loader; tests and the null calibration pass
synthetic data). Venues are analysed separately and never pooled: verdicts come from the
discovery venue's contemporary/recent windows; the long context and the replication venue
are reported beside them, labelled. The payload has no clock and no random draws; runtime
and memory go to ``meta`` (not part of the digest).
"""

from __future__ import annotations

import time
from collections import Counter

import numpy as np
import pandas as pd

from market_signal.research.discovery import STATEMENT
from market_signal.research.discovery import analysis as an
from market_signal.research.discovery import catalogue as cat
from market_signal.research.discovery.collect import (
    CoinInputs,
    VenueData,
    strategy_events,
    strategy_paths,
    vol_events,
)
from market_signal.research.discovery.spec import (
    AVAILABILITY_STATEMENT,
    CANDIDATE_VERDICTS,
    VERDICTS,
    DiscoveryManifest,
)
from market_signal.research.lab.batch import benjamini_hochberg
from market_signal.research.relative.study.stats import clustered_mean
from market_signal.research.structure.study.run import _peak_rss_mb, clean, digest

__all__ = ["digest", "evaluate", "evaluate_manifest"]

DAY_NS = an.DAY_NS
BRIEF = ("n", "assets", "net_mean", "t", "p_one_sided", "gross_mean", "cost_drag", "hit_rate",
         "excess_mean")  # fmt: skip


def _brief(s: dict) -> dict:
    return {k: s.get(k) for k in BRIEF}


def venue_data(man: DiscoveryManifest, venue: str, load, per_side: dict[str, float]) -> VenueData:
    v = man.venue(venue)
    coins = {}
    for coin in v.coins:
        cd = load(venue, coin)
        coins[coin] = CoinInputs(coin, cd.series, cd.funding_ns, cd.funding_rate, per_side[coin])
    return VenueData(venue, coins, reference=man.reference)


def stress_day_set(vd: VenueData, man: DiscoveryManifest) -> tuple[set, list]:
    """Phase 20 ``market_stress_v1`` on BTC daily closes rebuilt from the venue's 1h bars."""
    from market_signal.research.lifecycle.market import episodes, stress_days
    from market_signal.research.lifecycle.policy import policy

    btc = vd.coins.get(man.reference)
    if btc is None or "1h" not in btc.series or not len(btc.series["1h"]):
        return set(), []
    s = btc.series["1h"]
    day = s.open_time // DAY_NS
    df = pd.DataFrame({"day": day, "close": s.c}).groupby("day")["close"].last()
    ref = pd.DataFrame({"close_time": pd.to_datetime((df.index.to_numpy() + 1) * DAY_NS, utc=True),
                        "close": df.to_numpy()})  # fmt: skip
    days = stress_days(ref, policy(1).stress)
    ep = days.index[days["episode"].to_numpy()]
    # an episode flag at a daily close (day D's end) labels signals during day D
    return {int(t.value // DAY_NS) - 1 for t in ep}, episodes(days)


def _stress_split(ev: pd.DataFrame, h: int, stress: set) -> dict:
    e = an.evaluable(ev, h)
    if e.empty:
        return {}
    lab = np.array([(t // DAY_NS) in stress for t in e["signal_ns"].to_numpy(np.int64)])
    out = {}
    for name, m in (("all", np.ones(len(e), bool)), ("normal", ~lab), ("stress", lab)):
        x = e[f"net_{h}"].to_numpy(float)[m]
        out[name] = {"n": int(m.sum()), "net_mean": an._f(x.mean()) if m.any() else None,
                     "worst": an._f(x.min()) if m.any() else None}  # fmt: skip
    return out


def _side_summary(rows: dict, strategies: dict) -> dict:
    out = {}
    for side in ("long", "short"):
        keys = [k for k in rows if strategies[k].side == side]
        means = [rows[k]["contemporary"].get("net_mean") for k in keys]
        means = [m for m in means if m is not None]
        out[side] = {"variants": len(keys),
                     "verdicts": dict(Counter(rows[k]["verdict"] for k in keys)),
                     "mean_contemporary_net": an._f(np.mean(means)) if means else None,
                     "positive_net_share": an._f(np.mean([m > 0 for m in means])) if means else None,
                     "independent_per_day_total": sum(rows[k]["frequency"]["independent_per_day"] for k in keys)}  # fmt: skip
    return out


def _family_summary(rows: dict, strategies: dict, bh: dict) -> dict:
    out = {}
    for fam in cat.BH_FAMILIES:
        keys = [k for k in rows if strategies[k].bh_family == fam]
        if not keys:
            continue
        c = [rows[k]["contemporary"] for k in keys]
        best = max(keys, key=lambda k: rows[k]["contemporary"].get("t") or -99)
        g = [x.get("gross_mean") for x in c if x.get("gross_mean") is not None]
        d = [x.get("cost_drag") for x in c if x.get("cost_drag") is not None]
        n = [x.get("net_mean") for x in c if x.get("net_mean") is not None]
        out[fam] = {**bh.get(fam, {}), "variants": len(keys),
                    "verdicts": dict(Counter(rows[k]["verdict"] for k in keys)),
                    "mean_gross": an._f(np.mean(g)) if g else None,
                    "mean_cost_drag": an._f(np.mean(d)) if d else None,
                    "mean_net": an._f(np.mean(n)) if n else None,
                    "best": {"key": best, "t": rows[best]["contemporary"].get("t"),
                             "net_mean": rows[best]["contemporary"].get("net_mean"),
                             "verdict": rows[best]["verdict"]}}  # fmt: skip
    return out


def evaluate_manifest(man: DiscoveryManifest, costs: dict, load, *, study_id: str = "synthetic",
                      long_context: bool = True, replication: bool = True,
                      strategies: tuple | None = None) -> tuple[dict, dict]:  # fmt: skip
    """``costs``: (venue, coin) -> per-side cost fraction."""
    t0 = time.perf_counter()
    meta: dict = {"seconds": {}}
    strategies = strategies or cat.strategies()
    smap = {s.key: s for s in strategies}
    w = man.windows
    lo, hi = an.ns(w.contemporary_start), an.ns(w.end)
    r180, r90, r30 = an.ns(w.recent_180_start), an.ns(w.recent_90_start), an.ns(w.recent_30_start)
    dv = man.discovery
    vd = venue_data(man, dv.venue, load, {c: costs[(dv.venue, c)] for c in dv.coins})
    meta["source_bars"] = {f"{dv.venue}:{c}:{tf}": len(s) for c, ci in vd.coins.items()
                           for tf, s in ci.series.items()}  # fmt: skip
    stress, stress_eps = stress_day_set(vd, man)
    ev_start, ev_end = an.ns(dv.event_start), an.ns(dv.event_end)

    # ---------------------------------------------------------------- discovery venue
    ta = time.perf_counter()
    events: dict[str, pd.DataFrame] = {}
    rows: dict[str, dict] = {}
    floors: dict[str, float] = {}
    for s in strategies:
        ev = strategy_events(vd, s, ev_start, ev_end)
        events[s.key] = ev
        h = s.primary_horizon
        floors[s.key] = man.statistics.floor(s.primary_minutes)
        cw = an.window(ev, lo, hi)
        n_coins = sum(1 for c in vd.coins if not (s.universe == "alts" and c == man.reference))
        rows[s.key] = {
            "contemporary": an.stats(cw, h),
            "recent_180": an.stats(an.window(ev, r180, hi), h),
            "recent_90": an.stats(an.window(ev, r90, hi), h),
            "recent_30": an.stats(an.window(ev, r30, hi), h),
            "horizons": {str(x): _brief(an.stats(cw, x)) for x in s.horizons},
            "frequency": an.frequency(ev, lo, hi, n_coins),
            "recency_weighted": an.weighted(cw, h, hi, w.recency_half_life_days),
            "pattern": an.pattern(ev, h, lo, hi),
            "edge_state": an.edge_state(cw, h, lo, hi, floors[s.key], stress),
            "vol_state": an.bucket_contrast(cw, h),
            "stress": _stress_split(cw, h, stress),
            "floor": floors[s.key],
        }
    meta["seconds"]["discovery_strategies"] = round(time.perf_counter() - ta, 2)
    bh = an.apply_bh(rows, smap, man)
    an.all_verdicts(rows, smap, man, floors)

    # ---------------------------------------------------------------- vol forecast family
    vol_rows = {}
    for v in cat.vol_tests():
        ve = vol_events(vd, v, ev_start, ev_end)
        e = an.window(ve, lo, hi) if not ve.empty else ve
        e = e[e["independent"]] if not e.empty else e
        h = v.primary_horizon
        r = {"n": len(e)}
        if len(e) > 1:
            x = e[f"absx_{h}"].to_numpy(float)
            ok = np.isfinite(x)
            cm = clustered_mean(x[ok], (e["signal_ns"].to_numpy(np.int64) // DAY_NS)[ok])
            rx = e[f"relx_{h}"].to_numpy(float)
            r = {"n": int(ok.sum()), "abs_move_mean": an._f(np.nanmean(e[f"abs_{h}"])),
                 "abs_excess_mean": an._f(cm["mean"]), "t": an._f(cm.get("t")),
                 "p_two_sided": an._f(cm.get("p_value")),
                 "atr_relative_excess_mean": an._f(np.nanmean(rx)),
                 "answer": ("larger absolute move" if (cm.get("t") or 0) >= 2 else
                            "smaller absolute move" if (cm.get("t") or 0) <= -2 else "no clear difference")}  # fmt: skip
        vol_rows[v.key] = {"test_id": v.test_id, "hypothesis": v.hypothesis,
                           "primary_horizon": h, "timeframe": v.timeframe, **r}  # fmt: skip
    vq = benjamini_hochberg({k: (r.get("p_two_sided") if r.get("p_two_sided") is not None else 1.0)
                             for k, r in vol_rows.items()}) if vol_rows else {}  # fmt: skip
    for k, q in vq.items():
        vol_rows[k]["q_value"] = q

    # ---------------------------------------------------------------- contrasts
    contrasts: dict[str, dict] = {}
    for s in strategies:
        if s.baseline and s.baseline in smap:
            b = smap[s.baseline]
            contrasts[f"complexity:{s.key}"] = {
                "kind": "complexity", "strategy": s.key, "baseline": s.baseline,
                **an.complexity_contrast(events[s.key], events[b.key], s.primary_horizon,
                                         b.primary_horizon, lo, hi)}  # fmt: skip
        vs = rows[s.key]["vol_state"]
        if vs.get("p_two_sided") is not None:
            contrasts[f"vol_state:{s.key}"] = {"kind": "vol_state", "strategy": s.key,
                                               "high_minus_low": vs.get("high_minus_low"),
                                               "t": vs.get("t"), "p_two_sided": vs["p_two_sided"]}  # fmt: skip
    cq = benjamini_hochberg({k: (c.get("p_two_sided") if c.get("p_two_sided") is not None else 1.0)
                             for k, c in contrasts.items()}) if contrasts else {}  # fmt: skip
    for k, q in cq.items():
        contrasts[k]["q_value"] = q

    # ---------------------------------------------------------------- survivors
    interesting = [k for k in rows if rows[k]["verdict"] in ("INTERESTING", *CANDIDATE_VERDICTS)]
    candidates = [k for k in rows if rows[k]["verdict"] in CANDIDATE_VERDICTS]
    tp = time.perf_counter()
    for k in interesting:
        e = an.evaluable(an.window(events[k], lo, hi), smap[k].primary_horizon)
        p = strategy_paths(vd, smap[k], e)
        if p.empty:
            continue
        rows[k]["path"] = {
            "n": len(p), "mfe_mean": an._f(p["mfe"].mean()), "mae_mean": an._f(p["mae"].mean()),
            "mfe_median": an._f(p["mfe"].median()), "mae_median": an._f(p["mae"].median()),
            "t_mfe_min_median": an._f(p["t_mfe_min"].median()),
            "t_mae_min_median": an._f(p["t_mae_min"].median()),
            "favourable_first_share": an._f((p["first_touch"] == "favourable").mean()),
            "adverse_first_share": an._f((p["first_touch"] == "adverse").mean()),
            "ambiguous_share": an._f((p["first_touch"] == "ambiguous").mean()),
            "threshold": "0.5 x signal-timeframe ATR",
        }  # fmt: skip
    meta["seconds"]["paths"] = round(time.perf_counter() - tp, 2)
    ind_events = {k: an.evaluable(events[k], smap[k].primary_horizon) for k in interesting}
    cl, pairs = an.clusters(sorted(interesting), ind_events, smap, rows, man.clustering)
    for c in cl:
        for m in c["members"]:
            rows[m]["cluster"] = c["cluster_id"]
            rows[m]["representative"] = c["representative"] == m
    short = an.shortlist(cl, rows, smap, man.shortlist)
    ens_cfg = man.ensemble

    def ens(keys, a, b):
        return an.ensemble([ind_events.get(k, an.evaluable(events[k], smap[k].primary_horizon))
                            for k in keys], a, b, ens_cfg.dedup_minutes, ens_cfg.target_per_day)  # fmt: skip

    reps_int = [c["representative"] for c in cl]
    ensemble = {
        "shortlist": {"contemporary": ens(short, lo, hi), "recent_90": ens(short, r90, hi)},
        "all_candidates": {"contemporary": ens(candidates, lo, hi), "recent_90": ens(candidates, r90, hi)},
        "interesting_representatives": {"contemporary": ens(reps_int, lo, hi)},
        "whole_catalogue_reference": {"contemporary": ens(list(rows), lo, hi),
                                      "note": "every variant regardless of evidence; a ceiling, not a plan"},
    }  # fmt: skip
    tr = time.perf_counter()
    replay = {k: an.incubation_replay(events[k], smap[k], smap[k].primary_horizon, lo, hi,
                                      floors[k]) for k in sorted(set(candidates) | set(short))}  # fmt: skip
    meta["seconds"]["replay"] = round(time.perf_counter() - tr, 2)

    # ---------------------------------------------------------------- long context
    if long_context and dv.long_context is not None:
        tl = time.perf_counter()
        a, b = an.ns(dv.long_context.event_start), an.ns(dv.long_context.event_end)
        for s in strategies:
            if s.timeframe != "1h":
                rows[s.key]["long_context"] = None
                continue
            ev = strategy_events(vd, s, a, b, exec_tf="1h")
            st = an.stats(ev, s.primary_horizon)
            rows[s.key]["long_context"] = {**_brief(st), "execution": "1h grid (60-min entry delay)",
                                           "window": [dv.long_context.event_start.isoformat(),
                                                      dv.long_context.event_end.isoformat()]}  # fmt: skip
        meta["seconds"]["long_context"] = round(time.perf_counter() - tl, 2)

    # ---------------------------------------------------------------- replication venue
    rep_out = {}
    if replication:
        for v in man.venues:
            if v.role != "replication":
                continue
            trv = time.perf_counter()
            vr = venue_data(man, v.venue, load, {c: costs[(v.venue, c)] for c in v.coins})
            a, b = an.ns(v.event_start), an.ns(v.event_end)
            per = {}
            for s in strategies:
                per[s.key] = _brief(an.stats(strategy_events(vr, s, a, b), s.primary_horizon))
            agree = [k for k in candidates if (per[k].get("net_mean") or 0) > 0]
            rep_out[v.venue] = {"window": [v.event_start.isoformat(), v.event_end.isoformat()],
                                "strategies": per,
                                "candidates_positive_here": agree,
                                "note": "descriptive replication on the execution venue; not "
                                        "required for exploratory incubation"}  # fmt: skip
            meta["seconds"][f"replication_{v.venue}"] = round(time.perf_counter() - trv, 2)

    # ---------------------------------------------------------------- payload
    out_strats = []
    for s in strategies:
        r = rows[s.key]
        out_strats.append({
            "key": s.key, "strategy_id": s.strategy_id, "family": s.family,
            "bh_family": s.bh_family, "rule": s.rule, "side": s.side, "timeframe": s.timeframe,
            "context_timeframe": s.context_timeframe, "params": dict(s.params),
            "primary_horizon_minutes": s.primary_minutes,
            "horizons_minutes": [s.horizon_minutes(h) for h in s.horizons],
            "symmetric": s.symmetric, "hypothesis": s.hypothesis, "baseline": s.baseline,
            "complexity": s.complexity.model_dump(), **r,
        })  # fmt: skip
    verdict_counts = dict(Counter(rows[k]["verdict"] for k in rows))
    payload = clean({
        "study_id": study_id, "study_version": "intraday_discovery_v1",
        "evidence_class": "EXPLORATORY", "statement": STATEMENT,
        "availability": {"mode": "assumed", "assumed_latency_s": man.assumed_latency_s,
                         "statement": AVAILABILITY_STATEMENT},
        "catalogue": cat.summary(),
        "windows": {"contemporary": [w.contemporary_start.isoformat(), w.end.isoformat()],
                    "recent_180": [w.recent_180_start.isoformat(), w.end.isoformat()],
                    "recent_90": [w.recent_90_start.isoformat(), w.end.isoformat()],
                    "recent_30": [w.recent_30_start.isoformat(), w.end.isoformat()],
                    "discovery_venue": dv.venue},
        "stress_episodes": stress_eps,
        "verdict_counts": {v: verdict_counts.get(v, 0) for v in VERDICTS},
        "bh_families": bh,
        "families": _family_summary(rows, smap, bh),
        "sides": _side_summary(rows, smap),
        "strategies": out_strats,
        "volatility_forecast": vol_rows,
        "contrasts": contrasts,
        "clusters": cl, "overlap_pairs": pairs,
        "shortlist": short,
        "ensemble": ensemble,
        "incubation_replay": replay,
        "replication": rep_out,
    })  # fmt: skip
    meta["wall_seconds"] = round(time.perf_counter() - t0, 2)
    meta["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    meta["variants"] = len(strategies)
    return payload, meta


def evaluate(defn, load) -> tuple[dict, dict]:
    """Governance entry point (``structure_study.run``)."""
    costs = {(c.venue, c.coin): c.per_side for c in defn.costs}
    return evaluate_manifest(defn.manifest, costs, load, study_id=defn.study_id)
