"""Append-only Lab registration and lifecycle API over Prism's existing Store.

Each mutation commits before returning. Do not wrap calls in an outer transaction.
The API exposes no update/delete operation; a database owner can still change SQL data.
It records governance facts, not an evaluator or a guarantee against out-of-band research.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field

from market_signal.data.store import Store
from market_signal.models.domain import Timeframe, utcnow
from market_signal.research.lab.common import (
    LabModel,
    Name,
    PositiveInt,
    Symbol,
    Text,
    UTCDateTime,
    canonical_json,
    content_id,
    revalidate,
    strict_json,
)
from market_signal.research.lab.datasets import (
    DatasetCapture,
    DatasetManifest,
    snapshot_rows,
)
from market_signal.research.lab.policy import (
    AnyPlan,
    DatasetRole,
    PValue,
    ScreenPlan,
    parse_plan,
)
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.spec import Hypothesis, StrategyDefinition

ResultStatus = Literal[
    "succeeded", "rejected", "insufficient_data", "failed", "errored", "cancelled"
]


class LedgerError(ValueError):
    pass


class DuplicateExperiment(LedgerError):
    pass


class Submission(LabModel):
    submission_id: str
    received_at: UTCDateTime
    origin: Text
    family_id: Name
    hypothesis_id: str | None
    error: str | None

    @property
    def accepted(self) -> bool:
        return self.hypothesis_id is not None


class ExperimentRecord(LabModel):
    experiment_id: str  # unique immutable attempt, not the logical content ID
    logical_id: str
    attempt: PositiveInt
    strategy_id: str
    hypothesis_id: str
    submission_id: str
    plan_id: str
    dataset_id: str
    software_id: str
    family_id: Name
    batch_id: Name | None
    role: DatasetRole
    period_start: UTCDateTime
    period_end: UTCDateTime
    stage: Literal["event_study", "screen"]
    assets: tuple[Symbol, ...]
    timeframes: tuple[Timeframe, ...]
    origin: Text
    created_at: UTCDateTime
    rerun_of: str | None
    rerun_reason: Text | None


class ErrorInfo(LabModel):
    kind: Text
    message: Text


class _Registration(LabModel):
    role: DatasetRole
    assets: Annotated[tuple[Symbol, ...], Field(min_length=1)]
    origin: Text
    batch_id: Name | None = None
    rerun_of: Text | None = None
    rerun_reason: Text | None = None


class _Result(LabModel):
    status: ResultStatus
    verdict: Text | None = None
    p_values: tuple[PValue, ...] = ()
    error: ErrorInfo | None = None


def _new_id(prefix: str) -> str:
    return prefix + uuid4().hex


class Ledger:
    def __init__(self, store: Store):
        self.store = store
        # Verified snapshot rows keyed by the full series fingerprint. Blobs are content-
        # addressed and the API never rewrites them, so each is verified once per Ledger
        # instance instead of on every preregistration/start of a batch. Do not mutate.
        self._verified: dict[str, list[dict]] = {}
        if not store.con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='lab_experiments'"
        ).fetchone():
            raise LedgerError("Lab tables are absent; open Store writable once to apply migrations")

    def _one(self, sql: str, args: list) -> dict:
        cursor = self.store.con.execute(sql, args)
        names = [d[0] for d in cursor.description]
        row = cursor.fetchone()
        if row is None:
            raise LedgerError("record does not exist")
        return dict(zip(names, row, strict=True))

    def _put_payload(self, table: str, id_column: str, id_: str, payload: str) -> None:
        # Table/column identifiers are internal constants, never submitted values.
        row = self.store.con.execute(
            f"SELECT payload FROM {table} WHERE {id_column}=?", [id_]
        ).fetchone()
        if row:
            if canonical_json(json.loads(row[0])) != canonical_json(json.loads(payload)):
                raise LedgerError("identity already exists with different content")
            return
        self.store.con.execute(f"INSERT INTO {table} VALUES (?,?,?)", [id_, payload, utcnow()])

    def _put_software(self, software: SoftwareIdentity) -> None:
        data = software.model_dump(mode="python")
        data["packages"] = sorted(data["packages"])
        self._put_payload("lab_software", "software_id", software.software_id, canonical_json(data))

    def register_software(self, software: SoftwareIdentity) -> str:
        software = revalidate(software)
        with self.store.transaction():
            self._put_software(software)
        return software.software_id

    def submit(self, raw_json: str, *, family_id: str, origin: str) -> Submission:
        """Persist every submitted JSON document, including validation/lineage failures.

        Family and lineage are fixed when a strategy is first registered. Renaming a
        definition cannot place it in a fresh family or erase its ancestry.
        """
        receipt = Submission(
            submission_id=_new_id("submission_"),
            received_at=utcnow(),
            origin=origin,
            family_id=family_id,
            hypothesis_id=None,
            error=None,
        )
        family_id, origin = receipt.family_id, receipt.origin
        hypothesis_id, error = None, None
        with self.store.transaction():
            try:
                h = Hypothesis.model_validate(strict_json(raw_json))
                existing = self.store.con.execute(
                    "SELECT definition, family_id, parent_ids FROM lab_strategies WHERE strategy_id=?",
                    [h.strategy_id],
                ).fetchone()
                parents = sorted(h.parent_ids)
                if existing and (
                    existing[0] != h.definition.canonical_json()
                    or existing[1] != family_id
                    or json.loads(existing[2]) != parents
                ):
                    raise LedgerError(
                        "registered strategy definition, family and lineage are immutable"
                    )
                for parent in parents:
                    row = self.store.con.execute(
                        "SELECT family_id FROM lab_strategies WHERE strategy_id=?", [parent]
                    ).fetchone()
                    if row is None or row[0] != family_id:
                        raise LedgerError("parent must already be registered in the same family")
            except ValueError as exc:
                error = str(exc)
            else:
                if existing is None:
                    self.store.con.execute(
                        "INSERT INTO lab_strategies VALUES (?,?,?,?,?)",
                        [
                            h.strategy_id,
                            h.definition.canonical_json(),
                            family_id,
                            canonical_json(parents),
                            receipt.received_at,
                        ],
                    )
                document = h.model_dump(mode="python")
                document["definition"] = json.loads(h.definition.canonical_json())
                document["parent_ids"] = parents
                hypothesis_id = content_id("hypothesis_", document)
                payload = canonical_json(document)
                known = self.store.con.execute(
                    "SELECT payload FROM lab_hypotheses WHERE hypothesis_id=?", [hypothesis_id]
                ).fetchone()
                if known is None:
                    self.store.con.execute(
                        "INSERT INTO lab_hypotheses VALUES (?,?,?,?)",
                        [
                            hypothesis_id,
                            h.strategy_id,
                            payload,
                            receipt.received_at,
                        ],
                    )
                elif known[0] != payload:
                    raise LedgerError("hypothesis identity collision")
            self.store.con.execute(
                "INSERT INTO lab_submissions VALUES (?,?,?,?,?,?,?)",
                [
                    receipt.submission_id,
                    receipt.received_at,
                    origin,
                    family_id,
                    raw_json,
                    hypothesis_id,
                    error,
                ],
            )
        return Submission(
            **{**receipt.model_dump(), "hypothesis_id": hypothesis_id, "error": error}
        )

    def register_plan(self, plan: AnyPlan) -> str:
        plan = revalidate(plan)
        with self.store.transaction():
            row = self.store.con.execute(
                "SELECT plan_id FROM lab_plans WHERE name=? AND version=?",
                [plan.name, plan.version],
            ).fetchone()
            if row:
                if row[0] != plan.plan_id:
                    raise LedgerError("plan name/version is already frozen; declare a new version")
            else:
                self.store.con.execute(
                    "INSERT INTO lab_plans VALUES (?,?,?,?,?)",
                    [
                        plan.plan_id,
                        plan.name,
                        plan.version,
                        plan.canonical_json(),
                        utcnow(),
                    ],
                )
        return plan.plan_id

    def register_dataset(self, capture: DatasetCapture | DatasetManifest) -> str:
        """Store exact local input rows for strong fingerprints; never overwrite snapshots."""
        manifest = revalidate(capture.manifest if isinstance(capture, DatasetCapture) else capture)
        blobs = dict(capture.blobs) if isinstance(capture, DatasetCapture) else {}
        with self.store.transaction():
            for series in manifest.series:
                if series.strength != "content_sha256":
                    continue
                old = self.store.con.execute(
                    "SELECT payload FROM lab_snapshot_blobs WHERE sha256=?", [series.sha256]
                ).fetchone()
                data = (
                    blobs.get(series.sha256) if series.sha256 in blobs else old[0] if old else None
                )
                if data is None:
                    raise LedgerError(
                        "strong dataset requires a retained snapshot, not just a claimed hash"
                    )
                snapshot_rows(series, data)
                if old is None:
                    self.store.con.execute(
                        "INSERT INTO lab_snapshot_blobs VALUES (?,?)", [series.sha256, data]
                    )
                else:
                    snapshot_rows(series, old[0])
            self._put_payload(
                "lab_datasets", "dataset_id", manifest.dataset_id, manifest.canonical_json()
            )
            for digest in sorted({s.sha256 for s in manifest.series if s.sha256 is not None}):
                if not self.store.con.execute(
                    "SELECT 1 FROM lab_dataset_blobs WHERE dataset_id=? AND sha256=?",
                    [manifest.dataset_id, digest],
                ).fetchone():
                    self.store.con.execute(
                        "INSERT INTO lab_dataset_blobs VALUES (?,?)", [manifest.dataset_id, digest]
                    )
        return manifest.dataset_id

    def _verified_rows(self, series) -> list[dict]:
        # The key includes the full fingerprint: identical bytes under a different
        # selection/schema header must still be checked against that header.
        key = canonical_json(series.model_dump(mode="python"))
        if key not in self._verified:
            blob = self._one(
                "SELECT payload FROM lab_snapshot_blobs WHERE sha256=?", [series.sha256]
            )
            self._verified[key] = snapshot_rows(series, blob["payload"])
        return self._verified[key]

    def get_strategy(self, strategy_id: str) -> StrategyDefinition:
        row = self._one("SELECT definition FROM lab_strategies WHERE strategy_id=?", [strategy_id])
        definition = StrategyDefinition.model_validate_json(row["definition"])
        if definition.strategy_id != strategy_id:
            raise LedgerError("stored definition does not match its strategy ID")
        return definition

    def get_plan(self, plan_id: str) -> AnyPlan:
        row = self._one("SELECT payload FROM lab_plans WHERE plan_id=?", [plan_id])
        plan = parse_plan(row["payload"])
        if plan.plan_id != plan_id:
            raise LedgerError("stored plan does not match its plan ID")
        return plan

    def get_dataset(self, dataset_id: str) -> DatasetManifest:
        row = self._one("SELECT payload FROM lab_datasets WHERE dataset_id=?", [dataset_id])
        return DatasetManifest.model_validate_json(row["payload"])

    def read_dataset(self, dataset_id: str) -> tuple[list[dict], ...]:
        """Decode retained inputs. Use record_inspection for manual research access.

        Access to original market tables cannot be monitored by this API. This method
        does not assert that a holdout is untouched or start evaluation automatically.
        """
        out = []
        for series in self.get_dataset(dataset_id).series:
            if series.strength != "content_sha256":
                raise LedgerError("no retained content for weak fingerprint")
            out.append(self._verified_rows(series))
        return tuple(out)

    def preregister(
        self,
        submission_id: str,
        plan_id: str,
        dataset_id: str,
        *,
        role: DatasetRole,
        assets: tuple[str, ...],
        software: SoftwareIdentity,
        origin: str,
        batch_id: str | None = None,
        rerun_of: str | None = None,
        rerun_reason: str | None = None,
    ) -> ExperimentRecord:
        registration = _Registration(
            role=role,
            assets=assets,
            origin=origin,
            batch_id=batch_id,
            rerun_of=rerun_of,
            rerun_reason=rerun_reason,
        )
        role, rerun_of = registration.role, registration.rerun_of
        software = revalidate(software)
        if len(set(registration.assets)) != len(registration.assets):
            raise LedgerError("duplicate selected assets")
        assets = tuple(sorted(registration.assets))
        with self.store.transaction():
            submission = self._one(
                "SELECT * FROM lab_submissions WHERE submission_id=?", [submission_id]
            )
            if submission["hypothesis_id"] is None:
                raise LedgerError("invalid submission cannot be preregistered")
            hrow = self._one(
                "SELECT * FROM lab_hypotheses WHERE hypothesis_id=?", [submission["hypothesis_id"]]
            )
            hypothesis = Hypothesis.model_validate_json(hrow["payload"])
            plan, dataset = self.get_plan(plan_id), self.get_dataset(dataset_id)
            period = plan.period(role)
            definition = hypothesis.definition
            refs = [ref for c in definition.conditions for ref in c.references()]
            if definition.market != plan.market or definition.trigger_timeframe != plan.timeframe:
                raise LedgerError("strategy market/timeframe does not match plan")
            if any(ref.timeframe != plan.timeframe for ref in refs):
                raise LedgerError("initial event-study plan supports daily references only")
            if not set(assets) <= {c.symbol for c in plan.costs}:
                raise LedgerError("every selected asset needs a frozen cost assumption")
            # v2 screen datasets carry a fixed warmup region before the role period.
            data_start = plan.data_start(role) if isinstance(plan, ScreenPlan) else period.start
            if isinstance(plan, ScreenPlan) and len(assets) < plan.gates.min_assets_with_events:
                raise LedgerError("fewer assets than the plan's minimum assets with events")
            if dataset.strength != plan.required_fingerprint:
                raise LedgerError("preregistration requires strong retained dataset content")
            kinds = (
                ("perp_bars", "perp_funding")
                if plan.market == "perp"
                else ("bars", "corporate_actions")
            )
            expected = {(symbol, kind) for symbol in assets for kind in kinds}
            actual = [(s.selection.symbol, s.selection.kind) for s in dataset.series]
            if len(actual) != len(expected) or set(actual) != expected:
                raise LedgerError(
                    "dataset must contain exactly the selected assets and required input series"
                )
            for series in dataset.series:
                selection = series.selection
                if (
                    selection.source != plan.source
                    or selection.start != data_start
                    or selection.end != period.end
                ):
                    raise LedgerError(
                        "dataset source/window must match the selected plan role exactly"
                        + (
                            " (including the plan's warmup region)"
                            if data_start != period.start
                            else ""
                        )
                    )
                if selection.timeframe is not None and selection.timeframe != plan.timeframe:
                    raise LedgerError("dataset timeframe does not match plan")
                self._verified_rows(series)
            # Software is recorded per attempt, not part of the logical identity: a code
            # change must not let identical strategy/plan/data escape the duplicate guard.
            logical = {
                "identity_version": "1",
                "strategy_id": hypothesis.strategy_id,
                "plan_id": plan_id,
                "dataset_id": dataset_id,
                "role": role,
                "assets": assets,
                "timeframes": (plan.timeframe,),
                "stage": plan.stage,
            }
            logical_id = content_id("logical_", logical)
            prior = self.store.con.execute(
                "SELECT experiment_id, attempt FROM lab_experiments WHERE logical_id=? ORDER BY attempt DESC LIMIT 1",
                [logical_id],
            ).fetchone()
            if prior and (not rerun_of or not registration.rerun_reason):
                raise DuplicateExperiment(
                    f"logical experiment exists: {prior[0]}; an explicit rerun link and reason are required"
                )
            if rerun_of:
                previous = self.get_experiment(rerun_of)
                if previous.logical_id != logical_id:
                    raise LedgerError("rerun must use identical logical inputs")
            elif registration.rerun_reason:
                raise LedgerError("rerun_reason requires rerun_of")
            if not prior and rerun_of:
                raise LedgerError("cannot rerun a different logical experiment")
            self._put_software(software)
            experiment = ExperimentRecord(
                experiment_id=_new_id("experiment_"),
                logical_id=logical_id,
                attempt=prior[1] + 1 if prior else 1,
                strategy_id=hypothesis.strategy_id,
                hypothesis_id=submission["hypothesis_id"],
                submission_id=submission_id,
                plan_id=plan_id,
                dataset_id=dataset_id,
                software_id=software.software_id,
                family_id=submission["family_id"],
                batch_id=registration.batch_id,
                role=role,
                period_start=period.start,
                period_end=period.end,
                stage=plan.stage,
                assets=assets,
                timeframes=(plan.timeframe,),
                origin=registration.origin,
                created_at=utcnow(),
                rerun_of=rerun_of,
                rerun_reason=registration.rerun_reason,
            )
            row = experiment.model_dump(mode="python")
            for key in ("assets", "timeframes"):
                row[key] = canonical_json(row[key])
            self.store.con.execute(
                f"INSERT INTO lab_experiments ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                list(row.values()),
            )
        return experiment  # transaction has committed before an evaluator could start

    def get_experiment(self, experiment_id: str) -> ExperimentRecord:
        row = self._one("SELECT * FROM lab_experiments WHERE experiment_id=?", [experiment_id])
        for key in ("assets", "timeframes"):
            row[key] = json.loads(row[key])
        return ExperimentRecord.model_validate(row)

    def start(self, experiment_id: str, *, software: SoftwareIdentity) -> datetime:
        """Commit a start and conservative data-exposure record before evaluation."""
        software = revalidate(software)
        with self.store.transaction():
            experiment = self.get_experiment(experiment_id)
            if software.software_id != experiment.software_id:
                raise LedgerError(
                    "software differs from preregistration; register a new experiment"
                )
            if self.store.con.execute(
                "SELECT 1 FROM lab_starts WHERE experiment_id=?", [experiment_id]
            ).fetchone():
                raise LedgerError("attempt already started; use an explicit rerun")
            self.read_dataset(
                experiment.dataset_id
            )  # validate retained inputs before recording start
            now = utcnow()
            if now < experiment.created_at:
                raise LedgerError("clock precedes preregistration")
            self.store.con.execute("INSERT INTO lab_starts VALUES (?,?)", [experiment_id, now])
            self.store.con.execute(
                "INSERT INTO lab_inspections VALUES (?,?,?,?,?)",
                [
                    _new_id("inspection_"),
                    experiment_id,
                    now,
                    "evaluation_started",
                    "Conservative exposure: evaluation may now inspect all registered inputs",
                ],
            )
        return now

    def record_result(
        self,
        experiment_id: str,
        *,
        status: ResultStatus,
        metrics: dict | None = None,
        verdict: str | None = None,
        p_values: tuple[PValue, ...] = (),
        error: ErrorInfo | None = None,
    ) -> str:
        result = _Result.model_validate(
            {
                "status": status,
                "verdict": verdict,
                "p_values": [p.model_dump(mode="python") for p in p_values],
                "error": error.model_dump(mode="python") if error else None,
            }
        )
        if result.status in ("failed", "errored", "cancelled") and result.error is None:
            raise LedgerError("failure/error/cancellation requires error information")
        if result.status == "succeeded" and result.error is not None:
            raise LedgerError("successful result cannot contain an error")
        endpoints = [(p.test, p.endpoint) for p in result.p_values]
        if len(endpoints) != len(set(endpoints)):
            raise LedgerError("duplicate statistical endpoints")
        if metrics is not None and not isinstance(metrics, dict):
            raise LedgerError("metrics must be a JSON object")
        metrics_json = canonical_json(metrics if metrics is not None else {})
        result_id = _new_id("result_")
        with self.store.transaction():
            start = self._one(
                "SELECT started_at FROM lab_starts WHERE experiment_id=?", [experiment_id]
            )
            if self.store.con.execute(
                "SELECT 1 FROM lab_results WHERE experiment_id=?", [experiment_id]
            ).fetchone():
                raise LedgerError("terminal result is immutable; register a new attempt")
            now = utcnow()
            if now < start["started_at"]:
                raise LedgerError("completion clock precedes start")
            self.store.con.execute(
                "INSERT INTO lab_results VALUES (?,?,?,?,?,?,?,?)",
                [
                    result_id,
                    experiment_id,
                    now,
                    result.status,
                    result.verdict,
                    metrics_json,
                    canonical_json([p.model_dump(mode="python") for p in result.p_values]),
                    canonical_json(result.error.model_dump(mode="python"))
                    if result.error
                    else None,
                ],
            )
        return result_id

    def record_inspection(self, experiment_id: str, *, reason: str) -> str:
        """Record manual inspection without claiming it was a completed evaluation."""
        info = ErrorInfo(kind="manual_inspection", message=reason)
        with self.store.transaction():
            self.get_experiment(experiment_id)
            id_ = _new_id("inspection_")
            self.store.con.execute(
                "INSERT INTO lab_inspections VALUES (?,?,?,?,?)",
                [
                    id_,
                    experiment_id,
                    utcnow(),
                    info.kind,
                    info.message,
                ],
            )
        return id_

    def inspect_experiment(self, experiment_id: str) -> dict:
        experiment = self.get_experiment(experiment_id)
        starts = self._rows("SELECT * FROM lab_starts WHERE experiment_id=?", [experiment_id])
        results = self._rows("SELECT * FROM lab_results WHERE experiment_id=?", [experiment_id])
        for result in results:
            for key in ("metrics", "p_values", "error"):
                result[key] = json.loads(result[key]) if result[key] is not None else None
        return {
            "experiment": experiment.model_dump(mode="python"),
            "status": results[0]["status"] if results else "started" if starts else "preregistered",
            "start": starts[0] if starts else None,
            "results": results,
            "inspections": self._rows(
                "SELECT * FROM lab_inspections WHERE experiment_id=? ORDER BY recorded_at, inspection_id",
                [experiment_id],
            ),
        }

    def _rows(self, sql: str, args: list) -> list[dict]:
        cursor = self.store.con.execute(sql, args)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def list_strategies(self) -> list[dict]:
        return self._rows(
            "SELECT strategy_id, family_id, parent_ids, recorded_at FROM lab_strategies ORDER BY recorded_at, strategy_id",
            [],
        )

    def list_experiments(self) -> list[dict]:
        return self._rows(
            "SELECT e.experiment_id, e.logical_id, e.attempt, e.strategy_id, e.family_id, e.batch_id, e.role, e.created_at, "
            "coalesce(r.status, CASE WHEN s.experiment_id IS NULL THEN 'preregistered' ELSE 'started' END) AS status "
            "FROM lab_experiments e LEFT JOIN lab_starts s USING (experiment_id) "
            "LEFT JOIN lab_results r USING (experiment_id) ORDER BY e.created_at, e.experiment_id",
            [],
        )

    def exposures(
        self,
        *,
        start: datetime,
        end: datetime,
        family_id: str | None = None,
        strategy_id: str | None = None,
    ) -> list[dict]:
        """All recorded inspections with overlapping role windows, across dataset revisions.

        Empty output means no *recorded* exposure, never proof of an untouched holdout.
        Family includes descendants because registration fixes ancestry within a family.
        """
        from market_signal.research.lab.policy import Period

        window = Period(role="discovery", start=start, end=end)
        sql = (
            "SELECT i.*, e.strategy_id, e.family_id, e.dataset_id, e.plan_id, e.role, e.period_start, e.period_end "
            "FROM lab_inspections i JOIN lab_experiments e USING (experiment_id) "
            "WHERE e.period_start < ? AND e.period_end > ?"
        )
        args = [window.end, window.start]
        if family_id is not None:
            sql += " AND e.family_id=?"
            args.append(family_id)
        if strategy_id is not None:
            sql += " AND e.strategy_id=?"
            args.append(strategy_id)
        return self._rows(sql + " ORDER BY i.recorded_at, i.inspection_id", args)
