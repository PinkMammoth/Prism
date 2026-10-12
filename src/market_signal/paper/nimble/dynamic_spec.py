"""Separate paper successor. Never mutates the six-asset Nimble/event identities."""

from copy import deepcopy
from dataclasses import asdict
from functools import cache

from market_signal.paper.nimble import spec
from market_signal.research.lab.common import canonical_json, content_id
from market_signal.universe.policy import POLICY
from market_signal.universe.portability import mapping

VERSION = "paper_nimble_dynamic_v1"


@cache
def _definition():
    d = spec.definition()
    d["entry_universe_id"] = content_id(
        "paperuniverse_dynamic_",
        {
            "selection": POLICY.identity,
            "hypotheses": [h["hypothesis_id"] for h in mapping() if h["source_phase"] != 30],
        },
    )
    d.update(
        version=VERSION,
        universe_policy=asdict(POLICY),
        universe_policy_id=POLICY.identity,
        portability=mapping(),
        satellite_max_positions=2,
        priority="economic_ratio_desc_score_desc_asset_hypothesis_signal",
        comparison="six_asset_control_and_frozen_Phase30_cohorts_unchanged",
        satellite_fill="fresh_future_bid_ask_plus_depth_impact_conservative_v1",
        size="venue_szDecimals_floor_existing_20pct_notional_2x_leverage",
        activation="requires_prospective_history_and_validated_storage_budget",
    )
    return d


def definition():
    return deepcopy(_definition())


@cache
def canonical_definition():
    return canonical_json(definition())


@cache
def policy_id():
    return content_id("paperexecution_dynamic_", definition())


@cache
def universe_id():
    return definition()["entry_universe_id"]
