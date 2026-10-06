"""Orchestrate one Phase 20 historical lifecycle evaluation into a deterministic payload.

``evaluate(defn, load)``: ``load(venue, coin)`` returns the retained Lab ``Snapshot`` of the
frozen dataset (the governance adapter passes a ledger-backed loader that verifies hashes).
For each venue: causal market state and stress labels; for each catalogue strategy: the
causal outcome ledger, the three walk-forward lifecycle simulations (static, recent,
regime), the edge-state timeline and the edge profile at the data cutoff. Then
venue-level aggregates: lifecycle behaviour, static versus lifecycle use, recent-versus-
lifetime disagreements, the multi-edge view and stress episodes.

The payload has no clock and no random draws; runtime and memory go to ``meta`` (not part of
the result digest).
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from itertools import pairwise

import numpy as np
import pandas as pd

from market_signal.models.domain import AssetClass
from market_signal.research.lab.compiler import CompileCache, build_inputs
from market_signal.research.lab.spec import StrategyDefinition
from market_signal.research.lifecycle import estimators as es
from market_signal.research.lifecycle import market as mk
from market_signal.research.lifecycle.machine import (
    MODES,
    PARTICIPATING,
    edge_state_timeline,
    evaluation_times,
    simulate,
)
from market_signal.research.lifecycle.outcomes import strategy_outcomes
from market_signal.research.lifecycle.profile import build_profile
from market_signal.research.lifecycle.state import sign_class
from market_signal.research.lifecycle.study import (
    AVAILABILITY_STATEMENT,
    EXPLORATORY_STATEMENT,
    LifecycleStudyDefinition,
)
from market_signal.research.structure.study.run import _peak_rss_mb, clean, digest

__all__ = ["digest", "evaluate"]


def outcomes_digest(df: pd.DataFrame) -> str:
    cols = ["asset", "signal_time", "resolved_at", "net", "excess", "independent", "evaluable"]
    if df.empty:
        return hashlib.sha256(b"").hexdigest()
    x = df[cols].copy()
    x["signal_time"] = pd.to_datetime(x["signal_time"], utc=True).map(lambda t: t.isoformat())
    x["resolved_at"] = pd.to_datetime(x["resolved_at"], utc=True).map(lambda t: t.isoformat())
    for c in ("net", "excess"):
        x[c] = x[c].map(lambda v: None if not np.isfinite(v) else float(f"{v:.12g}"))
    return hashlib.sha256(x.to_json(orient="values").encode()).hexdigest()


def _current(state: pd.DataFrame, at: pd.Timestamp) -> dict[str, str]:
    pos = state.index.searchsorted(at, side="right") - 1
    if pos < 0:
        return {d: "unknown" for d in es.REGIME_DIMS}
    row = state.iloc[pos]
    return {d: row[d] for d in es.REGIME_DIMS}


def _codes(state: pd.DataFrame, times: np.ndarray, key: str) -> np.ndarray:
    labels = es.REGIME_LABELS[key]
    idx = state.index
    out = []
    for t in times:
        pos = idx.searchsorted(es.to_time(t), side="right") - 1
        v = state[key].iloc[pos] if pos >= 0 else "unknown"
        out.append(labels.index(v) if v in labels else -1)
    return np.array(out, dtype=np.int64)


def _weekly(ev: es.EventSet, lo: float, hi: float) -> pd.Series:
    m = (ev.t_res > lo) & (ev.t_res <= hi)
    wk = np.floor(ev.t_res[m] / 7).astype(np.int64)
    return pd.Series(ev.net[m]).groupby(wk).sum()


def evaluate(defn: LifecycleStudyDefinition, load) -> tuple[dict, dict]:
    man = defn.manifest
    pol = defn.lifecycle_policy()
    if pol.policy_id != defn.policy_id:
        raise ValueError("embedded policy does not reproduce its ID")
    floor = pol.floor.floor(man.horizon_bars)
    cutoff = pd.Timestamp(man.cutoff)
    t_cut = es.to_days(cutoff)
    t0 = time.perf_counter()
    meta: dict = {"seconds": {}, "rolling_evaluations": {}, "outcome_rows": {}}
    strategies = []
    for s in defn.strategies:
        d = StrategyDefinition.model_validate(s.definition)
        if d.strategy_id != s.strategy_id:
            raise ValueError(f"{s.name}: frozen definition does not reproduce its strategy ID")
        strategies.append((s, d))
    venues = {}
    for v in man.venues:
        tv = time.perf_counter()
        snaps = {c: load(v.venue, c) for c in v.coins}
        caches = {c: CompileCache(snaps[c]) for c in v.coins}
        frames = {c: build_inputs(snaps[c], c, "perp", AssetClass.CRYPTO, True)[0]
                  for c in v.coins}  # fmt: skip
        state = mk.market_state(frames, man.reference_coin, pol.market_state)
        days = mk.stress_days(frames[man.reference_coin], pol.stress)
        eps = mk.episodes(days)
        times = evaluation_times(es.to_days(v.first_evaluation), t_cut, pol.evaluation.cadence_days)
        codes = _codes(state, times, pol.market_state.lifecycle_key)
        current = _current(state, cutoff)
        costs = {c: (defn.cost(v.venue, c).fee_bps, defn.cost(v.venue, c).slippage_bps)
                 for c in v.coins}  # fmt: skip
        meta["seconds"][f"{v.venue}:inputs"] = round(time.perf_counter() - tv, 2)
        rows, evs, n_eval, n_rows = [], {}, 0, 0
        ts = time.perf_counter()
        for s, d in strategies:
            df, hs = strategy_outcomes(d, snaps, costs, horizon_bars=man.horizon_bars,
                                       baseline=pol.baseline, caches=caches)  # fmt: skip
            n_rows += len(df)
            if df.empty or hs is None:
                rows.append({"strategy": s.name, "family": s.family, "side": s.side,
                             "no_outcomes": True})  # fmt: skip
                continue
            df = mk.label_events(df, state, days)
            ev = es.EventSet.from_frame(df, history_start=hs, block_days=pol.inference.block_days)
            evs[s.name] = ev
            sims = {m: simulate(ev, times, pol, floor, m, current_regime=codes) for m in MODES}
            timeline = edge_state_timeline(ev, times, pol, floor)
            n_eval += len(times) * (1 + 1 + 3)  # edge state + recent track + 3 regime tracks
            turnover = sum(a != b for a, b in pairwise(timeline))
            meta_s = {"strategy_id": s.strategy_id, "strategy_name": s.name, "family": s.family,
                      "side": s.side, "venue": v.venue, "horizon_bars": man.horizon_bars,
                      "data_cutoff": cutoff,
                      "source": {"kind": "study_run", "study_id": defn.study_id,
                                 "policy_id": pol.policy_id},
                      "outcomes": {"digest": outcomes_digest(df), "signals": len(df),
                                   "independent_evaluable": len(ev)}}  # fmt: skip
            prof = build_profile(ev, t_cut, policy=pol, meta=meta_s, current_regime=current,
                                 sims=sims, curve_start=times[0] if len(times) else t_cut)  # fmt: skip
            rows.append({
                "strategy": s.name, "strategy_id": s.strategy_id, "family": s.family,
                "side": s.side, "params": s.params,
                "edge_state_timeline": {"counts": dict(Counter(timeline)), "changes": turnover,
                                        "ever_emerging": "EMERGING" in timeline},
                "profile": prof.model_dump(mode="json"), "profile_id": prof.profile_id,
            })  # fmt: skip
        meta["seconds"][f"{v.venue}:strategies"] = round(time.perf_counter() - ts, 2)
        meta["rolling_evaluations"][v.venue] = n_eval
        meta["outcome_rows"][v.venue] = n_rows
        venues[v.venue] = {
            "role": v.role,
            "coins": list(v.coins),
            "first_evaluation": v.first_evaluation.isoformat(),
            "evaluations": len(times),
            "current_regime": current,
            "stress_episodes": eps,
            "stress_days": int(days["stress_day"].sum()),
            "strategies": rows,
            "aggregate": aggregate(rows, evs, pol, floor, t_cut),
        }
    payload = clean({
        "study_id": defn.study_id, "study_version": defn.study_version,
        "evidence_class": defn.evidence_class, "statement": EXPLORATORY_STATEMENT,
        "availability": {"mode": defn.availability_mode, "statement": AVAILABILITY_STATEMENT},
        "policy_id": pol.policy_id, "horizon_bars": man.horizon_bars,
        "cutoff": cutoff.isoformat(), "economic_floor": floor,
        "strategies": len(defn.strategies), "venues": venues,
    })  # fmt: skip
    meta["wall_seconds"] = round(time.perf_counter() - t0, 2)
    meta["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    meta["strategies"] = len(defn.strategies)
    return payload, meta


# --------------------------------------------------------------------------- aggregates


def _mean(xs) -> float | None:
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else None


def _pooled_rest(sims: list[dict]) -> float | None:
    """Mean net per signal outcome that did NOT participate (dormant / watched periods)."""
    n = sum(s["events_in_span"] - s["events_participating"] for s in sims)
    tot = sum((s["net_mean_not_participating"] or 0.0)
              * (s["events_in_span"] - s["events_participating"]) for s in sims)  # fmt: skip
    return float(tot / n) if n else None


def aggregate(rows: list[dict], evs: dict, pol, floor: float, t_cut: float) -> dict:
    ok = [r for r in rows if not r.get("no_outcomes")]
    prof = {r["strategy"]: r["profile"] for r in ok}
    modes = {}
    for m in MODES:
        sims = [p["lifecycle"][m] for p in prof.values()]
        modes[m] = {
            "strategies": len(sims),
            "ever_participating": sum(s["events_participating"] > 0 for s in sims),
            "net_sum_total": float(sum(s["net_sum"] for s in sims)),
            "net_sum_median": float(np.median([s["net_sum"] for s in sims])) if sims else None,
            "events_participating": int(sum(s["events_participating"] for s in sims)),
            "net_mean_per_event": (float(sum(s["net_sum"] for s in sims))
                                   / max(1, sum(s["events_participating"] for s in sims))),
            "mean_max_drawdown": _mean([s["max_drawdown"] for s in sims]),
            "mean_active_time_share": _mean([s["active_time_share"] for s in sims]),
            "activations": int(sum(s["activations"] for s in sims)),
            "deactivations": int(sum(s["deactivations"] for s in sims)),
            "reactivations": int(sum(s["reactivations"] for s in sims)),
            "retirements": int(sum(s["retirements"] for s in sims)),
            "short_bursts": int(sum(s["short_bursts"] for s in sims)),
            "spells": int(sum(s["spells"] for s in sims)),
            "mean_spell_days": _mean([s.get("mean_spell_days") for s in sims]),
            "transitions_per_strategy_year": _mean([s.get("transitions_per_year") for s in sims]),
            "missed_upside_total": float(sum(s["missed_upside"] for s in sims)),
            "avoided_losses_total": float(sum(s["avoided_losses"] for s in sims)),
            "net_mean_not_participating_per_event": _pooled_rest(sims),
            "mean_reacquisition_success_share": _mean(
                [s.get("reacquisition_success_share") for s in sims]),
            "strategies_better_than_static": None,
        }  # fmt: skip
    for m in ("recent", "regime"):
        modes[m]["strategies_better_than_static"] = sum(
            p["lifecycle"][m]["net_sum"] > p["lifecycle"]["static"]["net_sum"]
            for p in prof.values())  # fmt: skip
    states = Counter(p["edge_state"]["state"] for p in prof.values())
    patterns = Counter(p["divergence"]["pattern"] for p in prof.values())
    final = Counter(s for p in prof.values()
                    for s in p["lifecycle"]["recent"]["final_states"].values())  # fmt: skip

    def brief(name: str) -> dict:
        p = prof[name]
        c = p["divergence"]["classes"]
        r = p["research_modes"]["recent"]
        ok_r = p["edge_state"]["recent_window"] is not None  # an inadequate window has no estimate
        return {"strategy": name, "lifetime_mean": p["lifetime"].get("mean"),
                "lifetime_t": p["lifetime"].get("t"),
                "recent_window": p["edge_state"]["recent_window"],
                "recent_mean": r.get("mean") if ok_r else None,
                "recent_t": r.get("t") if ok_r else None,
                "recent_n": r.get("n"),
                "classes": c, "edge_state": p["edge_state"]["state"],
                "pattern": p["divergence"]["pattern"],
                "recent_lifecycle_state": p["lifecycle"]["recent"]["final_states"].get("all")}  # fmt: skip

    poor_life_strong_recent = [brief(n) for n, p in prof.items()
                               if p["divergence"]["classes"]["lifetime"] in ("NEG", "FLAT", "NA")
                               and p["divergence"]["classes"]["recent"] == "POS"]  # fmt: skip
    strong_life_recent_gone = [brief(n) for n, p in prof.items()
                               if p["divergence"]["classes"]["lifetime"] == "POS"
                               and p["divergence"]["classes"]["recent"] != "POS"]  # fmt: skip
    ever_emerging = sorted(r["strategy"] for r in ok if r["edge_state_timeline"]["ever_emerging"])
    # multi-edge view: strategies participating at the cutoff (recent mode)
    live = [n for n, p in prof.items()
            if p["lifecycle"]["recent"]["final_states"].get("all") in PARTICIPATING]  # fmt: skip
    view = []
    for n in live:
        p = prof[n]
        ev = evs[n]
        k = ev.resolved(t_cut)
        lo = int(np.searchsorted(ev.t_res[:k], t_cut - 365, side="right"))
        view.append({"strategy": n, "expected_current_edge": p["weighted"]["hl90"].get("mean"),
                     "uncertainty": p["weighted"]["hl90"].get("se"),
                     "recent_drawdown_365d": es.drawdown(ev.net[lo:k], ev.t_res[lo:k])["max_drawdown"],
                     "edge_state": p["edge_state"]["state"],
                     "regime_tracks": p["lifecycle"]["regime"]["final_states"],
                     "current_regime": p["regime"]["current"]})  # fmt: skip
    corr = {}
    if len(live) >= 2:
        weekly = pd.concat({n: _weekly(evs[n], t_cut - 365, t_cut) for n in live}, axis=1).fillna(0)
        c = weekly.corr()
        corr = {a: {b: (None if not np.isfinite(c.loc[a, b]) else float(c.loc[a, b]))
                    for b in live} for a in live}  # fmt: skip
    eras: dict = {}
    for p in prof.values():
        for e in p["eras"]:
            eras.setdefault(e["era"], []).append(e)
    era_rows = [{"era": y, "strategies": len(v),
                 "positive_share": float(np.mean([(e["mean"] or 0) > 0 for e in v])),
                 "median_mean": float(np.median([e["mean"] for e in v if e["mean"] is not None]))
                 if any(e["mean"] is not None for e in v) else None}
                for y, v in sorted(eras.items())]  # fmt: skip
    stress = {"all_net_sum": float(sum(p["stress"]["all"]["net_sum"] for p in prof.values())),
              "normal_net_sum": float(sum(p["stress"]["normal"]["net_sum"] for p in prof.values())),
              "stress_net_sum": float(sum(p["stress"]["stress"]["net_sum"] for p in prof.values())),
              "lifetime_class_all_vs_normal": Counter(
                  f"{sign_class(p['stress']['all']['lifetime'], floor, pol)}->"
                  f"{sign_class(p['stress']['normal']['lifetime'], floor, pol)}"
                  for p in prof.values())}  # fmt: skip
    fam: dict = {}
    for r in ok:
        p = r["profile"]
        key = f"{r['family']}:{r['side']}"
        f = fam.setdefault(key, {"variants": 0, "static_net": 0.0, "recent_net": 0.0,
                                 "regime_net": 0.0, "recent_active_share": [], "states": Counter()})  # fmt: skip
        f["variants"] += 1
        for m in MODES:
            f[f"{m}_net"] += p["lifecycle"][m]["net_sum"]
        f["recent_active_share"].append(p["lifecycle"]["recent"]["active_time_share"])
        f["states"][p["edge_state"]["state"]] += 1
    for f in fam.values():
        f["recent_active_share"] = _mean(f["recent_active_share"])
        f["states"] = dict(f["states"])
    return {
        "strategies_with_outcomes": len(ok),
        "strategies_without_outcomes": [r["strategy"] for r in rows if r.get("no_outcomes")],
        "edge_states_at_cutoff": dict(states),
        "divergence_patterns_at_cutoff": dict(patterns),
        "recent_lifecycle_states_at_cutoff": dict(final),
        "modes": modes,
        "poor_lifetime_strong_recent": poor_life_strong_recent,
        "strong_lifetime_recent_gone": strong_life_recent_gone,
        "ever_emerging_during_walk_forward": ever_emerging,
        "multi_edge_view": {
            "note": "research view of simulated-participating strategies at the "
            "cutoff; not an allocation and not a candidate list",
            "strategies": view,
            "weekly_net_correlation_365d": corr,
        },
        "eras": era_rows,
        "stress": stress,
        "families": fam,
    }
