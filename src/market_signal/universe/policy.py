"""Frozen economic policy. A threshold change requires a new version/content identity."""

from dataclasses import asdict, dataclass

from market_signal.research.lab.common import content_id

CORE = ("BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE")


@dataclass(frozen=True)
class Policy:
    version: str = "hyperliquid_dynamic_universe_v1"
    normalization_version: str = "finite_cross_section_midrank_missing_zero_v1"
    movement_version: str = "prospective_10m_log_rv_elapsed_scaled_1h_v1"
    portability_version: str = "hyperliquid_satellite_portability_v1"
    transition_version: str = "consecutive_rank_dwell_risk_override_v1"
    cost_version: str = "depth_impact_conservative_v1"
    candidate_cap: int = 30
    satellite_cap: int = 6
    rich_satellite_cap: int = 6
    cadence_seconds: int = 600
    stale_seconds: int = 180
    listing_days: int = 7
    volume_usd_min: float = 5_000_000
    oi_usd_min: float = 2_000_000
    spread_bps_max: float = 15
    depth_usd_min: float = 25_000
    trades_per_minute_min: float = 2
    promotion_rank: int = 6
    demotion_rank: int = 10
    promotion_evaluations: int = 3
    demotion_evaluations: int = 6
    minimum_dwell_seconds: int = 21_600
    score_min: float = 0.35
    movement_cost_multiple: float = 3
    fee_bps: float = 4.5
    reference_notional_usd: float = 100
    slippage_floor_bps: float = 3
    light_observations: int = 7
    light_max_gap_seconds: int = 900
    micro_prior_windows: int = 288
    micro_history_days: int = 7
    technical_history_days: int = 45
    positioning_observations: int = 2
    positioning_max_history_minutes: int = 140
    funding_max_age_minutes: int = 80
    size_decimals_max: int = 8
    budget_minimum_runway_days: int = 30
    budget_max_age_seconds: int = 86400
    budget_cpu_seconds_per_minute_max: float = 30
    outcome_minutes: tuple[int, ...] = (15, 30, 60, 120, 240)
    movement_weights: tuple[float, ...] = (0.5, 0.3, 0.2)
    participation_weights: tuple[float, ...] = (0.4, 0.3, 0.3)
    positioning_weights: tuple[float, ...] = (0.5, 0.3, 0.2)
    blend_weights: tuple[float, ...] = (0.45, 0.35, 0.20)

    @property
    def identity(self):
        return content_id("universepolicy_", asdict(self))


POLICY = Policy()
