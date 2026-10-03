"""Immutable, separately versioned DAILY evaluation plans. No evaluator lives here.

Schema v1 (``EvaluationPlan``) is the Phase 2 event-study plan, kept byte-for-byte so its
plan IDs and records stay interpretable. Its funding literal names Prism's full-history
cadence method, which the Lab compiler does not implement, so v1 plans cannot be
screened. Schema v2 (``ScreenPlan``) freezes the Lab's causal funding method, a warmup
region and triage gates. Full research, walk-forward, sensitivity and portfolio policies
are deliberately not accepted. No plan produces a production verdict or promotion.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from market_signal.models.domain import AssetClass, Timeframe
from market_signal.research.lab.common import (
    Count,
    LabModel,
    Name,
    Number,
    PositiveInt,
    Probability,
    Symbol,
    Text,
    UTCDateTime,
    canonical_json,
    content_id,
)

DatasetRole = Literal["discovery", "development", "validation", "final_holdout"]


class Period(LabModel):
    """Permitted input access interval [start, end), not a claim of untouched data."""

    role: DatasetRole
    start: UTCDateTime
    end: UTCDateTime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start >= self.end:
            raise ValueError("period start must precede end")
        return self


class Horizon(LabModel):
    label: Annotated[str, Field(strict=True, pattern=r"^[a-z0-9_]{1,32}$")]
    bars: Annotated[int, Field(strict=True, ge=1, le=10_000)]


class AssetCosts(LabModel):
    symbol: Symbol
    fee_bps: Annotated[Number, Field(ge=0, lt=10_000)]
    slippage_bps: Annotated[Number, Field(ge=0, lt=10_000)]


class FundingPolicy(LabModel):
    # Pins the CURRENT approximation, including the known cadence limitation.
    aggregation: Literal["prism_daily_full_history_median_v1"] = (
        "prism_daily_full_history_median_v1"
    )
    settlement_alignment: Literal["nearest_minute"] = "nearest_minute"
    min_daily_coverage: Annotated[Number, Field(gt=0, le=1)] = 0.8
    partial_days: Literal["scale_to_expected_count"] = "scale_to_expected_count"
    missing_window: Literal["exclude"] = "exclude"


class StatisticsPolicy(LabModel):
    independent_events: Literal["greedy_gap_horizon_bars"] = "greedy_gap_horizon_bars"
    null: Literal["same_asset_side_eligible_without_replacement"] = (
        "same_asset_side_eligible_without_replacement"
    )
    alternative: Literal["greater"] = "greater"
    min_independent_events: PositiveInt = 30
    random_entry_samples: PositiveInt = 2000
    seed: Count = 12345
    multiple_testing: Literal["uncorrected"] = "uncorrected"


class EvaluationPlan(LabModel):
    schema_version: Literal["1"] = "1"
    name: Name
    version: PositiveInt
    market: Literal["spot", "perp"]
    source: Text  # exact stored source/venue; no provider fallback
    timeframe: Literal[Timeframe.D1] = Timeframe.D1
    stage: Literal["event_study"] = "event_study"
    periods: Annotated[tuple[Period, ...], Field(min_length=1, max_length=4)]
    costs: Annotated[tuple[AssetCosts, ...], Field(min_length=1)]
    horizons: Annotated[tuple[Horizon, ...], Field(min_length=1)]
    primary_horizon: str
    return_model: Literal["spot_total_return_v1", "perp_notional_v1"]
    entry: Literal["next_bar_open"] = "next_bar_open"
    horizon_exit: Literal["horizon_bar_close"] = "horizon_bar_close"
    baseline: Literal["same_asset_side_eligible_in_period"] = "same_asset_side_eligible_in_period"
    funding: FundingPolicy | None = None
    statistics: StatisticsPolicy = StatisticsPolicy()
    required_fingerprint: Literal["content_sha256"] = "content_sha256"
    promotion: Literal["disabled"] = "disabled"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        for values, label in (
            ([p.role for p in self.periods], "period roles"),
            ([c.symbol for c in self.costs], "cost symbols"),
            ([h.label for h in self.horizons], "horizon labels"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label}")
        periods = sorted(self.periods, key=lambda p: p.start)
        if any(a.end > b.start for a, b in pairwise(periods)):
            raise ValueError("dataset role periods must not overlap")
        if self.primary_horizon not in {h.label for h in self.horizons}:
            raise ValueError("primary horizon must be declared in horizons")
        expected = "perp_notional_v1" if self.market == "perp" else "spot_total_return_v1"
        if self.return_model != expected:
            raise ValueError("return model does not match market")
        if (self.funding is not None) != (self.market == "perp"):
            raise ValueError("funding policy is required for perps and forbidden for spot")
        return self

    def period(self, role: DatasetRole) -> Period:
        for period in self.periods:
            if period.role == role:
                return period
        raise ValueError(f"role {role!r} is not declared in this plan")

    def canonical_json(self) -> str:
        data = self.model_dump(mode="python")
        for field in ("periods", "costs", "horizons"):
            data[field] = sorted(data[field], key=canonical_json)
        return canonical_json(data)

    @property
    def plan_id(self) -> str:
        import json

        return content_id("plan_", json.loads(self.canonical_json()))


class PValue(LabModel):
    """Reported, uncorrected endpoint evidence for a future family-level analysis."""

    test: Text
    endpoint: Text  # e.g. pooled:long:1m; grouping remains explicit, never inferred
    value: Probability
    n_observations: Count


# --------------------------------------------------------------------------- schema v2


class CausalFundingPolicy(LabModel):
    """The funding treatment the Lab compiler actually implements (``lab_funding_day``).

    Expected settlements per day come from the median spacing of settlements in the
    trailing 7 days ending at each bar's end; a bar is missing if any summed settlement's
    minute-snapped ``available_at`` is after its close. Coverage is pinned to the compiler
    constant: a different value is a new policy version, not a plan parameter.
    """

    aggregation: Literal["lab_causal_trailing_7d_median_v1"] = "lab_causal_trailing_7d_median_v1"
    settlement_alignment: Literal["nearest_minute"] = "nearest_minute"
    availability: Literal["available_at_minute_by_bar_close"] = "available_at_minute_by_bar_close"
    min_daily_coverage: Literal[0.8] = 0.8
    partial_days: Literal["scale_to_expected_count"] = "scale_to_expected_count"
    missing_window: Literal["exclude"] = "exclude"


class ScreenAssetCosts(AssetCosts):
    # Spot compilation needs the class (calendar, class-month horizons); perps are crypto.
    asset_class: AssetClass | None = None


class ScreenGates(LabModel):
    """Deterministic triage thresholds, evaluated at the primary horizon only.

    The minimum independent event count is ``StatisticsPolicy.min_independent_events``.
    The p-value is the uncorrected one-sided random-entry test; ``None`` disables it.
    Passing every gate means "candidate for full research", never validated.
    """

    min_assets_with_events: PositiveInt = 2
    min_positive_asset_share: Probability = 0.6
    min_pooled_excess: Number = 0.0
    max_p_value: Probability | None = 0.1


class ScreenPlan(LabModel):
    """Schema v2: a DAILY fast-screen plan. Triage only; never a validation or promotion.

    ``warmup_days`` calendar days of data before each role period belong to the dataset:
    features may use them, while signals, baselines and outcomes are restricted to the
    role period [start, end). Outcomes whose exit bar falls after the period are not
    evaluable, so no observation after the period can affect a result.
    """

    schema_version: Literal["2"] = "2"
    name: Name
    version: PositiveInt
    market: Literal["spot", "perp"]
    source: Text
    timeframe: Literal[Timeframe.D1] = Timeframe.D1
    stage: Literal["screen"] = "screen"
    periods: Annotated[tuple[Period, ...], Field(min_length=1, max_length=4)]
    warmup_days: Annotated[int, Field(strict=True, ge=0, le=5000)]
    costs: Annotated[tuple[ScreenAssetCosts, ...], Field(min_length=1)]
    horizons: Annotated[tuple[Horizon, ...], Field(min_length=1, max_length=8)]
    primary_horizon: str
    return_model: Literal["spot_total_return_v1", "perp_notional_v1"]
    entry: Literal["next_bar_open"] = "next_bar_open"
    horizon_exit: Literal["horizon_bar_close"] = "horizon_bar_close"
    outcome_boundary: Literal["exit_within_role_period"] = "exit_within_role_period"
    baseline: Literal["same_asset_side_eligible_in_period"] = "same_asset_side_eligible_in_period"
    funding: CausalFundingPolicy | None = None
    statistics: StatisticsPolicy = StatisticsPolicy()
    gates: ScreenGates = ScreenGates()
    required_fingerprint: Literal["content_sha256"] = "content_sha256"
    promotion: Literal["disabled"] = "disabled"

    @model_validator(mode="after")
    def coherent(self) -> Self:
        EvaluationPlan.coherent(self)  # identical role/cost/horizon/return/funding rules
        if any(h.bars > 250 for h in self.horizons):
            raise ValueError("screen horizons are at most 250 bars")
        for c in self.costs:
            if self.market == "spot" and c.asset_class is None:
                raise ValueError("spot screen assets need an asset class")
            if self.market == "perp" and c.asset_class not in (None, AssetClass.CRYPTO):
                raise ValueError("perp screen assets are crypto")
        return self

    period = EvaluationPlan.period

    def data_start(self, role: DatasetRole) -> datetime:
        return self.period(role).start - timedelta(days=self.warmup_days)

    def asset_class(self, symbol: str) -> AssetClass | None:
        for c in self.costs:
            if c.symbol == symbol:
                return c.asset_class
        raise ValueError(f"no cost assumption for {symbol}")

    canonical_json = EvaluationPlan.canonical_json
    plan_id = EvaluationPlan.plan_id


AnyPlan = EvaluationPlan | ScreenPlan


def parse_plan(payload: str) -> AnyPlan:
    """Dispatch on schema version; v1 payloads (which may omit it) keep their meaning."""
    import json

    data = json.loads(payload)
    if data.get("schema_version", "1") == "2":
        return ScreenPlan.model_validate(data)
    return EvaluationPlan.model_validate(data)
