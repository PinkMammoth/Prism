"""Small frozen execution families; original entry/scientific identities are untouched."""

from __future__ import annotations

from functools import cache

from market_signal.paper.v2 import spec as baseline
from market_signal.research.lab.common import content_id

VERSION = "paper_nimble_v1"
THESIS_VERSION = "execution_thesis_v1"
EXIT_VERSION = "thesis_exit_v1"
REGIMES = (5, 15, 30, 60, 120, 240, 480)
REACTION_HORIZONS = (1, 5, 15, 30, 60, 120)
RISK = baseline.RiskPolicy().model_dump(mode="json")
EXECUTION = {
    "version": VERSION,
    "mode": "paper",
    "entry": "first_observed_future_bid_ask",
    "exit": "first_observed_future_bid_ask_after_decision",
    "fee_bps": 4.5,
    "slippage_bps": baseline.ExecutionPolicy().slippage_bps,
    "quote_max_age_seconds": 3,
    "entry_wait_seconds": 60,
    "monitor_seconds": 2,
    "state_monitor_seconds": 60,
    "data_failure_seconds": 180,
    "economic_cost_multiple": 2.0,
    "emergency_adverse_move": 0.10,
    "gap_loss_guard": "isolated_initial_margin_cap_with_explicit_liquidation_adjustment",
    "funding_near_seconds": 120,
    "funding_remaining_edge_fraction": 0.25,
    "funding_stale_lifetime_fraction": 0.5,
    "funding_tp_near_fraction": 0.8,
    "conflict": "existing_invalidation_first_then_cancel_opposing_fresh",
    "ambiguous": "INVALIDATED_with_AMBIGUOUS_ordering_no_wick_fill",
    "fill": "taker_bid_ask_plus_adverse_slippage_full_position",
    "notifications": "daily_summary_and_failure_streak_default",
    "degradation": {"minimum_trades": 100, "recent": 50, "mean_net": -0.005, "losses": 40},
}


def mapping(h) -> dict:
    key = h.source_key
    if key in EVENT_CONTRACTS:
        c = EVENT_CONTRACTS[key]
        return dict(
            objective="event_reaction",
            expiry_minutes=c["reaction_window_minutes"],
            max_hold_minutes=c["max_hold_minutes"],
            cooldown_minutes=2,
            state_rule="event_reaction",
        )
    if h.source_phase == 24:
        if key.startswith("absorption"):
            return dict(
                objective="structural",
                expiry_minutes=30,
                max_hold_minutes=60,
                cooldown_minutes=5,
                state_rule="absorption_defence_lost",
            )
        if key == "continuation_core":
            return dict(
                objective="volatility",
                expiry_minutes=5,
                max_hold_minutes=15,
                cooldown_minutes=2,
                state_rule="flow_reversal_two_minutes",
            )
        return dict(
            objective="volatility",
            expiry_minutes=15,
            max_hold_minutes=30,
            cooldown_minutes=5,
            state_rule="flow_reversal_two_minutes",
        )
    if h.source_phase == 23:
        if key == "oi_price_stall":
            return dict(
                objective="risk_multiple",
                expiry_minutes=30,
                max_hold_minutes=60,
                cooldown_minutes=15,
                state_rule="oi_expansion_lost",
            )
        return dict(
            objective="risk_multiple",
            expiry_minutes=60,
            max_hold_minutes=240 if key == "crowding_vulnerability" else 120,
            cooldown_minutes=15,
            state_rule="crowding_normalized"
            if key == "crowding_vulnerability"
            else "oi_expansion_lost",
        )
    trend = key.startswith("ema_cross_trend")
    return dict(
        objective="volatility",
        expiry_minutes=240 if trend else 60,
        max_hold_minutes=480 if trend else 240,
        cooldown_minutes=30,
        state_rule="price_structure_only",
    )


# Contracts only. No new event hypothesis enters the released 28-member experiment.
# A future source phase must register distinct entry identities and its trusted predicate.
EVENT_CONTRACTS = {
    "event_reaction_immediate_short_v1": {
        "side": "short",
        "entry": "trusted_source_high_materiality_bearish_salience",
        "confirmation": "none",
        "reaction_window_minutes": 5,
        "max_hold_minutes": 15,
        "objective": "event_reaction",
        "invalidation": "pre_event_level_reclaimed_or_two_minute_flow_reversal",
        "episode": "canonical_logical_event_id_or_chain_tx_id",
        "registered": False,
    },
    "event_reaction_confirmed_short_v1": {
        "side": "short",
        "entry": "same_event_plus_negative_price_and_flow",
        "confirmation": "negative_complete_minute_price_and_flow",
        "reaction_window_minutes": 15,
        "max_hold_minutes": 30,
        "objective": "event_reaction",
        "invalidation": "pre_event_level_reclaimed_or_two_minute_flow_reversal",
        "episode": "canonical_logical_event_id_or_chain_tx_id",
        "registered": False,
    },
}


def definition() -> dict:
    return {
        "version": VERSION,
        "thesis_version": THESIS_VERSION,
        "exit_version": EXIT_VERSION,
        "entry_universe_id": baseline.universe_id(),
        "hypotheses": [h.model_dump(mode="json") for h in baseline.bootstrap()],
        "mappings": {h.hypothesis_id: mapping(h) for h in baseline.bootstrap()},
        "research_horizons": baseline.HORIZONS,
        "holding_regimes": REGIMES,
        "execution": EXECUTION,
        "risk": RISK,
        "geometry": {
            "prior_complete_minutes": 15,
            "non_microstructure_fallback": "latest_known_1h_bar_technical_else_15m_bar_positioning",
            "volatility_target": "half_mean_true_range_15m_or_half_hourly_log_ATR",
            "invalidation": "prior_15m_extreme_plus_one_spread",
            "risk_multiple": 1.0,
            "structural_target": "opposite_prior_15m_extreme",
            "expiry_min_progress_target_fraction": 0.25,
            "flow_reversal_fraction": 0.35,
            "flow_reversal_minutes": 2,
            "defended_book_remaining_fraction": 0.5,
        },
        "event_contracts": EVENT_CONTRACTS,
    }


@cache
def policy_id() -> str:
    return content_id("paperexecution_nimble_", definition())


def risk_id() -> str:
    return baseline.RiskPolicy().policy_id
