"""Versioned, content-hashed sizing policies. Changing any field changes the policy id.

Every value here is frozen ex ante with a stated economic reason (see
docs/RISK_POSITION_SIZING_RESEARCH_V1.md §Policies). None was chosen from replay PnL.
"""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import Field

from market_signal.research.lab.common import LabModel, content_id

VERSION = "risk_position_sizing_research_v1"


class CostModel(LabModel):
    """Prism's existing paper execution costs, plus an explicit size-dependent impact term."""

    name: Literal["prism_paper_costs_sqrt_impact_v1"] = "prism_paper_costs_sqrt_impact_v1"
    fee_bps: float = 4.5  # paper_nimble_v1 taker fee, each side
    # Frozen per-asset slippage (paper_exploratory_execution_v2); satellites carry their own
    # conservative depth-impact classification on the record.
    impact: Literal["none", "sqrt_reference_notional"] = "sqrt_reference_notional"
    # Universe cost classification reference ($100). Below it the frozen bps apply unchanged.
    impact_reference_notional: float = 100.0
    depth_participation_max: float = 0.10  # never take more than 10% of displayed top depth
    funding_reserve: Literal["adverse_predicted_per_started_hour"] = (
        "adverse_predicted_per_started_hour"
    )

    def slippage_bps(self, frozen_bps: float, notional: float) -> float:
        if self.impact == "none" or notional <= self.impact_reference_notional:
            return frozen_bps
        return frozen_bps * (notional / self.impact_reference_notional) ** 0.5


class DrawdownStep(LabModel):
    drawdown: float = Field(ge=0, lt=1)
    multiplier: float = Field(gt=0, le=1)  # never above 1: no martingale, no recovery chasing


class SizingPolicy(LabModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.\[\]=,%-]{0,119}$")
    version: Literal[1] = 1
    framework: Literal["risk_position_sizing_research_v1"] = VERSION
    family: Literal[
        "fixed_allocation",
        "fixed_fractional_risk",
        "volatility_normalized",
        "invalidation_vol",
        "fractional_kelly",
        "evidence_weighted",
    ]
    status: Literal["CONTROL", "CANDIDATE", "RESEARCH_ONLY", "STRESS_ONLY"]
    # --- how much to risk ---------------------------------------------------------------
    notional_fraction: float | None = None  # fixed allocation only
    risk_fraction: float | None = None  # base intended loss at invalidation, fraction of equity
    distance: Literal["none", "invalidation", "volatility", "max_invalidation_volatility"] = (
        "invalidation"
    )
    volatility_k: float = 0.0
    min_loss_distance: float = 0.0  # absolute last-resort floor on the sizing distance
    missing_invalidation: Literal["reject", "fallback_notional"] = "reject"
    fallback_notional_fraction: float = 0.10
    include_costs_in_risk: bool = True
    cost_risk_reduce_above: float | None = None  # costs / loss distance above this: halve risk
    cost_risk_reject_above: float | None = None  # ... above this: reject
    # --- leverage: margin efficiency, never conviction -----------------------------------
    leverage_mode: Literal["fixed", "minimal"] = "minimal"
    leverage_cap: float = 3.0
    max_margin_fraction_per_position: float = 0.34
    enforce_liquidation_safety: bool = True
    liquidation_buffer_multiple: float = 2.0
    liquidation_min_distance: float = 0.125  # 1.25 x the 10% emergency guard
    # --- hard caps -----------------------------------------------------------------------
    max_notional_fraction: float = 1.0  # per position, x equity
    max_positions: int = 3
    gross_fraction: float = 1.5
    max_total_margin_fraction: float = 0.9
    max_net_directional_fraction: float | None = None
    satellite_max_positions: int = 2
    portfolio_risk_multiple: float | None = None  # open intended risk <= m x base risk budget
    cluster_risk_multiple: float | None = None  # same-direction crypto-beta risk cap
    satellite_beta: float = 1.5
    per_setup_max_positions: int | None = None
    capacity_mode: Literal["skip", "reduce_to_fit"] = "reduce_to_fit"
    min_notional: float = 10.0
    rounding: Literal["venue_size_decimals_floor"] = "venue_size_decimals_floor"
    # --- throttles (only ever reduce) ----------------------------------------------------
    drawdown_steps: tuple[DrawdownStep, ...] = ()
    halt_drawdown: float | None = None
    regime_ratio_threshold: float | None = None
    regime_multiplier: float = 1.0
    regime_unavailable_multiplier: float = 1.0
    # --- research-only evidence inputs (causal expanding window) -------------------------
    kelly_fraction: float | None = None
    evidence_scope: Literal["hypothesis", "pooled"] = "hypothesis"
    evidence_min_samples: int = 100
    evidence_max_risk_fraction: float | None = None
    evidence_floor_multiplier: float = 0.5
    evidence_ceiling_multiplier: float = 1.0
    cost_model: CostModel = CostModel()

    @property
    def policy_id(self) -> str:
        return content_id("sizingpolicy_", self.model_dump(mode="json"))

    def identity(self) -> dict:
        return {"policy_id": self.policy_id, **self.model_dump(mode="json")}


# Frozen drawdown ladder. At 1% risk, -5% is roughly five consecutive full losses; -10%
# ten; -20% is a regime the experiment should not keep paying for at full risk.
DRAWDOWN_LADDER = (
    DrawdownStep(drawdown=0.05, multiplier=0.75),
    DrawdownStep(drawdown=0.10, multiplier=0.5),
    DrawdownStep(drawdown=0.20, multiplier=0.25),
)
HALT_DRAWDOWN = 0.35  # stop new entries; a $100 account keeps ~$65 to keep learning with
VOLATILITY_K = 0.5  # the stop must be at least half the frozen volatility move = the vol target
MIN_LOSS_DISTANCE = 0.0025  # 25 bps: beneath this the stop is inside spread+slippage noise
REGIME_RATIO = 2.0  # trailing 24h basket RV at least twice its trailing 30d level
REGIME_MULTIPLIER = 0.5
FIXED_FRACTIONS = (0.0025, 0.005, 0.0075, 0.01, 0.015, 0.02)


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def baseline() -> SizingPolicy:
    """Exact current paper behaviour: paper_exploratory_risk_v2 as used by paper_nimble_v1."""
    return SizingPolicy(
        name="fixed_allocation_baseline_v1",
        family="fixed_allocation",
        status="CONTROL",
        notional_fraction=0.20,
        distance="none",
        missing_invalidation="fallback_notional",
        include_costs_in_risk=True,
        leverage_mode="fixed",
        leverage_cap=2.0,
        max_margin_fraction_per_position=1.0,
        enforce_liquidation_safety=False,
        max_notional_fraction=0.20,
        max_positions=3,
        gross_fraction=0.60,
        max_total_margin_fraction=1.0,
        satellite_max_positions=2,
        capacity_mode="skip",
        halt_drawdown=0.75,  # catastrophe guard: equity <= 25% of peak stops the run
        cost_model=CostModel(impact="none"),
    )


def fixed_fractional(r: float, *, leverage_cap: float = 3.0) -> SizingPolicy:
    return SizingPolicy(
        name=f"fixed_fractional_risk_v1[r={_pct(r)},lev={leverage_cap:g}]",
        family="fixed_fractional_risk",
        status="CANDIDATE",
        risk_fraction=r,
        distance="invalidation",
        leverage_cap=leverage_cap,
        portfolio_risk_multiple=3.0,
    )


def volatility_normalized(r: float) -> SizingPolicy:
    return SizingPolicy(
        name=f"volatility_normalized_risk_v1[r={_pct(r)}]",
        family="volatility_normalized",
        status="CANDIDATE",
        risk_fraction=r,
        distance="volatility",
        volatility_k=1.0,  # the thesis' own frozen causal volatility move, unscaled
        min_loss_distance=MIN_LOSS_DISTANCE,
        missing_invalidation="fallback_notional",
        portfolio_risk_multiple=3.0,
    )


def invalidation_vol(
    r: float, *, cost_aware: bool = True, leverage_cap: float = 3.0, cluster: bool = True
) -> SizingPolicy:
    tag = "" if cost_aware else ",costs=off"
    tag += "" if leverage_cap == 3.0 else f",lev={leverage_cap:g}"
    tag += "" if cluster else ",cluster=off"
    return SizingPolicy(
        name=f"invalidation_vol_risk_v1[r={_pct(r)}{tag}]",
        family="invalidation_vol",
        status="CANDIDATE",
        risk_fraction=r,
        distance="max_invalidation_volatility",
        volatility_k=VOLATILITY_K,
        min_loss_distance=MIN_LOSS_DISTANCE,
        cost_risk_reduce_above=0.25 if cost_aware else None,
        cost_risk_reject_above=0.50 if cost_aware else None,
        leverage_cap=leverage_cap,
        portfolio_risk_multiple=3.0,
        cluster_risk_multiple=2.0 if cluster else None,
        max_net_directional_fraction=1.5,
    )


def drawdown_throttled(r: float, *, leverage_cap: float = 3.0, regime: bool = False):
    base = invalidation_vol(r, leverage_cap=leverage_cap)
    name = ("regime_throttled_risk_v1" if regime else "drawdown_throttled_risk_v1") + base.name[
        len("invalidation_vol_risk_v1") :
    ]
    return base.model_copy(
        update=dict(
            name=name,
            drawdown_steps=DRAWDOWN_LADDER,
            halt_drawdown=HALT_DRAWDOWN,
            regime_ratio_threshold=REGIME_RATIO if regime else None,
            regime_multiplier=REGIME_MULTIPLIER if regime else 1.0,
        )
    )


def fractional_kelly(fraction: float, *, scope: str = "hypothesis") -> SizingPolicy:
    base = drawdown_throttled(0.005)
    tag = "" if scope == "hypothesis" else ",pooled"
    return base.model_copy(
        update=dict(
            name=f"fractional_kelly_research_v1[k={fraction:g}{tag}]",
            family="fractional_kelly",
            status="STRESS_ONLY" if fraction > 0.25 else "RESEARCH_ONLY",
            kelly_fraction=fraction,
            evidence_scope=scope,
            evidence_min_samples=100,
            evidence_max_risk_fraction=0.015,  # absolute ceiling regardless of estimate
        )
    )


def leverage_stress(leverage_cap: float, *, safety: bool = True) -> SizingPolicy:
    """Stress only: lets leverage bind (3x equity notional) to expose liquidation geometry."""
    return fixed_fractional(0.01, leverage_cap=leverage_cap).model_copy(
        update=dict(
            name=f"leverage_stress_v1[lev={leverage_cap:g},safety={'on' if safety else 'off'}]",
            status="STRESS_ONLY",
            max_notional_fraction=3.0,
            gross_fraction=4.0,
            max_total_margin_fraction=1.0,
            enforce_liquidation_safety=safety,
        )
    )


def evidence_weighted() -> SizingPolicy:
    base = drawdown_throttled(0.0075)
    return base.model_copy(
        update=dict(
            name="evidence_weighted_research_v1[r=0.75%]",
            family="evidence_weighted",
            status="RESEARCH_ONLY",
            evidence_min_samples=100,
            evidence_floor_multiplier=0.5,
            evidence_ceiling_multiplier=1.25,
            evidence_max_risk_fraction=0.01,
        )
    )


@cache
def catalogue() -> tuple[SizingPolicy, ...]:
    out = [baseline()]
    out += [fixed_fractional(r) for r in FIXED_FRACTIONS]
    out += [volatility_normalized(r) for r in (0.005, 0.01)]
    out += [invalidation_vol(r) for r in (0.005, 0.0075, 0.01, 0.015)]
    out += [invalidation_vol(0.01, cost_aware=False), invalidation_vol(0.01, cluster=False)]
    out += [drawdown_throttled(r) for r in (0.0025, 0.005, 0.0075, 0.01)]
    out += [drawdown_throttled(0.005, leverage_cap=x) for x in (1.0, 2.0)]
    out += [drawdown_throttled(0.01, leverage_cap=x) for x in (1.0, 2.0, 5.0)]
    out += [drawdown_throttled(r, regime=True) for r in (0.005, 0.0075, 0.01)]
    out += [fractional_kelly(k) for k in (0.125, 0.25, 0.5)]
    out += [fractional_kelly(k, scope="pooled") for k in (0.125, 0.25, 0.5)]
    out += [leverage_stress(x) for x in (5.0, 10.0)]
    out += [leverage_stress(x, safety=False) for x in (5.0, 10.0)]
    out += [evidence_weighted()]
    names = [p.name for p in out]
    if len(set(names)) != len(names):
        raise AssertionError("duplicate sizing policy names")
    return tuple(out)


def by_name(name: str) -> SizingPolicy:
    for p in catalogue():
        if p.name == name:
            return p
    raise KeyError(f"unknown sizing policy: {name}")
