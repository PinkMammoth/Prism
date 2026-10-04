"""Preregistered search batches: one frozen testing family, one FDR analysis per run.

A batch states "these N strategies are searched together on this plan, dataset and role,
with this primary test, correction and survivor rule" BEFORE any member is screened.
Freezing writes that definition once; it can never gain or lose members. A run
preregisters every member as a Phase 2 attempt, screens each with Phase 4
(``run_screen``, one shared workspace), and only then records one immutable analysis
holding raw p-values, BH q-values and family-aware statuses. Screen results are never
modified; the analysis is a separate layer that cites them.

``FDR_SURVIVOR`` is not validation. It only qualifies a strategy for deeper, independent
research on data the batch did not touch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

import numpy as np
import yaml
from pydantic import Field, model_validator

from market_signal.models.domain import Timeframe, utcnow
from market_signal.research.lab.common import (
    LabModel,
    Name,
    Number,
    Text,
    canonical_json,
    content_id,
    revalidate,
)
from market_signal.research.lab.ledger import ErrorInfo, Ledger, LedgerError
from market_signal.research.lab.policy import ScreenPlan
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.screen import run_screen
from market_signal.research.lab.spec import StrategyId, _UniqueKeyLoader

ANALYSIS_VERSION = "lab_batch_analysis_v1"
PRIMARY_TEST = "random_entry_mean_excess_v1"
# Phase 4 gates that measure substance (not the uncorrected p-value gate).
SUBSTANTIVE_GATES = (
    "min_independent_events",
    "min_assets_with_events",
    "min_pooled_excess",
    "min_positive_asset_share",
)
TESTABLE_TRIAGE = ("WEAK", "INTERESTING")


class BatchError(LedgerError):
    pass


# --------------------------------------------------------------------------- policy


class CorrectionPolicy(LabModel):
    """Frozen correction semantics. Each literal names one exact, versioned meaning, so a
    future method (e.g. Benjamini-Yekutieli, permutation) is a new value, never a change
    to what an existing batch meant."""

    method: Literal["benjamini_hochberg"] = "benjamini_hochberg"
    q: Annotated[Number, Field(gt=0, le=0.5)] = 0.1
    family: Literal["one_primary_test_per_strategy_v1"] = "one_primary_test_per_strategy_v1"
    test: Literal["random_entry_mean_excess_v1"] = PRIMARY_TEST
    # Members enter only if Phase 4 found enough independent events/assets (WEAK or
    # INTERESTING) and recorded the primary p-value; the filter uses counts, not p.
    eligibility: Literal["phase4_sufficient_events_v1"] = "phase4_sufficient_events_v1"


class SurvivorPolicy(LabModel):
    """A survivor needs substance AND corrected evidence. ``min_pooled_excess`` is the
    economic floor on pooled independent net excess at the primary horizon; it has no
    default because a "big enough" effect is a research decision, not a constant."""

    rule: Literal["substantive_gates_and_q_v1"] = "substantive_gates_and_q_v1"
    min_pooled_excess: Number


class BatchDefinition(LabModel):
    """The statistical identity of a testing family. Name and prose are metadata."""

    schema_version: Literal["1"] = "1"
    plan_id: Annotated[str, Field(pattern=r"^plan_[0-9a-f]{64}$")]
    dataset_id: Annotated[str, Field(pattern=r"^dataset_[0-9a-f]{64}$")]
    # Search never runs on validation or final-holdout data.
    role: Literal["discovery", "development"]
    primary_horizon: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,32}$")]
    members: Annotated[tuple[StrategyId, ...], Field(min_length=2, max_length=1000)]
    correction: CorrectionPolicy = CorrectionPolicy()
    survivor: SurvivorPolicy

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        if len(set(self.members)) != len(self.members):
            raise ValueError("duplicate batch members")
        return self

    def canonical_json(self) -> str:
        data = self.model_dump(mode="python")
        data["members"] = sorted(data["members"])  # order cannot create a new family
        return canonical_json(data)

    @property
    def batch_id(self) -> str:
        return content_id("batch_", json.loads(self.canonical_json()))


class BatchManifest(LabModel):
    """Declarative YAML for ``market lab batch create``."""

    name: Name
    description: Text
    plan: str
    dataset: str
    role: Literal["discovery", "development"]
    primary_horizon: str
    correction: CorrectionPolicy = CorrectionPolicy()
    survivor: SurvivorPolicy
    members: tuple[str, ...]

    @model_validator(mode="after")
    def valid_definition(self) -> Self:
        self.definition()  # strict: a manifest is invalid if its definition is
        return self

    def definition(self) -> BatchDefinition:
        return BatchDefinition(
            plan_id=self.plan,
            dataset_id=self.dataset,
            role=self.role,
            primary_horizon=self.primary_horizon,
            members=self.members,
            correction=self.correction,
            survivor=self.survivor,
        )


def load_manifest(path: Path) -> BatchManifest:
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return BatchManifest.model_validate(raw)


# --------------------------------------------------------------------------- BH


def benjamini_hochberg(pvalues: dict[str, float]) -> dict[str, float]:
    """BH adjusted p-values (q-values) for one family, keyed like the input.

    Sort ascending by (p, key); q_(i) = min_{j >= i} min(1, p_(j) * m / j). Tied p-values
    receive identical q-values, so the key order never matters. Rejecting q <= alpha is
    exactly the BH step-up procedure at level alpha. Inputs must be finite in [0, 1].
    """
    items = sorted(pvalues.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(items)
    if any(not (0.0 <= p <= 1.0) for _, p in items):
        raise ValueError("p-values must lie in [0, 1]")
    q, running = {}, 1.0
    for rank in range(m, 0, -1):
        key, p = items[rank - 1]
        running = min(running, p * m / rank)
        q[key] = min(running, 1.0)
    return {k: q[k] for k in pvalues}


# --------------------------------------------------------------------------- freeze


def _new_id(prefix: str) -> str:
    return prefix + uuid4().hex


def _rows(ledger: Ledger, sql: str, args: list) -> list[dict]:
    cursor = ledger.store.con.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _require_tables(ledger: Ledger) -> None:
    if not ledger.store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='lab_batches'"
    ).fetchone():
        raise BatchError("Lab batch tables are absent; open Store writable once to migrate")


def freeze_batch(ledger: Ledger, manifest: BatchManifest, *, origin: str) -> str:
    """Validate and permanently record a testing family. Nothing is screened here.

    Refuses: non-screen plans; roles other than discovery/development; any overlap of
    the dataset (warmup included) with a validation or final-holdout period; datasets
    that do not match the plan role; unknown, invalid, wrong-market or non-daily members;
    a member already preregistered on these inputs or frozen in another batch on them
    (its outcome may be known, so it cannot join a family "blind"); a Monte Carlo
    resolution too coarse for any member to pass BH; and a duplicate name/definition.
    """
    _require_tables(ledger)
    manifest = revalidate(manifest)
    definition = manifest.definition()
    if ledger.store.con.execute(
        "SELECT 1 FROM lab_batches WHERE name=?", [manifest.name]
    ).fetchone():
        raise BatchError(f"batch name {manifest.name!r} is taken; names are permanent")
    plan = ledger.get_plan(definition.plan_id)
    if not isinstance(plan, ScreenPlan):
        raise BatchError("batches need a schema v2 screen plan")
    period = plan.period(definition.role)
    data_start = plan.data_start(definition.role)
    if definition.primary_horizon != plan.primary_horizon:
        raise BatchError("batch primary horizon must be the plan's preregistered primary horizon")
    for other in plan.periods:
        if other.role in ("validation", "final_holdout") and (
            other.start < period.end and data_start < other.end
        ):
            raise BatchError(
                f"batch data [{data_start}, {period.end}) overlaps the plan's {other.role} "
                "period; search batches never touch confirmatory data"
            )
    dataset = ledger.get_dataset(definition.dataset_id)
    assets = tuple(
        sorted({s.selection.symbol for s in dataset.series if s.selection.kind.endswith("bars")})
    )
    for series in dataset.series:
        sel = series.selection
        if (sel.source, sel.start, sel.end) != (plan.source, data_start, period.end):
            raise BatchError("dataset must cover exactly the plan role window plus its warmup")
    if not assets or len(assets) < plan.gates.min_assets_with_events:
        raise BatchError("dataset has fewer assets than the plan's minimum assets with events")
    m = len(definition.members)
    floor = 1 / (plan.statistics.random_entry_samples + 1)
    if floor > definition.correction.q / m:
        raise BatchError(
            f"random_entry_samples={plan.statistics.random_entry_samples} gives a smallest "
            f"attainable p of {floor:.2g}, above the first BH threshold q/m = "
            f"{definition.correction.q / m:.2g}; no member could ever survive. Use a plan "
            "with more draws or a smaller family"
        )
    submissions = {}
    for strategy_id in sorted(definition.members):
        rule = ledger.get_strategy(strategy_id)  # raises if unregistered
        if rule.market != plan.market:
            raise BatchError(f"{strategy_id} is a {rule.market} strategy; plan is {plan.market}")
        refs = [r for c in rule.conditions for r in c.references()]
        if rule.trigger_timeframe != Timeframe.D1 or any(r.timeframe != Timeframe.D1 for r in refs):
            raise BatchError(f"{strategy_id} is not a daily definition")
        row = ledger.store.con.execute(
            "SELECT s.submission_id FROM lab_submissions s JOIN lab_hypotheses h "
            "USING (hypothesis_id) WHERE h.strategy_id=? ORDER BY s.received_at, s.submission_id "
            "LIMIT 1",
            [strategy_id],
        ).fetchone()
        if row is None:
            raise BatchError(f"{strategy_id} has no accepted submission")
        submissions[strategy_id] = row[0]
        seen = ledger.store.con.execute(
            "SELECT experiment_id FROM lab_experiments WHERE strategy_id=? AND plan_id=? "
            "AND dataset_id=? AND role=? LIMIT 1",
            [strategy_id, definition.plan_id, definition.dataset_id, definition.role],
        ).fetchone()
        if seen:
            raise BatchError(
                f"{strategy_id} was already preregistered on these inputs ({seen[0]}); its "
                "outcome may be known, so it cannot join a new family"
            )
    for other in _rows(ledger, "SELECT batch_id, payload FROM lab_batches", []):
        d = json.loads(other["payload"])["definition"]
        if other["batch_id"] == definition.batch_id:
            raise BatchError(f"identical testing family already frozen: {other['batch_id']}")
        if (d["plan_id"], d["dataset_id"], d["role"]) == (
            definition.plan_id,
            definition.dataset_id,
            definition.role,
        ) and set(d["members"]) & set(definition.members):
            raise BatchError(
                f"members overlap batch {other['batch_id']} on the same plan/dataset/role"
            )
    payload = {
        "definition": json.loads(definition.canonical_json()),
        "name": manifest.name,
        "description": manifest.description,
        "assets": list(assets),
        "submissions": submissions,
        "window": {"data_start": data_start, "start": period.start, "end": period.end},
    }
    with ledger.store.transaction():
        if ledger.store.con.execute(
            "SELECT 1 FROM lab_batches WHERE name=?", [manifest.name]
        ).fetchone():
            raise BatchError(f"batch name {manifest.name!r} is taken; names are permanent")
        ledger.store.con.execute(
            "INSERT INTO lab_batches VALUES (?,?,?,?,?)",
            [definition.batch_id, manifest.name, canonical_json(payload), origin, utcnow()],
        )
    return definition.batch_id


# --------------------------------------------------------------------------- run


def get_batch(ledger: Ledger, batch_id: str) -> dict:
    _require_tables(ledger)
    rows = _rows(ledger, "SELECT * FROM lab_batches WHERE batch_id=?", [batch_id])
    if not rows:
        raise BatchError("batch does not exist")
    payload = json.loads(rows[0]["payload"])
    definition = BatchDefinition.model_validate(payload["definition"])
    if definition.batch_id != batch_id:
        raise BatchError("stored batch does not match its identity")
    return {**rows[0], "payload": payload, "definition": definition}


def _runs(ledger: Ledger, batch_id: str) -> list[dict]:
    return _rows(
        ledger,
        "SELECT r.*, a.analysis_id, a.status AS analysis_status FROM lab_batch_runs r "
        "LEFT JOIN lab_batch_analyses a USING (run_id) WHERE r.batch_id=? ORDER BY r.attempt",
        [batch_id],
    )


def run_batch(
    ledger: Ledger,
    batch_id: str,
    *,
    software: SoftwareIdentity,
    rerun_of: str | None = None,
    rerun_reason: str | None = None,
    workspaces: dict | None = None,
) -> dict:
    """Run (or finish an interrupted run of) a frozen batch and record its analysis.

    A batch has at most one run unless a rerun names the earlier run and a reason; a
    rerun never alters the earlier run, its attempts, screens or analysis. An unfinished
    run (crash) is resumed with the same software: unstarted members are screened, and
    started members without a result are recorded as interrupted errors.
    """
    batch = get_batch(ledger, batch_id)
    definition: BatchDefinition = batch["definition"]
    software = revalidate(software)
    runs = _runs(ledger, batch_id)
    open_runs = [r for r in runs if r["analysis_id"] is None]
    if open_runs:
        run = open_runs[-1]
        if rerun_of:
            raise BatchError(f"run {run['run_id']} is unfinished; resume it before a rerun")
        if run["software_id"] != software.software_id:
            raise BatchError("unfinished run used different software; resume with that software")
    else:
        if runs and not (rerun_of and rerun_reason):
            raise BatchError(
                "batch already has a recorded analysis; an explicit rerun (rerun_of + reason) "
                "is required and will not change it"
            )
        if rerun_of and rerun_of not in {r["run_id"] for r in runs}:
            raise BatchError("rerun_of must be an earlier run of the same batch")
        if rerun_reason and not rerun_of:
            raise BatchError("rerun_reason requires rerun_of")
        ledger.register_software(software)
        run = {
            "run_id": _new_id("batchrun_"),
            "batch_id": batch_id,
            "attempt": len(runs) + 1,
            "software_id": software.software_id,
            "started_at": utcnow(),
            "rerun_of": rerun_of,
            "rerun_reason": rerun_reason,
        }
        with ledger.store.transaction():
            ledger.store.con.execute(
                "INSERT INTO lab_batch_runs VALUES (?,?,?,?,?,?,?)", list(run.values())
            )
    run_id = run["run_id"]
    previous = {}
    if run.get("rerun_of"):
        previous = {
            r["strategy_id"]: r["experiment_id"]
            for r in _rows(
                ledger,
                "SELECT strategy_id, experiment_id FROM lab_batch_members WHERE run_id=?",
                [run["rerun_of"]],
            )
        }
    linked = {
        r["strategy_id"]: r["experiment_id"]
        for r in _rows(
            ledger,
            "SELECT strategy_id, experiment_id FROM lab_batch_members WHERE run_id=?",
            [run_id],
        )
    }
    # 1. Preregister EVERY member before any screen of this run starts.
    try:
        for strategy_id in sorted(definition.members):
            if strategy_id in linked:
                continue
            prior = previous.get(strategy_id)
            attempt = ledger.preregister(
                batch["payload"]["submissions"][strategy_id],
                definition.plan_id,
                definition.dataset_id,
                role=definition.role,
                assets=tuple(batch["payload"]["assets"]),
                software=software,
                origin=f"batch:{batch_id}",
                batch_id=batch_id,
                rerun_of=prior,
                rerun_reason=f"batch rerun {run_id}: {run['rerun_reason']}" if prior else None,
            )
            with ledger.store.transaction():
                ledger.store.con.execute(
                    "INSERT INTO lab_batch_members VALUES (?,?,?)",
                    [run_id, strategy_id, attempt.experiment_id],
                )
            linked[strategy_id] = attempt.experiment_id
    except LedgerError as exc:
        return _record(ledger, run_id, "failed", {
            "analysis_version": ANALYSIS_VERSION, "batch_id": batch_id, "run_id": run_id,
            "error": f"preregistration failed before any screening: {exc}",
            "preregistered_members": len(linked), "members": len(definition.members),
        })  # fmt: skip
    # 2. Screen each member with Phase 4 and a shared workspace.
    workspaces = {} if workspaces is None else workspaces
    for strategy_id in sorted(definition.members):
        experiment_id = linked[strategy_id]
        state = ledger.inspect_experiment(experiment_id)
        if state["status"] == "preregistered":
            run_screen(ledger, experiment_id, software=software, workspaces=workspaces)
        elif state["status"] == "started":
            ledger.record_result(
                experiment_id,
                status="errored",
                verdict="ERROR",
                metrics={"triage": "ERROR"},
                error=ErrorInfo(kind="interrupted", message="batch run resumed after a crash"),
            )
    # 3. Correct and record the analysis from the stored, immutable screen results.
    payload = analyse(ledger, batch, run, linked)
    return _record(ledger, run_id, "completed", payload)


def _record(ledger: Ledger, run_id: str, status: str, payload: dict) -> dict:
    analysis_id = _new_id("analysis_")
    with ledger.store.transaction():
        if ledger.store.con.execute(
            "SELECT 1 FROM lab_batch_analyses WHERE run_id=?", [run_id]
        ).fetchone():
            raise BatchError("batch analysis is immutable; use an explicit rerun")
        ledger.store.con.execute(
            "INSERT INTO lab_batch_analyses VALUES (?,?,?,?,?)",
            [analysis_id, run_id, utcnow(), status, canonical_json(payload)],
        )
    return {"analysis_id": analysis_id, "run_id": run_id, "status": status, **payload}


# --------------------------------------------------------------------------- analysis


def _num(x) -> float | None:
    return None if x is None or not np.isfinite(x) else float(x)


def classify(member: dict, q: float | None, definition: BatchDefinition) -> str:
    if member["phase4_triage"] == "ERROR":
        return "ERROR"
    if not member["in_family"]:
        return "NOT_TESTABLE"
    if q is None or q > definition.correction.q:
        return "NOT_FDR_SIGNIFICANT"
    substantive = all(member["gate_checks"].get(g) is True for g in SUBSTANTIVE_GATES) and (
        member["excess_mean"] is not None
        and member["excess_mean"] >= definition.survivor.min_pooled_excess
    )
    return "FDR_SURVIVOR" if substantive else "FDR_SIGNIFICANT_FAILS_SCREEN"


def analyse(ledger: Ledger, batch: dict, run: dict, linked: dict[str, str]) -> dict:
    """Pure function of the stored screen results of this run's members."""
    definition: BatchDefinition = batch["definition"]
    members = []
    for strategy_id in sorted(definition.members):
        record = ledger.inspect_experiment(linked[strategy_id])
        (result,) = record["results"]
        metrics = result["metrics"] or {}
        triage = result["verdict"] or "ERROR"
        lineage = ledger.store.con.execute(
            "SELECT family_id, parent_ids FROM lab_strategies WHERE strategy_id=?", [strategy_id]
        ).fetchone()
        side = ledger.get_strategy(strategy_id).side
        endpoint = f"pooled:{side}:{definition.primary_horizon}"
        p = next(
            (
                v["value"]
                for v in result["p_values"]
                if v["test"] == definition.correction.test and v["endpoint"] == endpoint
            ),
            None,
        )
        primary = next(
            (a for a in metrics.get("aggregate", []) if a["horizon"] == definition.primary_horizon),
            {},
        )
        reason = None
        if triage == "ERROR":
            reason = "screen errored"
        elif triage not in TESTABLE_TRIAGE:
            reason = f"Phase 4 {triage}"
        elif p is None:
            reason = "no primary p-value recorded"
        members.append(
            {
                "strategy_id": strategy_id,
                "family_id": lineage[0],
                "parent_ids": json.loads(lineage[1]),
                "side": side,
                "experiment_id": linked[strategy_id],
                "result_id": result["result_id"],
                "phase4_status": result["status"],
                "phase4_triage": triage,
                "independent_events": primary.get("independent_events"),
                "assets_with_events": primary.get("assets_with_events"),
                "excess_mean": primary.get("excess_mean"),
                "net_mean": primary.get("net_mean"),
                "positive_asset_share": primary.get("positive_asset_share"),
                "gate_checks": metrics.get("gate_checks", {}),
                "raw_p": p,
                "in_family": reason is None,
                "excluded_reason": reason,
            }
        )
    family = {m["strategy_id"]: m["raw_p"] for m in members if m["in_family"]}
    q = benjamini_hochberg(family)
    # Diagnostic only: BH as if every preregistered member had been tested and failed
    # (p = 1). Never used for statuses.
    q_all = benjamini_hochberg(
        {m["strategy_id"]: (m["raw_p"] if m["in_family"] else 1.0) for m in members}
    )
    for m in members:
        m["q"] = q.get(m["strategy_id"])
        m["q_if_all_preregistered_tested"] = q_all[m["strategy_id"]] if m["in_family"] else None
        m["batch_status"] = classify(m, m["q"], definition)
    counts = {
        "preregistered": len(members),
        "testable": len(family),
        "correction_family": len(family),
        "no_events": sum(m["phase4_triage"] == "NO_EVENTS" for m in members),
        "insufficient_events": sum(m["phase4_triage"] == "INSUFFICIENT_EVENTS" for m in members),
        "errored": sum(m["batch_status"] == "ERROR" for m in members),
        "phase4_interesting": sum(m["phase4_triage"] == "INTERESTING" for m in members),
        "fdr_significant": sum(
            m["q"] is not None and m["q"] <= definition.correction.q for m in members
        ),
        "fdr_survivors": sum(m["batch_status"] == "FDR_SURVIVOR" for m in members),
    }
    families = []
    for family_id in sorted({m["family_id"] for m in members}):
        group = [m for m in members if m["family_id"] == family_id]
        effects = np.array(
            [m["excess_mean"] for m in group if m["in_family"] and m["excess_mean"] is not None],
            dtype=float,
        )
        ps = [m["raw_p"] for m in group if m["in_family"]]
        qs = [m["q"] for m in group if m["q"] is not None]
        families.append(
            {
                "family_id": family_id,
                "variants": len(group),
                "testable": sum(m["in_family"] for m in group),
                "survivors": sum(m["batch_status"] == "FDR_SURVIVOR" for m in group),
                "best_raw_p": min(ps) if ps else None,
                "best_q": min(qs) if qs else None,
                "min_excess": _num(effects.min()) if len(effects) else None,
                "median_excess": _num(np.median(effects)) if len(effects) else None,
                "max_excess": _num(effects.max()) if len(effects) else None,
            }
        )
    return {
        "analysis_version": ANALYSIS_VERSION,
        "batch_id": definition.batch_id,
        "run_id": run["run_id"],
        "attempt": run["attempt"],
        "software_id": run["software_id"],
        "rerun_of": run.get("rerun_of"),
        "plan_id": definition.plan_id,
        "dataset_id": definition.dataset_id,
        "role": definition.role,
        "primary_horizon": definition.primary_horizon,
        "correction": definition.correction.model_dump(mode="python"),
        "survivor": definition.survivor.model_dump(mode="python"),
        "counts": counts,
        "members": members,
        "families": families,
    }


# --------------------------------------------------------------------------- inspection


def list_batches(ledger: Ledger) -> list[dict]:
    _require_tables(ledger)
    out = []
    for row in _rows(ledger, "SELECT batch_id, name, origin, frozen_at, payload FROM lab_batches "
                     "ORDER BY frozen_at, batch_id", []):  # fmt: skip
        payload = json.loads(row.pop("payload"))
        runs = _runs(ledger, row["batch_id"])
        row["members"] = len(payload["definition"]["members"])
        row["status"] = _status(runs)
        out.append(row)
    return out


def _status(runs: list[dict]) -> str:
    if not runs:
        return "FROZEN"
    last = runs[-1]
    if last["analysis_id"] is None:
        return "RUNNING"
    return "COMPLETED" if last["analysis_status"] == "completed" else "FAILED"


def inspect_batch(ledger: Ledger, batch_id: str) -> dict:
    batch = get_batch(ledger, batch_id)
    runs = _runs(ledger, batch_id)
    analyses = []
    for run in runs:
        if run["analysis_id"] is not None:
            row = _rows(ledger, "SELECT * FROM lab_batch_analyses WHERE run_id=?", [run["run_id"]])[
                0
            ]
            row["payload"] = json.loads(row["payload"])
            analyses.append(row)
    return {
        "batch_id": batch_id,
        "name": batch["name"],
        "origin": batch["origin"],
        "frozen_at": batch["frozen_at"],
        "status": _status(runs),
        "batch": batch["payload"],
        "runs": runs,
        "analyses": analyses,
    }
