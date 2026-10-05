"""Research-level lifecycle state machine (``lifecycle_machine_v1``) and its causal simulation.

States::

    DISCOVERED -> WATCH -> ACTIVE_CANDIDATE -> PAPER_ACTIVE <-> DEGRADED -> DORMANT -> RETIRED
                                  ^                                            |
                                  +---------------- (evidence returns) --------+

- DISCOVERED: registered, no adequate evidence yet.
- WATCH: evidence adequate, activation not met.
- ACTIVE_CANDIDATE: activation met; must hold at ``confirm_evaluations`` consecutive
  evaluations, else it falls back to WATCH (or DORMANT, where it came from).
- PAPER_ACTIVE: *simulated* participation. The name follows the Phase 20 brief; it grants
  nothing to the real paper account, whose policies are unchanged.
- DEGRADED: continuation failed (weaker thresholds than activation: hysteresis). Still
  participating; ``deactivate_after`` consecutive failures make it DORMANT, recovery makes it
  PAPER_ACTIVE again. A hard failure (recent t <= -2, or a CUSUM alarm with a negative
  recent mean) goes straight to DORMANT.
- DORMANT: not participating, still observed (every signal's hypothetical outcome keeps
  feeding the evidence), so activation can be re-earned — a reacquisition.
- RETIRED: dormant for 730 days with a credibly negative lifetime; terminal for this track
  (a later attempt would be a new, separately recorded track).

Drawdown is never a reason on its own: continuation looks at per-event expectancy, its
uncertainty, a CUSUM against the expectation at activation, and (regime mode) regime-local
evidence. Every transition is appended with its reasons and evidence; nothing is
overwritten.

Simulation (``simulate``): at each evaluation date T (every ``cadence_days``), only outcomes
resolved by T are read; the state is updated; a signal at time s participates iff the state
from the latest evaluation at or before s is PAPER_ACTIVE or DEGRADED. Modes:

- ``static``: every signal participates (the "always on" comparison);
- ``recent``: one track on all outcomes;
- ``regime``: one track per label of the regime key (v1: BTC trend); a signal participates
  iff the track of its own entry regime participates. Unknown-regime signals never do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from market_signal.research.lifecycle.changepoint import ActiveCusum, Blocks
from market_signal.research.lifecycle.estimators import REGIME_LABELS, EventSet, iso
from market_signal.research.lifecycle.policy import LifecyclePolicy
from market_signal.research.lifecycle.state import classify, core_evidence, sign_class

LIFECYCLE_STATES = ("DISCOVERED", "WATCH", "ACTIVE_CANDIDATE", "PAPER_ACTIVE", "DEGRADED",
                    "DORMANT", "RETIRED")  # fmt: skip
PARTICIPATING = ("PAPER_ACTIVE", "DEGRADED")
MODES = ("static", "recent", "regime")


def activation(core: dict, floor: float, policy: LifecyclePolicy) -> tuple[bool, dict]:
    a = policy.activation
    r, w, life = core["recent"], core["weighted"], core["lifetime"]
    ok_r = bool(r.get("adequate"))
    checks = {
        "recent_adequate": ok_r,
        "recent_mean_at_least_floor": ok_r and r["mean"] is not None and r["mean"] >= floor,
        "recent_t_at_least_min": ok_r and r.get("t") is not None and r["t"] >= a.min_t,
        "weighted_positive": w.get("mean") is not None and w["mean"] > a.weighted_min_mean
        and (w.get("ess") or 0) >= a.weighted_min_ess,
        "breadth": ok_r and (r.get("positive_asset_share") or 0) >= a.min_positive_asset_share,
        "lifetime_plausible": not life.get("adequate") or life.get("t") is None
        or life["t"] > a.lifetime_min_t,
        "excess_not_materially_negative": r.get("excess_mean") is None
        or r["excess_mean"] >= -floor,
    }  # fmt: skip
    return all(checks.values()), checks


def continuation(core: dict, floor: float, policy: LifecyclePolicy,
                 cusum_alarm: bool) -> tuple[bool, bool, list[str]]:  # fmt: skip
    """(ok, hard_failure, reasons)."""
    c = policy.continuation
    r, w = core["recent"], core["weighted"]
    reasons = []
    if not r.get("adequate"):
        reasons.append("recent evidence inadequate (status must be re-earned)")
    elif r["mean"] < c.min_mean:
        reasons.append(f"recent mean {r['mean']:.4f} < {c.min_mean}")
    elif r.get("t") is None or r["t"] < c.min_t:
        reasons.append(f"recent t below {c.min_t}")
    if w.get("mean") is None or w["mean"] < -floor:
        reasons.append("recency-weighted mean below -floor")
    if cusum_alarm:
        reasons.append("CUSUM: realised outcomes fell below the activation expectation")
    hard = bool(r.get("adequate") and r.get("t") is not None and r["t"] <= c.hard_fail_t)
    if cusum_alarm and r.get("adequate") and r["mean"] < 0:
        hard = True
    if hard:
        reasons.append("hard failure: recent effect credibly negative")
    return not reasons, hard, reasons


def _brief(core: dict) -> dict:
    r = core["recent"]
    return {"recent_window": r.get("selected"), "recent_n": r.get("n"),
            "recent_mean": r.get("mean"), "recent_t": r.get("t"),
            "lifetime_mean": core["lifetime"].get("mean"), "lifetime_t": core["lifetime"].get("t"),
            "weighted_mean": core["weighted"].get("mean")}  # fmt: skip


@dataclass
class Track:
    ev: EventSet
    policy: LifecyclePolicy
    floor: float
    label: str = "all"
    state: str = "DISCOVERED"
    transitions: list = field(default_factory=list)
    confirm: int = 0
    failures: int = 0
    candidate_from: str = "WATCH"
    dormant_since: float | None = None
    was_dormant: bool = False
    cusum: ActiveCusum | None = None
    blocks: Blocks | None = None

    def __post_init__(self):
        self.blocks = Blocks.of(self.ev, self.policy.cusum.block_days)

    def _go(self, at: float, new: str, reasons: list[str], core: dict, **extra) -> None:
        self.transitions.append({"track": self.label, "at": iso(at), "at_days": at,
                                 "from": self.state, "to": new, "reasons": reasons,
                                 "evidence": _brief(core), **extra})  # fmt: skip
        self.state = new

    def step(self, at: float) -> str:
        p = self.policy
        alarm = False
        if self.state in PARTICIPATING and self.cusum is not None:
            self.cusum.advance(at)
            alarm = self.cusum.alarmed(at)
        if self.state == "RETIRED":
            return self.state
        core = core_evidence(self.ev, at, p)
        ok_act, checks = activation(core, self.floor, p)
        failed = [c for c, v in checks.items() if not v]
        if self.state == "DISCOVERED":
            if core["lifetime"]["adequate"] or core["recent"].get("adequate"):
                self._go(at, "WATCH", ["evidence adequate for assessment"], core)
            else:
                return self.state
        if self.state in ("WATCH", "DORMANT"):
            if (self.state == "DORMANT" and self.dormant_since is not None
                    and at - self.dormant_since >= p.retirement.dormant_days
                    and sign_class(core["lifetime"], self.floor, p) == p.retirement.lifetime_class):  # fmt: skip
                self._go(at, "RETIRED", ["dormant >= 730 days with a negative lifetime"], core)
                return self.state
            if ok_act:
                self.candidate_from, self.confirm = self.state, 1
                self._go(at, "ACTIVE_CANDIDATE", ["activation criteria met"], core)
                if self.confirm < p.activation.confirm_evaluations:
                    return self.state
            else:
                return self.state
        elif self.state == "ACTIVE_CANDIDATE":
            if not ok_act:
                self._go(at, self.candidate_from, ["activation not confirmed: " + ", ".join(failed)],
                         core)  # fmt: skip
                return self.state
            self.confirm += 1
        if self.state == "ACTIVE_CANDIDATE":
            if self.confirm >= p.activation.confirm_evaluations:
                r = core["recent"]
                # activation selects a lucky-high window, so its mean overstates what to
                # expect next; the reference is the one-standard-error lower bound, floored
                ref = max(self.floor, r["mean"] - (r.get("se") or 0.0))
                self.cusum = ActiveCusum(self.blocks, p.cusum, ref, self.blocks.complete(at))
                self.failures = 0
                self._go(at, "PAPER_ACTIVE", [f"activation confirmed at {self.confirm} "
                                              "consecutive evaluations"], core,
                         reacquisition=self.was_dormant, activation_reference=ref)  # fmt: skip
            return self.state
        ok, hard, reasons = continuation(core, self.floor, p, alarm)
        if hard:
            self._dormant(at, reasons, core)
        elif self.state == "PAPER_ACTIVE" and not ok:
            self.failures = 1
            self._go(at, "DEGRADED", reasons, core)
        elif self.state == "DEGRADED":
            if ok:
                self.failures = 0
                self._go(at, "PAPER_ACTIVE", ["continuation criteria restored"], core)
            else:
                self.failures += 1
                if self.failures >= p.continuation.deactivate_after:
                    self._dormant(at, [f"degraded for {self.failures} consecutive evaluations",
                                       *reasons], core)  # fmt: skip
        return self.state

    def _dormant(self, at: float, reasons: list[str], core: dict) -> None:
        self._go(at, "DORMANT", reasons, core)
        self.dormant_since, self.was_dormant, self.cusum, self.failures = at, True, None, 0


def evaluation_times(first: float, end: float, cadence: int) -> np.ndarray:
    """Evaluation dates first, first + cadence, ... strictly before ``end``."""
    n = int(np.floor((end - first - 1e-9) / cadence)) + 1 if end > first else 0
    return first + cadence * np.arange(max(n, 0), dtype=float)


def simulate(ev: EventSet, times: np.ndarray, policy: LifecyclePolicy, floor: float, mode: str,
             current_regime: np.ndarray | None = None) -> dict:  # fmt: skip
    """Walk forward through ``times``; see the module docstring. ``current_regime`` (codes of
    the regime key at each evaluation) only feeds the regime-mode active-time measure."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}")
    key = policy.market_state.lifecycle_key
    n_eval = len(times)
    if mode == "static":
        tracks, states = {}, {}
        part_codes = None
    elif mode == "recent":
        tracks = {"all": Track(ev, policy, floor)}
    else:
        tracks = {lab: Track(ev.subset(ev.regime[key] == code), policy, floor, label=lab)
                  for code, lab in enumerate(REGIME_LABELS[key])}  # fmt: skip
    if mode != "static":
        states = {lab: [] for lab in tracks}
        for t in times:
            for lab, tr in tracks.items():
                states[lab].append(tr.step(float(t)))
    # which events participate
    in_span = (ev.t_sig >= times[0]) if n_eval else np.zeros(len(ev), dtype=bool)
    if mode == "static":
        part = in_span.copy()
    else:
        j = np.searchsorted(times, ev.t_sig, side="right") - 1
        part = np.zeros(len(ev), dtype=bool)
        if mode == "recent":
            part_codes = {"all": np.array([s in PARTICIPATING for s in states["all"]])}
            ok = j >= 0
            part[ok] = part_codes["all"][j[ok]]
        else:
            part_codes = {lab: np.array([s in PARTICIPATING for s in states[lab]])
                          for lab in tracks}  # fmt: skip
            for code, lab in enumerate(REGIME_LABELS[key]):
                m = (j >= 0) & (ev.regime[key] == code)
                part[m] = part_codes[lab][j[m]]
        part &= in_span
    out = summarise(ev, part, in_span, times, tracks, part_codes, policy, mode, current_regime)
    return out


def _spells(times: np.ndarray, active: np.ndarray, end: float) -> list[tuple[float, float]]:
    spells, start = [], None
    for t, a in zip(times, active, strict=True):
        if a and start is None:
            start = t
        elif not a and start is not None:
            spells.append((start, t))
            start = None
    if start is not None:
        spells.append((start, end))
    return spells


def summarise(ev: EventSet, part: np.ndarray, in_span: np.ndarray, times: np.ndarray,
              tracks: dict, part_codes: dict | None, policy: LifecyclePolicy, mode: str,
              current_regime: np.ndarray | None) -> dict:  # fmt: skip
    end = float(times[-1] + policy.evaluation.cadence_days) if len(times) else 0.0
    years = (end - times[0]) / 365.25 if len(times) else 0.0
    x = ev.net
    taken = np.flatnonzero(part)  # resolution order
    rest = np.flatnonzero(in_span & ~part)
    cum = np.cumsum(x[taken]) if len(taken) else np.array([])
    mdd = (
        float((np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum).max())
        if len(cum)
        else 0.0
    )
    rest_sum = float(x[rest].sum()) if len(rest) else 0.0
    out = {
        "mode": mode,
        "events_in_span": int(in_span.sum()),
        "events_participating": len(taken),
        "participation_share": float(len(taken) / in_span.sum()) if in_span.sum() else None,
        "net_sum": float(x[taken].sum()) if len(taken) else 0.0,
        "net_mean_participating": float(x[taken].mean()) if len(taken) else None,
        "hit_rate_participating": float((x[taken] > 0).mean()) if len(taken) else None,
        "max_drawdown": mdd,
        "net_mean_not_participating": float(x[rest].mean()) if len(rest) else None,
        "missed_upside": max(0.0, rest_sum),
        "avoided_losses": max(0.0, -rest_sum),
        "years": years,
    }
    if mode == "static" or part_codes is None:
        out.update({"active_time_share": 1.0 if len(times) else None, "transitions": 0,
                    "activations": 0, "deactivations": 0, "reactivations": 0, "retirements": 0,
                    "spells": 0, "short_bursts": 0, "mean_spell_days": None,
                    "transitions_per_year": 0.0, "final_states": {}, "history": []})  # fmt: skip
        return out
    hist = sorted((t for tr in tracks.values() for t in tr.transitions),
                  key=lambda t: (t["at_days"], t["track"]))  # fmt: skip
    spells = []
    for lab, active in part_codes.items():
        spells += [(lab, s, e) for s, e in _spells(times, active, end)]
    durations = np.array([e - s for _, s, e in spells])
    if mode == "regime" and current_regime is not None:
        labs = REGIME_LABELS[policy.market_state.lifecycle_key]
        cur = [part_codes[labs[c]][i] if c >= 0 else False for i, c in enumerate(current_regime)]
        active_share = float(np.mean(cur)) if len(cur) else None
    else:
        stack = np.vstack(list(part_codes.values()))
        active_share = float(stack.any(axis=0).mean()) if stack.size else None
    acts = [h for h in hist if h["to"] == "PAPER_ACTIVE" and h["from"] == "ACTIVE_CANDIDATE"]
    reacq = [h for h in acts if h.get("reacquisition")]
    # did each reacquired spell earn a positive mean on the outcomes it covered?
    success = []
    for h in reacq:
        lab, s0 = h["track"], h["at_days"]
        s1 = next((e for la, s, e in spells if la == lab and s == s0), end)
        m = part & (ev.t_sig >= s0) & (ev.t_sig < s1)
        if mode == "regime":
            m &= ev.regime[policy.market_state.lifecycle_key] == REGIME_LABELS[
                policy.market_state.lifecycle_key
            ].index(lab)
        if m.any():
            success.append(float(x[m].mean()) > 0)
    out.update({
        "active_time_share": active_share,
        "transitions": len(hist),
        "transitions_per_year": len(hist) / years if years else None,
        "activations": len(acts),
        "deactivations": sum(h["to"] == "DORMANT" for h in hist),
        "reactivations": len(reacq),
        "reacquisition_success_share": float(np.mean(success)) if success else None,
        "retirements": sum(h["to"] == "RETIRED" for h in hist),
        "spells": len(spells),
        "short_bursts": int((durations < policy.evaluation.short_burst_days).sum()),
        "mean_spell_days": float(durations.mean()) if len(durations) else None,
        "median_spell_days": float(np.median(durations)) if len(durations) else None,
        "first_activation": acts[0]["at"] if acts else None,
        "final_states": {lab: tr.state for lab, tr in tracks.items()},
        "history": [{k: v for k, v in h.items() if k != "at_days"} for h in hist],
    })  # fmt: skip
    return out


def edge_state_timeline(ev: EventSet, times: np.ndarray, policy: LifecyclePolicy,
                        floor: float) -> list[str]:  # fmt: skip
    """Edge state at each evaluation (no hysteresis; for turnover reporting)."""
    return [classify(core_evidence(ev, float(t), policy), floor, policy)["state"] for t in times]
