"""Immutable, separately versioned DAILY event-study plans. No evaluator lives here.

Full research, walk-forward, sensitivity and portfolio policies are deliberately not
accepted yet. An event-study result is not a production verdict or promotion decision.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from market_signal.models.domain import Timeframe
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
