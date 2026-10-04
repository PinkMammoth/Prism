"""Preregistration, retained inputs, immutable history and honest fingerprint strength."""

from __future__ import annotations

import gzip
import json
import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.data.store import MIGRATIONS, Store
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.datasets import (
    DatasetCapture,
    DatasetManifest,
    SeriesFingerprint,
    SeriesSelection,
    capture_dataset,
)
from market_signal.research.lab.ledger import DuplicateExperiment, ErrorInfo, Ledger, LedgerError
from market_signal.research.lab.policy import EvaluationPlan, PValue
from market_signal.research.lab.provenance import SoftwareIdentity, capture_software
from market_signal.research.lab.spec import load_hypothesis

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2024, 1, 1, tzinfo=UTC)


def _seed(store):
    for symbol in ("BTC", "ETH"):
        for i in range(10):
            ts = START + timedelta(days=i)
            store.con.execute(
                "INSERT INTO perp_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    symbol,
                    "1d",
                    "hyperliquid",
                    ts,
                    100 + i,
                    105 + i,
                    95 + i,
                    101 + i,
                    10.0,
                    ts + timedelta(days=1),
                    START + timedelta(days=20),
                    "fixture",
                ],
            )
            store.con.execute(
                "INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?)",
                [
                    symbol,
                    "hyperliquid",
                    ts + timedelta(hours=1),
                    0.001,
                    None,
                    ts + timedelta(hours=1),
                    "market_close",
                    "fixture",
                ],
            )


def _plan():
    return EvaluationPlan.model_validate(
        {
            "name": "daily_events",
            "version": 1,
            "market": "perp",
            "source": "hyperliquid",
            "periods": [
                {"role": "discovery", "start": START, "end": START + timedelta(days=6)},
                {
                    "role": "final_holdout",
                    "start": START + timedelta(days=6),
                    "end": START + timedelta(days=12),
                },
            ],
            "costs": [
                {"symbol": "BTC", "fee_bps": 4.5, "slippage_bps": 2},
                {"symbol": "ETH", "fee_bps": 4.5, "slippage_bps": 2},
            ],
            "horizons": [{"label": "1d", "bars": 1}, {"label": "3d", "bars": 3}],
            "primary_horizon": "3d",
            "return_model": "perp_notional_v1",
            "funding": {},
        }
    )


def _selections(plan, role="discovery", assets=("BTC",)):
    period = plan.period(role)
    kinds = (
        ("perp_bars", "perp_funding") if plan.market == "perp" else ("bars", "corporate_actions")
    )
    return tuple(
        SeriesSelection(
            kind=kind,
            symbol=symbol,
            source=plan.source,
            timeframe="1d" if kind in ("bars", "perp_bars") else None,
            start=period.start,
            end=period.end,
        )
        for symbol in assets
        for kind in kinds
    )


@pytest.fixture
def env(store):
    _seed(store)
    ledger = Ledger(store)
    hypothesis = load_hypothesis(ROOT / "config/lab/examples/daily_trend.yaml")
    submission = ledger.submit(
        hypothesis.model_dump_json(), family_id="trend_family", origin="test"
    )
    assert submission.accepted, submission.error
    plan = _plan()
    plan_id = ledger.register_plan(plan)
    selections = _selections(plan)
    capture = capture_dataset(store, selections)
    dataset_id = ledger.register_dataset(capture)
    software = SoftwareIdentity(label="test", python_version="3.12", source_sha256="a" * 64)
    return SimpleNamespace(
        store=store,
        ledger=ledger,
        hypothesis=hypothesis,
        submission=submission,
        plan=plan,
        plan_id=plan_id,
        selections=selections,
        capture=capture,
        dataset_id=dataset_id,
        software=software,
    )


def _register(env, **overrides):
    args = dict(
        submission_id=env.submission.submission_id,
        plan_id=env.plan_id,
        dataset_id=env.dataset_id,
        role="discovery",
        assets=("BTC",),
        software=env.software,
        origin="test",
        batch_id="batch_one",
    )
    args.update(overrides)
    return env.ledger.preregister(**args)


def test_plan_identity_is_order_and_timezone_independent():
    plan = _plan()
    data = plan.model_dump(mode="json")
    for key in ("periods", "costs", "horizons"):
        data[key].reverse()
    data["periods"][-1]["start"] = "2024-01-01T01:00:00+01:00"
    equivalent = EvaluationPlan.model_validate(data)
    assert equivalent.plan_id == plan.plan_id
    assert EvaluationPlan.model_validate_json(plan.canonical_json()).plan_id == plan.plan_id
    data["costs"][0]["fee_bps"] += 0.1
    assert EvaluationPlan.model_validate(data).plan_id != plan.plan_id


@pytest.mark.parametrize(
    "field", ["walk_forward", "portfolio", "thresholds", "llm", "train_test_override"]
)
def test_unknown_or_unimplemented_policy_fields_fail(field):
    data = _plan().model_dump(mode="python")
    data[field] = {}
    with pytest.raises(ValidationError, match="Extra inputs"):
        EvaluationPlan.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("statistics", "multiple_testing"), "benjamini_hochberg"),
        (("statistics", "seed"), True),
        (("statistics", "random_entry_samples"), 0),
        (("funding", "min_daily_coverage"), float("nan")),
        (("funding", "missing_window"), "zero"),
        (("statistics", "unexpected"), 1),
    ],
)
def test_policy_rejects_unknown_methods_and_invalid_nested_numbers(path, value):
    data = _plan().model_dump(mode="python")
    data[path[0]][path[1]] = value
    with pytest.raises(ValidationError):
        EvaluationPlan.model_validate(data)


def test_plan_periods_methods_and_market_are_checked():
    for override in (
        {"timeframe": "4h"},
        {"stage": "full_research"},
        {"primary_horizon": "unknown"},
        {"return_model": "spot_total_return_v1"},
        {"funding": None},
        {"entry": "same_close"},
    ):
        with pytest.raises(ValidationError):
            EvaluationPlan.model_validate({**_plan().model_dump(mode="python"), **override})
    data = _plan().model_dump(mode="python")
    data["periods"][1]["start"] = START
    with pytest.raises(ValidationError, match="overlap"):
        EvaluationPlan.model_validate(data)
    data["periods"][1]["start"] = START.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone"):
        EvaluationPlan.model_validate(data)


def test_plan_versions_are_frozen_and_deeply_immutable(env):
    plan = env.plan
    assert env.ledger.register_plan(plan) == env.plan_id
    with pytest.raises(ValidationError, match="frozen"):
        plan.costs[0].fee_bps = 0.0
    changed = plan.model_dump(mode="python")
    changed["statistics"]["seed"] += 1
    with pytest.raises(LedgerError, match=r"version.*frozen"):
        env.ledger.register_plan(EvaluationPlan.model_validate(changed))
    changed["version"] = 2
    newer = EvaluationPlan.model_validate(changed)
    assert env.ledger.register_plan(newer) != env.plan_id
    assert env.ledger.get_plan(env.plan_id) == plan
    assert env.ledger.get_plan(newer.plan_id) == newer
    # Pydantic's unvalidated construction helpers cannot bypass the write boundary.
    unsafe = plan.model_copy(update={"stage": "full_research"})
    with pytest.raises(ValidationError):
        env.ledger.register_plan(unsafe)


def test_policy_and_strategy_remain_separate(env):
    document = env.hypothesis.model_dump(mode="json")
    document["definition"]["evaluation_plan"] = env.plan.model_dump(mode="json")
    invalid = env.ledger.submit(canonical_json(document), family_id="trend_family", origin="test")
    assert not invalid.accepted and "Extra inputs" in invalid.error


def test_dataset_ids_and_snapshots_are_deterministic(env):
    capture = capture_dataset(env.store, tuple(reversed(env.selections)))
    assert capture.manifest.dataset_id == env.dataset_id
    assert dict(capture.blobs) == dict(env.capture.blobs)
    assert env.ledger.register_dataset(capture) == env.dataset_id
    assert env.store.con.execute("SELECT count(*) FROM lab_snapshot_blobs").fetchone()[0] == 2
    manifest = DatasetManifest.model_validate_json(capture.manifest.canonical_json())
    assert manifest.dataset_id == env.dataset_id
    for part in manifest.series:
        assert part.strength == "content_sha256" and part.ingestion_run_ids == ("fixture",)
        assert (
            part.selection.start <= part.first_observed <= part.last_observed < part.selection.end
        )


@pytest.mark.parametrize(
    ("table", "assignment"),
    [
        ("perp_bars", "close=close+0.25"),
        ("perp_bars", "volume=volume+1"),
        ("perp_funding", "funding_rate=funding_rate+0.0001"),
        ("perp_funding", "available_at=available_at+INTERVAL '1 second'"),
        ("perp_bars", "ingest_run_id='reingested'"),
    ],
)
def test_content_or_provenance_revision_changes_dataset_identity(env, table, assignment):
    env.store.con.execute(f"UPDATE {table} SET {assignment} WHERE coin='BTC'")
    revised = capture_dataset(env.store, env.selections)
    assert revised.manifest.dataset_id != env.dataset_id
    assert [s.row_count for s in revised.manifest.series] == [
        s.row_count for s in env.capture.manifest.series
    ]


def test_retained_inputs_survive_market_revisions(env):
    original = env.ledger.read_dataset(env.dataset_id)
    env.store.con.execute("UPDATE perp_bars SET close=9999")
    env.store.con.execute("DELETE FROM perp_funding")
    assert env.ledger.read_dataset(env.dataset_id) == original
    experiment = _register(env)
    env.ledger.start(experiment.experiment_id, software=env.software)
    assert env.ledger.inspect_experiment(experiment.experiment_id)["status"] == "started"


def test_sources_and_empty_series_are_explicit_not_fallbacks(env):
    data = [s.model_dump(mode="python") for s in env.selections]
    for row in data:
        row["source"] = "nonexistent_venue"
    selections = tuple(SeriesSelection.model_validate(row) for row in data)
    empty = capture_dataset(env.store, selections)
    assert empty.manifest.strength == "content_sha256"
    assert all(s.row_count == 0 and s.first_observed is None for s in empty.manifest.series)
    assert empty.manifest.dataset_id != env.dataset_id
    env.ledger.register_dataset(empty)
    assert env.ledger.read_dataset(empty.manifest.dataset_id) == ([], [])
    with pytest.raises(LedgerError, match="source/window"):
        _register(env, dataset_id=empty.manifest.dataset_id)


@pytest.mark.parametrize("strength", ["metadata_only", "unavailable"])
def test_weak_fingerprints_are_labelled_and_cannot_start_research(env, strength):
    weak = DatasetManifest(
        series=tuple(
            SeriesFingerprint(
                selection=s, strength=strength, limitation="original input rows unavailable"
            )
            for s in env.selections
        )
    )
    dataset_id = env.ledger.register_dataset(weak)
    assert env.ledger.get_dataset(dataset_id).strength == strength
    with pytest.raises(LedgerError, match="strong retained"):
        _register(env, dataset_id=dataset_id)
    with pytest.raises(LedgerError, match="no retained"):
        env.ledger.read_dataset(dataset_id)
    with pytest.raises(ValidationError):
        SeriesFingerprint(selection=env.selections[0], strength=strength, sha256="a" * 64)


def test_snapshot_hash_schema_and_retention_are_verified_atomically(env):
    fresh = capture_dataset(env.store, _selections(env.plan, "final_holdout"))
    with pytest.raises(LedgerError, match="retained snapshot"):
        env.ledger.register_dataset(fresh.manifest)
    corrupt = list(fresh.blobs)
    digest, blob = corrupt[-1]
    corrupt[-1] = (digest, gzip.compress(gzip.decompress(blob) + b"[]\n", mtime=0))
    with pytest.raises(ValueError, match="hash mismatch"):
        env.ledger.register_dataset(DatasetCapture(fresh.manifest, tuple(corrupt)))
    assert env.store.con.execute("SELECT count(*) FROM lab_datasets").fetchone()[0] == 1
    assert env.store.con.execute("SELECT count(*) FROM lab_snapshot_blobs").fetchone()[0] == 2


def test_snapshot_limits_fail_without_truncating(env):
    with pytest.raises(ValueError, match="resource limit"):
        capture_dataset(env.store, env.selections, max_rows=1)
    with pytest.raises(ValueError, match="resource limit"):
        capture_dataset(env.store, env.selections, max_bytes=10)
    assert env.store.con.execute("SELECT 1").fetchone() == (1,)


def test_naive_timestamps_are_rejected_not_reinterpreted():
    from market_signal.research.lab.datasets import _cell

    with pytest.raises(ValueError, match="naive"):
        _cell(datetime(2024, 1, 1))
    assert _cell(datetime(2024, 1, 1, 1, tzinfo=UTC)) == "2024-01-01T01:00:00.000000+00:00"


def test_null_and_nan_remain_distinct_in_snapshots(env):
    env.store.con.execute("UPDATE perp_bars SET volume=NULL WHERE coin='BTC'")
    null = capture_dataset(env.store, env.selections)
    env.store.con.execute("UPDATE perp_bars SET volume=? WHERE coin='BTC'", [float("nan")])
    nan = capture_dataset(env.store, env.selections)
    assert null.manifest.dataset_id != nan.manifest.dataset_id
    env.ledger.register_dataset(nan)
    bars = next(
        rows
        for rows in env.ledger.read_dataset(nan.manifest.dataset_id)
        if rows and "volume" in rows[0]
    )
    assert all(math.isnan(row["volume"]) for row in bars)


@pytest.mark.parametrize(
    "raw", ["{bad", '{"name":"one","name":"two"}', '{"x":NaN}', '{"x":1e999}', "{}"]
)
def test_invalid_submissions_are_first_class_persistent_records(env, raw):
    receipt = env.ledger.submit(raw, family_id="trend_family", origin="test")
    assert not receipt.accepted and receipt.error
    row = env.store.con.execute(
        "SELECT raw_json,error FROM lab_submissions WHERE submission_id=?", [receipt.submission_id]
    ).fetchone()
    assert row == (raw, receipt.error)
    with pytest.raises(LedgerError, match="invalid submission"):
        _register(env, submission_id=receipt.submission_id)


def test_hypothesis_identity_receipts_lineage_and_fixed_family(env):
    again = env.ledger.submit(
        env.hypothesis.model_dump_json(), family_id="trend_family", origin="test"
    )
    assert again.submission_id != env.submission.submission_id
    assert again.hypothesis_id == env.submission.hypothesis_id
    renamed = env.hypothesis.model_dump(mode="json")
    renamed["name"] = "renamed_rule"
    alias = env.ledger.submit(canonical_json(renamed), family_id="trend_family", origin="test")
    assert alias.accepted and alias.hypothesis_id != again.hypothesis_id
    assert len(env.ledger.list_strategies()) == 1
    wrong_family = env.ledger.submit(
        canonical_json(renamed), family_id="fresh_family", origin="test"
    )
    assert not wrong_family.accepted and "immutable" in wrong_family.error
    child = deepcopy(renamed)
    child["definition"]["side"] = "short"
    child["parent_ids"] = [env.hypothesis.strategy_id]
    revision = env.ledger.submit(canonical_json(child), family_id="trend_family", origin="test")
    assert revision.accepted
    unknown_parent = deepcopy(child)
    unknown_parent["definition"]["cooldown_bars"] = 11
    unknown_parent["parent_ids"] = ["strategy_" + "f" * 64]
    invalid = env.ledger.submit(
        canonical_json(unknown_parent), family_id="trend_family", origin="test"
    )
    assert not invalid.accepted and "parent" in invalid.error
    assert len(env.ledger.list_strategies()) == 2


def test_preregistration_precedes_start_and_result(env):
    experiment = _register(env)
    view = env.ledger.inspect_experiment(experiment.experiment_id)
    assert view["status"] == "preregistered" and view["results"] == [] and view["inspections"] == []
    with pytest.raises(ValidationError, match="frozen"):
        experiment.dataset_id = "different"
    for id_ in (experiment.experiment_id, "missing"):
        with pytest.raises(LedgerError, match="does not exist"):
            env.ledger.record_result(id_, status="rejected")
    # SQL foreign keys enforce the existence of a start even outside the API.
    with pytest.raises(duckdb.ConstraintException):
        env.store.con.execute(
            "INSERT INTO lab_results VALUES (?,?,?,?,?,?,?,?)",
            [
                "bad",
                experiment.experiment_id,
                START,
                "rejected",
                None,
                "{}",
                "[]",
                None,
            ],
        )
    started = env.ledger.start(experiment.experiment_id, software=env.software)
    assert started >= experiment.created_at
    with pytest.raises(LedgerError, match="already started"):
        env.ledger.start(experiment.experiment_id, software=env.software)


@pytest.mark.parametrize(
    "status", ["succeeded", "rejected", "insufficient_data", "failed", "errored", "cancelled"]
)
def test_every_terminal_outcome_is_preserved(env, status):
    experiment = _register(env)
    env.ledger.start(experiment.experiment_id, software=env.software)
    error = (
        ErrorInfo(kind="test_error", message="Recorded failure")
        if status in ("failed", "errored", "cancelled")
        else None
    )
    metrics = {"count": 0, "nested": {"value": None}}
    id_ = env.ledger.record_result(
        experiment.experiment_id,
        status=status,
        metrics=metrics,
        verdict="NO_EDGE",
        error=error,
        p_values=(
            PValue(test="random_entry", endpoint="pooled:long:3d", value=0.5, n_observations=0),
        ),
    )
    metrics["nested"]["value"] = "mutated later"
    view = env.ledger.inspect_experiment(experiment.experiment_id)
    assert view["status"] == status and len(view["results"]) == 1
    result = view["results"][0]
    assert result["result_id"] == id_ and result["metrics"]["nested"]["value"] is None
    assert result["completed_at"] >= view["start"]["started_at"]
    with pytest.raises(LedgerError, match="immutable"):
        env.ledger.record_result(experiment.experiment_id, status="succeeded")
    assert env.ledger.inspect_experiment(experiment.experiment_id)["results"] == [result]


def test_result_validation_preserves_started_attempt(env):
    experiment = _register(env)
    env.ledger.start(experiment.experiment_id, software=env.software)
    for kwargs in (
        {"status": "failed"},
        {"status": "made_up"},
        {"status": "succeeded", "metrics": {"mean": float("nan")}},
        {"status": "succeeded", "metrics": {1: 1}},
        {"status": "succeeded", "error": ErrorInfo(kind="error", message="oops")},
    ):
        with pytest.raises(ValueError):
            env.ledger.record_result(experiment.experiment_id, **kwargs)
    assert env.ledger.inspect_experiment(experiment.experiment_id)["status"] == "started"
    env.ledger.record_result(
        experiment.experiment_id,
        status="errored",
        error=ErrorInfo(kind="interrupted", message="worker exited"),
    )


def test_exact_reruns_require_link_and_reason_preserving_prior_results(env):
    first = _register(env)
    env.ledger.start(first.experiment_id, software=env.software)
    env.ledger.record_result(first.experiment_id, status="rejected", metrics={"excess": -0.01})
    with pytest.raises(DuplicateExperiment, match=first.experiment_id):
        _register(env)
    with pytest.raises(DuplicateExperiment):
        _register(env, batch_id="another_batch")
    with pytest.raises(DuplicateExperiment):
        _register(env, rerun_of=first.experiment_id)
    second = _register(env, rerun_of=first.experiment_id, rerun_reason="Check reproducibility")
    assert second.experiment_id != first.experiment_id and second.logical_id == first.logical_id
    assert second.attempt == 2 and second.rerun_of == first.experiment_id
    env.ledger.start(second.experiment_id, software=env.software)
    env.ledger.record_result(second.experiment_id, status="succeeded", metrics={"excess": -0.01})
    views = env.ledger.list_experiments()
    assert [v["status"] for v in views] == ["rejected", "succeeded"]
    assert env.ledger.inspect_experiment(first.experiment_id)["results"][0]["metrics"] == {
        "excess": -0.01
    }


def test_renaming_hypothesis_does_not_hide_logical_duplicate(env):
    first = _register(env)
    data = env.hypothesis.model_dump(mode="json")
    data["name"] = "rename_after_failure"
    alias = env.ledger.submit(canonical_json(data), family_id="trend_family", origin="test")
    with pytest.raises(DuplicateExperiment):
        _register(env, submission_id=alias.submission_id)
    second = _register(
        env,
        submission_id=alias.submission_id,
        rerun_of=first.experiment_id,
        rerun_reason="Reproduction",
    )
    assert second.logical_id == first.logical_id and second.hypothesis_id != first.hypothesis_id


def test_changed_data_or_policy_are_new_logical_experiments(env):
    first = _register(env)
    env.store.con.execute("UPDATE perp_bars SET volume=volume+1 WHERE coin='BTC'")
    revised_id = env.ledger.register_dataset(capture_dataset(env.store, env.selections))
    revised = _register(env, dataset_id=revised_id)
    policy = env.plan.model_dump(mode="python")
    policy["version"] = 2
    policy["statistics"]["seed"] += 1
    newer_id = env.ledger.register_plan(EvaluationPlan.model_validate(policy))
    newer = _register(env, plan_id=newer_id)
    assert len({e.logical_id for e in (first, revised, newer)}) == 3
    assert all(e.attempt == 1 for e in (first, revised, newer))
    with pytest.raises(LedgerError, match="identical logical"):
        _register(
            env,
            dataset_id=revised_id,
            rerun_of=first.experiment_id,
            rerun_reason="Cannot call changed data reproduction",
        )


def test_changed_software_is_an_explicit_rerun_not_a_fresh_experiment(env):
    first = _register(env)
    changed_software = env.software.model_copy(update={"source_sha256": "b" * 64})
    # Editing code must not let identical strategy/plan/data escape the duplicate guard.
    with pytest.raises(DuplicateExperiment, match=first.experiment_id):
        _register(env, software=changed_software)
    with pytest.raises(LedgerError, match="software differs"):
        env.ledger.start(first.experiment_id, software=changed_software)
    rerun = _register(
        env,
        software=changed_software,
        rerun_of=first.experiment_id,
        rerun_reason="Reproduce under revised code",
    )
    assert rerun.logical_id == first.logical_id and rerun.attempt == 2
    assert rerun.software_id != first.software_id
    env.ledger.start(rerun.experiment_id, software=changed_software)
    with pytest.raises(LedgerError, match="software differs"):
        env.ledger.start(first.experiment_id, software=changed_software)
    software = env.store.con.execute("SELECT count(*) FROM lab_software").fetchone()[0]
    assert software == 2


def test_universe_order_is_canonical_and_changes_are_visible(env):
    first = _register(env)
    multi_id = env.ledger.register_dataset(
        capture_dataset(env.store, _selections(env.plan, assets=("ETH", "BTC")))
    )
    both = _register(env, dataset_id=multi_id, assets=("ETH", "BTC"))
    assert both.assets == ("BTC", "ETH") and both.logical_id != first.logical_id
    with pytest.raises(DuplicateExperiment):
        _register(env, dataset_id=multi_id, assets=("BTC", "ETH"))
    with pytest.raises(LedgerError, match="exactly the selected"):
        _register(env, assets=("BTC", "ETH"))
    with pytest.raises(LedgerError, match="cost assumption"):
        _register(env, assets=("SOL",))


def test_role_exposure_is_recorded_only_when_started_or_inspected(env):
    first = _register(env)
    final_id = env.ledger.register_dataset(
        capture_dataset(env.store, _selections(env.plan, "final_holdout"))
    )
    final = _register(env, dataset_id=final_id, role="final_holdout")
    interval = dict(
        start=START + timedelta(days=6), end=START + timedelta(days=12), family_id="trend_family"
    )
    assert env.ledger.exposures(**interval) == []
    env.ledger.start(first.experiment_id, software=env.software)
    assert env.ledger.exposures(**interval) == []
    env.ledger.record_inspection(final.experiment_id, reason="Operator inspected the holdout")
    env.ledger.start(final.experiment_id, software=env.software)
    exposures = env.ledger.exposures(**interval)
    assert [r["kind"] for r in exposures] == ["manual_inspection", "evaluation_started"]
    assert all(r["role"] == "final_holdout" and r["dataset_id"] == final_id for r in exposures)
    assert env.ledger.exposures(**{**interval, "family_id": "unrelated"}) == []
    with pytest.raises(LedgerError, match="source/window"):
        _register(env, dataset_id=final_id, role="discovery")


def test_persistence_and_read_only_cli(env):
    from market_signal.cli.main import app

    experiment = _register(env)
    env.ledger.start(experiment.experiment_id, software=env.software)
    env.ledger.record_result(experiment.experiment_id, status="rejected", metrics={"n": 3})
    path = env.store.path
    env.store.close()
    readonly = Store(path, read_only=True)
    try:
        loaded = Ledger(readonly)
        assert loaded.get_experiment(experiment.experiment_id) == experiment
        assert loaded.inspect_experiment(experiment.experiment_id)["status"] == "rejected"
        assert loaded.read_dataset(env.dataset_id)
        runner = CliRunner()
        for command in (
            ["strategies"],
            ["experiments"],
            ["experiment", experiment.experiment_id],
            ["plan", env.plan_id],
            ["dataset", env.dataset_id],
        ):
            result = runner.invoke(app, ["--db", str(path), "lab", *command])
            assert result.exit_code == 0, result.output
            json.loads(result.output)
        missing = runner.invoke(app, ["--db", str(path), "lab", "experiment", "missing"])
        assert missing.exit_code == 1 and "does not exist" in missing.output
    finally:
        readonly.close()


def test_migration_is_additive_and_old_rows_survive(tmp_path):
    path = tmp_path / "legacy.duckdb"
    db = duckdb.connect(str(path))
    db.execute("CREATE TABLE schema_version (version INTEGER)")
    for i, migration in enumerate(MIGRATIONS[:6], 1):
        db.execute(migration)
        db.execute("INSERT INTO schema_version VALUES (?)", [i])
    db.execute(
        "INSERT INTO research_runs (run_id,name,summary) VALUES ('legacy','old_strategy','{\"verdict\":\"REJECT\"}')"
    )
    db.close()
    readonly = Store(path, read_only=True)
    try:
        with pytest.raises(LedgerError, match="tables are absent"):
            Ledger(readonly)
        assert readonly.con.execute("SELECT max(version) FROM schema_version").fetchone() == (6,)
    finally:
        readonly.close()
    writable = Store(path)
    try:
        # every later additive migration (8: Lab batches) is applied on top
        assert writable.con.execute("SELECT max(version) FROM schema_version").fetchone() == (
            len(MIGRATIONS),
        )
        assert writable.con.execute("SELECT run_id,name,summary FROM research_runs").fetchone() == (
            "legacy",
            "old_strategy",
            '{"verdict":"REJECT"}',
        )
        writable.migrate()
        assert Ledger(writable).list_experiments() == []
        assert writable.con.execute(
            "SELECT count(*) FROM schema_version WHERE version=7"
        ).fetchone() == (1,)
    finally:
        writable.close()


def test_spot_manifest_requires_actions_and_retains_effective_dates(store):
    store.con.execute(
        "INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            "SPY",
            "1d",
            "tiingo",
            START,
            START + timedelta(hours=21),
            100,
            101,
            99,
            100,
            5,
            "fixture",
            START + timedelta(days=1),
        ],
    )
    store.con.execute(
        "INSERT INTO corporate_actions VALUES (?,?,?,?,?,?)",
        ["SPY", "tiingo", START.date(), 1, 0.1, "fixture"],
    )
    plan = _plan().model_dump(mode="python")
    plan.update(
        market="spot",
        source="tiingo",
        return_model="spot_total_return_v1",
        funding=None,
        costs=[{"symbol": "SPY", "fee_bps": 5, "slippage_bps": 3}],
    )
    plan = EvaluationPlan.model_validate(plan)
    hypothesis = load_hypothesis(ROOT / "config/lab/examples/daily_trend.yaml").model_dump(
        mode="json"
    )
    hypothesis["definition"]["market"] = "spot"
    hypothesis["definition"]["exit"]["atr"] = "wilder_14"
    ledger = Ledger(store)
    submission = ledger.submit(canonical_json(hypothesis), family_id="spot_trend", origin="test")
    capture = capture_dataset(store, _selections(plan, assets=("SPY",)))
    dataset_id = ledger.register_dataset(capture)
    plan_id = ledger.register_plan(plan)
    experiment = ledger.preregister(
        submission.submission_id,
        plan_id,
        dataset_id,
        role="discovery",
        assets=("SPY",),
        software=SoftwareIdentity(label="test", python_version="3.12"),
        origin="test",
    )
    assert experiment.assets == ("SPY",)
    actions = next(rows for rows in ledger.read_dataset(dataset_id) if rows and "date" in rows[0])
    assert actions[0]["date"] == START.date() and actions[0]["dividend"] == 0.1


def test_software_records_uncommitted_content_and_lock_changes(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    module = source / "example.py"
    module.write_text("VALUE = 1\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    first = capture_software(tmp_path)
    assert capture_software(tmp_path).software_id == first.software_id
    module.write_text("VALUE = 2\n")
    second = capture_software(tmp_path)
    assert second.source_sha256 != first.source_sha256
    (tmp_path / "uv.lock").write_text("version = 2\n")
    third = capture_software(tmp_path)
    assert third.lock_sha256 != second.lock_sha256
    assert len({first.software_id, second.software_id, third.software_id}) == 3
