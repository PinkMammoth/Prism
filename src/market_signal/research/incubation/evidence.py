"""Incubation evidence (``incubation_evidence_v1``): the small admission rule set, era
evidence and structured candidate explanations.

All estimates at time T read only outcomes resolved by T (``EventSet.resolved``), exactly as
Phase 20. The unit is the same: after-cost, after-funding net return per independent event
at the frozen horizon, side-signed (a short's profit is positive). Uncertainty is Phase 20's
``time_block_cluster_robust_v1`` standard error; ``t = mean / se`` is a standardised effect,
never a p-value.

Era evidence (lifetime, last 730/365/180/90 days, the policy window, the prospective
episode) is always reported side by side and never averaged into one score.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from market_signal.research.incubation.policy import IncubationPolicy
from market_signal.research.lifecycle.estimators import EventSet, iso, weighted_mean_se

ERAS = (("last_730d", 730), ("last_365d", 365), ("last_180d", 180), ("last_90d", 90))
ERA_MIN_EVENTS = 8  # an era with fewer independent outcomes is shown but marked insufficient
ERA_MIN_ASSETS = 2


def _f(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if np.isfinite(x) else None


@dataclass(frozen=True)
class Est:
    """Statistics of a slice of outcomes."""

    n: int
    assets: int
    mean: float | None
    se: float | None
    t: float | None
    excess_mean: float | None
    loo_mean: float | None  # mean without the asset contributing the largest net sum
    best_asset: str | None
    best_asset_share: float | None  # best asset's net sum / total positive net sum
    hit_rate: float | None
    net_sum: float

    def brief(self) -> dict:
        return {"n": self.n, "assets": self.assets, "mean": self.mean, "se": self.se,
                "t": self.t, "excess_mean": self.excess_mean, "loo_mean": self.loo_mean,
                "best_asset": self.best_asset, "hit_rate": self.hit_rate,
                "net_sum": self.net_sum}  # fmt: skip


def estimate(net: np.ndarray, asset: np.ndarray, cluster: np.ndarray, excess: np.ndarray,
             names: tuple[str, ...]) -> Est:  # fmt: skip
    n = len(net)
    if n == 0:
        return Est(0, 0, None, None, None, None, None, None, None, None, 0.0)
    mean, se = weighted_mean_se(net, np.ones(n), cluster)
    t = mean / se if np.isfinite(se) and se > 0 else None
    exm = np.isfinite(excess)
    sums = np.bincount(asset, weights=net, minlength=len(names))
    cnt = np.bincount(asset, minlength=len(names))
    present = cnt > 0
    best = int(np.argmax(np.where(present, sums, -np.inf)))
    rest = n - int(cnt[best])
    pos = float(sums[present & (sums > 0)].sum())
    return Est(
        n=n, assets=int(present.sum()), mean=_f(mean), se=_f(se), t=_f(t),
        excess_mean=_f(excess[exm].mean()) if exm.any() else None,
        loo_mean=_f((net.sum() - sums[best]) / rest) if rest > 0 else None,
        best_asset=names[best], best_asset_share=_f(sums[best] / pos) if pos > 0 else None,
        hit_rate=_f((net > 0).mean()), net_sum=float(net.sum()),
    )  # fmt: skip


def slice_est(ev: EventSet, lo: int, hi: int) -> Est:
    s = slice(lo, hi)
    return estimate(ev.net[s], ev.asset[s], ev.cluster[s], ev.excess[s], ev.assets)


def mask_est(ev: EventSet, m: np.ndarray) -> Est:
    return estimate(ev.net[m], ev.asset[m], ev.cluster[m], ev.excess[m], ev.assets)


def window_bounds(ev: EventSet, as_of: float, days: int) -> tuple[int, int]:
    """[lo, k): outcomes resolved in (as_of - days, as_of]."""
    k = ev.resolved(as_of)
    lo = int(np.searchsorted(ev.t_res, as_of - days, side="right"))
    return min(lo, k), k


def lifetime_hostile(life: Est, policy: IncubationPolicy) -> bool:
    """An adequate lifetime (Phase 7 standard: 30 events, 3 assets) that is credibly negative."""
    return (life.n >= 30 and life.assets >= 3 and life.t is not None
            and life.t <= policy.admission.hostile_lifetime_t)  # fmt: skip


def required_t(life: Est, policy: IncubationPolicy) -> float:
    a = policy.admission
    return a.min_t + (a.hostile_lifetime_extra_t if lifetime_hostile(life, policy) else 0.0)


def adequate(w: Est, policy: IncubationPolicy) -> list[str]:
    g = policy.window
    out = []
    if w.n < g.min_events:
        out.append(f"{w.n} independent outcomes < {g.min_events}")
    if w.assets < g.min_assets:
        out.append(f"{w.assets} assets < {g.min_assets}")
    return out


def admission(w: Est, life: Est, policy: IncubationPolicy, floor: float) -> tuple[bool, dict]:
    """The whole exploratory-paper rule set, as named checks (all must hold)."""
    a = policy.admission
    need_t = required_t(life, policy)
    ok_n = not adequate(w, policy)
    checks = {
        "sample_adequate": ok_n,
        "mean_at_least_floor": ok_n and w.mean is not None and w.mean >= floor,
        "t_at_least_required": ok_n and w.t is not None and w.t >= need_t,
        "not_one_asset": ok_n and w.loo_mean is not None and w.loo_mean >= a.loo_min_floors * floor,
        "not_worse_than_random_timing": w.excess_mean is None or w.excess_mean >= -floor,
    }  # fmt: skip
    checks = {k: bool(v) for k, v in checks.items()}
    return all(checks.values()), checks


def interesting(w: Est, policy: IncubationPolicy) -> bool:
    return w.n >= policy.watch.min_events and w.mean is not None and w.mean > 0


def deactivation(w: Est, episode: Est, policy: IncubationPolicy, regime_changed: bool,
                 regime_window: Est | None) -> list[str]:  # fmt: skip
    """Reasons an admitted candidate stops (empty = continue). Drawdown is never a reason."""
    d = policy.deactivation
    out = []
    if w.n < d.lapse_min_events:
        out.append(f"signal evidence lapsed: {w.n} outcomes in the window < {d.lapse_min_events}")
    elif w.t is not None and w.t <= d.exit_max_t:
        out.append(f"recent evidence reversed: window t {w.t:.2f} <= {d.exit_max_t}")
    if (episode.n >= d.episode_min_outcomes and episode.t is not None
            and episode.t <= d.episode_max_t):  # fmt: skip
        out.append(f"episode outcomes strongly adverse: t {episode.t:.2f} on {episode.n}")
    if (regime_changed and regime_window is not None
            and regime_window.n >= d.regime_min_events and (regime_window.mean or 0) < 0):  # fmt: skip
        out.append("regime changed since admission and outcomes in the current regime are negative")
    return out


def graduation(fwd: Est, research_mean: float | None, unavailable: int, policy,
               floor: float) -> tuple[bool, dict]:  # fmt: skip
    """CONFIRMED_PAPER rule on forward (episode) outcomes only."""
    g = policy.graduation
    total = fwd.n + unavailable
    checks = {
        "min_forward_outcomes": fwd.n >= g.min_outcomes,
        "min_assets": fwd.assets >= g.min_assets,
        "forward_mean_at_least_floor": fwd.mean is not None and fwd.mean >= floor,
        "forward_t": fwd.t is not None and fwd.t >= g.min_t,
        "not_one_asset": fwd.loo_mean is not None and fwd.loo_mean > 0,
        "agrees_with_research": research_mean is not None and fwd.mean is not None
        and np.sign(fwd.mean) == np.sign(research_mean),
        "execution_acceptable": total == 0 or unavailable / total <= g.max_unavailable_share,
    }  # fmt: skip
    checks = {k: bool(v) for k, v in checks.items()}
    return all(checks.values()), checks


# --------------------------------------------------------------------------- eras


def eras(ev: EventSet, as_of: float, policy_windows: dict[str, int] | None = None) -> dict:
    """Lifetime, last 730/365/180/90 days and each policy window, side by side."""
    k = ev.resolved(as_of)

    def row(lo: int) -> dict:
        e = slice_est(ev, lo, k)
        return {**e.brief(), "sufficient": e.n >= ERA_MIN_EVENTS and e.assets >= ERA_MIN_ASSETS,
                "first_resolved": iso(ev.t_res[lo]) if k > lo else None}  # fmt: skip

    out = {"lifetime": row(0)}
    for label, days in (
        *ERAS,
        *((f"window_{p.lower()}", d) for p, d in (policy_windows or {}).items()),
    ):
        lo, _ = window_bounds(ev, as_of, days)
        out[label] = {**row(lo), "days": days}
    return out


def explain(level: str, checks: dict | None, w: Est | None, life: Est | None, *, side: str,
            floor: float, regime: dict | None, episode: dict | None, prospective: dict | None,
            policy_profile: str) -> dict:  # fmt: skip
    """Plain structured fields; no narrative. Caveats are fixed strings chosen by rule."""
    caveats = [
        "exploratory paper admission grants no live permission",
        "rolling windows overlap; t is a standardised effect, not a p-value",
    ]
    if life is not None and life.t is not None and life.t < 0:
        caveats.append("lifetime evidence is negative; admission rests on recent evidence")
    if w is not None and w.n < 20:
        caveats.append(f"small recent sample ({w.n} independent outcomes)")
    if w is not None and w.best_asset_share is not None and w.best_asset_share > 0.6:
        caveats.append(f"recent profit concentrated in {w.best_asset}")
    if not (prospective or {}).get("outcomes"):
        caveats.append("no prospective shadow outcomes yet")
    return {
        "policy": policy_profile,
        "level": level,
        "why": [k for k, v in (checks or {}).items() if v] if level in ("EXPLORATORY_PAPER",
                                                                        "CONFIRMED_PAPER") else [],
        "failed_checks": [k for k, v in (checks or {}).items() if not v],
        "direction": side,
        "recent_expectancy": None if w is None else w.mean,
        "recent_t": None if w is None else w.t,
        "sample_size": None if w is None else w.n,
        "assets": None if w is None else w.assets,
        "economic_floor": floor,
        "lifetime_context": None if life is None else {"n": life.n, "mean": life.mean, "t": life.t},
        "current_regime": regime,
        "episode": episode,
        "prospective_observations": prospective,
        "caveats": caveats,
    }  # fmt: skip
