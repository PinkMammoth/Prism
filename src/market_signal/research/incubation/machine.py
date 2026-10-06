"""The fast incubation state machine (``incubation_machine_v1``), episodes and the
CONSERVATIVE benchmark adapter.

Levels::

    INSUFFICIENT / NEUTRAL / WATCH  --(one admission)-->  EXPLORATORY_PAPER  --(forward)-->  CONFIRMED_PAPER
              ^                                                 |                               |
              +------------------ DORMANT <---(any deactivation)+-------------------------------+
                                     |
                                     +--(admission met again, with new evidence)--> new episode

- WATCH: interesting recent behaviour (positive mean on a few outcomes), not enough for paper.
- EXPLORATORY_PAPER: admitted at ONE evaluation meeting the frozen rule set; generates
  shadow intents for every independent signal. Continues while evidence is non-hostile.
- CONFIRMED_PAPER: forward (episode) outcomes support the hypothesis (``Graduation``);
  falls back to EXPLORATORY_PAPER if the forward mean turns negative. No LIVE state exists.
- DORMANT: deactivated (evidence reversed, lapsed, strongly adverse episode outcomes, or a
  hostile regime change). Still observed; re-admission opens a NEW episode once at least one
  new outcome has resolved (a dormant candidate cannot flip back on unchanged evidence).

Hysteresis is a single band: admission needs t >= ``min_t`` (e.g. 1.0); continuation only
needs t > ``exit_max_t`` (e.g. 0.0). Nothing waits for a weekly confirmation.

Causality: at evaluation T only outcomes resolved by T are read; a signal at s participates
iff the level from the latest evaluation at or before s is admitted (Phase 20 semantics).
Episode outcomes are those of signals at or after the episode start (and at or after
``prospective_from``: in live operation the freeze time, so CONFIRMED rests on genuinely
forward observations only). Every episode has a deterministic ID.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from market_signal.research.incubation import evidence as ie
from market_signal.research.incubation.policy import (
    ADMITTED,
    AnyPolicy,
    ConservativeBenchmark,
    IncubationPolicy,
)
from market_signal.research.lab.common import content_id
from market_signal.research.lifecycle.estimators import EventSet, iso
from market_signal.research.lifecycle.machine import Track, evaluation_times

NO_REGIME = -1


def episode_id(policy_id: str, strategy_key: str, admitted_at: float) -> str:
    return content_id("incep_", {"policy_id": policy_id, "strategy": strategy_key,
                                 "admitted_at": iso(admitted_at)})  # fmt: skip


@dataclass
class Replay:
    profile: str
    policy_id: str
    times: np.ndarray  # evaluation times of the replayed cadence (days)
    levels: list[str]
    transitions: list[dict]
    episodes: list[dict]
    participate: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    confirmed: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    computed: int = 0
    skipped: int = 0

    def level_at(self, t: float) -> str:
        j = int(np.searchsorted(self.times, t, side="right")) - 1
        return self.levels[j] if j >= 0 else "INSUFFICIENT"

    def open_episode(self) -> dict | None:
        return self.episodes[-1] if self.episodes and self.episodes[-1]["end"] is None else None


def _participation(ev: EventSet, times: np.ndarray, levels: list[str]) -> tuple[np.ndarray, np.ndarray]:  # fmt: skip
    lv = np.array(levels, dtype=object)
    j = np.searchsorted(times, ev.t_sig, side="right") - 1
    ok = j >= 0
    part = np.zeros(len(ev), dtype=bool)
    conf = np.zeros(len(ev), dtype=bool)
    if len(times):
        cur = lv[np.clip(j, 0, len(lv) - 1)]
        part = ok & np.isin(cur, ADMITTED)
        conf = ok & (cur == "CONFIRMED_PAPER")
    return part, conf


def _episode_mask(ev: EventSet, k: int, start: float, prospective_from: float) -> np.ndarray:
    m = np.zeros(len(ev), dtype=bool)
    m[:k] = ev.t_sig[:k] >= max(start, prospective_from)
    return m


def _brief(w: ie.Est, life: ie.Est) -> dict:
    return {"window_n": w.n, "window_assets": w.assets, "window_mean": w.mean,
            "window_t": w.t, "window_loo_mean": w.loo_mean, "window_excess_mean": w.excess_mean,
            "lifetime_n": life.n, "lifetime_mean": life.mean, "lifetime_t": life.t}  # fmt: skip


def _finish(ev: EventSet, rep: Replay) -> Replay:
    rep.participate, rep.confirmed = _participation(ev, rep.times, rep.levels)
    for e in rep.episodes:
        hi = np.inf if e["end"] is None else e["end"]
        m = rep.participate & (ev.t_sig >= e["start"]) & (ev.t_sig < hi)
        e["outcomes"] = int(m.sum())
        e["net_sum"] = float(ev.net[m].sum()) if m.any() else 0.0
        e["net_mean"] = float(ev.net[m].mean()) if m.any() else None
        e["admitted_at"], e["deactivated_at"] = iso(e["start"]), iso(e["end"]) if e["end"] else None
    return rep


# --------------------------------------------------------------------------- incubation


def replay_incubation(ev: EventSet, times: np.ndarray, policy: IncubationPolicy, floor: float, *,
                      strategy_key: str = "synthetic", regime_at: np.ndarray | None = None,
                      prospective_from: float = -np.inf) -> Replay:  # fmt: skip
    """Walk the evaluation times in order (see the module docstring)."""
    pid = policy.policy_id
    rep = Replay(policy.profile, pid, np.asarray(times, dtype=float), [], [], [])
    state, ep, dormant_k, last_key = "INSUFFICIENT", None, None, None
    life_cache: dict[int, ie.Est] = {}
    trend = ev.regime.get("trend") if ev.regime else None

    def go(t: float, new: str, reasons: list[str], w: ie.Est, life: ie.Est, **extra) -> None:
        nonlocal state
        rep.transitions.append({"at": iso(t), "at_days": t, "from": state, "to": new,
                                "reasons": reasons, "evidence": _brief(w, life), **extra})  # fmt: skip
        state = new

    for i, t in enumerate(rep.times):
        t = float(t)
        lo, k = ie.window_bounds(ev, t, policy.window.days)
        reg = int(regime_at[i]) if regime_at is not None else NO_REGIME
        key = (lo, k, reg if state in ADMITTED else None)
        if key == last_key:
            rep.levels.append(state)
            rep.skipped += 1
            continue
        last_key = key
        rep.computed += 1
        w = ie.slice_est(ev, lo, k)
        if k not in life_cache:
            life_cache.clear()
            life_cache[k] = ie.slice_est(ev, 0, k)
        life = life_cache[k]
        if state in ADMITTED:
            em = _episode_mask(ev, k, ep["start"], prospective_from)
            epi = ie.mask_est(ev, em)
            changed = reg != NO_REGIME and ep["regime"] != NO_REGIME and reg != ep["regime"]
            rw = None
            if changed and trend is not None:
                rm = np.zeros(len(ev), dtype=bool)
                rm[lo:k] = trend[lo:k] == reg
                rw = ie.mask_est(ev, rm)
            reasons = ie.deactivation(w, epi, policy, changed, rw)
            if reasons:
                go(t, "DORMANT", reasons, w, life, episode_id=ep["episode_id"])
                ep["end"], ep["deactivation_reasons"] = t, reasons
                ep, dormant_k = None, k
            else:
                ok_g, _ = ie.graduation(epi, ep["admission_mean"], 0, policy, floor)
                if state == "EXPLORATORY_PAPER" and ok_g:
                    go(t, "CONFIRMED_PAPER", ["forward evidence met the graduation rule"], w,
                       life, episode_id=ep["episode_id"], forward=epi.brief())  # fmt: skip
                    ep.setdefault("confirmed_at", iso(t))
                elif (state == "CONFIRMED_PAPER" and epi.mean is not None
                      and epi.mean < policy.graduation.demote_below_mean):  # fmt: skip
                    go(t, "EXPLORATORY_PAPER", ["forward mean fell below the demotion level"], w,
                       life, episode_id=ep["episode_id"], forward=epi.brief())  # fmt: skip
            rep.levels.append(state)
            continue
        ok, checks = ie.admission(w, life, policy, floor)
        fresh = (state != "DORMANT" or dormant_k is None
                 or k - dormant_k >= policy.admission.readmit_new_outcomes)  # fmt: skip
        if ok and fresh:
            eid = episode_id(pid, strategy_key, t)
            reactivation = dormant_k is not None
            go(t, "EXPLORATORY_PAPER", ["admission rule met: " + ", ".join(checks)], w, life,
               episode_id=eid, reactivation=reactivation)  # fmt: skip
            ep = {"episode_id": eid, "policy_id": pid, "strategy": strategy_key, "start": t,
                  "end": None, "regime": reg, "reactivation": reactivation,
                  "admission_mean": w.mean, "admission_evidence": {**w.brief(), "checks": checks},
                  "lifetime_at_admission": life.brief(), "deactivation_reasons": None}  # fmt: skip
            rep.episodes.append(ep)
        elif state != "DORMANT":
            new = ("WATCH" if ie.interesting(w, policy)
                   else "NEUTRAL" if w.n >= policy.watch.min_events else "INSUFFICIENT")  # fmt: skip
            if new != state:
                go(
                    t,
                    new,
                    [k_ for k_, v in checks.items() if not v] or ["not interesting"],
                    w,
                    life,
                )
        rep.levels.append(state)
    return _finish(ev, rep)


# --------------------------------------------------------------------------- conservative


def replay_conservative(ev: EventSet, times: np.ndarray, bench: ConservativeBenchmark,
                        floor: float, *, strategy_key: str = "synthetic",
                        prospective_from: float = -np.inf) -> Replay:  # fmt: skip
    """The unchanged Phase 20 recent-mode Track at its own weekly cadence from ``times[0]``;
    lifecycle states are mapped to levels. The same forward-only graduation overlay as the
    other profiles is applied inside its participating spells (it never changes the Track)."""
    lp = bench.lifecycle()
    times = np.asarray(times, dtype=float)
    weekly = (evaluation_times(times[0], times[-1] + 1e-9, lp.evaluation.cadence_days)
              if len(times) else times)  # fmt: skip
    track = Track(ev, lp, floor)
    pid = bench.policy_id
    rep = Replay(bench.profile, pid, weekly, [], [], [])
    level, ep = "INSUFFICIENT", None
    for t in weekly:
        t = float(t)
        new = bench.level(track.step(t))
        rep.computed += 1
        if new in ADMITTED and ep is None:
            eid = episode_id(pid, strategy_key, t)
            h = track.transitions[-1]
            ep = {"episode_id": eid, "policy_id": pid, "strategy": strategy_key, "start": t,
                  "end": None, "regime": NO_REGIME, "reactivation": bool(h.get("reacquisition")),
                  "admission_mean": (h.get("evidence") or {}).get("recent_mean"),
                  "admission_evidence": h.get("evidence"), "deactivation_reasons": None}  # fmt: skip
            rep.episodes.append(ep)
        elif new not in ADMITTED and ep is not None:
            ep["end"], ep["deactivation_reasons"] = t, track.transitions[-1]["reasons"]
            ep = None
        if ep is not None:  # forward graduation overlay
            k = ev.resolved(t)
            epi = ie.mask_est(ev, _episode_mask(ev, k, ep["start"], prospective_from))
            ok_g, _ = ie.graduation(epi, ep["admission_mean"], 0, bench, floor)
            if level == "CONFIRMED_PAPER":
                new = ("EXPLORATORY_PAPER" if epi.mean is not None
                       and epi.mean < bench.graduation.demote_below_mean else "CONFIRMED_PAPER")  # fmt: skip
            elif ok_g:
                new = "CONFIRMED_PAPER"
                ep.setdefault("confirmed_at", iso(t))
        if new != level:
            last = track.transitions[-1] if track.transitions else {}
            rep.transitions.append({"at": iso(t), "at_days": t, "from": level, "to": new,
                                    "reasons": last.get("reasons", []),
                                    "lifecycle_state": track.state,
                                    "episode_id": ep["episode_id"] if ep else None})  # fmt: skip
            level = new
        rep.levels.append(level)
    return _finish(ev, rep)


def replay(ev: EventSet, times: np.ndarray, policy: AnyPolicy, floor: float, **kw) -> Replay:
    if isinstance(policy, ConservativeBenchmark):
        kw.pop("regime_at", None)
        return replay_conservative(ev, times, policy, floor, **kw)
    return replay_incubation(ev, times, policy, floor, **kw)
