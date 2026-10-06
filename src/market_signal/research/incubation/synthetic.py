"""Synthetic temporary-edge calibration for fast incubation (``incubation_calibration_v1``).

Phase 20 calibrated against edges that lasted years. Exploratory paper exists for edges that
last days to months, so every scenario here has a long null history (lifetime context), then
one or more SHORT planted edges, then null again:

| Scenario | Planted per-event after-cost edge (by signal time) |
|---|---|
| null | 0 throughout |
| edge14 / edge30 / edge60 / edge90 | +edge for 14 / 30 / 60 / 90 days |
| intermittent | +edge for 30 days, three times, 60 days apart (recurring temporary edge) |
| reversing | +edge for 45 days, then -edge for 45 days |

Outcome ledgers are generated as in Phase 20 (fat-tailed noise, half of it a common weekly
shock, declustered signals, a 3-state trend regime), so numbers are comparable. Every
profile is replayed causally: CONSERVATIVE at its weekly cadence, BALANCED/AGGRESSIVE
daily. Same seed -> same numbers, for any worker count.

The question is not "how fast on average" but "what kinds of temporary edges can Prism
capture at all?" — hence the response surface over strength, lifespan, signal frequency and
volatility (``surface``). A policy that needs 150 days to find a 30-day edge is useless for
this mandate; one that never finds it is honest about the information rate.

``design_grid`` is the pre-declared synthetic search that chose the BALANCED / AGGRESSIVE
thresholds (``DESIGN_RULES``, written before the grid was first run). Historical replays
never fed it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from market_signal.research.incubation.machine import Replay, replay
from market_signal.research.incubation.opportunity import opportunity_rate
from market_signal.research.incubation.policy import (
    POLICIES,
    PROFILES,
    Admission,
    AnyPolicy,
    Deactivation,
    IncubationPolicy,
    Window,
)
from market_signal.research.lifecycle.estimators import REGIME_DIMS, EventSet, to_days
from market_signal.research.lifecycle.machine import evaluation_times
from market_signal.research.lifecycle.synthetic import _agg, regime_path

CALIBRATION_VERSION = "incubation_calibration_v1"
SCENARIOS: dict[str, tuple[tuple[int, int, int], ...]] = {
    # (offset from the edge start in days, length in days, sign)
    "null": (),
    "edge14": ((0, 14, 1),),
    "edge30": ((0, 30, 1),),
    "edge60": ((0, 60, 1),),
    "edge90": ((0, 90, 1),),
    "intermittent": ((0, 30, 1), (90, 30, 1), (180, 30, 1)),
    "reversing": ((0, 45, 1), (45, 45, -1)),
}


def legs(scenario: str) -> tuple[tuple[int, int, int], ...]:
    """A named scenario, or ``life<N>``: one +edge of N days (the response surface)."""
    if scenario.startswith("life"):
        return ((0, int(scenario[4:]), 1),)
    return SCENARIOS[scenario]


FREQUENCIES = {  # (assets, signal probability per asset-day before declustering)
    "low": (3, 1 / 16),
    "base": (5, 1 / 8),
    "high": (20, 1 / 4),
}
DESIGN_RULES = {
    "note": "written before the design grid was first run; synthetic data only",
    "AGGRESSIVE": "maximise the mean captured share of a 30-day +3%/event edge subject to "
    "mean null participation <= 40%",
    "BALANCED": "maximise the mean captured share of a 60-day +3%/event edge subject to mean "
    "null participation <= 15% and <= 4 null admissions per strategy-year",
    "ties": "prefer the higher admission t, then the longer window",
}


@dataclass(frozen=True)
class TempSpec:
    history_days: int = 540  # null history before the first edge (lifetime context)
    after_days: int = 240  # null observation after the scenario's last planted day
    assets: int = 5
    horizon: int = 10
    signal_prob: float = 1 / 8
    sigma: float = 0.10
    market_share: float = 0.5
    tail_df: int = 5
    regime_dwell_days: float = 90.0
    edge: float = 0.03
    start: str = "2022-01-03"
    warmup_days: int = 120  # first evaluation after this many days

    @property
    def t0(self) -> float:
        return to_days(self.start)

    @property
    def edge_start(self) -> float:
        return self.t0 + self.history_days

    def end(self, scenario: str) -> float:
        last = max((o + n for o, n, _ in legs(scenario)), default=0)
        return self.edge_start + last + self.after_days


def planted(scenario: str, t: np.ndarray, spec: TempSpec) -> np.ndarray:
    mu = np.zeros(len(t))
    for off, n, sign in legs(scenario):
        a = spec.edge_start + off
        mu[(t >= a) & (t < a + n)] = sign * spec.edge
    return mu


def generate(scenario: str, seed: int, spec: TempSpec = TempSpec()) -> tuple[EventSet, np.ndarray, np.ndarray]:  # fmt: skip
    """(events, planted mu per event, trend-regime code per day from t0)."""
    rng = np.random.default_rng(seed)
    days = int(spec.end(scenario) - spec.t0) + spec.horizon + 1
    reg = regime_path(rng, days, spec.regime_dwell_days)
    shocks = rng.standard_normal(days // 7 + 2)
    h = spec.horizon
    t_sig, asset = [], []
    for a in range(spec.assets):
        fire = np.flatnonzero(rng.random(days - h) < spec.signal_prob)
        last = -(10**9)
        for d in fire:
            if d - last >= h:
                t_sig.append(d)
                asset.append(a)
                last = d
    order = np.lexsort((np.array(asset), np.array(t_sig)))
    d_sig = np.array(t_sig, dtype=np.int64)[order]
    asset_a = np.array(asset, dtype=np.int64)[order]
    df = spec.tail_df
    eps = rng.standard_t(df, size=len(d_sig)) / np.sqrt(df / (df - 2))
    rho = spec.market_share
    noise = spec.sigma * (np.sqrt(rho) * shocks[d_sig // 7] + np.sqrt(1 - rho) * eps)
    t = spec.t0 + d_sig.astype(float)
    m = planted(scenario, t, spec)
    net = m + noise
    n = len(net)
    regime = {d: np.full(n, -1, dtype=np.int64) for d in REGIME_DIMS}
    regime["trend"] = reg[d_sig]
    t_res = t + h
    nan = np.full(n, np.nan)
    # sorted by signal time; with a constant horizon that is also resolution order
    ev = EventSet(assets=tuple(f"S{i}" for i in range(spec.assets)), t_sig=t, t_res=t_res,
                  net=net, excess=net.copy(), gross=net + 0.0015, mae=nan, mfe=nan,
                  cost=np.full(n, 0.0015), funding=np.zeros(n), asset=asset_a,
                  stress=np.zeros(n, dtype=bool), regime=regime, all_res=t_res,
                  all_valid_cum=np.arange(n + 1), history_start=spec.t0 + h)  # fmt: skip
    return ev, m, reg


def _times(spec: TempSpec, scenario: str) -> np.ndarray:
    return evaluation_times(spec.t0 + spec.warmup_days, spec.end(scenario), 1)


def _first_admitted(rep: Replay, after: float) -> tuple[float | None, bool]:
    """(first time >= ``after`` at which the level is admitted, already admitted at ``after``)."""
    already = rep.level_at(after) in ("EXPLORATORY_PAPER", "CONFIRMED_PAPER")
    if already:
        return after, True
    for e in rep.episodes:
        if e["start"] >= after:
            return e["start"], False
    return None, False


def _deactivated_after(rep: Replay, t: float) -> float | None:
    for e in rep.episodes:
        if e["start"] <= t and (e["end"] is None or e["end"] > t):
            return e["end"]
    return None


def truth_metrics(scenario: str, ev: EventSet, mu: np.ndarray, rep: Replay, spec: TempSpec) -> dict:
    span0, span1 = spec.t0 + spec.warmup_days, spec.end(scenario)
    span = (ev.t_sig >= span0) & (ev.t_sig < span1)
    part = rep.participate & span
    years = (span1 - span0) / 365.25
    edge, null, neg = span & (mu > 0), span & (mu == 0), span & (mu < 0)
    taken = np.flatnonzero(part)
    cum = np.cumsum(ev.net[taken]) if len(taken) else np.zeros(0)
    mdd = (
        float((np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum).max())
        if len(cum)
        else 0.0
    )
    starts = np.array([e["start"] for e in rep.episodes])
    lengths = np.array([(e["end"] if e["end"] is not None else span1) - e["start"]
                        for e in rep.episodes])  # fmt: skip
    planted_at = planted(scenario, starts, spec) if len(starts) else np.zeros(0)
    false_ep = planted_at <= 0
    null_years = max(null.sum(), 1) / max(span.sum(), 1) * years  # null share of the span
    sig = pd.DataFrame({"t_sig": ev.t_sig, "asset": ev.asset, "side": "long", "admitted": part})
    opp = opportunity_rate(sig[span], span0, span1)
    res = {
        "trades_per_year": len(taken) / years if years else None,
        "net_per_trade": float(ev.net[taken].mean()) if len(taken) else None,
        "net_sum": float(ev.net[taken].sum()) if len(taken) else 0.0,
        "max_drawdown": mdd,
        "participation_share": float(part[span].mean()) if span.any() else None,
        "null_participation_share": float(part[null].mean()) if null.any() else None,
        "false_admissions_per_year": float(false_ep.sum() / null_years) if null_years else None,
        "false_active_mean_days": float(lengths[false_ep].mean()) if false_ep.any() else None,
        "false_confirmations": sum(1 for e, f in zip(rep.episodes, false_ep, strict=True)
                                   if f and e.get("confirmed_at")),
        "episodes_per_year": len(rep.episodes) / years if years else None,
        "short_episodes_lt_7d": int((lengths < 7).sum()),
        "transitions_per_year": len(rep.transitions) / years if years else None,
        "independent_opportunities_per_day": opp["independent_opportunities_per_day"],
        "zero_opportunity_day_share": opp["zero_opportunity_day_share"],
        "median_days_between_opportunities": opp["median_days_between_opportunities"],
    }  # fmt: skip
    if scenario == "null":
        return res
    a = spec.edge_start
    first_len = legs(scenario)[0][1]
    first, already = _first_admitted(rep, a)
    res["already_admitted_at_edge_start"] = already
    # a detection only counts while the first edge could still pay: before it ends + horizon
    useful = first is not None and first < a + first_len
    res["detected_during_edge"] = useful  # includes "already admitted" (by null luck)
    res["newly_detected_during_edge"] = useful and not already
    res["detection_delay_days"] = None if (first is None or already) else first - a
    res["useful_detection_delay_days"] = res["detection_delay_days"] if useful else None
    res["captured_edge_share"] = float(part[edge].mean()) if edge.any() else None
    res["captured_edge_net"] = float(ev.net[part & edge].sum())
    res["planted_edge_total"] = float(mu[edge].sum())
    res["captured_planted_share"] = (float(mu[part & edge].sum() / mu[edge].sum())
                                     if edge.any() else None)  # fmt: skip
    res["edge_outcomes_before_admission"] = (int((edge & (ev.t_sig < first)).sum())
                                             if first is not None else int(edge.sum()))  # fmt: skip
    res["confirmed_during_edge"] = any(
        e.get("confirmed_at") and a <= to_days(e["confirmed_at"]) < a + first_len + spec.horizon
        for e in rep.episodes)  # fmt: skip
    end_edge = a + max(o + n for o, n, s in legs(scenario) if s > 0)
    if scenario == "reversing":
        end_edge = a + 45
    off = _deactivated_after(rep, end_edge)
    active_at_end = rep.level_at(end_edge) in ("EXPLORATORY_PAPER", "CONFIRMED_PAPER")
    res["admitted_at_edge_end"] = active_at_end
    res["deactivation_delay_days"] = (off - end_edge if active_at_end and off is not None
                                      else None)  # fmt: skip
    after = part & (ev.t_sig >= end_edge) & (ev.t_sig < end_edge + 120)
    res["trades_after_edge_end_120d"] = int(after.sum())
    res["net_after_edge_end_120d"] = float(ev.net[after].sum())
    if neg.any():
        res["reversed_period_participation_share"] = float(part[neg].mean())
        res["net_in_reversed_period"] = float(ev.net[part & neg].sum())
    if scenario == "intermittent":
        hits = []
        for off_, n, _ in legs(scenario):
            m = edge & (ev.t_sig >= a + off_) & (ev.t_sig < a + off_ + n)
            hits.append(bool((part & m).any()))
        res["bursts_participated"] = int(sum(hits))
        res["reactivations"] = sum(1 for e in rep.episodes if e.get("reactivation"))
    return res


def run_one(scenario: str, seed: int, spec: TempSpec, policies: dict[str, AnyPolicy]) -> dict:
    ev, mu, reg = generate(scenario, seed, spec)
    floor = POLICIES["BALANCED"].floor.floor(spec.horizon)
    times = _times(spec, scenario)
    cur = reg[np.clip((times - spec.t0).astype(int), 0, len(reg) - 1)]
    out = {}
    for name, pol in policies.items():
        rep = replay(ev, times, pol, floor, regime_at=cur)
        out[name] = truth_metrics(scenario, ev, mu, rep, spec)
    return out


def _job(args) -> dict:
    scenario, seed, spec, policies = args
    return run_one(scenario, seed, spec, policies)


def _map(jobs: list, workers: int) -> list:
    if workers <= 1:
        return [_job(j) for j in jobs]
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    ctx = multiprocessing.get_context("spawn")  # the parent may hold DuckDB/BLAS threads
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        return list(pool.map(_job, jobs, chunksize=8))


def _policies(names=PROFILES) -> dict[str, AnyPolicy]:
    return {n: POLICIES[n] for n in names}


def calibrate(seeds: int = 200, null_seeds: int = 300, edges: tuple[float, ...] = (0.03, 0.06),
              spec: TempSpec = TempSpec(), workers: int = 1, surface_seeds: int = 25,
              grid_seeds: int = 60, surface: bool = True, grid: bool = True) -> dict:  # fmt: skip
    """The full Phase 21 calibration report (deterministic)."""
    t0 = time.perf_counter()
    pols = _policies()
    report: dict = {"version": CALIBRATION_VERSION,
                    "policies": {n: p.policy_id for n, p in pols.items()},
                    "spec": {**spec.__dict__}, "edges": list(edges), "scenarios": {}}  # fmt: skip
    keys, groups = [], []
    for scenario in SCENARIOS:
        for edge in (0.0,) if scenario == "null" else edges:
            sp = replace(spec, edge=edge or spec.edge)
            n = null_seeds if scenario == "null" else seeds
            keys.append(("scenario", scenario if scenario == "null" else f"{scenario}@{edge:g}"))
            groups.append([(scenario, 20_000 + s, sp, pols) for s in range(n)])
    if surface:
        for cell, sp, scen in surface_cells(spec):
            keys.append(("surface", cell))
            groups.append([(scen, 40_000 + s, sp, pols) for s in range(surface_seeds)])
    if grid:
        for label, pol in design_policies().items():
            for scen, edge in (("null", 0.03), ("edge30", 0.03), ("edge60", 0.03)):
                keys.append(("grid", f"{label}|{scen}"))
                groups.append([(scen, 60_000 + s, replace(spec, edge=edge), {"p": pol})
                               for s in range(grid_seeds)])  # fmt: skip
    flat = [j for g in groups for j in g]
    results = _map(flat, workers)  # order preserved: identical for any worker count
    i, surf, gr = 0, {}, {}
    for (kind, key), g in zip(keys, groups, strict=True):
        chunk, i = results[i : i + len(g)], i + len(g)
        if kind == "scenario":
            report["scenarios"][key] = {"seeds": len(g), "policies": {
                n: _agg([r[n] for r in chunk]) for n in pols}}  # fmt: skip
        elif kind == "surface":
            surf[key] = {n: _surface_cell([r[n] for r in chunk]) for n in pols}
        else:
            label, scen = key.split("|")
            gr.setdefault(label, {})[scen] = _agg([r["p"] for r in chunk])
    if surface:
        report["response_surface"] = {"seeds_per_cell": surface_seeds, "cells": surf}
    if grid:
        report["design_grid"] = {"rules": DESIGN_RULES, "seeds": grid_seeds,
                                 "configs": {k: design_policies()[k].model_dump(mode="json",
                                             include={"window", "admission", "deactivation"})
                                             for k in gr},
                                 "results": gr, "selection": select(gr)}  # fmt: skip
    report["seconds"] = round(time.perf_counter() - t0, 1)
    return report


# --------------------------------------------------------------------------- response surface

SURFACE_LIFESPANS = (14, 30, 60, 90, 180)
SURFACE_STRENGTHS = (0.015, 0.03, 0.06)
SURFACE_VOLS = (0.05, 0.10)


def surface_cells(spec: TempSpec) -> list[tuple[str, TempSpec, str]]:
    """Single-edge cells over lifespan x strength x frequency x volatility (``life<N>``)."""
    out = []
    for life in SURFACE_LIFESPANS:
        for freq, (assets, prob) in FREQUENCIES.items():
            for vol in SURFACE_VOLS:
                for e in SURFACE_STRENGTHS:
                    sp = replace(spec, edge=e, sigma=vol, assets=assets, signal_prob=prob)
                    out.append((f"life={life}|freq={freq}|sigma={vol:g}|edge={e:g}", sp,
                                f"life{life}"))  # fmt: skip
    return out


def _surface_cell(rows: list[dict]) -> dict:
    det = [r["detected_during_edge"] for r in rows]
    new = [r["newly_detected_during_edge"] for r in rows]
    already = [r["already_admitted_at_edge_start"] for r in rows]
    cap = [r["captured_planted_share"] for r in rows if r["captured_planted_share"] is not None]
    delay = [r["detection_delay_days"] for r in rows if r["detection_delay_days"] is not None]
    nullp = [
        r["null_participation_share"] for r in rows if r["null_participation_share"] is not None
    ]
    return {"detected_share": float(np.mean(det)) if det else None,
            "newly_detected_share": float(np.mean(new)) if new else None,
            "already_admitted_share": float(np.mean(already)) if already else None,
            "captured_mean": float(np.mean(cap)) if cap else None,
            "delay_median": float(np.median(delay)) if delay else None,
            "null_participation_mean": float(np.mean(nullp)) if nullp else None}  # fmt: skip


# --------------------------------------------------------------------------- design grid


def design_policies() -> dict[str, IncubationPolicy]:
    """The pre-declared grid: window {21, 30, 45, 60} days x admission t {0.5, ..., 2.5}
    (t 2.0 / 2.5 were added after the first run found no feasible BALANCED member).
    Sample gates scale with the window (4 outcomes per 30 days, at least 4); the exit band
    sits one standard error below admission; the lapse gate is half the sample gate."""
    out = {}
    for days in (21, 30, 45, 60):
        n = max(4, round(4 * days / 30))
        for t in (0.5, 1.0, 1.5, 2.0, 2.5):
            out[f"w{days}_t{t:g}"] = IncubationPolicy(
                profile="BALANCED",
                version=900,  # grid members are never stored or used live
                window=Window(days=days, min_events=n),
                admission=Admission(min_t=t, loo_min_floors=0.0 if t >= 1 else -1.0),
                deactivation=Deactivation(exit_max_t=t - 1.0, lapse_min_events=max(2, n // 2)),
            )
    return out


def select(results: dict) -> dict:
    """Apply ``DESIGN_RULES`` to the grid results."""

    def m(label, scen, key):
        v = results[label][scen].get(key) or {}
        return v.get("mean")

    def pick(target: str, ok) -> str | None:
        rows = [(m(lb, target, "captured_planted_share") or 0.0,
                 float(lb.split("_t")[1]), int(lb.split("_")[0][1:]), lb)
                for lb in results if ok(lb)]  # fmt: skip
        return max(rows)[3] if rows else None

    aggressive = pick("edge30", lambda lb: (m(lb, "null", "null_participation_share") or 1) <= 0.40)
    balanced = pick("edge60", lambda lb: (m(lb, "null", "null_participation_share") or 1) <= 0.15
                    and (m(lb, "null", "false_admissions_per_year") or 99) <= 4)  # fmt: skip
    return {"AGGRESSIVE": aggressive, "BALANCED": balanced}
