"""Phase 30 frozen reaction rules. No text sentiment or statistical fitting."""

from functools import cache

from market_signal.research.lab.common import LabModel, content_id

from . import spec

VERSION = "paper_nimble_event_v1"
HORIZONS = (1, 5, 15, 30, 60, 120, 240)
REJECTIONS = (
    "STALE_EVENT",
    "LOW_MATERIALITY",
    "UNSUPPORTED_EVENT_CLASS",
    "AMBIGUOUS_DIRECTION",
    "ASSET_OUTSIDE_UNIVERSE",
    "VENUE_UNAVAILABLE",
    "UNECONOMIC_TARGET",
    "CONFIRMATION_MISSING",
    "DATA_DEGRADED",
    "DUPLICATE_EPISODE",
    "CONFLICTING_EVENT",
    "EXPOSURE_LIMIT",
)
# Structured Statuspage component names, not incident prose. Unknown components fail closed.
TRADING_COMPONENTS = {
    "coinbase": ("Coinbase - Advanced Trade", "Trading", "Coinbase - API"),
    "kraken": ("REST", "Kraken API"),
    "hyperliquid": ("API", "Hyperliquid L1"),
    "solana": ("Mainnet Beta - Cluster",),
}
TRADING_COMPONENT_IDS = {
    "coinbase": ("jgjv19xyfvpv", "tjhgmj0ysvln", "zzhmzjz0wqj6"),
    "kraken": ("cfpbbhgnlnbf", "qkyy5vkk5zpx"),
    "hyperliquid": ("s68kw588jdx3", "jlh8rvlcnhcv"),
    "solana": ("qxs6x3yc9cxg",),
}
OFFICIAL_STATUS = {
    "coinbase_status_v1": ("coinbase", "status.coinbase.com"),
    "kraken_status_v1": ("kraken", "status.kraken.com"),
    "hyperliquid_status_v1": ("hyperliquid", "hyperliquid.statuspage.io"),
    "solana_status_v1": ("solana", "status.solana.com"),
}
PROTOCOL_ASSETS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "hyperliquid": "HYPE",
    "chainlink": "LINK",
    "aave": "AAVE",
}
FAMILIES = {
    "venue_disruption": dict(
        side="short",
        assets=("BTC",),
        categories=("exchange_outage",),
        age_seconds=300,
        windows=(20, 45),
        holds=(60, 90),
        materiality=40,
    ),
    "protocol_stress": dict(
        side="short",
        assets=tuple(spec.baseline.COINS),
        categories=("chain_halt", "exploit_confirmed", "emergency_pause"),
        age_seconds=3600,
        windows=(60, 120),
        holds=(120, 240),
        materiality=60,
    ),
    "sec_spot_etf_approval_surprise": dict(
        side="long",
        assets=("BTC", "ETH"),
        categories=("regulatory_action",),
        age_seconds=5400,
        windows=(120, 240),
        holds=(240, 480),
        materiality=60,
    ),
}


class Hypothesis(LabModel):
    name: str
    family: str
    side: str
    source_phase: int = 30
    source_key: str
    variant: str
    horizon_minutes: int
    parameters: dict
    feature_version: str = VERSION

    @property
    def hypothesis_id(self):
        return content_id("eventhyp_v1_", self.model_dump(mode="json"))


@cache
def bootstrap():
    return tuple(
        Hypothesis(
            name=f"{family}_{variant}_v1",
            family="event:" + family,
            source_key=family,
            side=rule["side"],
            variant=variant,
            horizon_minutes=rule["windows"][i],
            parameters={**rule, "variant": variant},
        )
        for family, rule in FAMILIES.items()
        for i, variant in enumerate(("immediate", "confirmed"))
    )


def mapping(h):
    i = h.variant == "confirmed"
    return dict(
        objective="event_reaction",
        expiry_minutes=FAMILIES[h.source_key]["windows"][i],
        max_hold_minutes=FAMILIES[h.source_key]["holds"][i],
        cooldown_minutes=2,
        state_rule="event_reaction",
    )


@cache
def definition():
    return {
        **spec.definition(),
        "version": VERSION,
        "baseline_policy_id": spec.policy_id(),
        "baseline_entry_universe_id": spec.baseline.universe_id(),
        "entry_universe_id": content_id(
            "paperuniverse_event_registry_",
            [h.model_dump(mode="json") for h in (*spec.baseline.bootstrap(), *bootstrap())],
        ),
        "hypotheses": [
            h.model_dump(mode="json") for h in (*spec.baseline.bootstrap(), *bootstrap())
        ],
        "mappings": {
            h.hypothesis_id: spec.mapping(h) for h in (*spec.baseline.bootstrap(), *bootstrap())
        },
        "event_rules": FAMILIES,
        "official_status": OFFICIAL_STATUS,
        "trading_components": TRADING_COMPONENTS,
        "trading_component_ids": TRADING_COMPONENT_IDS,
        "protocol_assets": PROTOCOL_ASSETS,
        "event_rejections": REJECTIONS,
        "classification": {
            "version": "event_classification_v1",
            "confidence": "per_report_Phase23_source_cap_at_least_REPORTED; never_merged_prose_facts",
            "venue": "allowlisted_official_status_major_or_critical_impact_and_major_outage_trading_component_and_investigating_or_identified",
            "protocol": "SOL_official_major_or_critical_cluster_outage_or_explicit_native_protocol_plus_direct_asset_confirmed_exploit_or_pause_loss_at_least_10000000usd",
            "protocol_min_loss_usd": 10_000_000,
            "security_source_types": ("ai_monitor", "protocol_announcement", "security_advisory"),
            "regulatory": "SEC_official_structured_spot_etf_approved_BTC_or_ETH_same_docket_as_prior_official_rejected_expectation_known_before_first_seen_within_24h",
            "research_only": (
                "stablecoin_depeg",
                "bridge_exploit",
                "generic_regulatory_action",
                "lawsuit",
                "critical_vulnerability",
                "exchange_deposit_withdrawal_disruption",
                "ordinary_releases",
            ),
            "resolution": "monitoring_resolved_completed_denied_no_opposite_trade",
            "freshness": "publication_or_structured_official_state_transition_information_time; prose_and_corroboration_do_not_refresh",
            "episode": "one_logical_event_asset_per_cohort_ever; no_reopen_on_revision_or_escalation",
            "confluence_minutes": 15,
            "venue_safety": "active_major_or_critical_official_Hyperliquid_exchange_incident_blocks_successor_entry_and_exit_fills_until_official_recovery",
        },
        "event_research_horizons": HORIZONS,
        "availability": "selected_report_receipt_matched_context_outbox_clock; legacy_missing_receipt_uses_first_notice_not_before_report_observation",
        "confirmation": "first_fully_post_availability_complete_minute_signed_return_gt_0_and_signed_taker_fraction_ge_0.10; latest_signal_at_most_90s_old",
        "objective": "pre_event_mid_plus_side_half_pre_event_15m_volatility_sqrt_window_over_15",
        "reference": "latest_complete_production_bid_ask_known_before_first_seen; age_at_most_120s",
        "invalidation": "pre_event_mid_plus_one_entry_spread_against_side_or_two_opposing_flow_minutes_or_resolution_or_conflict",
        "conflict_seconds": 900,
        "cohorts": "independent_100usd_immediate_and_confirmed_accounts_no_pooled_evidence",
    }


@cache
def policy_id():
    return content_id("paperexecution_event_", definition())


@cache
def universe_id(variant):
    return content_id(
        "paperuniverse_event_",
        {
            "policy_id": policy_id(),
            "variant": variant,
            "hypotheses": [
                h.hypothesis_id
                for h in (*spec.baseline.bootstrap(), *bootstrap())
                if h.source_phase != 30 or h.variant == variant
            ],
        },
    )
