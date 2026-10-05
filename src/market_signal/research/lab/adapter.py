"""Full-research adapter: frozen Lab strategies through Prism's existing research machinery.

Pure functions; nothing here writes to the ledger (``validation.py`` owns governance).

Lab strategy -> Phase 3 compiler -> Phase 4 ``screen`` (``perp_asset_events`` semantics,
``side_forward_returns``, ``run_event_study``, ``decluster``, random-entry null) ->
Prism ``backtest.robustness`` (``make_folds``, ``walk_forward``, ``window_excess``,
``plateau_verdict``) -> one descriptive full-research payload.

The strategy is frozen. Nothing in this module chooses, tunes or ranks parameters:

- **Walk-forward** is Prism's ``walk_forward`` called with a ONE-point grid (the frozen
  definition) and zero training length, so its selection step is a no-op by construction
  and every fold is "the same strategy on the next chronological block", with block-local
  baselines (``window_excess``). It measures fold consistency, not optimisation.
- **Sensitivity** never mutates the strategy. It re-evaluates the already registered
  Phase 6 neighbours (one adjacent step in one family parameter) on the same data and
  classifies the TARGET with Prism's ``plateau_verdict`` (plus the Phase 7 neighbourhood
  label). The target is reported whether or not a neighbour does better.
- **Portfolio simulation** is unsupported: its risk/sizing assumptions are not frozen in a
  governed policy, venue max leverage is the latest snapshot rather than point-in-time,
  and cross-symbol bar ordering is an open audit item. It is reported as such, never faked.
- Prism's legacy ``automatic_verdict`` is NOT used: it can issue a positive verdict with
  walk-forward or sensitivity missing. Full-research statuses here require every
  prescribed check to be complete.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from market_signal.backtest.events import baseline_bars
from market_signal.backtest.robustness import make_folds, plateau_verdict, walk_forward
from market_signal.research.lab.common import (
    LabModel,
    Name,
    Number,
    PositiveInt,
    Probability,
    canonical_json,
    content_id,
)
from market_signal.research.lab.compiler import Snapshot
from market_signal.research.lab.evidence import (
    EvidencePolicy,
    asset_summary,
    horizon_summary,
    neighbourhood_summary,
)
from market_signal.research.lab.policy import DatasetRole, ScreenPlan
from market_signal.research.lab.screen import ScreenResult, ScreenWorkspace, screen
from market_signal.research.lab.spec import StrategyDefinition

ADAPTER_VERSION = "lab_full_research_adapter_v1"

# What generic Lab strategies receive from Prism's full research, step by step. Stored with
# every result so a reader can see what was and was not done (never implied compatibility).
STEPS = {
    "event_study": ("supported", "Phase 4 screen = perp_asset_events/side_forward_returns + "
                    "run_event_study on the retained snapshot; parity with the stored discovery "
                    "result is checked"),
    "independent_events": ("supported", "backtest.events.decluster (gap >= horizon bars)"),
    "costs_slippage": ("supported", "per-asset fee + slippage frozen in the plan"),
    "funding": ("adapted", "Lab causal funding (trailing 7-day cadence, availability by bar "
                "close), not Prism's full-history cadence; missing funding excludes the outcome"),
    "random_entry_baseline": ("supported", "Phase 4 random_entry_mean_excess_v1 at the primary "
                              "horizon (same-asset/side eligible pool, plan seed and draws)"),
    "walk_forward": ("adapted", "Prism walk_forward with a one-point grid and zero training "
                     "length: the frozen strategy on chronological blocks, block-local "
                     "baselines, no parameter reselection"),
    "sensitivity": ("adapted", "registered Phase 6 neighbours re-evaluated on the same data; "
                    "Prism plateau_verdict classifies the target; no winner is selected"),
    "cross_asset": ("supported", "per-asset rows, breadth, concentration and leave-largest-out"),
    "horizon_profile": ("supported", "all plan horizons; only the primary is tested"),
    "regime_splits": ("skipped", "the perp runner has no regime/trend/rate splits and Lab "
                      "snapshots hold no benchmark or regime series"),
    "portfolio_simulation": ("unsupported", "risk/sizing not frozen in a governed policy; venue "
                             "max leverage is the latest snapshot (not point-in-time); "
                             "cross-symbol bar ordering is an open audit item"),
    "legacy_automatic_verdict": ("skipped", "can return a positive verdict with missing "
                                 "walk-forward/sensitivity; Lab statuses require completeness"),
}  # fmt: skip


class FullResearchPolicy(LabModel):
    """Versioned full-research classification. Any change is a new version (new ID).

    The sensitivity thresholds are frozen copies of Prism's ``config/backtest.yaml``
    defaults (``plateau_min_neighbour_ratio``, ``fragile_if_neighbours_fail``), so a
    config edit cannot silently change a Lab result.
    """

    name: Name = "lab_full_research_policy"
    version: PositiveInt = 1
    fold_months: Literal[6] = 6  # make_folds test length; the shortest fold Prism allows
    min_fold_events: PositiveInt = 5
    min_adequate_folds: PositiveInt = 3
    consistent_fold_share: Probability = 0.67
    inconsistent_fold_share: Probability = 0.34
    plateau_min_neighbour_ratio: Probability = 0.5
    fragile_if_neighbours_fail: Probability = 0.5
    min_independent_events: PositiveInt = 30
    min_assets_with_events: PositiveInt = 3
    min_effect: Number = 0.0

    @property
    def policy_id(self) -> str:
        return content_id("frpolicy_", self.model_dump(mode="python"))


FULL_RESEARCH_POLICIES = {1: FullResearchPolicy()}


def _num(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if np.isfinite(x) else None


def _sign(x) -> int:
    return 0 if x is None or x == 0 else (1 if x > 0 else -1)


def primary_row(metrics: dict, primary: str) -> dict:
    return next((a for a in metrics.get("aggregate", []) if a["horizon"] == primary), {})


# --------------------------------------------------------------------------- parity


def parity(recomputed: dict, stored: dict) -> dict:
    """Does the adapter reproduce a stored Phase 4 result bit for bit (same JSON)?"""
    diffs = [
        key
        for key in ("aggregate", "per_asset", "triage")
        if canonical_json(_jsonable(recomputed.get(key))) != canonical_json(stored.get(key))
    ]
    return {"exact": not diffs, "compared": ["aggregate", "per_asset", "triage"], "differs": diffs}


def _jsonable(value):
    import json

    return json.loads(canonical_json(value)) if value is not None else None


# --------------------------------------------------------------------------- walk-forward


def frozen_walk_forward(
    result: ScreenResult, plan: ScreenPlan, role: DatasetRole, policy: FullResearchPolicy
) -> dict:
    """The frozen strategy on consecutive chronological blocks of the role period.

    Reuses Prism's ``walk_forward`` with grid = [frozen] (one point) and zero training
    length; the "chosen" parameters are therefore always the frozen ones (asserted).
    Events are selected by signal time; an outcome may end in the next block (known Prism
    convention). With no training, that cannot leak into any selection.
    """
    period = plan.period(role)
    primary = plan.primary_horizon
    bars = {h.label: h.bars for h in plan.horizons}[primary]
    events = result.events
    base = baseline_bars(list(result.asset_events))
    folds = make_folds(
        pd.Timestamp(period.start), pd.Timestamp(period.end), 0.0, policy.fold_months / 12
    )
    if events.empty or not folds:
        return {"status": "insufficient", "label": "insufficient", "folds": [],
                "reason": "no events" if events.empty else "role period shorter than one fold"}  # fmt: skip
    gap = {ae.symbol: bars for ae in result.asset_events}
    table, summary = walk_forward(
        lambda _params: events, base, [{}], {}, folds, primary, gap,
        pd.Timedelta(days=bars + 5), min_train_events=1, objective="median_excess",
    )  # fmt: skip
    if not table["chosen"].astype(str).str.startswith("default").all():
        raise RuntimeError("frozen walk-forward selected a parameter; the grid must be one point")
    rows = []
    for f, r in zip(folds, table.to_dict("records"), strict=True):
        n = int(r["test_n"])
        rows.append({
            "fold": int(r["fold"]), "start": f.test_start.isoformat(), "end": f.test_end.isoformat(),
            "independent_events": n, "excess_mean": _num(r["test_excess_mean"]),
            "excess_median": _num(r["test_excess_median"]), "hit_rate": _num(r["test_hit"]),
            "adequate": n >= policy.min_fold_events,
            "sign": _sign(_num(r["test_excess_mean"])),
        })  # fmt: skip
    adequate = [r for r in rows if r["adequate"]]
    positive = sum(r["sign"] > 0 for r in adequate)
    share = positive / len(adequate) if adequate else None
    if len(adequate) < policy.min_adequate_folds:
        label = "insufficient"
    elif share >= policy.consistent_fold_share:
        label = "consistent"
    elif share <= policy.inconsistent_fold_share:
        label = "inconsistent"
    else:
        label = "mixed"
    return {
        "status": "complete" if label != "insufficient" else "insufficient",
        "label": label,
        "definition": "frozen strategy; consecutive blocks; block-local baseline; no reselection",
        "horizon": primary,
        "folds": rows,
        "adequate_folds": len(adequate),
        "positive_adequate_folds": positive,
        "positive_share": _num(share),
        "pooled_block_excess_mean": _num(summary["oos_excess_mean"]),
        "pooled_block_events": int(summary["oos_n"]),
        "parameter_reselection": False,
    }


# --------------------------------------------------------------------------- sensitivity


def neighbour_sensitivity(
    target: dict,
    neighbours: list[dict],
    plan: ScreenPlan,
    role: DatasetRole,
    evidence_policy: EvidencePolicy,
    policy: FullResearchPolicy,
) -> dict:
    """Robustness of the frozen target across its registered neighbours (no selection).

    ``target``/``neighbours``: ``{strategy_id, name, lineage, metrics}`` where ``metrics``
    are Phase 4 metrics evaluated on the SAME plan role and snapshot. Returns neighbour
    rows, Phase 7's sign-agreement label, Prism's ``plateau_verdict`` for the target, the
    effect dispersion and a knife-edge flag.
    """
    primary = plan.primary_horizon
    rows, records = [], []
    for m in (target, *neighbours):
        agg = primary_row(m["metrics"], primary)
        triage = m["metrics"].get("triage")
        excess = _num(agg.get("excess_mean")) if triage in ("WEAK", "INTERESTING") else None
        row = {
            "strategy_id": m["strategy_id"],
            "name": m.get("name"),
            "params": (m.get("lineage") or {}).get("params"),
            "target": m is target,
            "triage": triage,
            "independent_events": agg.get("independent_events", 0),
            "assets_with_events": agg.get("assets_with_events", 0),
            "excess_mean": excess,
            "hit_rate": _num(agg.get("hit_rate")),
            "sign": _sign(excess),
        }
        rows.append(row)
        records.append({**m, "phase4_triage": triage, "excess_mean": excess,
                        "family_id": m.get("family_id")})  # fmt: skip
    if target.get("lineage") is None:
        return {"status": "unavailable", "label": "unavailable", "rows": rows,
                "reason": "no structured-family lineage: no neighbours to compare"}  # fmt: skip
    phase7 = neighbourhood_summary(records[0], records, evidence_policy)
    testable = [r for r in rows[1:] if r["excess_mean"] is not None]
    effects = [r["excess_mean"] for r in testable]
    prism = {"verdict": "UNDEFINED", "detail": "no testable neighbours"}
    params = target["lineage"]["params"]
    usable = [r for r in rows if r["excess_mean"] is not None and r["params"]]
    varying = [k for k in params if len({r["params"][k] for r in usable}) > 1]
    if testable and len(varying) <= 2:
        axes = {k: sorted({r["params"][k] for r in usable}) for k in varying}
        table = pd.DataFrame(
            [{**{k: r["params"][k] for k in varying}, "n_indep": r["independent_events"],
              "excess_mean": r["excess_mean"]} for r in usable]
        )  # fmt: skip
        prism = plateau_verdict(
            table, {k: params[k] for k in varying}, axes, policy.min_independent_events,
            policy.plateau_min_neighbour_ratio, policy.fragile_if_neighbours_fail,
        )  # fmt: skip
        prism = {
            k: (_num(v) if isinstance(v, float | np.floating) else v) for k, v in prism.items()
        }
    elif len(varying) > 2:
        prism = {"verdict": "UNSUPPORTED", "detail": "plateau_verdict handles at most 2 axes"}
    verdict = prism["verdict"]
    complete = verdict in ("PLATEAU", "MIXED", "FRAGILE", "NO_EDGE")
    return {
        "status": "complete" if complete else "insufficient",
        "label": verdict.lower() if complete else "insufficient",
        "definition": "registered family variants one adjacent step away in one parameter",
        "horizon": primary,
        "rows": rows,
        "phase7_neighbourhood": phase7,
        "prism_plateau_verdict": prism,
        "testable_neighbours": len(testable),
        "neighbour_excess_std": _num(np.std(effects, ddof=1)) if len(effects) > 1 else None,
        "neighbour_excess_range": [_num(min(effects)), _num(max(effects))] if effects else None,
        "same_direction_share": phase7.get("support_share"),
        "knife_edge": verdict == "FRAGILE" or bool(phase7.get("isolated_spike")),
        "winner_selection": False,
    }


# --------------------------------------------------------------------------- full research


def evaluate(
    definition: StrategyDefinition,
    plan: ScreenPlan,
    snapshot: Snapshot,
    role: DatasetRole,
    assets: tuple[str, ...],
    workspace: ScreenWorkspace | None = None,
) -> ScreenResult:
    """The frozen definition through the Phase 4 screen (no ledger writes)."""
    return screen(definition, plan, snapshot, role, assets, workspace=workspace)


def full_research(
    target: dict,
    neighbours: list[dict],
    plan: ScreenPlan,
    snapshot: Snapshot,
    role: DatasetRole,
    assets: tuple[str, ...],
    evidence_policy: EvidencePolicy,
    policy: FullResearchPolicy,
) -> dict:
    """Deeper research on the role's data for one frozen strategy (pure).

    ``target``/``neighbours``: ``{strategy_id, name, lineage, definition, stored_metrics}``.
    Every evaluation shares one workspace (identical inputs, features and returns).
    """
    ws = ScreenWorkspace(snapshot)
    primary = plan.primary_horizon
    result = evaluate(target["definition"], plan, snapshot, role, assets, ws)
    metrics = result.metrics
    checks = {"parity": parity(metrics, target["stored_metrics"])}
    evaluated = []
    for n in neighbours:
        r = evaluate(n["definition"], plan, snapshot, role, assets, ws)
        evaluated.append({**n, "metrics": r.metrics,
                          "parity": parity(r.metrics, n["stored_metrics"])
                          if n.get("stored_metrics") else None})  # fmt: skip
    agg = primary_row(metrics, primary)
    horizons = {h.label: h.bars for h in plan.horizons}
    payload = {
        "adapter_version": ADAPTER_VERSION,
        "policy_id": policy.policy_id,
        "role": role,
        "window": [plan.period(role).start, plan.period(role).end],
        "steps": {k: {"status": s, "detail": d} for k, (s, d) in STEPS.items()},
        "event_study": {
            "horizon": primary,
            "independent_events": agg.get("independent_events"),
            "assets_with_events": agg.get("assets_with_events"),
            "excess_mean": agg.get("excess_mean"),
            "excess_median": agg.get("excess_median"),
            "net_mean": agg.get("net_mean"),
            "net_median": agg.get("net_median"),
            "gross_mean": agg.get("gross_mean"),
            "hit_rate": agg.get("hit_rate"),
            "excess_t": agg.get("excess_t"),
            "raw_p_random_entry": agg.get("p_value_random_entry"),
            "triage": metrics.get("triage"),
        },
        "assets": asset_summary(metrics["per_asset"], agg, primary, evidence_policy),
        "horizons": horizon_summary(
            metrics["aggregate"], horizons, primary, evidence_policy.horizon_dead_band
        ),
        "walk_forward": frozen_walk_forward(result, plan, role, policy),
        "sensitivity": neighbour_sensitivity(
            {**target, "metrics": metrics}, evaluated, plan, role, evidence_policy, policy
        ),
        "portfolio_simulation": {
            "status": "unsupported",
            "reason": STEPS["portfolio_simulation"][1],
        },
        "parity": {
            "target": checks["parity"],
            "neighbours": {n["strategy_id"]: n["parity"] for n in evaluated},
        },
        "compile_digests": {s: m["compile_digest"] for s, m in metrics["assets"].items()},
    }
    payload["status"], payload["checks"], payload["reasons"] = classify(payload, policy)
    return _jsonable(payload)


def classify(payload: dict, policy: FullResearchPolicy) -> tuple[str, dict, list[str]]:
    """FULL_RESEARCH_* status. Positive only when every prescribed check completed."""
    es, wf, sens, assets = (payload["event_study"], payload["walk_forward"],
                            payload["sensitivity"], payload["assets"])  # fmt: skip
    effect = es["excess_mean"]
    checks = {
        "parity": payload["parity"]["target"]["exact"],
        "sample": (es["independent_events"] or 0) >= policy.min_independent_events
        and (es["assets_with_events"] or 0) >= policy.min_assets_with_events,
        "walk_forward_complete": wf["status"] == "complete",
        "sensitivity_complete": sens["status"] == "complete",
    }
    reasons = []
    if not checks["parity"]:
        reasons.append(
            f"adapter did not reproduce the stored discovery result ({payload['parity']['target']['differs']})"
        )
        return "FULL_RESEARCH_ERROR", checks, reasons
    if not checks["sample"]:
        reasons.append(
            f"needs >= {policy.min_independent_events} independent events on >= "
            f"{policy.min_assets_with_events} assets"
        )
    if not checks["walk_forward_complete"]:
        reasons.append(f"walk-forward incomplete ({wf.get('reason') or wf['label']})")
    if not checks["sensitivity_complete"]:
        reasons.append(f"sensitivity incomplete ({sens.get('reason') or sens['label']})")
    if not all(checks.values()):
        return "FULL_RESEARCH_INSUFFICIENT", checks, reasons
    adverse = []
    if effect is None or effect <= policy.min_effect:
        adverse.append(f"pooled excess {effect} is not in the expected direction")
    if wf["label"] == "inconsistent":
        adverse.append(f"positive in {wf['positive_adequate_folds']}/{wf['adequate_folds']} blocks")
    if sens["label"] in ("fragile", "no_edge"):
        adverse.append(f"parameter sensitivity: {sens['label']}")
    if adverse:
        return "FULL_RESEARCH_INCONSISTENT", checks, reasons + adverse
    if wf["label"] == "consistent" and sens["label"] == "plateau" and not assets["concentrated"]:
        return "FULL_RESEARCH_CONSISTENT", checks, ["every prescribed check complete and agreeing"]
    mixed = []
    if wf["label"] != "consistent":
        mixed.append(f"walk-forward {wf['label']}")
    if sens["label"] != "plateau":
        mixed.append(f"sensitivity {sens['label']}")
    if assets["concentrated"]:
        mixed.append("asset-concentrated")
    return "FULL_RESEARCH_MIXED", checks, mixed
