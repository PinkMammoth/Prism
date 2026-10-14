"""Offline freeze checks: no Store fixture, paper registration, or account creation."""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from market_signal.paper.nimble import dynamic_spec
from market_signal.paper.nimble import spec as nimble
from market_signal.paper.v2 import engine as control_engine
from market_signal.paper.v2 import spec as control_spec
from market_signal.research.lab.common import canonical_json, content_id, strict_json
from market_signal.sizing import cohort_costs, cohort_spec, policy, prospective
from market_signal.universe import execution

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "specs/sizing/prospective_sizing_cohort_v2.json"


@pytest.fixture
def spec():
    return strict_json(SPEC_PATH.read_text())


def opportunity(spec):
    row = dict.fromkeys(spec["pairing_contract"]["source_fields"])
    row.update(
        source_hypothesis_id="h1",
        source_hypothesis_version="paper_hypothesis_v2_1",
        signal_timestamp="2027-01-01T00:00:00.000000+00:00",
        asset="ETH",
        side="long",
        source_episode_id="episode1",
        entry_reference_price=100.0,
        entry_quote={"bid": 99.99, "ask": 100.01},
        entry_quote_id="quote1",
        causal_market_data_frontier="2027-01-01T00:00:01.000000+00:00",
        eligibility_evaluated_at="2027-01-01T00:00:02.000000+00:00",
        thesis_evidence={"stop": 98.0, "target": 103.0},
        volatility_move=0.02,
        volatility_move_definition={"version": "entry_volatility_move_v1"},
        stop_invalidation={"price": 98.0},
        target={"price": 103.0},
        expiry={"minutes": 30},
        max_hold_minutes=60,
        policy_independent_eligibility={"eligible": True, "reason_codes": []},
        unavailable_reason_codes=[],
        universe_snapshot_id="universe1",
        priority_inputs={"score": 0.5},
        attribution={"family": "technical", "regime": "UNAVAILABLE"},
    )
    row["thesis_evidence_hash"] = content_id("", row["thesis_evidence"])
    identity = {key: row[key] for key in spec["pairing_contract"]["identity_fields"]}
    row["source_opportunity_id"] = content_id("sourceopportunity_", identity)
    return row


def candidate_inputs():
    return dict(
        side="long",
        invalidation_price=98.0,
        volatility_move=0.02,
        volatility_move_definition={"version": "entry_volatility_move_v1"},
        equity=100.0,
        peak_equity=100.0,
        bid=99.99,
        ask=100.01,
        quote_evidence={"id": "q1"},
        slippage_bps=2.0,
        fee_bps=4.5,
        funding_rate_hourly=0.0,
        depth_usd=25000.0,
        depth_evidence={"id": "d1"},
        venue_max_leverage=25.0,
        size_decimals=4,
        margin_constraints={"mode": "isolated"},
        open_exposures=[],
        max_hold_minutes=60,
    )


def test_canonical_hash_and_schema_are_pinned(spec):
    validated = cohort_spec.validate_frozen_spec(spec)
    assert validated.spec_id == cohort_spec.SPEC_ID
    assert cohort_spec.spec_hash(spec) == SPEC_PATH.with_suffix(".sha256").read_text().split()[0]
    reordered = dict(reversed(list(spec.items())))
    assert cohort_spec.spec_hash(reordered) == cohort_spec.FROZEN_SHA256
    assert cohort_spec.spec_hash(json.loads(canonical_json(spec))) == cohort_spec.FROZEN_SHA256
    with pytest.raises(ValueError):
        cohort_spec.spec_hash({"x": float("nan")})
    with pytest.raises(ValueError):
        strict_json('{"x":1,"x":2}')
    with pytest.raises(ValueError):
        cohort_spec.validate_frozen_spec({**spec, "activation_enabled": True})


def test_control_and_candidate_match_approved_policy_evidence(spec):
    evidence = strict_json((ROOT / spec["provenance"]["sizing_evidence"]).read_text())
    for cohort_id, current in (
        ("dynamic_lab_fixed_v1", policy.baseline()),
        ("dynamic_lab_risk_sizing_v1", prospective.candidate()),
    ):
        frozen = spec["cohorts"][cohort_id]
        effective = cohort_costs.effective_policy(cohort_id)
        assert frozen["parent_sizing_policy"] == current.identity()
        assert frozen["sizing_policy_id"] == effective.policy_id
        assert frozen["sizing_policy_definition"] == effective.model_dump(mode="json")
        assert current.identity() in evidence
        assert frozen["sizing_policy_hash"] == effective.policy_id.removeprefix("sizingpolicy_")
        assert frozen["account_starting_equity_usd"] == 100.0
    risk = spec["shared_components"]["current_control_risk_definition"]
    assert risk == control_spec.RiskPolicy().model_dump(mode="json")
    assert (
        risk["notional_fraction"],
        risk["leverage"],
        risk["max_positions"],
        risk["gross_fraction"],
        risk["min_notional"],
        risk["catastrophe_drawdown"],
    ) == (0.2, 2.0, 3, 0.6, 10.0, 0.75)
    assert nimble.EXECUTION["emergency_adverse_move"] == 0.10
    candidate = spec["cohorts"]["dynamic_lab_risk_sizing_v1"]["sizing_policy_definition"]
    assert candidate["name"] == "prospective_drawdown_throttled_risk_v1[r=0.5%,lev=2]"
    assert candidate["risk_fraction"] == 0.005
    assert candidate["halt_drawdown"] == 0.35
    assert candidate["drawdown_steps"] == spec["drawdown"]["ladder"]
    assert canonical_json(
        spec["shared_components"]["dynamic_execution_definition"]
    ) == canonical_json(dynamic_spec.definition())


@pytest.mark.parametrize("equity,fill", [(100, 3000.01), (200, 101.123), (53.21, 0.00987)])
def test_control_rounding_matches_current_engine(spec, equity, fill):
    risk = control_spec.RiskPolicy()
    for asset, decimals in risk.size_decimals.items():
        units = control_engine.size(equity, fill, asset)[0]
        from market_signal.sizing.engine import floor_units

        assert units == floor_units(equity * 0.20, fill, decimals)
    for decimals in (0, 2, 8):
        units, notional = execution.size(equity, fill, "SAT", decimals=decimals)
        assert units == floor_units(equity * 0.20, fill, decimals)
        assert notional == units * fill
    assert (
        spec["cohorts"]["dynamic_lab_fixed_v1"]["sizing_policy_definition"]["capacity_mode"]
        == "skip"
    )


def test_source_definitions_and_artifact_are_unchanged(spec):
    for path, digest in spec["provenance"]["source_hashes"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    artifact = ROOT / spec["provenance"]["sizing_evidence"]
    assert (
        hashlib.sha256(artifact.read_bytes()).hexdigest()
        == spec["provenance"]["sizing_evidence_sha256"]
    )


def test_shared_source_contract_and_immutable_identity(spec):
    row = opportunity(spec)
    oid = cohort_spec.validate_opportunity(spec, row)
    assert "cohort_id" not in spec["pairing_contract"]["identity_fields"]
    assert set(spec["cohorts"]) == {"dynamic_lab_fixed_v1", "dynamic_lab_risk_sizing_v1"}
    for extra in ("cohort_id", "equity", "run_id", "account_snapshot"):
        with pytest.raises(ValueError, match="source fields"):
            cohort_spec.validate_opportunity(spec, {**row, extra: "forbidden"})
    for field in spec["pairing_contract"]["identity_fields"]:
        changed = deepcopy(row)
        changed[field] = "short" if field == "side" else "changed"
        with pytest.raises(ValueError):
            cohort_spec.validate_opportunity(spec, changed)
    changed = deepcopy(row)
    changed["thesis_evidence"]["stop"] = 97
    with pytest.raises(ValueError, match="hash"):
        cohort_spec.validate_opportunity(spec, changed)
    changed = {**row, "causal_market_data_frontier": "2027-01-02T00:00:00.000000+00:00"}
    with pytest.raises(ValueError, match="noncausal"):
        cohort_spec.validate_opportunity(spec, changed)
    assert oid == cohort_spec.validate_opportunity(spec, row)


@pytest.mark.parametrize(
    "alias",
    [
        "2027-01-01T00:00:00Z",
        "2027-01-01T00:00:00+00:00",
        "2026-12-31T19:00:00.000000-05:00",
        "2027-01-01T00:00:00",
    ],
)
def test_timestamp_aliases_cannot_create_duplicate_source_identities(spec, alias):
    row = opportunity(spec)
    row["signal_timestamp"] = alias
    identity = {key: row[key] for key in spec["pairing_contract"]["identity_fields"]}
    row["source_opportunity_id"] = content_id("sourceopportunity_", identity)
    with pytest.raises(ValueError, match="timestamp"):
        cohort_spec.validate_opportunity(spec, row)


def test_path_dependence_retained_without_signal_feedback(spec):
    allowed = spec["permitted_divergence"]
    assert {
        "quantity",
        "capital_or_risk_admission",
        "cohort_halt",
        "account_local_cooldown",
        "account_local_hypothesis_degradation",
        "quantity_dependent_common_cost_dollars",
    } <= set(allowed)
    assert not {"different_source_signal", "different_exit_geometry", "different_priority"} & set(
        allowed
    )
    assert spec["paired_analysis"]["partitions"] == [
        "both_taken",
        "control_only",
        "risk_only",
        "both_skipped",
    ]
    assert "cohort_id" in spec["signal_contract"]["prohibited_upstream_inputs"]
    assert "same" in spec["signal_contract"]["timing"]


@pytest.mark.parametrize(
    "section",
    [
        "cohorts",
        "shared_components",
        "signal_contract",
        "evidence",
        "primary_endpoint",
        "slippage_limitation",
        "commissioning",
        "missing_data",
        "analysis_cadence",
    ],
)
def test_semantic_mutation_requires_successor_id_and_hash(spec, section):
    changed = deepcopy(spec)
    changed[section]["silent_redesign"] = True
    assert cohort_spec.spec_hash(changed) != cohort_spec.spec_hash(spec)
    with pytest.raises(ValueError, match="new ID/hash"):
        cohort_spec.validate_frozen_spec(changed)
    with pytest.raises(ValueError, match="new specification ID"):
        cohort_spec.require_successor(spec, changed)
    changed["spec_id"] = "prospective_sizing_cohort_v3"
    changed["schema_version"] = 3
    cohort_spec.require_successor(spec, changed)
    # A successor does not retroactively become the frozen v1.
    with pytest.raises(ValueError):
        cohort_spec.validate_frozen_spec(changed)


@pytest.mark.parametrize("missing", list(candidate_inputs()))
def test_missing_candidate_fields_fail_closed(spec, missing):
    if missing == "side":
        expected = "INVALID_SIDE"
    else:
        expected = spec["missing_data"]["candidate_required_inputs"][missing]["reason"]
    inputs = candidate_inputs()
    del inputs[missing]
    assert expected in cohort_spec.candidate_input_rejections(spec, inputs)
    inputs[missing] = None
    assert expected in cohort_spec.candidate_input_rejections(spec, inputs)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1, True, "0.02"])
def test_candidate_does_not_invent_volatility(spec, invalid):
    inputs = candidate_inputs()
    inputs["volatility_move"] = invalid
    assert "VOLATILITY_MOVE_UNAVAILABLE" in cohort_spec.candidate_input_rejections(spec, inputs)


def test_candidate_input_consistency(spec):
    inputs = candidate_inputs()
    assert cohort_spec.candidate_input_rejections(spec, inputs) == ()
    for changes, reason in (
        ({"bid": 101.0}, "INVALID_QUOTE"),
        ({"invalidation_price": 102}, "INVALIDATION_BREACHED"),
        ({"peak_equity": 99}, "INVALID_EQUITY_PEAK"),
        ({"size_decimals": 4.5}, "VENUE_SIZE_CONSTRAINTS_UNAVAILABLE"),
    ):
        assert reason in cohort_spec.candidate_input_rejections(spec, {**inputs, **changes})
    # Do not create a new raw-quote invalidation rule: v1 measures from modeled fill.
    for side, stop in (("long", 100.02), ("short", 99.98)):
        assert (
            cohort_spec.candidate_input_rejections(
                spec, {**inputs, "side": side, "invalidation_price": stop}
            )
            == ()
        )


def test_no_activation_account_db_runtime_or_filesystem_side_effects(spec, monkeypatch):
    row, inputs = opportunity(spec), candidate_inputs()

    def forbidden(*args, **kwargs):
        raise AssertionError("spec validation must have no I/O or activation side effects")

    import duckdb

    monkeypatch.setattr(duckdb, "connect", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    cohort_spec.validate_frozen_spec(spec)
    cohort_spec.validate_opportunity(spec, row)
    assert cohort_spec.candidate_input_rejections(spec, inputs) == ()
    cohort_spec.require_successor(spec, deepcopy(spec))
    assert prospective.ENABLED is False and prospective.REGISTERED is False
    with pytest.raises(RuntimeError, match="not activated"):
        prospective.runtime_size(None, 100, 100, ())
    for cohort in spec["cohorts"].values():
        assert cohort["account_created"] is False
        assert cohort["registered"] is False and cohort["started"] is False
    assert spec["scope"]["runtime_profile_changes_allowed"] is False


def test_contract_module_has_no_runtime_transport_or_write_imports():
    tree = ast.parse(Path(cohort_spec.__file__).read_text())
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module)
    assert imports == {
        "__future__",
        "hashlib",
        "math",
        "datetime",
        "pydantic",
        "market_signal.research.lab.common",
    }
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"open", "exec", "eval"}
        for node in ast.walk(tree)
    )


def test_evidence_and_start_gates_do_not_reward_early_results(spec):
    gate = spec["commissioning"]
    assert gate["combine"] == "ALL"
    assert (
        gate["minimum_wall_hours"],
        gate["minimum_eligible_source_opportunities"],
        gate["minimum_finalized_trades"],
    ) == (24, 30, 10)
    decision = spec["evidence"]["states"]["DECISION_ELIGIBLE"]
    assert decision["market_days"] >= 60 and decision["finalized_per_cohort"] >= 200
    assert decision["both_taken_finalized"] >= 200 and decision["regimes"] >= 2
    assert spec["start_gate"]["all_required"] is True
    assert any("explicit user approval" in p for p in spec["start_gate"]["prerequisites"])
    assert spec["activation_contract"]["pending_fields"]
