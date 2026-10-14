"""Offline paired-experiment cost adapter. No registration, account or runtime API.

Both prospective arms use the current paper cost law for estimates AND fills. The
research policy catalogue and historical replay remain untouched. This deliberately
does not implement the future dispatcher/account lifecycle or grant execution authority.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from market_signal.research.lab.common import LabModel, Number, content_id

from .engine import SizingDecision, floor_units, size
from .model import AccountSnapshot, SizingRequest
from .policy import CostModel, baseline
from .prospective import candidate

CONTROL = "dynamic_lab_fixed_v1"
CANDIDATE = "dynamic_lab_risk_sizing_v1"
VERSION = "prospective_common_paper_costs_v1"
ADAPTER_VERSION = "prospective_sizing_common_cost_adapter_v1"
CORE_BPS = (("BTC", 2.0), ("ETH", 2.0), ("SOL", 4.0), ("HYPE", 6.0), ("LINK", 6.0), ("AAVE", 8.0))


def definition() -> dict:
    return {
        "version": VERSION,
        "adapter_version": ADAPTER_VERSION,
        "mode": "offline_paper_experiment_contract_only",
        "fee_schedule": {"version": "paper_taker_4_5bps_v1", "bps_each_side": 4.5},
        "slippage": {
            "version": "shared_current_paper_slippage_v1",
            "impact": "none",
            "quantity_impact_formula": "slippage_bps = frozen_quote_base_slippage_bps",
            "core_bps": dict(CORE_BPS),
            "satellite_classification": "depth_impact_conservative_v1",
            "satellite_reference_notional_usd": 100.0,
            "satellite_formula": "max(3, 20*100/depth_usd, venue_impact_bps, 0.05*rv_bps); same causal quote classification for both arms",
            "cohort_specific_overrides": False,
        },
        "fill": "entry long ask*(1+slip); entry short bid*(1-slip); exit long bid*(1-slip); exit short ask*(1+slip); full rounded quantity",
        "quote_source": "same frozen causal Hyperliquid bid/ask and cost-classification snapshot",
        "liquidity_inputs": "same quote/depth evidence ID and source snapshot; candidate depth participation limit is an admission constraint, not another fill model",
        "funding": "hourly settled entry notional plus adverse predicted reserve per started holding hour; same rate/frontier and treatment, quantity changes dollars",
        "rounding": "venue_size_decimals_floor; adapter rejects an unrounded quantity, never rounds up",
        "minimum_notional_usd": 10.0,
        "risk_estimation": "same common impact=none CostModel for both prospective sizing estimates and execution; never mix the historical candidate's sqrt estimates with common fills",
        "historical_definitions": "unchanged; original policy hashes retained as parents, prospective effective policy hashes recorded separately",
        "runtime_wired": False,
    }


def model_id() -> str:
    return content_id("prospectivecostmodel_", definition())


def slippage_model_id() -> str:
    return content_id("prospectiveslippage_", definition()["slippage"])


def common_sizing_cost_model() -> CostModel:
    """Keep the existing model type/parameters; select the control's current cost law."""
    return CostModel(impact="none")


def effective_policy(cohort_id: str):
    if cohort_id == CONTROL:
        return baseline()  # exact original policy, no new control risk/admission rule
    if cohort_id == CANDIDATE:
        parent = candidate()
        return parent.model_copy(
            update={
                "name": "prospective_" + parent.name,
                "cost_model": common_sizing_cost_model(),
            }
        )
    raise ValueError("unknown prospective cohort")


def policy_binding(cohort_id: str) -> dict:
    if cohort_id not in (CONTROL, CANDIDATE):
        raise ValueError("unknown prospective cohort")
    parent = baseline() if cohort_id == CONTROL else candidate()
    effective = effective_policy(cohort_id)
    return {
        "adapter_version": ADAPTER_VERSION,
        "cohort_id": cohort_id,
        "parent_sizing_policy": parent.identity(),
        "effective_sizing_policy": effective.identity(),
        "execution_cost_model_id": model_id(),
        "slippage_model_id": slippage_model_id(),
    }


class CostInputs(LabModel):
    """One shared snapshot; no cohort, account, risk fraction or impact override field."""

    asset: str = Field(min_length=1)
    quote_id: str = Field(min_length=1)
    liquidity_evidence_id: str = Field(min_length=1)
    bid: Number = Field(gt=0)
    ask: Number = Field(gt=0)
    base_slippage_bps: Number = Field(ge=0)
    predicted_funding_rate_hourly: Number
    max_hold_minutes: int = Field(strict=True, gt=0, le=480)
    size_decimals: int = Field(strict=True, ge=0, le=8)
    depth_usd: Number | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_quote(self):
        if self.bid > self.ask:
            raise ValueError("invalid quote")
        expected = dict(CORE_BPS).get(self.asset)
        if expected is not None and self.base_slippage_bps != expected:
            raise ValueError("core cost classification differs from frozen paper law")
        return self

    @property
    def input_id(self):
        return content_id("prospectivecostinput_", self.model_dump(mode="json"))


@dataclass(frozen=True)
class ExecutionCost:
    model_id: str
    slippage_model_id: str
    input_id: str
    quantity: float
    quote_side_price: float
    fill_price: float
    notional: float
    slippage_bps: float
    slippage_dollars: float
    spread_dollars: float
    fee_bps: float
    fee_dollars: float
    adverse_funding_reserve_dollars: float


def costs_for_quantity(
    inputs: CostInputs,
    quantity: float,
    side: Literal["long", "short"],
    *,
    leg: Literal["entry", "exit"] = "entry",
) -> ExecutionCost:
    """One common law, evaluated at the independently chosen final rounded quantity."""
    # Revalidate even model_copy/model_construct values; never trust a bypassed model.
    inputs = CostInputs.model_validate(inputs.model_dump(mode="python"))
    if type(quantity) not in (int, float) or not math.isfinite(quantity) or quantity < 0:
        raise ValueError("invalid quantity")
    step = Decimal(10) ** -inputs.size_decimals
    if Decimal(str(quantity)) % step:
        raise ValueError("unrounded venue quantity")
    if side not in ("long", "short") or leg not in ("entry", "exit"):
        raise ValueError("invalid side/leg")
    position_sign = 1 if side == "long" else -1
    trade_sign = position_sign if leg == "entry" else -position_sign
    quote_price = inputs.ask if trade_sign > 0 else inputs.bid
    slip = common_sizing_cost_model().slippage_bps(inputs.base_slippage_bps, quantity * quote_price)
    fill = quote_price * (1 + trade_sign * slip / 10000)
    if not math.isfinite(fill) or fill <= 0:
        raise ValueError("impossible fill")
    notional = quantity * fill
    fee = notional * common_sizing_cost_model().fee_bps / 10000
    reserve = (
        notional
        * math.ceil(inputs.max_hold_minutes / 60)
        * max(0.0, position_sign * inputs.predicted_funding_rate_hourly)
        if leg == "entry"
        else 0.0
    )
    return ExecutionCost(
        model_id(),
        slippage_model_id(),
        inputs.input_id,
        quantity,
        quote_price,
        fill,
        notional,
        slip,
        quantity * abs(fill - quote_price),
        quantity * abs(quote_price - (inputs.bid + inputs.ask) / 2),
        4.5,
        fee,
        reserve,
    )


def cohort_costs(cohort_id: str, inputs: CostInputs, quantity: float, side, *, leg="entry"):
    """The cohort label validates routing only; it cannot select cost parameters."""
    if cohort_id not in (CONTROL, CANDIDATE):
        raise ValueError("unknown prospective cohort")
    return costs_for_quantity(inputs, quantity, side, leg=leg)


def control_quantity(equity: float, inputs: CostInputs, side) -> float:
    """Current fixed allocation exactly; no depth cap, leverage or admission cleanup.

    Account admission/halt remains the frozen runtime's responsibility, not this helper.
    """
    if type(equity) not in (int, float) or not math.isfinite(equity) or equity <= 0:
        raise ValueError("invalid equity")
    fill = costs_for_quantity(inputs, 0.0, side).fill_price
    return floor_units(equity * 0.20, fill, inputs.size_decimals)


def candidate_proposal(
    request: SizingRequest, account: AccountSnapshot, inputs: CostInputs
) -> SizingDecision:
    """Offline proposal under common costs. Full source/readiness gates still precede it."""
    policy = effective_policy(CANDIDATE)
    inputs = CostInputs.model_validate(inputs.model_dump(mode="python"))
    for field, input_field in (
        ("asset", "asset"),
        ("bid", "bid"),
        ("ask", "ask"),
        ("slippage_bps", "base_slippage_bps"),
        ("funding_rate_hourly", "predicted_funding_rate_hourly"),
        ("max_hold_minutes", "max_hold_minutes"),
        ("size_decimals", "size_decimals"),
        ("depth_usd", "depth_usd"),
    ):
        if getattr(request, field) != getattr(inputs, input_field):
            return SizingDecision(False, "COMMON_COST_INPUT_MISMATCH", policy.policy_id)
    for value, reason in (
        (request.invalidation_price, "MISSING_OR_INVALID_INVALIDATION"),
        (request.causal_volatility, "VOLATILITY_MOVE_UNAVAILABLE"),
        (request.depth_usd, "LIQUIDITY_DEPTH_UNAVAILABLE"),
        (account.peak_equity, "PEAK_EQUITY_UNAVAILABLE"),
    ):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            return SizingDecision(False, reason, policy.policy_id)
    return size(request, account, policy)
