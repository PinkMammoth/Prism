""" "What did the Lab cook?" — machine-readable outputs for agents, and the frozen shortlist.

Everything here is a pure function of a stored, digested discovery result: no window,
threshold or date can be passed in, so an agent cannot alter research thresholds. The
recommended incubation policy is a fixed mapping of the verdict, never a judgement call:

| Verdict | Recommended Phase 21-style policy |
|---|---|
| STRONG_INCUBATION_CANDIDATE | BALANCED |
| INCUBATION_CANDIDATE | AGGRESSIVE |
| INTERESTING | none (watch; not eligible) |

Eligibility is not activation: ``eligibility`` produces the exact identities a FUTURE,
separately frozen intraday incubation universe could use. Nothing here touches the frozen
Phase 21 baseline or any runtime.
"""

from __future__ import annotations

from market_signal.research.discovery import catalogue as cat
from market_signal.research.discovery.spec import CANDIDATE_VERDICTS
from market_signal.research.lab.common import content_id

COOK_VERSION = "discovery_cook_v1"
ELIGIBILITY_VERSION = "intraday_incubation_eligibility_v1"
POLICY_FOR = {"STRONG_INCUBATION_CANDIDATE": "BALANCED", "INCUBATION_CANDIDATE": "AGGRESSIVE",
              "INTERESTING": None}  # fmt: skip


def caveats(s: dict, replication: dict | None) -> list[str]:
    """Fixed strings chosen by rule; no narrative."""
    out = ["exploratory: discovery evidence only, no live permission",
           "assumed-latency backfilled history"]  # fmt: skip
    route = s.get("route")
    w = s.get(route or "contemporary") or {}
    if route in ("recent_180", "recent_90"):
        out.append(f"admitted on the {route} window only (an emerging/temporary edge)")
    if (w.get("n") or 0) < 60:
        out.append(f"small sample ({w.get('n')} independent events)")
    if (w.get("top5_share") or 0) > 0.35:
        out.append("profit concentrated in a few events")
    pa = w.get("per_asset") or {}
    if pa and w.get("best_asset") and (w.get("positive_asset_share") or 0) < 0.67:
        out.append(f"not broad across assets (best: {w['best_asset']})")
    lc = s.get("long_context") or {}
    if lc.get("net_mean") is not None and lc["net_mean"] <= 0:
        out.append("long-context (pre-2024-11, 1h grid) after-cost effect was not positive")
    g, d = w.get("gross_mean"), w.get("cost_drag")
    if g and d and g > 0 and d / g > 0.5:
        out.append(f"costs consume {d / g:.0%} of the gross edge")
    if (s.get("frequency") or {}).get("independent_per_day", 0) < 1:
        out.append("fewer than one independent signal per day")
    if replication is not None:
        r = replication.get(s["key"]) or {}
        if (r.get("n") or 0) == 0:
            out.append("no events on the execution venue (Hyperliquid) window")
        elif (r.get("net_mean") or 0) <= 0:
            out.append("not positive on the short Hyperliquid replication window")
    st = s.get("stress") or {}
    if (st.get("stress") or {}).get("n", 0) and (st["stress"].get("net_mean") or 0) < 0:
        out.append("negative during stress periods")
    if (s.get("edge_state") or {}).get("state") in ("DECAYING", "DEAD"):
        out.append(f"Phase 20 edge state {s['edge_state']['state']}")
    return out


def cook(payload: dict, *, include_interesting: bool = True) -> dict:
    reps = {c["representative"]: c for c in payload.get("clusters", [])}
    rep_venue = next(iter((payload.get("replication") or {}).values()), None)
    repl = rep_venue["strategies"] if rep_venue else None
    want = CANDIDATE_VERDICTS + (("INTERESTING",) if include_interesting else ())
    items = []
    for s in payload["strategies"]:
        if s["verdict"] not in want:
            continue
        route = s.get("route") or "contemporary"
        w = s.get(route) or {}
        items.append({
            "strategy_id": s["strategy_id"], "key": s["key"], "catalogue": cat.CATALOGUE_VERSION,
            "hypothesis": s["hypothesis"], "family": s["family"], "bh_family": s["bh_family"],
            "side": s["side"], "timeframe": s["timeframe"],
            "context_timeframe": s["context_timeframe"],
            "primary_horizon_minutes": s["primary_horizon_minutes"],
            "frequency": {k: (s.get("frequency") or {}).get(k) for k in
                          ("raw_per_day", "independent_per_day", "per_asset_per_day",
                           "median_hours_between", "zero_signal_day_share")},
            "evidence_window": route,
            "recent_effect": {k: (s.get(x) or {}).get("net_mean") for k, x in
                              (("recent_180", "recent_180"), ("recent_90", "recent_90"))},
            "contemporary": {k: (s.get("contemporary") or {}).get(k) for k in
                             ("n", "net_mean", "t", "q_value")} | {"q_value": s.get("q_value")},
            "lifetime_context": s.get("long_context"),
            "costs": {"gross_mean": w.get("gross_mean"), "cost_drag": w.get("cost_drag"),
                      "net_mean": w.get("net_mean")},
            "evidence_state": s["verdict"],
            "edge_state": (s.get("edge_state") or {}).get("state"),
            "pattern": (s.get("pattern") or {}).get("label"),
            "caveats": caveats(s, repl),
            "overlap_cluster": s.get("cluster"),
            "cluster_representative": bool(s.get("representative")),
            "in_shortlist": s["key"] in payload.get("shortlist", []),
            "recommended_incubation_policy": POLICY_FOR.get(s["verdict"]),
            "complexity": s["complexity"]["score"],
        })  # fmt: skip
    order = {v: i for i, v in enumerate(("STRONG_INCUBATION_CANDIDATE", "INCUBATION_CANDIDATE",
                                         "INTERESTING"))}  # fmt: skip
    items.sort(key=lambda x: (order[x["evidence_state"]], not x["in_shortlist"], x["key"]))
    return {"version": COOK_VERSION, "study_id": payload.get("study_id"),
            "question": "What did the Lab cook?", "statement": payload.get("statement"),
            "verdict_counts": payload.get("verdict_counts"), "shortlist": payload.get("shortlist"),
            "ensemble_shortlist": (payload.get("ensemble") or {}).get("shortlist"),
            "clusters_with_candidates": len([c for c in reps if c in {i["key"] for i in items}]),
            "items": items}  # fmt: skip


def eligibility(payload: dict, run_digest: str | None) -> dict:
    """The frozen shortlist eligible for a FUTURE intraday incubation universe."""
    by = {s["key"]: s for s in payload["strategies"]}
    members = []
    for k in payload.get("shortlist", []):
        s = by[k]
        members.append({
            "strategy_id": s["strategy_id"], "key": k, "catalogue": cat.CATALOGUE_VERSION,
            "feature_version": "intraday_features_v1", "family": s["family"],
            "bh_family": s["bh_family"], "rule": s["rule"], "params": s["params"],
            "side": s["side"], "timeframe": s["timeframe"],
            "context_timeframe": s["context_timeframe"],
            "entry": cat.ENTRY_RULE, "exit": cat.EXIT_RULE,
            "primary_horizon_minutes": s["primary_horizon_minutes"],
            "secondary_horizons_minutes": [m for m in s["horizons_minutes"]
                                           if m != s["primary_horizon_minutes"]],
            "expected_independent_per_day": (s.get("frequency") or {}).get("independent_per_day"),
            "verdict": s["verdict"], "recommended_policy": POLICY_FOR[s["verdict"]],
        })  # fmt: skip
    body = {"version": ELIGIBILITY_VERSION, "study_id": payload.get("study_id"),
            "result_digest": run_digest, "members": members,
            "ensemble": (payload.get("ensemble") or {}).get("shortlist"),
            "activation": "NOT ACTIVATED: requires a new intraday incubation runner and a new, "
                          "separate prospective freeze; the Phase 21 baseline is unchanged"}  # fmt: skip
    return {**body, "eligibility_id": content_id("ielig_", body)}


__all__ = ["COOK_VERSION", "ELIGIBILITY_VERSION", "POLICY_FOR", "caveats", "cook", "eligibility"]
