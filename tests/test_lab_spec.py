"""Research definition identities and the boundary between hypotheses and evaluation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from market_signal.research.lab.spec import Hypothesis, StrategyDefinition, load_hypothesis

EXAMPLE = Path(__file__).resolve().parents[1] / "config/lab/examples/daily_trend.yaml"


@pytest.fixture
def document():
    return load_hypothesis(EXAMPLE).model_dump(mode="json")


def test_yaml_json_and_canonical_definition_round_trip(document):
    hypothesis = Hypothesis.model_validate(document)
    restored = Hypothesis.model_validate_json(hypothesis.model_dump_json())
    rule = StrategyDefinition.model_validate_json(hypothesis.definition.canonical_json())
    assert restored == hypothesis
    assert rule.strategy_id == hypothesis.strategy_id
    # A persisted ID is a compatibility boundary, not just an equality within one run.
    assert hypothesis.strategy_id == (
        "strategy_95eb683c13efd9bd0b91460553c9f78389c8bea392dd780c69312d4ee4b2118a"
    )
    del document["definition"]["cooldown_bars"]
    del document["definition"]["exit"]["target_r"]
    assert Hypothesis.model_validate(document).strategy_id == hypothesis.strategy_id


def test_renaming_and_reordering_cannot_reset_identity(document):
    document["definition"]["conditions"].append(
        {"left": {"name": "rsi_14", "timeframe": "1d"}, "op": "lt", "right": 70}
    )
    before = Hypothesis.model_validate(document)
    document.update(name="renamed", hypothesis="Different prose", source="imported")
    document["created_at"] = "2026-10-04T12:00:00+01:00"
    document["parent_ids"] = ["strategy_" + "a" * 64]
    document["definition"]["conditions"].reverse()
    document["definition"]["conditions"][0]["right"] = 70.0
    assert Hypothesis.model_validate(document).strategy_id == before.strategy_id


def test_negative_zero_has_one_identity(document):
    clause = document["definition"]["conditions"][0]
    clause["right"] = -0.0
    before = Hypothesis.model_validate(document)
    clause["right"] = 0
    assert Hypothesis.model_validate(document).strategy_id == before.strategy_id


@pytest.mark.parametrize(
    ("field", "value"),
    [("side", "short"), ("trigger_timeframe", "4h"), ("cooldown_bars", 11)],
)
def test_behavior_changes_get_new_identity(document, field, value):
    original = Hypothesis.model_validate(document)
    document["definition"][field] = value
    assert Hypothesis.model_validate(document).strategy_id != original.strategy_id


def test_feature_threshold_and_exit_changes_get_new_identity(document):
    original = Hypothesis.model_validate(document).strategy_id
    changed = deepcopy(document)
    changed["definition"]["conditions"][0]["right"]["name"] = "sma_200"
    assert Hypothesis.model_validate(changed).strategy_id != original
    changed["definition"]["conditions"][0]["right"] = 50.0
    threshold_id = Hypothesis.model_validate(changed).strategy_id
    changed["definition"]["conditions"][0]["right"] = 51.0
    assert Hypothesis.model_validate(changed).strategy_id != threshold_id
    document["definition"]["exit"]["atr"] = "wilder_14"
    assert Hypothesis.model_validate(document).strategy_id != original


def test_nested_rules_are_frozen_and_detached_from_input(document):
    hypothesis = Hypothesis.model_validate(document)
    before = hypothesis.strategy_id
    document["definition"]["conditions"][0]["left"]["name"] = "rsi_14"
    assert hypothesis.strategy_id == before
    assert isinstance(hypothesis.definition.conditions, tuple)
    with pytest.raises(ValidationError, match="frozen"):
        hypothesis.definition.conditions[0].left.name = "rsi_14"
    with pytest.raises(ValidationError, match="frozen"):
        hypothesis.definition.cooldown_bars = 0


@pytest.mark.parametrize("field", ["costs", "statistics", "train_end", "results", "universe"])
def test_hypothesis_cannot_set_evaluation_policy(document, field):
    document[field] = {}
    with pytest.raises(ValidationError, match="Extra inputs"):
        Hypothesis.model_validate(document)
    del document[field]
    document["definition"][field] = {}
    with pytest.raises(ValidationError, match="Extra inputs"):
        Hypothesis.model_validate(document)


@pytest.mark.parametrize("threshold", [True, "70", float("nan"), float("inf"), -float("inf")])
def test_thresholds_require_finite_numbers(document, threshold):
    document["definition"]["conditions"][0]["right"] = threshold
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(document)


@pytest.mark.parametrize("count", [True, -1, 1.5, "10", 10_001])
def test_cooldown_is_a_bounded_integer(document, count):
    document["definition"]["cooldown_bars"] = count
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(document)


def test_unknown_features_operators_and_nested_fields_are_rejected(document):
    for field, value in [("op", "eval"), ("expression", "close.shift(-1) > close")]:
        changed = deepcopy(document)
        changed["definition"]["conditions"][0][field] = value
        with pytest.raises(ValidationError):
            Hypothesis.model_validate(changed)
    for name in ("oi_change_24h", "future_return", "funding_percentile"):
        document["definition"]["conditions"][0]["left"]["name"] = name
        with pytest.raises(ValidationError):
            Hypothesis.model_validate(document)


def test_condition_count_and_duplicates_are_rejected(document):
    rule = document["definition"]
    condition = rule["conditions"][0]
    for clauses in ([], [condition] * 2, [condition] * 5):
        rule["conditions"] = clauses
        with pytest.raises(ValidationError):
            Hypothesis.model_validate(document)


def test_multitimeframe_references_are_explicit_and_cannot_be_faster(document):
    rule = document["definition"]
    rule["trigger_timeframe"] = "1h"
    rule["conditions"][0]["left"]["timeframe"] = "4h"
    assert Hypothesis.model_validate(document).definition.trigger_timeframe.value == "1h"
    rule["trigger_timeframe"] = "1d"
    with pytest.raises(ValidationError, match="at least as slow"):
        Hypothesis.model_validate(document)
    rule["conditions"][0]["left"]["timeframe"] = "1d"
    rule["conditions"][0]["right"]["timeframe"] = "4h"
    with pytest.raises(ValidationError, match="at least as slow"):
        Hypothesis.model_validate(document)


def test_funding_is_perp_daily_and_spot_is_long_only(document):
    rule = document["definition"]
    rule["conditions"] = [
        {"left": {"name": "funding_day", "timeframe": "1d"}, "op": "gt", "right": 0.001}
    ]
    assert Hypothesis.model_validate(document).definition.market == "perp"
    rule["conditions"][0]["left"]["timeframe"] = "4h"
    with pytest.raises(ValidationError, match="only on daily"):
        Hypothesis.model_validate(document)
    rule["conditions"][0]["left"]["timeframe"] = "1d"
    rule["market"] = "spot"
    with pytest.raises(ValidationError, match="requires the perp"):
        Hypothesis.model_validate(document)
    rule["side"] = "short"
    with pytest.raises(ValidationError, match="long-only"):
        Hypothesis.model_validate(document)


def test_version_timestamp_and_lineage_validation(document):
    document["created_at"] = "2026-10-03T00:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        Hypothesis.model_validate(document)
    document["created_at"] += "Z"
    document["parent_ids"] = [Hypothesis.model_validate(document).strategy_id]
    with pytest.raises(ValidationError, match="own revision parent"):
        Hypothesis.model_validate(document)
    document["parent_ids"] = []
    document["definition"]["schema_version"] = "2"
    with pytest.raises(ValidationError):
        Hypothesis.model_validate(document)


@pytest.mark.parametrize(
    "payload",
    [
        "name: one\nname: two\n",
        "definition:\n  side: long\n  side: short\n",
        "!!python/object/apply:builtins.print [should_not_execute]",
        "base: &base {side: long}\ndefinition: {<<: *base}",
    ],
)
def test_yaml_rejects_ambiguous_or_executable_input(tmp_path, payload):
    path = tmp_path / "bad.yaml"
    path.write_text(payload)
    with pytest.raises((ValueError, yaml.YAMLError)):
        load_hypothesis(path)
