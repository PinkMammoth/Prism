"""Full research and independent validation of explicitly registered Lab strategies (Phase 9).

Discovery and validation stay separate:

- **Full research** (stage ``full_research``) runs Prism's deeper research methods through
  ``adapter.py`` on the SAME discovery data the strategy came from. It answers "what do we
  know from discovery, looked at more carefully?" and never claims independence.
- **Validation** (stage ``validation``) evaluates the frozen strategy on a period that an
  evaluation plan explicitly reserves as ``validation``. It answers the narrower question
  "did the expected-direction historical effect persist on unseen data?".

Rules:

- A registration is explicit (never automatic) and freezes the strategy, the historical
  profile and its tier, the source batch/experiment, the validation plan/period, the
  policies and the semantic versions. Its ID is the content hash; re-registering identical
  content is refused. Strategy definitions are immutable by ID, so a "nearby better"
  variant is a different strategy and needs its own lineage and registration.
- The validation plan must equal the source plan except for its name, version and extra
  reserved periods. The validation window (including its warmup region) may not overlap
  the source plan's data region or any ``final_holdout`` period, which is never read.
- Validation is evaluated once the reserved period is COMPLETE. Before that, or when the
  bars available cannot possibly yield an adequate sample, the attempt is recorded as
  ``VALIDATION_INSUFFICIENT`` without reading any validation-window outcome, so no
  exposure is consumed. Otherwise the run uses the Phase 2 lifecycle (preregister ->
  start, which permanently records the exposure in ``lab_inspections``) and the Phase 4
  ``run_screen`` on the ``validation`` role; Phase 9 only classifies the stored result.
- Prior recorded exposure of the strategy or its family to the validation window is
  reported. Exposure that predates the registration means independence is compromised;
  later prospective observation of the same days (Phase 8) is reported as concurrent and
  never pooled with validation statistics.
- Results are append-only: one terminal result per run; reruns are explicit, linked and
  never overwrite earlier results.
- Evidence extension writes NEW schema-4 profiles that cite the historical profile and the
  full_research/validation results. VALIDATED stays unreachable.
"""

from __future__ import annotations

import json
import math
import traceback
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import uuid4

import pandas as pd
from pydantic import Field, model_validator

from market_signal.models.domain import utcnow
from market_signal.research.lab.adapter import (
    ADAPTER_VERSION,
    FULL_RESEARCH_POLICIES,
    full_research,
    primary_row,
)
from market_signal.research.lab.common import (
    LabModel,
    Name,
    Number,
    PositiveInt,
    Probability,
    Symbol,
    canonical_json,
    content_id,
)
from market_signal.research.lab.compiler import COMPILER_VERSION, Snapshot
from market_signal.research.lab.datasets import SeriesSelection, capture_dataset
from market_signal.research.lab.evidence import (
    EvidencePolicy,
    EvidenceProfile,
    EvidenceSource,
    asset_summary,
    neighbourhood_summary,
    record_profiles,
)
from market_signal.research.lab.ledger import Ledger, LedgerError, ordered_now
from market_signal.research.lab.policy import Period, ScreenPlan
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.screen import SCREEN_VERSION, run_screen
from market_signal.research.lab.vocabulary import VOCABULARY_VERSION

VALIDATION_VERSION = "lab_validation_v1"
EXTENSION_BUILDER_VERSION = "lab_evidence_extension_builder_v1"
ELIGIBLE_TIERS = ("EXPLORATORY", "RESEARCH_SUPPORTED")
FULL_STATUSES = ("FULL_RESEARCH_CONSISTENT", "FULL_RESEARCH_MIXED", "FULL_RESEARCH_INCONSISTENT",
                 "FULL_RESEARCH_INSUFFICIENT", "FULL_RESEARCH_ERROR")  # fmt: skip
VALIDATION_STATUSES = ("VALIDATION_SUPPORTIVE", "VALIDATION_MIXED", "VALIDATION_ADVERSE",
                       "VALIDATION_INSUFFICIENT", "VALIDATION_ERROR")  # fmt: skip
EXTENSION_LIMITATIONS = (
    "Full research re-examines the discovery data; it is not independent evidence.",
    "Validation tests the frozen historical hypothesis on a reserved period; it does not "
    "optimise the strategy.",
    "Prospective forward evidence and historical validation are separate evidence sources "
    "and are never pooled as if they were the same experiment.",
)


class ResearchError(LedgerError):
    pass


def semantics() -> dict[str, str]:
    """Versions whose change would make full-research/validation results incomparable."""
    return {
        "screen_version": SCREEN_VERSION,
        "compiler_version": COMPILER_VERSION,
        "vocabulary_version": VOCABULARY_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "validation_version": VALIDATION_VERSION,
    }


# --------------------------------------------------------------------------- policies


class ValidationPolicy(LabModel):
    """Versioned validation sample rules and descriptive statuses (never action decisions).

    Sample thresholds equal the discovery evidence policy's: they are not loosened to fit a
    short validation period. p-values are recorded but never decide a status.
    """

    name: Name = "lab_validation_policy"
    version: PositiveInt = 1
    min_independent_events: PositiveInt = 30
    min_assets_with_events: PositiveInt = 3
    min_completion_share: Probability = 0.8  # evaluable / raw primary-horizon signals
    min_eligible_share: Probability = 0.8  # eligible in-window bars / in-window bars
    supportive_min_positive_asset_share: Probability = 0.6
    supportive_min_effect: Number = 0.0
    adverse_max_effect: Number = 0.0
    adverse_max_positive_asset_share: Probability = 0.5
    neighbour_checks: bool = True

    @property
    def policy_id(self) -> str:
        return content_id("valpolicy_", self.model_dump(mode="python"))


class ExtensionPolicy(LabModel):
    """How full_research/validation sources may change a historical tier (conservative).

    - Full research alone never changes a tier: it re-reads the discovery data.
    - EXPLORATORY -> RESEARCH_SUPPORTED only with VALIDATION_SUPPORTIVE on a first look
      whose independence is intact on record, AND FULL_RESEARCH_CONSISTENT.
    - VALIDATION_ADVERSE caps EXPLORATORY/RESEARCH_SUPPORTED at INCONCLUSIVE.
    - VALIDATED is unreachable: it is reserved for a future standard that includes
      prospective forward and final-holdout evidence.
    """

    name: Name = "lab_evidence_extension_policy"
    version: PositiveInt = 1
    promote_from: Literal["EXPLORATORY"] = "EXPLORATORY"
    promote_to: Literal["RESEARCH_SUPPORTED"] = "RESEARCH_SUPPORTED"
    promotion_requires: Literal[
        "supportive_independent_first_look_and_consistent_full_research"
    ] = "supportive_independent_first_look_and_consistent_full_research"
    adverse_caps_at: Literal["INCONCLUSIVE"] = "INCONCLUSIVE"
    validated_reachable: Literal[False] = False

    @property
    def policy_id(self) -> str:
        return content_id("extpolicy_", self.model_dump(mode="python"))


VALIDATION_POLICIES = {1: ValidationPolicy()}
EXTENSION_POLICIES = {1: ExtensionPolicy()}


# --------------------------------------------------------------------------- registration


class ResearchRegistration(LabModel):
    """Frozen full-research/validation registration; any change is a new registration."""

    schema_version: Literal["1"] = "1"
    label: Name | None = None
    strategy_id: str
    strategy_name: str
    source_profile_id: str
    historical_tier: Literal["EXPLORATORY", "RESEARCH_SUPPORTED"]
    evidence_policy_id: str
    ledger_family: str
    family: str | None
    family_version: int | None
    params: dict | None
    market: Literal["perp"]
    side: Literal["long", "short"]
    source_plan_id: str
    source_dataset_id: str
    source_experiment_id: str
    source_result_id: str
    source_role: Literal["discovery", "development"]
    batch_id: str | None
    batch_run_id: str | None
    analysis_id: str | None
    assets: Annotated[tuple[Symbol, ...], Field(min_length=1)]
    neighbour_strategy_ids: tuple[str, ...]
    validation_plan_id: str
    validation_period: Period
    warmup_days: Annotated[int, Field(strict=True, ge=0, le=5000)]
    full_research_policy_id: str
    validation_policy_id: str
    extension_policy_id: str
    semantics: dict[str, str]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.validation_period.role != "validation":
            raise ValueError("the validation period must be a plan-reserved validation role")
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("duplicate assets")
        if self.strategy_id in self.neighbour_strategy_ids:
            raise ValueError("a strategy is not its own neighbour")
        return self

    @property
    def registration_id(self) -> str:
        return content_id("research_", self.model_dump(mode="python"))

    @property
    def data_start(self) -> datetime:
        return self.validation_period.start - timedelta(days=self.warmup_days)


def _ts(value) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _rows(ledger: Ledger, sql: str, args: list) -> list[dict]:
    cursor = ledger.store.con.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def _require_tables(ledger: Ledger) -> None:
    if not ledger.store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='lab_research_registrations'"
    ).fetchone():
        raise ResearchError("Phase 9 tables are absent; open Store writable once to migrate")


def _profile(ledger: Ledger, profile_id: str) -> EvidenceProfile:
    row = ledger.store.con.execute(
        "SELECT payload FROM lab_evidence_profiles WHERE profile_id=?", [profile_id]
    ).fetchone()
    if row is None:
        raise ResearchError(f"unknown evidence profile {profile_id}")
    profile = EvidenceProfile.model_validate_json(row[0])
    if profile.profile_id != profile_id:
        raise ResearchError("stored profile does not match its ID")
    return profile


def _evidence_policy(ledger: Ledger, policy_id: str) -> EvidencePolicy:
    row = ledger.store.con.execute(
        "SELECT payload FROM lab_evidence_policies WHERE policy_id=?", [policy_id]
    ).fetchone()
    if row is None:
        raise ResearchError(f"unknown evidence policy {policy_id}")
    return EvidencePolicy.model_validate_json(row[0])


_PLAN_FIELDS_ALLOWED_TO_DIFFER = {"name", "version", "periods"}


def check_plan_compatibility(source: ScreenPlan, validation: ScreenPlan) -> None:
    """The validation plan = the source plan + extra reserved periods, nothing else."""
    a, b = source.model_dump(mode="python"), validation.model_dump(mode="python")
    differ = sorted(k for k in a if k not in _PLAN_FIELDS_ALLOWED_TO_DIFFER and a[k] != b[k])
    if differ:
        raise ResearchError(
            f"validation plan differs from the source plan in {differ}; only name, version "
            "and additional reserved periods may differ"
        )
    reserved = {canonical_json(p.model_dump(mode="python")) for p in validation.periods}
    missing = [p.role for p in source.periods
               if canonical_json(p.model_dump(mode="python")) not in reserved]  # fmt: skip
    if missing:
        raise ResearchError(f"validation plan must keep the source plan's periods {missing}")


def _overlaps(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def build_registration(
    ledger: Ledger, profile_id: str, validation_plan_id: str, *, label: str | None = None
) -> ResearchRegistration:
    """Pure read: the frozen registration a ``register`` call would record (all checks)."""
    from market_signal.research.lab.evidence import gather

    profile = _profile(ledger, profile_id)
    if profile.profile_schema not in ("1", "2"):
        raise ResearchError("register from a historical (schema 1/2) profile, not an extension")
    if profile.tier not in ELIGIBLE_TIERS:
        raise ResearchError(
            f"profile tier {profile.tier} is not eligible; full research starts from "
            f"{' or '.join(ELIGIBLE_TIERS)} profiles (explicitly selected)"
        )
    screen_src = next((s for s in profile.sources if s.stage == "fast_screen"), None)
    fdr = next((s for s in profile.sources if s.stage == "batch_fdr"), None)
    if screen_src is None:
        raise ResearchError("profile cites no fast_screen source")
    source_plan = ledger.get_plan(screen_src.records["plan_id"])
    plan = ledger.get_plan(validation_plan_id)
    if not isinstance(source_plan, ScreenPlan) or not isinstance(plan, ScreenPlan):
        raise ResearchError("full research and validation need v2 screen plans")
    if source_plan.market != "perp" or source_plan.funding is None:
        raise ResearchError("Phase 9 supports daily perp strategies screened under a v2 plan")
    check_plan_compatibility(source_plan, plan)
    try:
        period = plan.period("validation")
    except ValueError:
        raise ResearchError(
            "the validation plan reserves no `validation` period; validation never borrows "
            "discovery, development or final-holdout data"
        ) from None
    experiment = ledger.get_experiment(screen_src.records["experiment_id"])
    if experiment.role not in ("discovery", "development"):
        raise ResearchError("the historical evidence must come from discovery/development data")
    src_period = source_plan.period(experiment.role)
    src_start = src_period.start - timedelta(days=source_plan.warmup_days)
    data_start = period.start - timedelta(days=plan.warmup_days)
    if _overlaps(period.start, period.end, src_start, src_period.end):
        raise ResearchError(
            "the validation period overlaps the source data region (warmup included): it "
            "is not untouched by the discovery experiment"
        )
    for p in plan.periods:
        if p.role == "final_holdout" and _overlaps(data_start, period.end, p.start, p.end):
            raise ResearchError("the validation data region would read the final holdout")
    strategy_id = profile.subject["strategy_id"]
    if experiment.strategy_id != strategy_id:
        raise ResearchError("profile and source experiment cite different strategies")
    definition = ledger.get_strategy(strategy_id)  # immutable by ID: the frozen strategy
    neighbours = tuple(sorted(profile.neighbourhood.get("neighbour_strategy_ids") or ()))
    if fdr is not None:  # neighbours must be batch members with recorded results
        records, _ = gather(ledger, fdr.records["batch_id"], fdr.records["run_id"])
        members = {r["strategy_id"] for r in records}
        if strategy_id not in members or not set(neighbours) <= members:
            raise ResearchError("strategy and neighbours must be members of the source batch")
    subject = profile.subject
    return ResearchRegistration(
        label=label,
        strategy_id=strategy_id,
        strategy_name=subject["name"],
        source_profile_id=profile_id,
        historical_tier=profile.tier,
        evidence_policy_id=profile.policy_id,
        ledger_family=subject["ledger_family"],
        family=subject.get("family"),
        family_version=subject.get("family_version"),
        params=subject.get("params"),
        market="perp",
        side=definition.side,
        source_plan_id=source_plan.plan_id,
        source_dataset_id=screen_src.records["dataset_id"],
        source_experiment_id=experiment.experiment_id,
        source_result_id=screen_src.records["result_id"],
        source_role=experiment.role,
        batch_id=fdr.records.get("batch_id") if fdr else None,
        batch_run_id=fdr.records.get("run_id") if fdr else None,
        analysis_id=fdr.records.get("analysis_id") if fdr else None,
        assets=tuple(sorted(experiment.assets)),
        neighbour_strategy_ids=neighbours,
        validation_plan_id=plan.plan_id,
        validation_period=period,
        warmup_days=plan.warmup_days,
        full_research_policy_id=FULL_RESEARCH_POLICIES[max(FULL_RESEARCH_POLICIES)].policy_id,
        validation_policy_id=VALIDATION_POLICIES[max(VALIDATION_POLICIES)].policy_id,
        extension_policy_id=EXTENSION_POLICIES[max(EXTENSION_POLICIES)].policy_id,
        semantics=semantics(),
    )


def _policy_by_id(table: dict, policy_id: str):
    for p in table.values():
        if p.policy_id == policy_id:
            return p
    raise ResearchError(f"policy {policy_id} is not a released version in this software")


def independence(
    ledger: Ledger,
    reg: ResearchRegistration,
    *,
    registered_at: datetime | None = None,
    own_experiments: tuple[str, ...] = (),
) -> dict:
    """Recorded exposure of the strategy/family to the validation window.

    Empty means no *recorded* exposure, never proof that the data are untouched (direct
    market-table access cannot be observed). Exposure before the registration compromises
    independence; prospective observation afterwards is concurrent and reported only.
    """
    p = reg.validation_period
    cutoff = _ts(registered_at) if registered_at is not None else None
    rows = ledger.exposures(start=p.start, end=p.end, family_id=reg.ledger_family)
    lab = {"strategy_before": [], "family_before": [], "after": [], "own": []}
    for r in rows:
        item = {"inspection_id": r["inspection_id"], "experiment_id": r["experiment_id"],
                "strategy_id": r["strategy_id"], "role": r["role"], "kind": r["kind"],
                "recorded_at": _ts(r["recorded_at"]).isoformat()}  # fmt: skip
        if r["experiment_id"] in own_experiments:
            lab["own"].append(item)
        elif cutoff is not None and _ts(r["recorded_at"]) >= cutoff:
            lab["after"].append(item)
        elif r["strategy_id"] == reg.strategy_id:
            lab["strategy_before"].append(item)
        else:
            lab["family_before"].append(item)
    fwd = {"evaluations": 0, "signals": 0, "outcomes_before_registration": 0}
    if ledger.store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='lab_forward_evaluations'"
    ).fetchone():
        ev = ledger.store.con.execute(
            "SELECT count(*), coalesce(sum(CASE WHEN e.fired THEN 1 ELSE 0 END), 0), "
            "min(e.evaluated_at) FROM lab_forward_evaluations e JOIN lab_forward_trackings t "
            "USING (tracking_id) WHERE t.strategy_id=? AND e.bar_close >= ? AND e.bar_close < ?",
            [reg.strategy_id, p.start, p.end],
        ).fetchone()
        early = (
            ledger.store.con.execute(
                "SELECT count(*) FROM lab_forward_outcomes o JOIN lab_forward_evaluations e "
                "USING (evaluation_id) JOIN lab_forward_trackings t USING (tracking_id) "
                "WHERE t.strategy_id=? AND e.bar_close >= ? AND e.bar_close < ? "
                "AND o.recorded_at < ?",
                [reg.strategy_id, p.start, p.end, cutoff.to_pydatetime()],
            ).fetchone()[0]
            if cutoff is not None
            else 0
        )
        fwd = {"evaluations": int(ev[0]), "signals": int(ev[1]),
               "first_evaluated_at": _ts(ev[2]).isoformat() if ev[2] is not None else None,
               "outcomes_before_registration": int(early)}  # fmt: skip
    if lab["strategy_before"] or fwd["outcomes_before_registration"]:
        label = "compromised"
    elif lab["family_before"]:
        label = "family_exposed"
    else:
        label = "untouched_on_record"
    return {
        "label": label,
        "window": [p.start.isoformat(), p.end.isoformat()],
        "lab_exposures": lab,
        "forward_observations": fwd,
        "concurrent_prospective_tracking": fwd["evaluations"] > 0,
        "note": "recorded exposure only; prospective forward days inside the window are the "
        "same market days as validation and are never pooled with it",
    }


def register(
    ledger: Ledger,
    profile_id: str,
    validation_plan_id: str,
    *,
    reason: str,
    origin: str,
    software: SoftwareIdentity,
    label: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Freeze a registration. Explicit only: nothing is registered automatically."""
    _require_tables(ledger)
    if not reason or not reason.strip():
        raise ResearchError("a registration needs a recorded reason")
    reg = build_registration(ledger, profile_id, validation_plan_id, label=label)
    now = _ts(now or utcnow())
    existing = ledger.store.con.execute(
        "SELECT registered_at FROM lab_research_registrations WHERE registration_id=?",
        [reg.registration_id],
    ).fetchone()
    if existing:
        raise ResearchError(
            f"{reg.registration_id} is already registered (since {_ts(existing[0]).isoformat()});"
            " a different configuration or --label is a new registration"
        )
    indep = independence(ledger, reg, registered_at=now)
    out = {
        "registration_id": reg.registration_id,
        "registered_at": now.isoformat(),
        "definition": json.loads(canonical_json(reg.model_dump(mode="python"))),
        "independence": indep,
        "dry_run": dry_run,
        "note": "research only: registration grants no trading, alert or approval status",
    }
    if dry_run:
        return out
    software_id = ledger.register_software(software)
    with ledger.store.transaction():
        for table in (FULL_RESEARCH_POLICIES, VALIDATION_POLICIES, EXTENSION_POLICIES):
            p = table[max(table)]
            if not ledger.store.con.execute(
                "SELECT 1 FROM lab_research_policies WHERE policy_id=?", [p.policy_id]
            ).fetchone():
                ledger.store.con.execute(
                    "INSERT INTO lab_research_policies VALUES (?,?,?)",
                    [p.policy_id, canonical_json(p.model_dump(mode="python")), utcnow()],
                )
        ledger.store.con.execute(
            "INSERT INTO lab_research_registrations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [reg.registration_id, reg.strategy_id, profile_id, reg.historical_tier,
             reg.validation_plan_id, now.to_pydatetime(), reason.strip(), origin, software_id,
             canonical_json(reg.model_dump(mode="python")), canonical_json(indep)],
        )  # fmt: skip
    return out


def get_registration(ledger: Ledger, registration_id: str) -> tuple[ResearchRegistration, dict]:
    _require_tables(ledger)
    rows = _rows(
        ledger,
        "SELECT * FROM lab_research_registrations WHERE registration_id=?",
        [registration_id],
    )
    if not rows:
        raise ResearchError(f"unknown registration {registration_id}")
    row = rows[0]
    reg = ResearchRegistration.model_validate_json(row["definition"])
    if reg.registration_id != registration_id:
        raise ResearchError("stored registration does not match its ID")
    row["independence"] = json.loads(row["independence"])
    return reg, row


def list_registrations(ledger: Ledger) -> list[dict]:
    _require_tables(ledger)
    out = []
    for row in _rows(
        ledger,
        "SELECT * FROM lab_research_registrations ORDER BY registered_at, registration_id",
        [],
    ):
        reg = ResearchRegistration.model_validate_json(row["definition"])
        latest = {}
        for stage in ("full_research", "validation"):
            r = _latest_result(ledger, row["registration_id"], stage)
            latest[stage] = r["status"] if r else None
        out.append({
            "registration_id": row["registration_id"], "strategy": reg.strategy_name,
            "strategy_id": reg.strategy_id, "historical_tier": reg.historical_tier,
            "validation_window": [reg.validation_period.start, reg.validation_period.end],
            "registered_at": row["registered_at"], "latest": latest,
        })  # fmt: skip
    return out


# --------------------------------------------------------------------------- runs


def _runs(ledger: Ledger, registration_id: str, stage: str) -> list[dict]:
    rows = _rows(
        ledger,
        "SELECT r.*, x.result_id, x.status, x.completed_at FROM lab_research_runs r "
        "LEFT JOIN lab_research_results x USING (run_id) WHERE r.registration_id=? AND r.stage=? "
        "ORDER BY r.attempt",
        [registration_id, stage],
    )
    return rows


def _latest_result(ledger: Ledger, registration_id: str, stage: str) -> dict | None:
    done = [r for r in _runs(ledger, registration_id, stage) if r["result_id"]]
    if not done:
        return None
    row = done[-1]
    payload = ledger.store.con.execute(
        "SELECT payload FROM lab_research_results WHERE result_id=?", [row["result_id"]]
    ).fetchone()[0]
    return {**row, "payload": json.loads(payload)}


def _result(ledger: Ledger, result_id: str) -> dict:
    rows = _rows(
        ledger,
        "SELECT x.*, r.registration_id, r.stage, r.attempt, r.experiment_id FROM "
        "lab_research_results x JOIN lab_research_runs r USING (run_id) WHERE x.result_id=?",
        [result_id],
    )
    if not rows:
        raise ResearchError(f"unknown research result {result_id}")
    return {**rows[0], "payload": json.loads(rows[0]["payload"])}


def _open_run(
    ledger: Ledger,
    registration_id: str,
    stage: str,
    software: SoftwareIdentity,
    *,
    rerun_of: str | None,
    rerun_reason: str | None,
    evaluated_before: bool,
    experiment_id: str | None = None,
) -> tuple[str, int]:
    """Append a run row. A new attempt after an evaluated one is an explicit rerun."""
    prior = _runs(ledger, registration_id, stage)
    if evaluated_before and not (rerun_of and rerun_reason and rerun_reason.strip()):
        raise ResearchError(
            f"{stage} was already evaluated for this registration "
            f"({prior[-1]['run_id']}); a rerun needs --rerun-of and --rerun-reason and never "
            "replaces the earlier result"
        )
    if rerun_of is not None and rerun_of not in {r["run_id"] for r in prior}:
        raise ResearchError("rerun_of must be an earlier run of the same registration and stage")
    if rerun_reason and not rerun_of:
        raise ResearchError("rerun_reason requires rerun_of")
    software_id = ledger.register_software(software)
    run_id = "researchrun_" + uuid4().hex
    attempt = (prior[-1]["attempt"] + 1) if prior else 1
    with ledger.store.transaction():
        ledger.store.con.execute(
            "INSERT INTO lab_research_runs VALUES (?,?,?,?,?,?,?,?,?)",
            [run_id, registration_id, stage, attempt, software_id, utcnow(), experiment_id,
             rerun_of, rerun_reason.strip() if rerun_reason else None],
        )  # fmt: skip
    return run_id, attempt


def _close_run(ledger: Ledger, run_id: str, status: str, payload: dict) -> str:
    allowed = FULL_STATUSES + VALIDATION_STATUSES
    if status not in allowed:
        raise ResearchError(f"unknown status {status}")
    result_id = "researchresult_" + uuid4().hex
    with ledger.store.transaction():
        if ledger.store.con.execute(
            "SELECT 1 FROM lab_research_results WHERE run_id=?", [run_id]
        ).fetchone():
            raise ResearchError("terminal result is immutable; start an explicit rerun")
        started = ledger.store.con.execute(
            "SELECT started_at FROM lab_research_runs WHERE run_id=?", [run_id]
        ).fetchone()[0]
        ledger.store.con.execute(
            "INSERT INTO lab_research_results VALUES (?,?,?,?,?)",
            [result_id, run_id, ordered_now(started), status, canonical_json(payload)],
        )
    return result_id


def _error_payload(exc: Exception) -> dict:
    tb = "".join(traceback.format_exception(exc, limit=-4)).strip()
    return {"error": {"kind": type(exc).__name__, "message": f"{exc}\n{tb}"[:9000]}}


def _batch_records(ledger: Ledger, reg: ResearchRegistration) -> dict[str, dict]:
    from market_signal.research.lab.evidence import gather

    if reg.batch_id is None:
        exp = ledger.get_experiment(reg.source_experiment_id)
        result = ledger.inspect_experiment(exp.experiment_id)["results"]
        return {reg.strategy_id: {"strategy_id": reg.strategy_id, "name": reg.strategy_name,
                                  "lineage": None, "metrics": result[0]["metrics"],
                                  "family_id": reg.ledger_family,
                                  "experiment_id": exp.experiment_id}}  # fmt: skip
    records, _ = gather(ledger, reg.batch_id, reg.batch_run_id)
    return {r["strategy_id"]: r for r in records}


def _member(ledger: Ledger, rec: dict) -> dict:
    return {
        "strategy_id": rec["strategy_id"],
        "name": rec.get("name"),
        "lineage": rec.get("lineage"),
        "family_id": rec.get("family_id"),
        "definition": ledger.get_strategy(rec["strategy_id"]),
        "stored_metrics": rec.get("metrics") or {},
    }


def run_full_research(
    ledger: Ledger,
    registration_id: str,
    *,
    software: SoftwareIdentity,
    rerun_of: str | None = None,
    rerun_reason: str | None = None,
) -> dict:
    """Stage A on the source discovery data. Every post-start failure is recorded."""
    reg, _ = get_registration(ledger, registration_id)
    _check_semantics(reg)
    prior = _runs(ledger, registration_id, "full_research")
    run_id, attempt = _open_run(
        ledger, registration_id, "full_research", software, rerun_of=rerun_of,
        rerun_reason=rerun_reason, evaluated_before=bool(prior),
    )  # fmt: skip
    try:
        plan = ledger.get_plan(reg.source_plan_id)
        records = _batch_records(ledger, reg)
        target = _member(ledger, records[reg.strategy_id])
        neighbours = [_member(ledger, records[s]) for s in reg.neighbour_strategy_ids]
        snapshot = Snapshot.from_ledger(ledger, reg.source_dataset_id)
        payload = full_research(
            target, neighbours, plan, snapshot, reg.source_role, reg.assets,
            _evidence_policy(ledger, reg.evidence_policy_id),
            _policy_by_id(FULL_RESEARCH_POLICIES, reg.full_research_policy_id),
        )  # fmt: skip
    except Exception as exc:  # recorded, never hidden
        payload = {**_error_payload(exc), "status": "FULL_RESEARCH_ERROR"}
    payload["provenance"] = _provenance(reg, run_id, attempt, software, reg.source_dataset_id)
    payload["independence"] = {
        "label": "not_independent",
        "note": "full research re-reads the discovery data the strategy was selected on",
    }
    status = payload["status"]
    result_id = _close_run(ledger, run_id, status, payload)
    return {"run_id": run_id, "result_id": result_id, "attempt": attempt, "status": status,
            "reasons": payload.get("reasons"), "error": payload.get("error")}  # fmt: skip


def _check_semantics(reg: ResearchRegistration) -> None:
    if reg.semantics != semantics():
        raise ResearchError(
            "semantic versions differ from the registration "
            f"({reg.semantics} vs {semantics()}); register again so incomparable results "
            "are never mixed"
        )


def _provenance(reg, run_id, attempt, software, dataset_id) -> dict:
    return {
        "registration_id": reg.registration_id,
        "run_id": run_id,
        "attempt": attempt,
        "software_id": software.software_id,
        "strategy_id": reg.strategy_id,
        "source_profile_id": reg.source_profile_id,
        "dataset_id": dataset_id,
        "semantics": semantics(),
    }


# --------------------------------------------------------------------------- validation


def _bar_counts(ledger: Ledger, reg: ResearchRegistration, plan: ScreenPlan) -> dict[str, dict]:
    """Bars per asset in the validation window: timestamps only, never prices/outcomes."""
    p = reg.validation_period
    out = {}
    for symbol in reg.assets:
        n, last = ledger.store.con.execute(
            "SELECT count(*), max(close_time) FROM perp_bars WHERE coin=? AND source=? AND "
            "timeframe='1d' AND close_time >= ? AND close_time < ?",
            [symbol, plan.source, p.start, p.end],
        ).fetchone()
        out[symbol] = {"bars": int(n), "last_close": _ts(last).isoformat() if last else None}
    return out


def readiness(ledger: Ledger, reg: ResearchRegistration, *, now: datetime | None = None) -> dict:
    """Pre-outcome gates: is the reserved period complete, and could it ever be adequate?

    Uses bar timestamps and counts only. The capacity bound is the most independent
    primary-horizon events the bars could produce (one per horizon gap per asset), so a
    bound below the policy minimum means the sample is necessarily inadequate.
    """
    plan = ledger.get_plan(reg.validation_plan_id)
    policy = _policy_by_id(VALIDATION_POLICIES, reg.validation_policy_id)
    p = reg.validation_period
    now = _ts(now or utcnow())
    counts = _bar_counts(ledger, reg, plan)
    h = {x.label: x.bars for x in plan.horizons}[plan.primary_horizon]
    expected_bars = max(int((p.end - p.start) / timedelta(days=1)), 0)
    last = max((_ts(c["last_close"]) for c in counts.values() if c["last_close"]), default=None)
    complete = now >= _ts(p.end) and last is not None and last >= _ts(p.end) - timedelta(days=1)
    capacity, possible_assets = 0, 0
    for c in counts.values():
        evaluable = max(c["bars"] - h, 0)  # signal bars whose T+h exit is in the window
        c["max_independent_primary_events"] = math.ceil(evaluable / h) if evaluable else 0
        capacity += c["max_independent_primary_events"]
        possible_assets += c["max_independent_primary_events"] > 0
    reasons = []
    if not complete:
        reasons.append(
            f"validation period [{p.start.date()}, {p.end.date()}) is incomplete: stored bars "
            f"through {last.date() if last else 'none'} of {expected_bars} days; it is evaluated "
            "once complete, so its outcomes are looked at exactly once"
        )
    if capacity < policy.min_independent_events or possible_assets < policy.min_assets_with_events:
        reasons.append(
            f"capacity: at most {capacity} independent {plan.primary_horizon} events on "
            f"{possible_assets} assets from the stored bars (policy needs "
            f"{policy.min_independent_events} on {policy.min_assets_with_events})"
        )
    return {
        "complete": complete,
        "evaluable": complete and not reasons,
        "now": now.isoformat(),
        "window": [p.start.isoformat(), p.end.isoformat()],
        "expected_days": expected_bars,
        "bars": counts,
        "primary_horizon": plan.primary_horizon,
        "capacity_independent_primary_events": capacity,
        "assets_with_possible_events": possible_assets,
        "reasons": reasons,
    }


def preview_validation(
    ledger: Ledger, registration_id: str, *, now: datetime | None = None
) -> dict:
    """Read-only dry run: what validation would use, before consuming any validation data."""
    reg, row = get_registration(ledger, registration_id)
    plan = ledger.get_plan(reg.validation_plan_id)
    profile = _profile(ledger, reg.source_profile_id)
    ready = readiness(ledger, reg, now=now)
    src = ledger.get_plan(reg.source_plan_id).period(reg.source_role)
    hist_days = (src.end - src.start) / timedelta(days=1)
    hist_events = profile.sample.get("independent_events") or 0
    projected = hist_events * ready["expected_days"] / hist_days if hist_days else None
    own = tuple(r["experiment_id"] for r in _runs(ledger, registration_id, "validation")
                if r["experiment_id"])  # fmt: skip
    policy = _policy_by_id(VALIDATION_POLICIES, reg.validation_policy_id)
    return {
        "registration_id": registration_id,
        "strategy": {
            "strategy_id": reg.strategy_id,
            "name": reg.strategy_name,
            "side": reg.side,
            "params": reg.params,
        },
        "source_profile": {"profile_id": reg.source_profile_id, "tier": reg.historical_tier},
        "validation_plan_id": reg.validation_plan_id,
        "period": ready["window"],
        "data_region_with_warmup": [reg.data_start.isoformat(), ready["window"][1]],
        "final_holdout": [
            [p.start.isoformat(), p.end.isoformat()]
            for p in plan.periods
            if p.role == "final_holdout"
        ]
        or None,
        "assets": list(reg.assets),
        "horizons": {h.label: h.bars for h in plan.horizons},
        "primary_horizon": plan.primary_horizon,
        "readiness": ready,
        "independence_now": independence(
            ledger, reg, registered_at=row["registered_at"], own_experiments=own
        ),
        "projection": {
            "historical_independent_events": hist_events,
            "historical_window_days": hist_days,
            "projected_independent_events_at_completion": projected,
            "policy_min_independent_events": policy.min_independent_events,
            "note": "discovery event rate scaled to the validation length; not a promise",
        },
        "would_consume_validation_data": ready["evaluable"],
    }


def classify_validation(
    metrics: dict, profile: EvidenceProfile, policy: ValidationPolicy, ev_policy: EvidencePolicy
) -> tuple[str, dict, list[str], dict]:
    """VALIDATION_* status from a stored validation-role screen. p-values never decide it."""
    primary = profile.evaluation["primary_horizon"]
    agg = primary_row(metrics, primary)
    assets = asset_summary(metrics.get("per_asset", []), agg, primary, ev_policy)
    n = agg.get("independent_events") or 0
    with_events = agg.get("assets_with_events") or 0
    raw = agg.get("raw_signals") or 0
    completion = (agg.get("evaluable_events") or 0) / raw if raw else None
    meta = metrics.get("assets", {})
    window = sum(m["window_bars"] for m in meta.values())
    eligible_share = sum(m["eligible_bars"] for m in meta.values()) / window if window else None
    excess, share = agg.get("excess_mean"), agg.get("positive_asset_share")
    checks = {
        "min_independent_events": n >= policy.min_independent_events,
        "min_assets_with_events": with_events >= policy.min_assets_with_events,
        "horizon_completion": completion is None or completion >= policy.min_completion_share,
        "eligible_share": eligible_share is not None
        and eligible_share >= policy.min_eligible_share,
    }
    sample = {"independent_events": n, "assets_with_events": with_events, "raw_signals": raw,
              "evaluable_events": agg.get("evaluable_events"), "completion_share": completion,
              "eligible_share": eligible_share, "checks": checks}  # fmt: skip
    reasons = [f"not met: {k}" for k, ok in checks.items() if not ok]
    if not all(checks.values()):
        return "VALIDATION_INSUFFICIENT", sample, reasons, assets
    if (excess is not None and excess > policy.supportive_min_effect
            and (share or 0) >= policy.supportive_min_positive_asset_share
            and not assets["dominated_by_one_asset"]):  # fmt: skip
        return (
            "VALIDATION_SUPPORTIVE",
            sample,
            ["expected-direction effect persisted, broadly"],
            assets,
        )
    if (excess is None or excess <= policy.adverse_max_effect) and (
        share or 0
    ) <= policy.adverse_max_positive_asset_share:
        return "VALIDATION_ADVERSE", sample, ["effect absent or reversed on most assets"], assets
    return "VALIDATION_MIXED", sample, ["neither broadly supportive nor broadly adverse"], assets


def compare(profile: EvidenceProfile, historical_metrics: dict, metrics: dict) -> dict:
    """Historical (discovery) versus validation, side by side; nothing pooled."""
    primary = profile.evaluation["primary_horizon"]
    h, v = profile.effect, primary_row(metrics, primary)
    he, ve = h.get("excess_mean"), v.get("excess_mean")

    def per_asset(m):
        return {r["symbol"]: r["excess_mean"] for r in m.get("per_asset", [])
                if r["horizon"] == primary and r["independent_events"]}  # fmt: skip

    ha, va = per_asset(historical_metrics), per_asset(metrics)
    both = sorted(set(ha) & set(va))
    return {
        "horizon": primary,
        "historical": {"excess_mean": he, "net_mean": h.get("net_mean"),
                       "hit_rate": h.get("hit_rate"),
                       "independent_events": profile.sample.get("independent_events"),
                       "assets_with_events": profile.sample.get("assets_with_events"),
                       "positive_asset_share": profile.assets.get("positive_share"),
                       "raw_p": profile.statistics.get("raw_p"), "q": profile.statistics.get("q")},
        "validation": {"excess_mean": ve, "net_mean": v.get("net_mean"),
                       "net_median": v.get("net_median"), "hit_rate": v.get("hit_rate"),
                       "independent_events": v.get("independent_events"),
                       "assets_with_events": v.get("assets_with_events"),
                       "positive_asset_share": v.get("positive_asset_share"),
                       "max_asset_event_share": v.get("max_asset_event_share"),
                       "raw_p_descriptive": v.get("p_value_random_entry")},
        "same_direction": (he is not None and ve is not None and (he > 0) == (ve > 0) and ve != 0),
        "effect_ratio": (ve / he) if he and ve is not None else None,
        "per_asset": [{"symbol": s, "historical_excess": ha[s], "validation_excess": va[s],
                       "same_direction": (ha[s] > 0) == (va[s] > 0)} for s in both],
        "assets_same_direction": sum((ha[s] > 0) == (va[s] > 0) for s in both),
        "note": "positive excess = the strategy's expected direction (side-adjusted returns)",
    }  # fmt: skip


def _submission(ledger: Ledger, reg: ResearchRegistration, strategy_id: str) -> str:
    if reg.batch_id is not None:
        from market_signal.research.lab.batch import inspect_batch

        subs = inspect_batch(ledger, reg.batch_id)["batch"]["submissions"]
        if strategy_id in subs:
            return subs[strategy_id]
    if strategy_id == reg.strategy_id:
        return ledger.get_experiment(reg.source_experiment_id).submission_id
    raise ResearchError(f"no submission recorded for {strategy_id}")


def _prepare_validation(
    ledger: Ledger,
    reg: ResearchRegistration,
    strategy_id: str,
    dataset_id: str,
    software: SoftwareIdentity,
    *,
    rerun_of: str | None = None,
    rerun_reason: str | None = None,
) -> tuple[str, bool]:
    """Phase 2 preregistration on the validation role (committed; no exposure yet).

    Returns (experiment_id, needs_screen). An identical logical experiment that already
    has a terminal result is reused rather than looked at again.
    """
    prior = ledger.store.con.execute(
        "SELECT experiment_id FROM lab_experiments WHERE strategy_id=? AND plan_id=? AND "
        "dataset_id=? AND role='validation' AND assets=? ORDER BY attempt DESC LIMIT 1",
        [strategy_id, reg.validation_plan_id, dataset_id, canonical_json(list(reg.assets))],
    ).fetchone()
    if prior and rerun_of is None:
        if not ledger.inspect_experiment(prior[0])["results"]:
            raise ResearchError(f"validation attempt {prior[0]} is unfinished; rerun explicitly")
        return prior[0], False
    exp = ledger.preregister(
        _submission(ledger, reg, strategy_id), reg.validation_plan_id, dataset_id,
        role="validation", assets=reg.assets, software=software,
        origin=f"lab_validation:{reg.registration_id}", rerun_of=rerun_of,
        rerun_reason=rerun_reason,
    )  # fmt: skip
    return exp.experiment_id, True


def _look(ledger: Ledger, experiment_id: str, software: SoftwareIdentity, needs_screen: bool):
    """Phase 4 run_screen: Phase 2 start (permanent exposure record) + one terminal result."""
    if needs_screen:
        run_screen(ledger, experiment_id, software=software)
    return ledger.inspect_experiment(experiment_id)


def run_validation(
    ledger: Ledger,
    registration_id: str,
    *,
    software: SoftwareIdentity,
    now: datetime | None = None,
    rerun_of: str | None = None,
    rerun_reason: str | None = None,
) -> dict:
    """Stage B. Pre-outcome gates first; then one governed look at the reserved period."""
    reg, row = get_registration(ledger, registration_id)
    _check_semantics(reg)
    profile = _profile(ledger, reg.source_profile_id)
    policy = _policy_by_id(VALIDATION_POLICIES, reg.validation_policy_id)
    prior = _runs(ledger, registration_id, "validation")
    evaluated = [r for r in prior if r["experiment_id"]]
    ready = readiness(ledger, reg, now=now)
    if not ready["evaluable"]:
        run_id, attempt = _open_run(
            ledger, registration_id, "validation", software, rerun_of=rerun_of,
            rerun_reason=rerun_reason, evaluated_before=bool(evaluated),
        )  # fmt: skip
        payload = {
            "status": "VALIDATION_INSUFFICIENT",
            "gate": "pre_outcome",
            "reasons": ready["reasons"],
            "readiness": ready,
            "exposure": "none: no validation-window outcome was computed and no Phase 2 "
            "evaluation was started",
            "provenance": _provenance(reg, run_id, attempt, software, None),
        }
        result_id = _close_run(ledger, run_id, payload["status"], payload)
        return {"run_id": run_id, "result_id": result_id, "attempt": attempt,
                "status": payload["status"], "reasons": ready["reasons"], "exposure": False}  # fmt: skip
    own = tuple(r["experiment_id"] for r in evaluated)
    indep = independence(ledger, reg, registered_at=row["registered_at"], own_experiments=own)
    plan = ledger.get_plan(reg.validation_plan_id)
    selections = tuple(
        SeriesSelection(kind=kind, symbol=s, source=plan.source,
                        timeframe="1d" if kind == "perp_bars" else None,
                        start=reg.data_start, end=reg.validation_period.end)
        for s in reg.assets for kind in ("perp_bars", "perp_funding")
    )  # fmt: skip
    dataset_id = ledger.register_dataset(capture_dataset(ledger.store, selections))
    exp_rerun = None
    if evaluated:
        if not (rerun_of and rerun_reason and rerun_reason.strip()):
            raise ResearchError(
                f"validation was already evaluated for this registration "
                f"({evaluated[-1]['run_id']}); a rerun needs --rerun-of and --rerun-reason, "
                "reproduces the same look and never restores independence"
            )
        prev = ledger.get_experiment(evaluated[-1]["experiment_id"])
        if prev.dataset_id == dataset_id and prev.strategy_id == reg.strategy_id:
            exp_rerun = prev.experiment_id
    # Order: preregister (committed, no exposure) -> run row citing it -> start + screen.
    experiment_id, needs_screen = _prepare_validation(
        ledger, reg, reg.strategy_id, dataset_id, software, rerun_of=exp_rerun,
        rerun_reason=rerun_reason if exp_rerun else None,
    )  # fmt: skip
    run_id, attempt = _open_run(
        ledger, registration_id, "validation", software, rerun_of=rerun_of,
        rerun_reason=rerun_reason, evaluated_before=bool(evaluated), experiment_id=experiment_id,
    )  # fmt: skip
    payload = {"gate": "evaluated", "readiness": ready, "independence": indep,
               "first_look": not own, "prior_own_runs": [r["run_id"] for r in evaluated]}  # fmt: skip
    try:
        inspected = _look(ledger, experiment_id, software, needs_screen)
        result = inspected["results"][0]
        if result["status"] == "errored":
            raise ResearchError(f"validation screen errored: {result['error']}")
        metrics = result["metrics"]
        ev_policy = _evidence_policy(ledger, reg.evidence_policy_id)
        status, sample, reasons, assets = classify_validation(metrics, profile, policy, ev_policy)
        historical = next(
            r["metrics"]
            for r in ledger.inspect_experiment(reg.source_experiment_id)["results"]
            if r["result_id"] == reg.source_result_id
        )
        payload.update(
            status=status, reasons=reasons, sample=sample, assets=assets,
            comparison=compare(profile, historical, metrics),
            validation_experiment={"experiment_id": experiment_id,
                                   "result_id": result["result_id"],
                                   "screen_triage_under_plan_gates": result["verdict"]},
        )  # fmt: skip
        if policy.neighbour_checks and reg.neighbour_strategy_ids:
            payload["neighbours"] = _validation_neighbours(
                ledger, reg, dataset_id, software, metrics, status, ev_policy
            )
    except Exception as exc:
        payload.update(status="VALIDATION_ERROR", **_error_payload(exc))
    payload["provenance"] = _provenance(reg, run_id, attempt, software, dataset_id)
    result_id = _close_run(ledger, run_id, payload["status"], payload)
    return {"run_id": run_id, "result_id": result_id, "attempt": attempt,
            "status": payload["status"], "reasons": payload.get("reasons"),
            "experiment_id": experiment_id, "exposure": experiment_id is not None,
            "error": payload.get("error")}  # fmt: skip


def _validation_neighbours(ledger, reg, dataset_id, software, target_metrics, status, ev_policy):
    """Descriptive knife-edge check: the same validation look for each registered neighbour.

    Each neighbour gets its own governed attempt (and recorded exposure). Nothing is
    selected; the target's status never depends on these rows.
    """
    records = _batch_records(ledger, reg)
    target = {**records[reg.strategy_id], "phase4_triage": target_metrics.get("triage")}
    primary = ledger.get_plan(reg.validation_plan_id).primary_horizon
    target["excess_mean"] = primary_row(target_metrics, primary).get("excess_mean")
    peers, rows = [target], []
    for sid in reg.neighbour_strategy_ids:
        exp_id, fresh = _prepare_validation(ledger, reg, sid, dataset_id, software)
        inspected = _look(ledger, exp_id, software, fresh)
        m = inspected["results"][0]["metrics"] or {}
        agg = primary_row(m, primary)
        peers.append({**records[sid], "phase4_triage": m.get("triage"),
                      "excess_mean": agg.get("excess_mean")})  # fmt: skip
        rows.append({"strategy_id": sid, "name": records[sid].get("name"),
                     "experiment_id": exp_id, "newly_evaluated": fresh,
                     "triage": m.get("triage"), "independent_events": agg.get("independent_events"),
                     "excess_mean": agg.get("excess_mean")})  # fmt: skip
    nb = neighbourhood_summary(target, peers, ev_policy)
    return {
        "rows": rows,
        "neighbourhood": nb,
        "depends_on_single_parameterisation": status == "VALIDATION_SUPPORTIVE"
        and nb.get("label") in ("isolated", "no_testable_neighbours"),
        "note": "descriptive robustness only; no neighbour can replace the frozen strategy",
    }


# --------------------------------------------------------------------------- evidence


def extend_profile(
    ledger: Ledger,
    registration_id: str,
    *,
    software: SoftwareIdentity,
    full_result_id: str | None = None,
    validation_result_id: str | None = None,
) -> dict:
    """Append a NEW schema-4 profile extending the frozen historical profile."""
    reg, _ = get_registration(ledger, registration_id)
    base = _profile(ledger, reg.source_profile_id)
    ext = _policy_by_id(EXTENSION_POLICIES, reg.extension_policy_id)
    full = (
        _result(ledger, full_result_id)
        if full_result_id
        else _latest_result(ledger, registration_id, "full_research")
    )
    if full is None:
        raise ResearchError("run full research before extending the profile")
    val = (
        _result(ledger, validation_result_id)
        if validation_result_id
        else _latest_result(ledger, registration_id, "validation")
    )
    for r, stage in ((full, "full_research"), (val, "validation")):
        if r is not None and (r["registration_id"] != registration_id or r["stage"] != stage):
            raise ResearchError(f"{r['result_id']} is not a {stage} result of this registration")
    tier, support, limit = _extended_tier(base.tier, full, val)
    fp = full["payload"]
    sources = [*base.sources, EvidenceSource(
        stage="full_research",
        records={"registration_id": registration_id, "run_id": full["run_id"],
                 "result_id": full["result_id"], "dataset_id": reg.source_dataset_id},
        versions={**reg.semantics, "full_research_policy": reg.full_research_policy_id,
                  "extension_policy": ext.policy_id},
    )]  # fmt: skip
    vblock = None
    if val is not None:
        vp = val["payload"]
        sources.append(EvidenceSource(
            stage="validation",
            records={k: v for k, v in {
                "registration_id": registration_id, "run_id": val["run_id"],
                "result_id": val["result_id"], "experiment_id": val["experiment_id"],
                "dataset_id": vp.get("provenance", {}).get("dataset_id")}.items() if v},
            versions={**reg.semantics, "validation_policy": reg.validation_policy_id,
                      "extension_policy": ext.policy_id},
        ))  # fmt: skip
        vblock = {
            "status": val["status"], "result_id": val["result_id"],
            "window": [reg.validation_period.start, reg.validation_period.end],
            "gate": vp.get("gate"), "reasons": vp.get("reasons"), "sample": vp.get("sample"),
            "comparison": vp.get("comparison"), "independence": vp.get("independence"),
            "first_look": vp.get("first_look"),
            "neighbours": (vp.get("neighbours") or {}).get("neighbourhood"),
        }  # fmt: skip
    data = base.model_dump(mode="python")
    data.update(
        profile_schema="4",
        builder={"version": EXTENSION_BUILDER_VERSION, "software_id": ledger.register_software(software)},
        sources=tuple(s.model_dump(mode="python") for s in sources),
        extends=base.profile_id,
        forward=None,
        tier=tier,
        components={**base.components, "full_research": full["status"].lower(),
                    "validation": val["status"].lower() if val else "not_run"},
        supporting=(*base.supporting, *support),
        limiting=(*base.limiting, *limit),
        limitations=(*base.limitations, *EXTENSION_LIMITATIONS),
        full_research={
            "status": full["status"], "result_id": full["result_id"],
            "reasons": fp.get("reasons"), "checks": fp.get("checks"),
            "event_study": fp.get("event_study"),
            "walk_forward": {k: (fp.get("walk_forward") or {}).get(k) for k in (
                "label", "adequate_folds", "positive_adequate_folds", "positive_share",
                "pooled_block_excess_mean")},
            "sensitivity": {k: (fp.get("sensitivity") or {}).get(k) for k in (
                "label", "testable_neighbours", "same_direction_share", "neighbour_excess_std",
                "neighbour_excess_range", "knife_edge")},
            "steps": fp.get("steps"),
            "historical_tier": base.tier,
            "extension_policy_id": ext.policy_id,
        },
        validation=vblock,
    )  # fmt: skip
    profile = EvidenceProfile.model_validate(json.loads(canonical_json(data)))
    out = record_profiles(ledger, [profile], _evidence_policy(ledger, base.policy_id))
    return {"profile_id": profile.profile_id, "extends": base.profile_id,
            "historical_tier": base.tier, "tier": tier, "tier_changed": tier != base.tier,
            "new_profiles": out["new"], "full_research": full["status"],
            "validation": val["status"] if val else None}  # fmt: skip


def _extended_tier(base_tier: str, full: dict, val: dict | None) -> tuple[str, list, list]:
    support, limit = [], []
    fs = full["status"]
    (support if fs == "FULL_RESEARCH_CONSISTENT" else limit).append(f"full research: {fs}")
    if val is None:
        limit.append("validation not run: tier unchanged (full research alone never changes it)")
        return base_tier, support, limit
    vs, vp = val["status"], val["payload"]
    indep = (vp.get("independence") or {}).get("label")
    first = vp.get("first_look") is True
    if vs == "VALIDATION_SUPPORTIVE":
        support.append("validation supportive on the reserved period")
        if indep != "untouched_on_record":
            limit.append(f"validation independence: {indep}")
        if not first:
            limit.append("validation result is not the first look at the reserved period")
        if (
            base_tier == "EXPLORATORY"
            and indep == "untouched_on_record"
            and first
            and (fs == "FULL_RESEARCH_CONSISTENT")
        ):
            support.append(
                "promoted: supportive independent validation and consistent full research"
            )
            return "RESEARCH_SUPPORTED", support, limit
        return base_tier, support, limit
    if vs == "VALIDATION_ADVERSE":
        limit.append("validation adverse on the reserved period")
        if base_tier in ("EXPLORATORY", "RESEARCH_SUPPORTED"):
            return "INCONCLUSIVE", support, limit
        return base_tier, support, limit
    limit.append(f"validation: {vs} (tier unchanged)")
    return base_tier, support, limit


# --------------------------------------------------------------------------- inspection


def show(ledger: Ledger, registration_id: str, *, now: datetime | None = None) -> dict:
    """Historical / full research / validation / forward, side by side and never pooled."""
    reg, row = get_registration(ledger, registration_id)
    profile = _profile(ledger, reg.source_profile_id)
    runs = {s: _runs(ledger, registration_id, s) for s in ("full_research", "validation")}
    full = _latest_result(ledger, registration_id, "full_research")
    val = _latest_result(ledger, registration_id, "validation")
    fp = (full or {}).get("payload") or {}
    vp = (val or {}).get("payload") or {}
    forward = []
    from market_signal.research.lab import forward as fwd

    for t in fwd.trackings(ledger):
        if t["strategy_id"] != reg.strategy_id:
            continue
        try:
            s = fwd.forward_summary(ledger, t["tracking_id"], as_of=now)
            prim = next((h for h in s["horizons"] if h.get("primary")), {})
            summary = {"observation": s["observation"], "maturity": s["maturity"],
                       "primary_horizon": prim}  # fmt: skip
        except Exception as exc:  # display only; never blocks the report
            summary = {"error": str(exc)}
        forward.append({"tracking_id": t["tracking_id"], "status": t["status"],
                        "enrolled_at": t["enrolled_at"], "summary": summary})  # fmt: skip
    extended = [
        {"profile_id": r[0], "tier": r[1]}
        for r in ledger.store.con.execute(
            "SELECT profile_id, tier FROM lab_evidence_profiles WHERE strategy_id=? AND "
            "json_extract_string(payload, '$.profile_schema')='4' AND "
            "json_extract_string(payload, '$.extends')=? ORDER BY recorded_at",
            [reg.strategy_id, reg.source_profile_id],
        ).fetchall()
    ]
    return {
        "registration_id": registration_id,
        "registered_at": row["registered_at"],
        "reason": row["reason"],
        "definition": json.loads(canonical_json(reg.model_dump(mode="python"))),
        "independence_at_registration": row["independence"],
        "historical": {
            "profile_id": profile.profile_id, "tier": profile.tier, "effect": profile.effect,
            "sample": profile.sample,
            "assets": {k: profile.assets.get(k) for k in (
                "assets_with_events", "positive_share", "max_asset_event_share",
                "dominated_by_one_asset", "events_by_asset")},
            "neighbourhood": {k: profile.neighbourhood.get(k) for k in (
                "label", "testable_neighbours", "same_direction", "support_share")},
            "raw_p": profile.statistics.get("raw_p"), "q": profile.statistics.get("q"),
            "batch_status": profile.statistics.get("batch_status"),
        },
        "full_research": {
            "status": full["status"] if full else None,
            "result_id": full["result_id"] if full else None,
            "reasons": fp.get("reasons"), "event_study": fp.get("event_study"),
            "walk_forward": fp.get("walk_forward"), "sensitivity": fp.get("sensitivity"),
            "portfolio_simulation": fp.get("portfolio_simulation"), "error": fp.get("error"),
        },
        "validation": {
            "status": val["status"] if val else None,
            "result_id": val["result_id"] if val else None,
            "gate": vp.get("gate"), "reasons": vp.get("reasons"), "sample": vp.get("sample"),
            "comparison": vp.get("comparison"), "independence": vp.get("independence"),
            "neighbours": vp.get("neighbours"), "error": vp.get("error"),
        },
        "forward": {
            "trackings": forward,
            "note": "prospective evidence: a separate source, never merged into validation",
        },
        "runs": runs,
        "extended_profiles": extended,
    }  # fmt: skip
