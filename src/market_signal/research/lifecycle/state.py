"""Edge state (``edge_state_v1``), recent-vs-lifetime divergence, decay and diagnostics.

Edge states describe the apparent *temporal state* of an edge. They are not evidence tiers,
co-pilot eligibility, paper eligibility or allocations, and no consumer reads them.

Each estimate gets a sign class (policy ``classes``): ``NA`` when its gates fail, ``POS``
when mean >= economic floor and t >= 1, ``NEG`` when t <= -1, else ``FLAT``. With
L = lifetime, R = recent (180 d, else the latest 50 events within 730 d) and S = short
(90 d, else the latest 20 within 365 d):

| State | Rule (first match) |
|---|---|
| INSUFFICIENT | L = NA and R = NA |
| DEAD | R = NEG; or R in {FLAT, NA} and L != POS (no credible edge now) |
| DECAYING | R = POS and S = NEG; or R = FLAT and L = POS |
| STABLE | R = POS, L = POS and they agree (|R - L| <= max(floor, 0.5 |L|)) |
| EMERGING | R = POS and L != POS |
| ACTIVE | R = POS otherwise (e.g. recent much stronger than a positive lifetime) |
| DORMANT | R = NA and L = POS (a historical edge with no current sample) |

The divergence pattern is reported separately and in words; it never changes the state.
"""

from __future__ import annotations

from math import erf, sqrt

import numpy as np

from market_signal.research.lifecycle.estimators import (
    EventSet,
    lifetime,
    pick,
    stats,
    weighted,
    window,
)
from market_signal.research.lifecycle.policy import LifecyclePolicy

STATES = ("EMERGING", "ACTIVE", "STABLE", "DECAYING", "DORMANT", "DEAD", "INSUFFICIENT")
DIVERGENCE = ("emerging", "strengthening", "stable", "decaying", "historical_only", "reversing",
              "consistently_absent", "insufficient", "indeterminate")  # fmt: skip


def sign_class(w: dict | None, floor: float, policy: LifecyclePolicy) -> str:
    if not w or not w.get("adequate") or w.get("t") is None or w.get("mean") is None:
        return "NA"
    c = policy.classes
    if w["mean"] >= floor and w["t"] >= c.pos_min_t:
        return "POS"
    if w["t"] <= c.neg_max_t:
        return "NEG"
    return "FLAT"


def core_evidence(ev: EventSet, as_of: float, policy: LifecyclePolicy) -> dict:
    """The estimates the lifecycle machine needs at one evaluation (kept small for speed)."""
    r = policy.recent
    w = {lab: window(ev, as_of, policy.window(lab), policy)
         for lab in (r.recent, r.recent_fallback, r.short, r.short_fallback, r.contemporary)}  # fmt: skip
    return {
        "lifetime": lifetime(ev, as_of, policy),
        "recent": pick(w, r.recent, r.recent_fallback),
        "short": pick(w, r.short, r.short_fallback),
        "contemporary": w[r.contemporary],
        "weighted": weighted(ev, as_of, r.weighted_half_life_days, policy),
    }


def agree(recent: float, life: float, floor: float, policy: LifecyclePolicy) -> bool:
    return abs(recent - life) <= max(floor, policy.classes.agreement_share * abs(life))


def classify(core: dict, floor: float, policy: LifecyclePolicy) -> dict:
    L = sign_class(core["lifetime"], floor, policy)
    R = sign_class(core["recent"], floor, policy)
    S = sign_class(core["short"], floor, policy)
    C = sign_class(core["contemporary"], floor, policy)
    classes = {"lifetime": L, "recent": R, "short": S, "contemporary": C}
    if L == "NA" and R == "NA":
        state, why = "INSUFFICIENT", "neither lifetime nor recent evidence passes its gates"
    elif R == "NEG":
        state, why = "DEAD", "recent effect is credibly negative"
    elif R in ("FLAT", "NA") and L != "POS":
        state, why = "DEAD", "no credible edge now and none established historically"
    elif (R == "POS" and S == "NEG") or (R == "FLAT" and L == "POS"):
        state, why = "DECAYING", ("short window credibly negative inside a positive recent window"
                                  if R == "POS" else "lifetime edge, recent evidence weak")  # fmt: skip
    elif R == "POS" and L == "POS" and agree(core["recent"]["mean"], core["lifetime"]["mean"],
                                              floor, policy):  # fmt: skip
        state, why = "STABLE", "recent and lifetime estimates are positive and agree"
    elif R == "POS" and L != "POS":
        state, why = "EMERGING", "recent edge without lifetime support"
    elif R == "POS":
        state, why = "ACTIVE", "recent edge; lifetime positive but materially different"
    else:  # R == "NA" and L == "POS"
        state, why = "DORMANT", "lifetime edge, too few recent outcomes to assess it now"
    return {"state": state, "reason": why, "classes": classes,
            "recent_window": core["recent"].get("selected"),
            "short_window": core["short"].get("selected")}  # fmt: skip


def divergence(core: dict, floor: float, policy: LifecyclePolicy) -> dict:
    """First-class recent-versus-lifetime comparison, in words and numbers."""
    L = sign_class(core["lifetime"], floor, policy)
    R = sign_class(core["recent"], floor, policy)
    S = sign_class(core["short"], floor, policy)
    C = sign_class(core["contemporary"], floor, policy)
    lm, rm = core["lifetime"].get("mean"), core["recent"].get("mean")
    if L == "NA" and R == "NA":
        pattern = "insufficient"
    elif L != "POS" and R == "POS":
        pattern = "emerging"
    elif L == "POS" and R == "NEG":
        pattern = "reversing"
    elif L == "POS" and R == "POS":
        pattern = ("stable" if agree(rm, lm, floor, policy)
                   else "strengthening" if rm > lm else "decaying")  # fmt: skip
        if pattern == "stable" and S in ("FLAT", "NEG"):
            pattern = "decaying"
    elif L == "POS" and C == "POS":
        pattern = "decaying"
    elif L == "POS":
        pattern = "historical_only"
    elif L in ("FLAT", "NEG") and R in ("FLAT", "NEG", "NA"):
        pattern = "consistently_absent"
    else:
        pattern = "indeterminate"
    diff = None
    if lm is not None and rm is not None:
        se = np.hypot(core["lifetime"].get("se") or np.nan, core["recent"].get("se") or np.nan)
        diff = {"recent_minus_lifetime": rm - lm,
                "standardised": float((rm - lm) / se) if np.isfinite(se) and se > 0 else None,
                "note": "windows overlap the lifetime sample; descriptive only"}  # fmt: skip
    return {"pattern": pattern, "classes": {"lifetime": L, "recent": R, "short": S,
                                            "contemporary": C}, "difference": diff}  # fmt: skip


# --------------------------------------------------------------------------- decay


def curve(ev: EventSet, as_of: float, policy: LifecyclePolicy, start: float) -> list[dict]:
    """Chronological evidence curve: the policy's curve window evaluated every
    ``curve_step_days`` from ``start`` to ``as_of`` (each point causal at its own date)."""
    step = policy.evaluation.curve_step_days
    w = policy.window(policy.evaluation.curve_window)
    pts = []
    t = as_of
    while t >= start:
        pts.append(t)
        t -= step
    out = []
    for t in sorted(pts):
        s = window(ev, t, w, policy)
        k = ev.resolved(t)
        cum = ev.net[:k]
        dd = 0.0
        if len(cum):
            c = np.cumsum(cum)
            dd = float((np.maximum.accumulate(np.concatenate([[0.0], c]))[1:] - c)[-1])
        out.append({"as_of": t, "adequate": s["adequate"], "n": s["n"], "mean": s.get("mean"),
                    "se": s.get("se"), "t": s.get("t"), "excess_mean": s.get("excess_mean"),
                    "hit_rate": s.get("hit_rate"), "mae_mean": s.get("mae_mean"),
                    "mfe_mean": s.get("mfe_mean"), "cost_mean": s.get("cost_mean"),
                    "assets": s.get("assets", 0), "drawdown_from_peak": dd,
                    "cumulative_net": float(cum.sum()) if len(cum) else 0.0})  # fmt: skip
    return out


def decay(ev: EventSet, as_of: float, core: dict, pts: list[dict], floor: float,
          policy: LifecyclePolicy) -> dict:  # fmt: skip
    """Descriptive decay metrics. No literal physical half-life is claimed."""
    k = ev.resolved(as_of)
    cut = int(np.searchsorted(ev.t_res[:k], as_of - 365, side="right"))
    older, newer = stats(ev, 0, cut), stats(ev, cut, k)
    d_rec = None
    if older.get("mean") is not None and newer.get("mean") is not None:
        se = np.hypot(older.get("se") or np.nan, newer.get("se") or np.nan)
        d_rec = {"last_365d_mean": newer["mean"], "older_mean": older["mean"],
                 "last_365d_n": newer["n"], "older_n": older["n"],
                 "difference": newer["mean"] - older["mean"],
                 "standardised": float((newer["mean"] - older["mean"]) / se)
                 if np.isfinite(se) and se > 0 else None}  # fmt: skip
    ok = [p for p in pts if p["adequate"] and p["mean"] is not None]
    peak = max(ok, key=lambda p: p["mean"]) if ok else None
    current = ok[-1] if ok and ok[-1]["as_of"] == pts[-1]["as_of"] else None
    last_year = [p for p in ok if p["as_of"] > as_of - 365]
    slope = None
    if len(last_year) >= 3:
        x = np.array([p["as_of"] for p in last_year]) / 365.0
        y = np.array([p["mean"] for p in last_year])
        slope = float(np.polyfit(x - x.mean(), y, 1)[0])

    def trailing(pred) -> int:
        n = 0
        for p in reversed(pts):
            if not (p["adequate"] and p["mean"] is not None and pred(p["mean"])):
                break
            n += 1
        return n

    half = None
    if peak is not None and peak["mean"] > floor:
        after = [p for p in ok if p["as_of"] > peak["as_of"] and p["mean"] <= peak["mean"] / 2]
        if after:
            half = after[0]["as_of"] - peak["as_of"]
    wl = None
    w, life = core["weighted"], core["lifetime"]
    if w.get("mean") is not None and life.get("mean") is not None:
        se = np.hypot(w.get("se") or np.nan, life.get("se") or np.nan)
        wl = {"weighted_minus_lifetime": w["mean"] - life["mean"],
              "standardised": float((w["mean"] - life["mean"]) / se)
              if np.isfinite(se) and se > 0 else None}  # fmt: skip
    return {
        "recent_vs_older": d_rec,
        "peak_rolling_mean": peak["mean"] if peak else None,
        "days_since_peak": (as_of - peak["as_of"]) if peak else None,
        "decay_from_peak": ((peak["mean"] - current["mean"]) / abs(peak["mean"]))
        if peak and current and peak["mean"] != 0 else None,
        "rolling_slope_per_year": slope,
        "consecutive_windows_below_zero": trailing(lambda m: m < 0),
        "consecutive_windows_below_floor": trailing(lambda m: m < floor),
        "weighted_vs_lifetime": wl,
        "descriptive_peak_to_half_days": half,
        "note": "descriptive (lab_edge_decay_v1); rolling windows overlap, so consecutive points "
                "are not independent and no physical half-life is implied",
    }  # fmt: skip


def _phi(z: float) -> float:
    return 0.5 * (1 + erf(z / sqrt(2)))


def diagnostics(core: dict, floor: float, regime: dict | None, forward: dict | None,
                pts_decay: dict) -> dict:  # fmt: skip
    """Continuous diagnostics; lifecycle states sit on top of these."""
    r = core["recent"]
    t, se, mean = r.get("t"), r.get("se"), r.get("mean")
    short, contemporary = core["short"], core["contemporary"]
    dscore = None
    if short.get("mean") is not None and contemporary.get("mean") is not None:
        s2 = np.hypot(short.get("se") or np.nan, contemporary.get("se") or np.nan)
        if np.isfinite(s2) and s2 > 0:
            dscore = float((contemporary["mean"] - short["mean"]) / s2)
    rs = (regime or {}).get("current_regime_historical") or {}
    return {
        "recent_effect": mean,
        "recent_uncertainty": se,
        "evidence_strength": t,
        "normal_approx_confidence_positive": _phi(t) if t is not None else None,
        "economic_margin": (mean - floor) / se if mean is not None and se else None,
        "decay_score": dscore,
        "current_regime_support": rs.get("t"),
        "forward_support": None if not forward else {
            "maturity": forward.get("maturity"), "net_mean": forward.get("net_mean"),
            "independent_resolved": forward.get("independent_resolved")},
        "rolling_slope_per_year": pts_decay.get("rolling_slope_per_year"),
        "note": "normal_approx_confidence_positive = Phi(recent t): a standardised-effect "
                "summary, NOT the probability that the edge is real",
    }  # fmt: skip
