"""Immutable, versioned edge profiles: "does this strategy appear to have a credible edge now?"

A profile is evaluated at one time (``as_of``) from outcomes resolved by then, under one
frozen lifecycle policy, and holds every evidence dimension side by side — lifetime,
rolling windows, recency-weighted, regime-local, stress-split, prospective (Phase 8, read
only) — plus the edge state, the recent-vs-lifetime divergence, decay metrics, change
detection, continuous diagnostics, the simulated lifecycle and the chronological curve.
Recent evidence never overwrites lifetime evidence: both are always present.

``profile_id`` is the content hash of the whole profile, so the same data, cutoff and policy
give the same profile (tested), and a later evaluation is a new profile. Profiles are stored
append-only in ``lab_edge_profiles`` (migration 20); nothing updates or deletes them.

A profile has no alert, trade, sizing, leverage or approval field (the Phase 7 key scan
applies), and ``PAPER_ACTIVE`` inside it is a *simulated* lifecycle state. No consumer
policy reads profiles.
"""

from __future__ import annotations

import json
from typing import Literal

import numpy as np
import pandas as pd

from market_signal.models.domain import utcnow
from market_signal.research.lab.common import (
    LabModel,
    Name,
    PositiveInt,
    Text,
    UTCDateTime,
    canonical_json,
    content_id,
)
from market_signal.research.lab.ledger import Ledger, LedgerError
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lifecycle import changepoint as cp
from market_signal.research.lifecycle import estimators as es
from market_signal.research.lifecycle import state as st
from market_signal.research.lifecycle.policy import METHODOLOGY_VERSION, LifecyclePolicy

PROFILE_SCHEMA = "1"
STATEMENT = (
    "Phase 20 edge profile (EXPLORATORY). It describes whether a strategy appears to have a "
    "credible edge at its evaluation time under a frozen lifecycle policy. Edge and lifecycle "
    "states are research descriptions, not evidence tiers, co-pilot or paper eligibility, or "
    "allocations; PAPER_ACTIVE is a simulated state. Temporary profitability is not assumed "
    "to be durable: active status must continually be re-earned."
)
LIMITATIONS = (
    "Historical outcomes are reconstructed from backfilled daily bars and funding; no "
    "historical evaluation was observed live.",
    "Rolling windows overlap; consecutive estimates are not independent evidence.",
    "Uncertainty is a large-sample cluster-robust approximation (time blocks); no p-values.",
    "The regime vocabulary is deliberately coarse and BTC-referenced.",
    "Stress labels are descriptive; no outcome is excluded from any decision.",
    "Forward (Phase 8) evidence is reported, never weighted into the edge state (v1).",
)
EDGE_STATES = st.STATES


class EdgeProfile(LabModel):
    schema_version: Literal["1"] = PROFILE_SCHEMA
    methodology_version: Literal["edge_lifecycle_v1"] = METHODOLOGY_VERSION
    evidence_class: Literal["EXPLORATORY"] = "EXPLORATORY"
    statement: Literal[STATEMENT] = STATEMENT  # type: ignore[valid-type]
    consumers: Literal["none"] = "none"
    policy_id: Text
    strategy_id: Text
    strategy_name: Name
    family: Name | None
    side: Literal["long", "short"]
    venue: Literal["binance", "hyperliquid", "synthetic"]
    horizon_bars: PositiveInt
    as_of: UTCDateTime
    data_cutoff: UTCDateTime
    source: dict
    outcomes: dict
    edge_state: dict
    divergence: dict
    lifetime: dict
    windows: dict
    weighted: dict
    research_modes: dict
    regime: dict
    stress: dict
    tails: dict
    eras: list
    decay: dict
    change_detection: dict
    diagnostics: dict
    lifecycle: dict
    curve: list
    forward: dict
    limitations: tuple[Text, ...] = LIMITATIONS

    @property
    def profile_id(self) -> str:
        return content_id("edgeprofile_", json.loads(self.model_dump_json()))


# --------------------------------------------------------------------------- evidence blocks


def regime_evidence(ev: es.EventSet, as_of: float, current: dict[str, str],
                    policy: LifecyclePolicy) -> dict:  # fmt: skip
    """Lifetime / recent evidence in the current coarse regime, and per-label dependence."""
    ms = policy.market_state
    dims = ms.similarity
    known = all(current.get(d) in es.REGIME_LABELS[d] for d in dims)
    out: dict = {"current": current, "similarity": list(dims), "match": "exact_coarse_bins"}
    if known:
        mask = np.ones(len(ev), dtype=bool)
        for d in dims:
            mask &= ev.regime[d] == es.REGIME_LABELS[d].index(current[d])
        sub = ev.subset(mask)
        hist = es.lifetime(sub, as_of, policy)
        w = {lab: es.window(sub, as_of, policy.window(lab), policy)
             for lab in (ms.regime_recent, ms.regime_recent_fallback)}  # fmt: skip
        recent = es.pick(w, ms.regime_recent, ms.regime_recent_fallback)
        out["current_regime_historical"] = _short(hist)
        out["recent_current_regime"] = _short(recent)
    else:
        out["current_regime_historical"] = out["recent_current_regime"] = None
        out["note"] = "current regime unknown on a similarity dimension"
    dep = {}
    for d in es.REGIME_DIMS:
        rows = {}
        for code, lab in enumerate(es.REGIME_LABELS[d]):
            s = es.lifetime(ev.subset(ev.regime[d] == code), as_of, policy)
            rows[lab] = _short(s)
        dep[d] = rows
    out["dependence"] = dep
    return out


def _short(s: dict | None) -> dict | None:
    if s is None:
        return None
    keep = ("label", "selected", "kind", "adequate", "insufficient_reasons", "n", "assets", "ess",
            "mean", "se", "t", "hit_rate", "excess_mean", "positive_asset_share")  # fmt: skip
    return {k: s[k] for k in keep if k in s}


def stress_split(ev: es.EventSet, as_of: float, policy: LifecyclePolicy) -> dict:
    """All / normal / stress evidence, always together (stress exclusion never stands alone)."""
    k = ev.resolved(as_of)
    s_mask = ev.stress
    out = {}
    for name, mask in (("all", np.ones(len(ev), dtype=bool)), ("normal", ~s_mask),
                       ("stress", s_mask)):  # fmt: skip
        sub = ev.subset(mask)
        kk = sub.resolved(as_of)
        out[name] = {"lifetime": _short(es.lifetime(sub, as_of, policy)),
                     "contemporary": _short(es.window(sub, as_of, policy.window("d730"), policy)),
                     "net_sum": float(sub.net[:kk].sum()),
                     "max_drawdown": es.drawdown(sub.net[:kk], sub.t_res[:kk])["max_drawdown"]}  # fmt: skip
    n_stress = int(s_mask[:k].sum())
    out["stress_event_share"] = float(n_stress / k) if k else None
    total = out["all"]["net_sum"]
    out["stress_net_contribution"] = out["stress"]["net_sum"]
    out["note"] = (
        "stress = holding window touched a market_stress_v1 episode; the full "
        "series is the evidence, normal/stress are descriptive splits"
    )
    out["total_net_sum"] = total
    return out


def research_modes(windows: dict, life: dict, policy: LifecyclePolicy) -> dict:
    """Explicitly named research modes (no silent truncation)."""
    r = policy.recent
    return {"lifetime": _short(life),
            "contemporary": {**(_short(windows[r.contemporary]) or {}), "window": r.contemporary},
            "recent": {**(_short(es.pick(windows, r.recent, r.recent_fallback)) or {}),
                       "window": r.recent, "fallback": r.recent_fallback},
            "rolling": "see curve (window " + policy.evaluation.curve_window + ")"}  # fmt: skip


def lifecycle_block(sims: dict[str, dict], as_of: float) -> dict:
    """Simulated lifecycle per mode: summary, final state and transition history."""
    out = {"simulated": True,
           "note": "walk-forward simulation under the frozen policy; states grant nothing"}  # fmt: skip
    for mode, s in sims.items():
        out[mode] = {k: v for k, v in s.items()}
    return out


def build_profile(ev: es.EventSet, as_of: float, *, policy: LifecyclePolicy, meta: dict,
                  current_regime: dict[str, str], sims: dict[str, dict], curve_start: float,
                  forward: dict | None = None) -> EdgeProfile:  # fmt: skip
    """All evidence dimensions at ``as_of``. ``meta``: strategy_id, strategy_name, family,
    side, venue, horizon_bars, data_cutoff, source, outcomes."""
    floor = policy.floor.floor(meta["horizon_bars"])
    core = st.core_evidence(ev, as_of, policy)
    windows = es.all_windows(ev, as_of, policy)
    weighted = {f"hl{h}": es.weighted(ev, as_of, h, policy)
                for h in policy.weighting.half_lives_days}  # fmt: skip
    regime = regime_evidence(ev, as_of, current_regime, policy)
    k = ev.resolved(as_of)
    pts = st.curve(ev, as_of, policy, curve_start)
    dec = st.decay(ev, as_of, core, pts, floor, policy)
    fwd = forward or {"status": "none", "note": "no Phase 8 tracking of this strategy and venue"}
    life = core["lifetime"]
    hierarchy = {
        "lifetime": st.sign_class(life, floor, policy),
        "recent": st.sign_class(core["recent"], floor, policy),
        "current_regime_historical": st.sign_class(regime.get("current_regime_historical"),
                                                   floor, policy),
        "recent_current_regime": st.sign_class(regime.get("recent_current_regime"), floor,
                                               policy),
        "forward": fwd.get("maturity") or fwd.get("status"),
    }  # fmt: skip
    regime["hierarchy"] = hierarchy
    for p in pts:
        p["as_of"] = es.iso(p["as_of"])
    return EdgeProfile(
        policy_id=policy.policy_id,
        strategy_id=meta["strategy_id"],
        strategy_name=meta["strategy_name"],
        family=meta.get("family"),
        side=meta["side"],
        venue=meta["venue"],
        horizon_bars=meta["horizon_bars"],
        as_of=es.to_time(as_of).to_pydatetime(),
        data_cutoff=pd.Timestamp(meta["data_cutoff"]).to_pydatetime(),
        source=meta["source"],
        outcomes={
            **meta.get("outcomes", {}),
            "resolved_by_as_of": k,
            "economic_floor": floor,
        },
        edge_state=st.classify(core, floor, policy),
        divergence=st.divergence(core, floor, policy),
        lifetime=life,
        windows=windows,
        weighted=weighted,
        research_modes=research_modes(windows, life, policy),
        regime=regime,
        stress=stress_split(ev, as_of, policy),
        tails=es.tails(ev, 0, k),
        eras=es.eras(ev, as_of),
        decay=dec,
        change_detection={
            "online": cp.online_cusum(ev, as_of, policy.cusum),
            "retrospective": cp.retrospective_segments(ev, as_of),
        },
        diagnostics=st.diagnostics(core, floor, regime, fwd if forward else None, dec),
        lifecycle=lifecycle_block(sims, as_of),
        curve=pts,
        forward=fwd,
    )


# --------------------------------------------------------------------------- forward (read only)


def forward_block(ledger: Ledger, strategy_id: str, venue: str, as_of) -> dict | None:
    """Phase 8 prospective evidence for this strategy and venue, read-only, as recorded by
    ``as_of`` (``forward_summary`` reads only rows recorded by then). Reported beside the
    retrospective evidence; v1 gives it no weight in the edge state."""
    from market_signal.research.lab import forward as fw

    try:
        rows = [t for t in fw.trackings(ledger)
                if t["definition"].strategy_id == strategy_id and t["definition"].source == venue]  # fmt: skip
    except LedgerError:
        return None
    if not rows:
        return None
    out = []
    for t in rows:
        s = fw.forward_summary(ledger, t["tracking_id"], as_of=as_of)
        prim = next(h for h in s["horizons"] if h["primary"])
        out.append({"tracking_id": t["tracking_id"], "status": t["status"],
                    "maturity": s["maturity"]["level"],
                    "independent_resolved": prim["independent_resolved"],
                    "net_mean": (prim.get("net") or {}).get("mean"),
                    "excess_mean": prim.get("excess_mean"),
                    "direction_vs_historical": prim.get("direction_vs_historical")})  # fmt: skip
    best = max(out, key=lambda r: r["independent_resolved"])
    return {"status": "tracked", "trackings": out, "maturity": best["maturity"],
            "independent_resolved": best["independent_resolved"], "net_mean": best["net_mean"],
            "as_of": pd.Timestamp(as_of).isoformat(),
            "note": "Phase 8 forward evidence, read-only; reported, not weighted (v1)"}  # fmt: skip


# --------------------------------------------------------------------------- persistence


def _require(ledger: Ledger) -> None:
    if not ledger.store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='lab_edge_profiles'"
    ).fetchone():
        raise LedgerError("lab_edge_profiles is absent; open Store writable once to migrate")


def record_profiles(ledger: Ledger, profiles: list[EdgeProfile], *,
                    software: SoftwareIdentity) -> dict:  # fmt: skip
    """Append profiles; an identical profile already stored is a no-op (never rewritten)."""
    _require(ledger)
    ledger.register_software(software)
    inserted = existing = 0
    with ledger.store.transaction():
        for p in profiles:
            pid = p.profile_id
            if ledger.store.con.execute(
                "SELECT 1 FROM lab_edge_profiles WHERE profile_id=?", [pid]
            ).fetchone():
                existing += 1
                continue
            ledger.store.con.execute(
                "INSERT INTO lab_edge_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [pid, p.strategy_id, p.strategy_name, p.venue, p.as_of, p.data_cutoff,
                 p.policy_id, p.methodology_version, p.edge_state["state"],
                 str(p.source.get("run_id") or p.source.get("kind")), utcnow(),
                 software.software_id, canonical_json(p.model_dump(mode="json"))],
            )  # fmt: skip
            inserted += 1
    return {"inserted": inserted, "existing": existing}


def load_profiles(ledger: Ledger, *, strategy: str | None = None, venue: str | None = None,
                  as_of: str | None = None) -> list[dict]:  # fmt: skip
    """Stored profiles (newest evaluation first). ``strategy`` matches the ID or the name.
    ``as_of`` selects a stored evaluation time; it can never create a new window."""
    _require(ledger)
    sql = "SELECT profile_id, payload FROM lab_edge_profiles WHERE 1=1"
    args: list = []
    if strategy:
        sql += " AND (strategy_id=? OR strategy_name=?)"
        args += [strategy, strategy]
    if venue:
        sql += " AND venue=?"
        args.append(venue)
    if as_of:
        sql += " AND as_of=?"
        args.append(pd.Timestamp(as_of).to_pydatetime())
    sql += " ORDER BY as_of DESC, venue, strategy_name, profile_id"
    out = []
    for pid, payload in ledger.store.con.execute(sql, args).fetchall():
        prof = EdgeProfile.model_validate_json(payload)
        if prof.profile_id != pid:
            raise LedgerError(f"stored profile {pid} does not match its content")
        out.append({"profile_id": pid, **json.loads(payload)})
    return out


def with_forward(p: EdgeProfile, forward: dict | None, source: dict) -> EdgeProfile:
    """The same evidence with a read-only forward block and the governed source attached."""
    data = p.model_dump(mode="json")
    data["source"] = {**data["source"], **source}
    if forward:
        data["forward"] = forward
        data["diagnostics"]["forward_support"] = {
            "maturity": forward.get("maturity"), "net_mean": forward.get("net_mean"),
            "independent_resolved": forward.get("independent_resolved")}  # fmt: skip
        data["regime"]["hierarchy"]["forward"] = forward.get("maturity")
    return EdgeProfile.model_validate(data)


def profiles_from_run(ledger: Ledger, run_id: str) -> list[EdgeProfile]:
    """The cutoff profiles of a COMPLETED lifecycle study run, verified against the payload,
    with the run as their source and Phase 8 forward evidence (read-only) as of the cutoff."""
    from market_signal.research.lab import structure_study as ss

    row = ledger.store.con.execute(
        "SELECT r.study_id, s.result_id, s.status, s.result_digest FROM lab_structure_runs r "
        "JOIN lab_structure_results s USING (run_id) WHERE r.run_id=?",
        [run_id],
    ).fetchone()
    if not row or row[2] != "COMPLETED":
        raise LedgerError(f"{run_id} is not a completed study run")
    payload = ss.result_payload(ledger, run_id)
    if payload.get("study_version") != "edge_lifecycle_v1":
        raise LedgerError(f"{run_id} is not a lifecycle study run")
    source = {"run_id": run_id, "result_id": row[1], "result_digest": row[3]}
    out = []
    for venue in payload["venues"].values():
        for r in venue["strategies"]:
            if r.get("no_outcomes"):
                continue
            p = EdgeProfile.model_validate(r["profile"])
            if p.profile_id != r["profile_id"]:
                raise LedgerError(f"{r['strategy']}: stored profile does not reproduce its ID")
            fwd = forward_block(ledger, p.strategy_id, p.venue, p.as_of)
            out.append(with_forward(p, fwd, source))
    return out
