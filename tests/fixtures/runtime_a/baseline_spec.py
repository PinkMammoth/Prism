"""Frozen Phase 25A definitions. Changing a field requires a new universe and run."""

from __future__ import annotations

from typing import Literal

from market_signal.research.discovery.catalogue import strategies
from market_signal.research.lab.common import LabModel, content_id
from market_signal.research.microdir.spec import HYPOTHESES

VERSION = "paper_exploratory_v2_2"
COINS = ("AAVE", "BTC", "ETH", "HYPE", "LINK", "SOL")
HORIZONS = (15, 30, 60, 120, 240, 480)
TECHNICAL_KEYS = (
    "ema_cross_trend[n=20]:1h:long",
    "ema_stretch_fade[k=3.0]:1h:long",
    "ema_stretch_fade[k=4.0]:1h:long",
    "compression_breakout[n=24,pct=0.35]:1h:long",
    "expansion_continuation[k=3.0]:1h:long",
    "expansion_fade[k=3.0]:1h:long",
    "exhaustion[n=24,z=3.0,strong_close=no]:1h:long",
)
TECHNICAL_CONTROL_KEYS = tuple(key.replace(":long", ":short") for key in TECHNICAL_KEYS)
ALL_TECHNICAL_KEYS = TECHNICAL_KEYS + TECHNICAL_CONTROL_KEYS
TECHNICAL_CONTROL_VERDICT_REASON = "contemporary net effect credibly negative"
REJECTIONS = (
    "NO_SIGNAL",
    "DATA_STALE",
    "FEATURE_WARMUP",
    "HYPOTHESIS_NOT_REGISTERED",
    "COOLDOWN",
    "MAX_POSITIONS",
    "EXPOSURE_CAP",
    "DUPLICATE_EPISODE",
    "MIN_NOTIONAL",
    "COST_FILTER",
    "INVALID_TIMING",
    "CONTEXT_UNAVAILABLE",
    "CONFLICT",
    "CATASTROPHE_GUARD",
    "KILL_SWITCH",
    "HYPOTHESIS_DISABLED",
    "FUNDING_UNAVAILABLE",
    "EXECUTION_EXPIRED",
)
LIFECYCLE = ("ENABLED_EXPLORATORY", "DISABLED", "DEGRADED", "RETIRED")


class Hypothesis(LabModel):
    name: str
    family: str
    source_phase: Literal[22, 23, 24]
    side: Literal["long", "short"]
    source_key: str
    horizon_minutes: Literal[15, 30, 60, 120, 240, 480]
    cadence_minutes: Literal[15, 60]
    feature_version: str
    scientific_verdict: str | None = None
    parameters: dict
    definition_version: Literal["paper_hypothesis_v2_1"] = "paper_hypothesis_v2_1"

    @property
    def hypothesis_id(self) -> str:
        return content_id("paperhyp_v2_", self.model_dump(mode="json"))

    @property
    def cooldown_minutes(self) -> int:
        return max(15, self.horizon_minutes // 2)


def bootstrap() -> tuple[Hypothesis, ...]:
    catalogue = {s.key: s for s in strategies()}
    out = []
    for key in ALL_TECHNICAL_KEYS:
        s = catalogue[key]
        control = key in TECHNICAL_CONTROL_KEYS
        out.append(
            Hypothesis(
                name=key,
                family=("EXPLORATORY_TECHNICAL_CONTROL:" if control else "EXPLORATORY_TECHNICAL:")
                + s.bh_family,
                source_phase=22,
                side=s.side,
                source_key=key,
                horizon_minutes=s.primary_horizon * 60,
                cadence_minutes=60,
                feature_version=s.feature_version,
                scientific_verdict="REJECTED" if control else "INTERESTING",
                parameters={
                    "strategy": s.model_dump(mode="json"),
                    "strategy_id": s.strategy_id,
                    "warmup_rule": "required_primitives_finite_current_and_previous_v1",
                    "baseline_digest": "d6c9e4a060b69b02da40c3a260a40a337aaa8a3007e6ac382ce87ca089b7c4ad",
                    **(
                        {"scientific_verdict_reasons": [TECHNICAL_CONTROL_VERDICT_REASON]}
                        if control
                        else {}
                    ),
                },
            )
        )
    for rule, horizon in (
        ("crowding_vulnerability", 120),
        ("oi_price_stall", 60),
        ("oi_directional_pressure", 120),
    ):
        for side in ("long", "short"):
            out.append(
                Hypothesis(
                    name=f"{rule}_{side}_v1",
                    family="positioning:" + rule,
                    source_phase=23,
                    side=side,
                    source_key=rule,
                    horizon_minutes=horizon,
                    cadence_minutes=60,
                    feature_version="positioning_context_v1",
                    parameters={
                        "oi_change_1h_pct_min": 0.5,
                        "price_progress_1h_pct": 0.1,
                        "funding_hourly_extreme": 0.000025,
                        "skew": "crowded_short_like" if side == "long" else "crowded_long_like",
                    },
                )
            )
    # Exact Phase 24B predicates, symmetric sides; no new statistical admission gates.
    for key, horizon in (
        ("continuation_core", 15),
        ("absorption_book", 60),
        ("absorption_oi_rising", 60),
        ("continuation_persistent", 30),
    ):
        src = next(h for h in HYPOTHESES if h.key == key)
        for side in ("long", "short"):
            out.append(
                Hypothesis(
                    name=f"{key}_{side}_v1",
                    family="microstructure:" + src.family,
                    source_phase=24,
                    side=side,
                    source_key=key,
                    horizon_minutes=horizon,
                    cadence_minutes=15,
                    feature_version="microdir_features_v1",
                    parameters={
                        "action": src.action,
                        "requires": list(src.requires),
                        "normalization_min_prior_windows": 288,
                    },
                )
            )
    return tuple(out)


class AdmissionPolicy(LabModel):
    name: Literal["paper_exploratory_admission_v2"] = "paper_exploratory_admission_v2"
    version: Literal[1] = 1
    mode: Literal["paper"] = "paper"
    max_signal_age_minutes: int = 20
    conflict_rule: Literal["cancel_both"] = "cancel_both"
    episode_minutes: int = 10
    graduation_required: Literal[False] = False
    min_closed_for_degradation: int = 100
    degradation_recent_trades: int = 50
    degradation_mean_net_return: float = -0.005
    dormant_days: int = 30

    @property
    def policy_id(self) -> str:
        return content_id("paperadmission_v2_", self.model_dump(mode="json"))


class RiskPolicy(LabModel):
    name: Literal["paper_exploratory_risk_v2"] = "paper_exploratory_risk_v2"
    version: Literal[1] = 1
    mode: Literal["paper"] = "paper"
    starting_equity: float = 100.0
    notional_fraction: float = 0.20
    leverage: float = 2.0
    max_positions: int = 3
    gross_fraction: float = 0.60
    catastrophe_drawdown: float = 0.75
    min_notional: float = 10.0
    max_entry_wait_minutes: int = 20
    margin_mode: Literal["isolated"] = "isolated"
    averaging: Literal[False] = False
    pyramiding: Literal[False] = False
    size_decimals: dict[str, int] = {"BTC": 5, "ETH": 4, "SOL": 2, "HYPE": 2, "LINK": 1, "AAVE": 2}
    max_leverage: dict[str, int] = {
        "BTC": 40,
        "ETH": 25,
        "SOL": 20,
        "HYPE": 10,
        "LINK": 10,
        "AAVE": 10,
    }

    @property
    def policy_id(self) -> str:
        return content_id("paperrisk_v2_", self.model_dump(mode="json"))


class ExecutionPolicy(LabModel):
    name: Literal["paper_exploratory_execution_v2"] = "paper_exploratory_execution_v2"
    version: Literal[1] = 1
    mode: Literal["paper"] = "paper"
    fee_bps: float = 4.5
    slippage_bps: dict[str, float] = {"BTC": 2, "ETH": 2, "SOL": 4, "HYPE": 6, "LINK": 6, "AAVE": 8}
    entry: Literal["first_future_complete_minute_mid_else_15m_open"] = (
        "first_future_complete_minute_mid_else_15m_open"
    )
    exit: Literal["fixed_horizon_first_complete_quote_at_or_after"] = (
        "fixed_horizon_first_complete_quote_at_or_after"
    )
    funding: Literal["hourly_settled_entry_notional"] = "hourly_settled_entry_notional"

    @property
    def policy_id(self) -> str:
        return content_id("paperexecution_v2_", self.model_dump(mode="json"))


def definition() -> dict:
    return {
        "version": VERSION,
        "hypotheses": [h.model_dump(mode="json") for h in bootstrap()],
        "admission": AdmissionPolicy().model_dump(mode="json"),
        "risk": RiskPolicy().model_dump(mode="json"),
        "execution": ExecutionPolicy().model_dump(mode="json"),
        "horizons_minutes": HORIZONS,
        "coins": COINS,
    }


def universe_id() -> str:
    return content_id("paperuniverse_v2_", definition())
