"""Synthetic edge-lifecycle calibration: series with KNOWN edge paths.

Outcome ledgers are generated directly (one row per independent signal), so thousands of
lifecycle simulations stay cheap. Each scenario plants a per-event after-cost edge path
mu(t, regime):

| Scenario | mu |
|---|---|
| stable | +edge throughout |
| dead | 0 throughout (the null) |
| emerging | 0 before the change, +edge after |
| decaying | +edge before the change, 0 after |
| reversing | +edge before, -edge after |
| regime | +edge in the "up" trend regime, 0 in "neutral", -edge/2 in "down" |

Noise per event = sigma x (sqrt(rho) x common weekly shock + sqrt(1 - rho) x Student-t(5)
scaled to unit variance): co-timed outcomes share a market shock and tails are fat, as in
crypto. Signals arrive per asset with probability ``signal_prob`` per day and are declustered
at the horizon (independent by construction). The trend regime is a 3-state Markov chain
with mean dwell ``regime_dwell_days``.

``calibrate`` reports, per scenario and edge size, the quantities of section 26-27/51 of the
Phase 20 brief: false activation rate, false-active time and duration, false reactivation,
false-positive strategy-years, detection delay and outcomes missed before activation,
deactivation delay and losses after the true edge died, captured planted edge, and the
share of dead-period outcomes that participated. Same seed -> same numbers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

import numpy as np

from market_signal.research.lifecycle.estimators import REGIME_DIMS, EventSet, to_days
from market_signal.research.lifecycle.machine import evaluation_times, simulate
from market_signal.research.lifecycle.policy import Activation, LifecyclePolicy, policy

SCENARIOS = ("stable", "dead", "emerging", "decaying", "reversing", "regime")
CALIBRATION_VERSION = "lab_lifecycle_calibration_v1"


@dataclass(frozen=True)
class SyntheticSpec:
    years: float = 6.0
    assets: int = 5
    horizon: int = 10
    signal_prob: float = 1 / 8
    sigma: float = 0.10
    market_share: float = 0.5
    tail_df: int = 5
    regime_dwell_days: float = 90.0
    edge: float = 0.03
    start: str = "2019-01-07"
    warmup_days: int = 120  # first evaluation after this many days

    @property
    def t0(self) -> float:
        return to_days(self.start)

    @property
    def change(self) -> float:
        return self.t0 + self.years * 365 / 2

    @property
    def end(self) -> float:
        return self.t0 + self.years * 365


def mu(scenario: str, t: np.ndarray, regime: np.ndarray, spec: SyntheticSpec) -> np.ndarray:
    e = spec.edge
    after = t >= spec.change
    if scenario == "stable":
        return np.full(len(t), e)
    if scenario == "dead":
        return np.zeros(len(t))
    if scenario == "emerging":
        return np.where(after, e, 0.0)
    if scenario == "decaying":
        return np.where(after, 0.0, e)
    if scenario == "reversing":
        return np.where(after, -e, e)
    if scenario == "regime":
        return np.select([regime == 2, regime == 1], [e, 0.0], -e / 2)
    raise ValueError(f"unknown scenario {scenario}")


def regime_path(rng: np.random.Generator, days: int, dwell: float) -> np.ndarray:
    out = np.empty(days, dtype=np.int64)
    s = int(rng.integers(3))
    switch = rng.random(days) < 1 / dwell
    pick = rng.integers(1, 3, size=days)
    for d in range(days):
        if switch[d]:
            s = (s + pick[d]) % 3
        out[d] = s
    return out


def generate(scenario: str, seed: int, spec: SyntheticSpec = SyntheticSpec()) -> tuple[EventSet, np.ndarray, np.ndarray]:  # fmt: skip
    """(events, planted mu per event, regime code per day from t0)."""
    rng = np.random.default_rng(seed)
    days = int(spec.years * 365)
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
    d_sig = np.array(t_sig)[order]
    asset = np.array(asset, dtype=np.int64)[order]
    r = reg[d_sig]
    df = spec.tail_df
    eps = rng.standard_t(df, size=len(d_sig)) / np.sqrt(df / (df - 2))
    rho = spec.market_share
    noise = spec.sigma * (np.sqrt(rho) * shocks[d_sig // 7] + np.sqrt(1 - rho) * eps)
    t = spec.t0 + d_sig.astype(float)
    m = mu(scenario, t, r, spec)
    net = m + noise
    n = len(net)
    nan = np.full(n, np.nan)
    regime = {d: np.full(n, -1, dtype=np.int64) for d in REGIME_DIMS}
    regime["trend"] = r
    t_res = t + h
    ev = EventSet(assets=tuple(f"S{i}" for i in range(spec.assets)), t_sig=t, t_res=t_res,
                  net=net, excess=net.copy(), gross=net + 0.0015, mae=nan, mfe=nan,
                  cost=np.full(n, 0.0015), funding=np.zeros(n), asset=asset,
                  stress=np.zeros(n, dtype=bool), regime=regime, all_res=t_res,
                  all_valid_cum=np.arange(n + 1), history_start=spec.t0 + h)  # fmt: skip
    return ev, m, reg


def _first(history: list[dict], to: str, after: float, track: str | None = None) -> float | None:
    for h in history:
        t = to_days(h["at"])
        if (h["to"] == to and t >= after and (track is None or h["track"] == track)
                and (to != "PAPER_ACTIVE" or h["from"] == "ACTIVE_CANDIDATE")):  # fmt: skip
            return t
    return None


def participating_at(history: list[dict], t: float) -> bool:
    """Whether any track's latest state at or before ``t`` participates."""
    last: dict[str, str] = {}
    for h in history:
        if to_days(h["at"]) <= t:
            last[h["track"]] = h["to"]
    return any(s in ("PAPER_ACTIVE", "DEGRADED") for s in last.values())


def run_one(scenario: str, seed: int, spec: SyntheticSpec, pol: LifecyclePolicy,
            modes=("static", "recent", "regime")) -> dict:  # fmt: skip
    ev, m, reg = generate(scenario, seed, spec)
    floor = pol.floor.floor(spec.horizon)
    times = evaluation_times(spec.t0 + spec.warmup_days, spec.end, pol.evaluation.cadence_days)
    cur = reg[np.clip((times - spec.t0).astype(int), 0, len(reg) - 1)]
    out = {}
    for mode in modes:
        sim = simulate(ev, times, pol, floor, mode, current_regime=cur)
        part = _participation(ev, times, sim, mode, pol)
        out[mode] = _truth_metrics(scenario, ev, m, part, sim, spec)
    return out


def _participation(ev: EventSet, times, sim: dict, mode: str, pol) -> np.ndarray:
    """Recompute the participation mask from the recorded transitions (same rule as
    ``simulate``), so truth metrics can be attached per event."""
    in_span = ev.t_sig >= times[0]
    if mode == "static":
        return in_span
    labels = ("down", "neutral", "up")
    tracks = ["all"] if mode == "recent" else list(labels)
    state = {tr: np.zeros(len(times), dtype=bool) for tr in tracks}
    for tr in tracks:
        cur = False
        hist = [h for h in sim["history"] if h["track"] == tr]
        hi = 0
        for i, t in enumerate(times):
            while hi < len(hist) and to_days(hist[hi]["at"]) <= t + 1e-9:
                cur = hist[hi]["to"] in ("PAPER_ACTIVE", "DEGRADED")
                hi += 1
            state[tr][i] = cur
    j = np.searchsorted(times, ev.t_sig, side="right") - 1
    part = np.zeros(len(ev), dtype=bool)
    ok = j >= 0
    if mode == "recent":
        part[ok] = state["all"][j[ok]]
    else:
        for code, lab in enumerate(labels):
            msk = ok & (ev.regime["trend"] == code)
            part[msk] = state[lab][j[msk]]
    return part & in_span


def _truth_metrics(scenario, ev, m, part, sim, spec) -> dict:
    span = ev.t_sig >= (spec.t0 + spec.warmup_days)
    edge = (m > 0) & span
    dead = (m <= 0) & span
    hist = sim["history"]
    years = sim["years"] or 1.0
    res = {
        "net_sum": float(ev.net[part].sum()),
        "participation_share": float(part[span].mean()) if span.any() else None,
        "captured_edge_share": float(part[edge].mean()) if edge.any() else None,
        "dead_period_participation_share": float(part[dead].mean()) if dead.any() else None,
        "activations_per_year": sim.get("activations", 0) / years,
        "reactivations_per_year": sim.get("reactivations", 0) / years,
        "deactivations_per_year": sim.get("deactivations", 0) / years,
        "transitions_per_year": sim.get("transitions_per_year") or 0.0,
        "active_time_share": sim.get("active_time_share"),
        "mean_spell_days": sim.get("mean_spell_days"),
        "short_bursts": sim.get("short_bursts", 0),
        "false_positive_strategy_years": None,
    }
    if scenario == "dead":
        res["false_positive_strategy_years"] = (sim.get("active_time_share") or 0) * years
    if scenario in ("emerging",) and sim["mode"] != "static":
        # delays are measured on runs NOT already (falsely) participating at emergence
        already = participating_at(hist, spec.change)
        first = _first(hist, "PAPER_ACTIVE", spec.change)
        res["participating_at_emergence"] = already
        res["detection_delay_days"] = None if already or first is None else first - spec.change
        res["not_detected_by_end"] = (not already) and first is None
        pre = edge & (ev.t_sig < (first if first is not None else np.inf))
        res["edge_outcomes_missed_before_activation"] = None if already else int(pre.sum())
        res["false_activation_before_change"] = any(
            h["to"] == "PAPER_ACTIVE" and h["from"] == "ACTIVE_CANDIDATE"
            and to_days(h["at"]) < spec.change for h in hist)  # fmt: skip
    if scenario in ("decaying", "reversing") and sim["mode"] != "static":
        active = participating_at(hist, spec.change)
        off = _first(hist, "DORMANT", spec.change)
        res["active_at_change"] = active
        res["deactivation_delay_days"] = None if off is None or not active else off - spec.change
        res["not_deactivated_by_end"] = active and off is None
        after = part & (ev.t_sig >= spec.change)
        res["outcomes_after_edge_death"] = int(after.sum())
        res["net_after_edge_death"] = float(ev.net[after].sum())
    return res


def _agg(rows: list[dict]) -> dict:
    keys = sorted({k for r in rows for k in r})
    out = {}
    for k in keys:
        vals = [r.get(k) for r in rows]
        if all(isinstance(v, bool) for v in vals if v is not None) and any(
            isinstance(v, bool) for v in vals
        ):
            v = [x for x in vals if x is not None]
            out[k] = {"share_true": float(np.mean(v)) if v else None, "n": len(v)}
            continue
        nums = [float(v) for v in vals if isinstance(v, int | float) and not isinstance(v, bool)]
        if not nums:
            out[k] = {"n": 0, "missing": len(vals)}
            continue
        a = np.array(nums)
        out[k] = {"mean": float(a.mean()), "median": float(np.median(a)),
                  "p10": float(np.quantile(a, 0.1)), "p90": float(np.quantile(a, 0.9)),
                  "n": len(a), "missing": len(vals) - len(a)}  # fmt: skip
    return out


def _job(args) -> dict:
    scenario, seed, spec, pol, modes = args
    return run_one(scenario, seed, spec, pol, modes=modes)


def _map(jobs: list, workers: int) -> list:
    if workers <= 1:
        return [_job(j) for j in jobs]
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    # spawn, not fork: the parent may hold threads (DuckDB, BLAS)
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        return list(pool.map(_job, jobs, chunksize=8))


def calibrate(seeds: dict[str, int] | None = None, edges: tuple[float, ...] = (0.015, 0.03),
              spec: SyntheticSpec = SyntheticSpec(), pol: LifecyclePolicy | None = None,
              threshold_grid: tuple[float, ...] = (1.5, 2.0, 2.5),
              grid_seeds: int = 60, workers: int = 1) -> dict:  # fmt: skip
    """The full calibration report (deterministic). ``threshold_grid`` re-runs the null and
    emerging scenarios with alternative activation t thresholds to expose the
    responsiveness-versus-noise tradeoff. The grid is descriptive: v1's threshold is the
    frozen policy's, not the grid's best."""
    pol = pol or policy()
    seeds = seeds or {"dead": 300, "stable": 100, "emerging": 100, "decaying": 100,
                      "reversing": 100, "regime": 100}  # fmt: skip
    t0 = time.perf_counter()
    report: dict = {"version": CALIBRATION_VERSION, "policy_id": pol.policy_id,
                    "spec": {**spec.__dict__}, "edges": list(edges), "scenarios": {}}  # fmt: skip
    jobs, keys = [], []
    for scenario in SCENARIOS:
        for edge in (0.0,) if scenario == "dead" else edges:
            sp = replace(spec, edge=edge if edge else spec.edge)
            keys.append((scenario if scenario == "dead" else f"{scenario}@{edge:g}", scenario))
            jobs.append([(scenario, 1000 + s, sp, pol, ("static", "recent", "regime"))
                         for s in range(seeds[scenario])])  # fmt: skip
    grid_pols = {thr: LifecyclePolicy(version=pol.version, activation=Activation(
        **{**pol.activation.model_dump(), "min_t": thr})) for thr in threshold_grid}  # fmt: skip
    for thr, p2 in grid_pols.items():
        for scenario in ("dead", "emerging"):
            keys.append((f"grid:{thr:g}:{scenario}", scenario))
            jobs.append([(scenario, 5000 + s, replace(spec, edge=max(edges)), p2, ("recent",))
                         for s in range(grid_seeds)])  # fmt: skip
    flat = [j for group in jobs for j in group]
    results = _map(flat, workers)  # order preserved: identical output for any worker count
    grid: dict = {}
    i = 0
    for (key, scenario), group in zip(keys, jobs, strict=True):
        chunk, i = results[i : i + len(group)], i + len(group)
        if key.startswith("grid:"):
            _, thr, _ = key.split(":")
            grid.setdefault(f"min_t={thr}", {})[scenario] = _agg([r["recent"] for r in chunk])
        else:
            report["scenarios"][key] = {"seeds": len(group), "modes": {
                m: _agg([r[m] for r in chunk]) for m in ("static", "recent", "regime")}}  # fmt: skip
    report["activation_threshold_tradeoff"] = {"note": "descriptive; seeds 5000+, recent mode, "
                                               f"edge {max(edges):g}", "grid": grid}  # fmt: skip
    report["seconds"] = round(time.perf_counter() - t0, 1)
    return report
