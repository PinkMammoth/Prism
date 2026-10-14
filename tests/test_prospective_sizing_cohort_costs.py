"""Prospective common cost law: parity, current-control preservation and offline boundary."""

from __future__ import annotations

import builtins
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from market_signal.paper.nimble.model import fill
from market_signal.paper.v2.engine import size as current_size
from market_signal.research.lab.common import strict_json
from market_signal.sizing import cohort_costs as costs
from market_signal.sizing import cohort_spec, policy, prospective
from market_signal.sizing.model import AccountSnapshot, SizingRequest
from market_signal.universe.execution import size as satellite_size

ROOT = Path(__file__).resolve().parents[1]


def inputs(asset="ETH", **overrides):
    return costs.CostInputs.model_validate(
        dict(
            asset=asset,
            quote_id="q1",
            liquidity_evidence_id="d1",
            bid=99.99,
            ask=100.01,
            base_slippage_bps=dict(costs.CORE_BPS).get(asset, 12.0),
            predicted_funding_rate_hourly=0.0001,
            max_hold_minutes=61,
            size_decimals=4,
            depth_usd=25000.0,
            **overrides,
        )
    )


def request(inp):
    return SizingRequest(
        asset=inp.asset,
        side="long",
        cohort="CORE",
        hypothesis_id="h1",
        family="f",
        decision_at="2026-10-14T00:00:00+00:00",
        bid=inp.bid,
        ask=inp.ask,
        invalidation_price=98.0,
        target_price=103.0,
        emergency_price=90.0,
        causal_volatility=0.02,
        slippage_bps=inp.base_slippage_bps,
        funding_rate_hourly=inp.predicted_funding_rate_hourly,
        max_hold_minutes=inp.max_hold_minutes,
        size_decimals=inp.size_decimals,
        venue_max_leverage=25.0,
        depth_usd=inp.depth_usd,
    )


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize("leg", ["entry", "exit"])
@pytest.mark.parametrize("quantity", [0.5, 1.0, 2.0, 10.0])
def test_same_size_identical_costs_including_above_historical_impact_reference(side, leg, quantity):
    inp = inputs()
    control = costs.cohort_costs(costs.CONTROL, inp, quantity, side, leg=leg)
    risk = costs.cohort_costs(costs.CANDIDATE, inp, quantity, side, leg=leg)
    assert control == risk
    assert risk.slippage_bps == 2.0
    assert risk.fee_dollars == pytest.approx(risk.notional * 0.00045)
    assert risk.adverse_funding_reserve_dollars == pytest.approx(
        risk.notional * 0.0002 if side == "long" and leg == "entry" else 0.0
    )


@pytest.mark.parametrize("asset", ["BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE", "SAT"])
@pytest.mark.parametrize("side", ["long", "short"])
def test_current_control_fills_rounding_and_quantity_preserved(asset, side):
    inp = inputs(asset)
    quote = SimpleNamespace(
        asset=asset, bid=inp.bid, ask=inp.ask, slippage_bps=inp.base_slippage_bps
    )
    for leg in ("entry", "exit"):
        small = costs.costs_for_quantity(inp, 0.5, side, leg=leg)
        large = costs.costs_for_quantity(inp, 10.0, side, leg=leg)
        assert small.fill_price == fill(quote, side, entry=leg == "entry")
        assert large.fill_price == small.fill_price
        assert large.fee_dollars == pytest.approx(small.fee_dollars * 20)
        assert large.slippage_dollars == pytest.approx(small.slippage_dollars * 20)
        assert large.spread_dollars == pytest.approx(small.spread_dollars * 20)
    entry = fill(quote, side, entry=True)
    # High equity/depth stress must NOT sneak a candidate liquidity cap into control.
    for equity in (100, 1000, 100000):
        decimals = current_size(equity, entry, asset)[0] if asset != "SAT" else None
        if decimals is not None:
            from market_signal.paper.v2.spec import RiskPolicy

            inp = inp.model_copy(update={"size_decimals": RiskPolicy().size_decimals[asset]})
            assert costs.control_quantity(equity, inp, side) == decimals
        else:
            assert (
                costs.control_quantity(equity, inp, side)
                == satellite_size(equity, entry, asset, decimals=inp.size_decimals)[0]
            )


def test_parent_candidate_risk_parameters_and_historical_hashes_untouched():
    parent = prospective.candidate()
    effective = costs.effective_policy(costs.CANDIDATE)
    assert (
        parent.policy_id
        == "sizingpolicy_4a388694567e95a884f5e42e4736240bce6690a3fce07caf10105f051c962983"
    )
    assert costs.effective_policy(costs.CONTROL) == policy.baseline()
    original, derived = parent.model_dump(), effective.model_dump()
    assert original.pop("name") != derived.pop("name")
    parent_cost, prospective_cost = original.pop("cost_model"), derived.pop("cost_model")
    assert original == derived
    assert parent_cost.pop("impact") == "sqrt_reference_notional"
    assert prospective_cost.pop("impact") == "none"
    assert parent_cost == prospective_cost
    assert parent.cost_model.slippage_bps(2, 400) == 4
    assert effective.cost_model.slippage_bps(2, 400) == 2


@pytest.mark.parametrize("equity", [100, 500, 1000])
def test_candidate_risk_notional_margin_reconcile_to_final_common_cost(equity):
    inp = inputs()
    decision = costs.candidate_proposal(request(inp), AccountSnapshot(equity, equity), inp)
    assert decision.accepted
    execution = costs.cohort_costs(costs.CANDIDATE, inp, decision.units, "long")
    assert execution.fill_price == decision.entry_fill
    assert execution.notional == pytest.approx(decision.notional)
    assert execution.slippage_bps == decision.slippage_bps
    assert decision.margin == pytest.approx(decision.notional / decision.leverage)
    assert decision.intended_risk <= equity * 0.005
    assert decision.policy_id == costs.effective_policy(costs.CANDIDATE).policy_id


@pytest.mark.parametrize(
    "field,value",
    [("slippage_bps", 4), ("bid", 99), ("funding_rate_hourly", 0), ("depth_usd", None)],
)
def test_candidate_cannot_use_another_cost_snapshot(field, value):
    inp = inputs()
    decision = costs.candidate_proposal(
        replace(request(inp), **{field: value}), AccountSnapshot(100, 100), inp
    )
    assert not decision.accepted and decision.reason == "COMMON_COST_INPUT_MISMATCH"


@pytest.mark.parametrize(
    "field,reason",
    [
        ("causal_volatility", "VOLATILITY_MOVE_UNAVAILABLE"),
        ("invalidation_price", "MISSING_OR_INVALID_INVALIDATION"),
    ],
)
def test_candidate_missing_geometry_fails_closed(field, reason):
    inp = inputs()
    decision = costs.candidate_proposal(
        replace(request(inp), **{field: None}), AccountSnapshot(100, 100), inp
    )
    assert not decision.accepted and decision.reason == reason


@pytest.mark.parametrize("quantity", [-1, True, float("nan"), float("inf"), 0.00001])
def test_invalid_or_unrounded_size_is_not_fabricated(quantity):
    with pytest.raises(ValueError):
        costs.costs_for_quantity(inputs(), quantity, "long")


def test_model_snapshot_fail_closed_and_no_cohort_override():
    data = inputs().model_dump()
    for missing in (
        "bid",
        "ask",
        "base_slippage_bps",
        "predicted_funding_rate_hourly",
        "quote_id",
        "liquidity_evidence_id",
        "size_decimals",
    ):
        with pytest.raises(ValidationError):
            costs.CostInputs.model_validate({k: v for k, v in data.items() if k != missing})
    for extra in ("impact", "cohort_id", "fee_bps"):
        with pytest.raises(ValidationError):
            costs.CostInputs.model_validate({**data, extra: "override"})
    with pytest.raises(ValueError):
        costs.costs_for_quantity(inputs().model_copy(update={"base_slippage_bps": 4}), 1, "long")
    with pytest.raises(ValueError):
        costs.cohort_costs("unknown", inputs(), 1, "long")


def test_final_manifest_cost_binding_and_successor_lineage():
    spec = strict_json((ROOT / "specs/sizing/prospective_sizing_cohort_v2.json").read_text())
    old = strict_json((ROOT / "specs/sizing/prospective_sizing_cohort_v1.json").read_text())
    assert (
        cohort_spec.spec_hash(old)
        == "a0a06bf429b405c9dc45423fa6aa09fd17b746b3ec1194b02fd5643ee78116dd"
    )
    cohort_spec.require_successor(old, spec)
    contract = spec["execution_cost_contract"]
    assert contract["definition"] == costs.definition()
    assert contract["model_id"] == costs.model_id()
    assert (
        contract["adapter_source_sha256"]
        == hashlib.sha256(Path(costs.__file__).read_bytes()).hexdigest()
    )
    for cid, cohort in spec["cohorts"].items():
        assert cohort["cost_model_id"] == costs.model_id()
        assert cohort["slippage_model_id"] == costs.slippage_model_id()
        assert contract["bindings"][cid] == costs.policy_binding(cid)
    assert contract["no_runtime_integration"] is True


def test_cost_adapter_has_no_io_registration_or_runtime_side_effects(monkeypatch):
    import duckdb

    inp = inputs()
    req = request(inp)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline experiment adapter must not write or activate")

    monkeypatch.setattr(duckdb, "connect", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    for cid in (costs.CONTROL, costs.CANDIDATE):
        costs.policy_binding(cid)
        costs.cohort_costs(cid, inp, 1.0, "long")
    costs.control_quantity(100, inp, "long")
    costs.candidate_proposal(req, AccountSnapshot(100, 100), inp)
    assert prospective.ENABLED is False and prospective.REGISTERED is False
    assert costs.definition()["runtime_wired"] is False
