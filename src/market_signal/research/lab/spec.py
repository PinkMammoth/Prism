"""Versioned hypothesis documents, independent of the evaluation methodology.

This module describes rules; it neither calculates features nor executes strategies.
Feature references name the vocabulary in ``vocabulary.py``, grounded in existing Prism
calculations. Accepting a document does NOT establish data availability, PIT correctness,
or eligibility for a research/paper/live run; ``compiler.py`` establishes data
availability for DAILY definitions against a registered snapshot.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from market_signal.models.domain import Timeframe
from market_signal.research.lab.vocabulary import parse_feature

# Schema v1 pins these meanings to the implementations documented in STRATEGY_LAB.md.
# In particular, roc_1m is a DAILY calendar-class horizon and funding_* are daily.
# The original nine names (close, sma_20/50/200, ema_21, rsi_14, rel_volume, roc_1m,
# funding_day) keep their meaning; vocabulary v1 adds parameterised canonical tokens
# without changing the FeatureRef shape, so previously registered strategy IDs are stable.


def _feature_name(name: str) -> str:
    parse_feature(name)
    return name


FeatureName = Annotated[
    str,
    Field(strict=True, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
    AfterValidator(_feature_name),
]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
BarCount = Annotated[int, Field(strict=True, ge=1, le=10_000)]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=10_000)]
StrategyId = Annotated[str, Field(pattern=r"^strategy_[0-9a-f]{64}$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class FeatureRef(_Model):
    name: FeatureName
    timeframe: Timeframe

    @model_validator(mode="after")
    def daily_features(self) -> Self:
        if parse_feature(self.name).spec.daily_only and self.timeframe != Timeframe.D1:
            raise ValueError(f"{self.name} is defined only on daily bars in vocabulary v1")
        return self


class Condition(_Model):
    """One comparison; all conditions in a definition must hold.

    No expression strings, Python callables, arbitrary feature names, or nested logic.
    A range can be expressed as two comparisons. ``crosses_above`` at bar T means
    left > right at T and left <= right at T-1 (``crosses_below`` mirrors it); both bars
    must be defined. Equality is deliberately absent: exact float equality of continuous
    features is not a robust rule. Compiler semantics are documented in STRATEGY_LAB.md.
    """

    left: FeatureRef
    op: Literal["gt", "ge", "lt", "le", "crosses_above", "crosses_below"]
    right: Number | FeatureRef

    @model_validator(mode="after")
    def distinct_sides(self) -> Self:
        if self.right == self.left:
            raise ValueError("a condition cannot compare a feature with itself")
        return self

    def references(self) -> tuple[FeatureRef, ...]:
        return (self.left, self.right) if isinstance(self.right, FeatureRef) else (self.left,)


class ExitIntent(_Model):
    """Strategy exit intent; fills, costs, sizing and liquidation remain engine policy.

    ATR is sampled on the trigger timeframe at the signal close. The stop is that close
    minus stop_atr * ATR for longs, plus it for shorts. max_hold_bars counts trigger bars,
    with the existing engines' next-open time exit. target_r is in initial stop-distance
    units from the filled entry, matching ExitRules/PerpRules; it is not an ATR target.
    """

    atr: Literal["wilder_14", "perp_sma_14"]
    stop_atr: Annotated[Number, Field(gt=0, le=10)]
    max_hold_bars: BarCount
    target_r: Annotated[Number, Field(gt=0, le=20)] | None = None


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class StrategyDefinition(_Model):
    """One directional rule, separate from its name, universe and evaluation plan.

    Feature timeframes encode context/setup dependencies without fixed role names.
    Each dependency must be at least as slow as the trigger. This is a structural check,
    NOT a substitute for backward joins on feature availability times.
    """

    schema_version: Literal["1"] = "1"
    market: Literal["spot", "perp"]
    side: Literal["long", "short"]
    trigger_timeframe: Timeframe
    conditions: Annotated[tuple[Condition, ...], Field(min_length=1, max_length=4)]
    cooldown_bars: Annotated[int, Field(strict=True, ge=0, le=10_000)] = 10
    exit: ExitIntent

    @model_validator(mode="after")
    def supported_contract(self) -> Self:
        if self.market == "spot" and self.side != "long":
            raise ValueError("Prism's spot engine is long-only")
        refs = [ref for condition in self.conditions for ref in condition.references()]
        if any(ref.timeframe.seconds < self.trigger_timeframe.seconds for ref in refs):
            raise ValueError("feature timeframes must be at least as slow as the trigger")
        if self.market == "spot":
            for ref in refs:
                if parse_feature(ref.name).spec.market == "perp":
                    raise ValueError(f"{ref.name} requires the perp market")
        if self.market == "spot" and self.exit.atr != "wilder_14":
            raise ValueError("spot exit intent uses wilder_14 ATR")
        clauses = [self._condition_json(c) for c in self.conditions]
        if len(set(clauses)) != len(clauses):
            raise ValueError("duplicate conditions are not allowed")
        return self

    @staticmethod
    def _condition_json(condition: Condition) -> str:
        data = condition.model_dump(mode="json")
        # Numerically equal thresholds must have the same identity, including -0.0.
        if data["right"] == 0:
            data["right"] = 0.0
        return _json(data)

    def canonical_json(self) -> str:
        data = self.model_dump(mode="json")
        # Conjunction order cannot manufacture a new strategy ID.
        data["conditions"] = [
            json.loads(c) for c in sorted(self._condition_json(c) for c in self.conditions)
        ]
        return _json(data)

    @property
    def strategy_id(self) -> str:
        return "strategy_" + hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


class Hypothesis(_Model):
    """Authored metadata around a rule; future ledger receipt times are separate.

    A new name/source/timestamp never resets the rule's identity. Parent links record
    intent only here; their existence and lineage must be checked by the future ledger.
    Assets, periods, venues, costs and test boundaries belong to an operator-owned run
    plan, not to this document. No evaluation settings are accepted.
    """

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")]
    hypothesis: Text
    source: Text
    created_at: AwareDatetime
    parent_ids: tuple[StrategyId, ...] = ()
    definition: StrategyDefinition

    @model_validator(mode="after")
    def lineage(self) -> Self:
        if len(set(self.parent_ids)) != len(self.parent_ids):
            raise ValueError("duplicate parent IDs are not allowed")
        if self.definition.strategy_id in self.parent_ids:
            raise ValueError("a definition cannot be its own revision parent")
        return self

    @property
    def strategy_id(self) -> str:
        return self.definition.strategy_id


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        out = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValueError("hypothesis YAML mapping keys must be strings")
            if key in out:
                raise ValueError(f"duplicate hypothesis YAML key: {key}")
            out[key] = self.construct_object(value_node, deep=deep)
        return out


def load_hypothesis(path: Path) -> Hypothesis:
    """Load data only, with duplicate keys and unknown fields rejected.

    SafeLoader also rejects Python tags. YAML merge keys are deliberately unsupported:
    documents must contain their resolved rules, not inherit mutable external defaults.
    """
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return Hypothesis.model_validate(raw)
