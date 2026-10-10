"""Context taxonomy, confidence ladder, relevance windows and materiality rules (all versioned).

Everything here is a fixed, reviewed table. Changing any table is a new version string, never
an edit in place, because stored events record the version they were classified under.

Materiality means *potentially market-relevant*. It is never a direction, a trade signal or a
probability of profit. No rule here maps an event to long/short.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

TAXONOMY_VERSION = "context_taxonomy_v2"
MATERIALITY_VERSION = "materiality_v1"
RELEVANCE_VERSION = "relevance_windows_v1"


class Category(StrEnum):
    MACRO = "macro"  # scheduled macro / financial calendar
    MARKET_STRUCTURE = "market_structure"  # exchanges, listings, stablecoins, ETF flows
    PROTOCOL = "protocol"  # protocol / asset-specific developments
    SECURITY = "security"  # exploits, key compromises, emergency pauses


# subcategory -> category. Bounded v1 vocabulary; anything else is rejected at validation.
SUBCATEGORIES: dict[str, Category] = {
    # Phase 28A additive types. Existing v1 classification/materiality tables are unchanged.
    "global_risk_shock": Category.MACRO,
    "critical_vulnerability": Category.SECURITY,
    # macro (scheduled)
    "us_cpi": Category.MACRO, "us_core_cpi": Category.MACRO, "us_pce": Category.MACRO,
    "us_core_pce": Category.MACRO, "us_nfp": Category.MACRO, "us_unemployment": Category.MACRO,
    "us_gdp": Category.MACRO, "us_ppi": Category.MACRO, "us_retail_sales": Category.MACRO,
    "us_ism_pmi": Category.MACRO, "fomc_decision": Category.MACRO, "fed_speech": Category.MACRO,
    "treasury_auction": Category.MACRO, "boj_decision": Category.MACRO,
    "ecb_decision": Category.MACRO, "boe_decision": Category.MACRO,
    "rate_decision_other": Category.MACRO, "inflation_release_other": Category.MACRO,
    "liquidity_fixture": Category.MACRO, "central_bank_statement": Category.MACRO,
    # crypto market structure
    "exchange_outage": Category.MARKET_STRUCTURE,
    "exchange_deposit_withdrawal_disruption": Category.MARKET_STRUCTURE,
    "exchange_insolvency": Category.MARKET_STRUCTURE, "listing": Category.MARKET_STRUCTURE,
    "delisting": Category.MARKET_STRUCTURE, "risk_parameter_change": Category.MARKET_STRUCTURE,
    "stablecoin_depeg": Category.MARKET_STRUCTURE, "etf_flow": Category.MARKET_STRUCTURE,
    "treasury_flow": Category.MARKET_STRUCTURE, "custody_disruption": Category.MARKET_STRUCTURE,
    "exchange_maintenance": Category.MARKET_STRUCTURE,
    # protocol / asset
    "chain_halt": Category.PROTOCOL, "governance_proposal": Category.PROTOCOL,
    "governance_result": Category.PROTOCOL, "token_unlock": Category.PROTOCOL,
    "protocol_upgrade": Category.PROTOCOL, "treasury_action": Category.PROTOCOL,
    "validator_issue": Category.PROTOCOL, "regulatory_action": Category.PROTOCOL,
    "lawsuit": Category.PROTOCOL, "partnership": Category.PROTOCOL,
    "tokenomics_change": Category.PROTOCOL,
    # security
    "exploit_suspected": Category.SECURITY, "exploit_confirmed": Category.SECURITY,
    "bridge_exploit": Category.SECURITY, "stolen_funds_movement": Category.SECURITY,
    "key_compromise": Category.SECURITY, "emergency_pause": Category.SECURITY,
}  # fmt: skip


class Confidence(StrEnum):
    """Evidence level of an event, not of any trade. Updates may raise or lower it."""

    UNCONFIRMED = "UNCONFIRMED"  # rumour / single secondary report
    REPORTED = "REPORTED"  # reputable secondary source (wire, established outlet)
    CONFIRMED = "CONFIRMED"  # corroborated, or acknowledged by the affected party
    OFFICIAL = "OFFICIAL"  # primary source (statistics agency, central bank, exchange notice)
    DENIED = "DENIED"  # retracted / denied by the affected party or the original source


CONFIDENCE_RANK = {Confidence.DENIED: 0, Confidence.UNCONFIRMED: 1, Confidence.REPORTED: 2,
                   Confidence.CONFIRMED: 3, Confidence.OFFICIAL: 4}  # fmt: skip


class SourceType(StrEnum):
    OFFICIAL_STATISTICS = "official_statistics"
    CENTRAL_BANK = "central_bank"
    EXCHANGE_NOTICE = "exchange_notice"
    EXCHANGE_API = "exchange_api"  # structured venue state (e.g. a perp universe diff)
    PROTOCOL_ANNOUNCEMENT = "protocol_announcement"
    SECURITY_ADVISORY = "security_advisory"
    REGULATOR = "regulator"
    NEWS_WIRE = "news_wire"
    NEWS_API = "news_api"
    RSS = "rss"
    SOCIAL = "social"
    AI_MONITOR = "ai_monitor"  # e.g. a ChatGPT Work monitoring session's output
    MANUAL = "manual"


# Tier 1 = official/structured, 2 = quality news/RSS, 3 = AI/social monitoring.
SOURCE_TIER = {
    SourceType.OFFICIAL_STATISTICS: 1, SourceType.CENTRAL_BANK: 1, SourceType.EXCHANGE_NOTICE: 1,
    SourceType.EXCHANGE_API: 1, SourceType.PROTOCOL_ANNOUNCEMENT: 1,
    SourceType.SECURITY_ADVISORY: 1, SourceType.REGULATOR: 1, SourceType.NEWS_WIRE: 2,
    SourceType.NEWS_API: 2, SourceType.RSS: 2, SourceType.SOCIAL: 3, SourceType.AI_MONITOR: 3,
    SourceType.MANUAL: 2,
}  # fmt: skip

# The highest confidence a FIRST report from this source type may claim. A rumour on social
# media cannot arrive as CONFIRMED; corroboration later raises it through an update.
MAX_INITIAL_CONFIDENCE = {
    1: Confidence.OFFICIAL, 2: Confidence.CONFIRMED, 3: Confidence.REPORTED,
}  # fmt: skip


def cap_confidence(requested: Confidence, source_type: SourceType) -> Confidence:
    cap = MAX_INITIAL_CONFIDENCE[SOURCE_TIER[source_type]]
    if requested == Confidence.DENIED:
        return requested
    return requested if CONFIDENCE_RANK[requested] <= CONFIDENCE_RANK[cap] else cap


# --------------------------------------------------------------------------- relevance windows


@dataclass(frozen=True)
class Window:
    """Active horizon around an anchor: scheduled -> ``event_time``; unscheduled -> the
    first time Prism saw it. ``pre`` only applies to scheduled events (an unscheduled event
    cannot be active before Prism knew of it)."""

    pre: timedelta
    post: timedelta


H, D = timedelta(hours=1), timedelta(days=1)
RELEVANCE: dict[str, Window] = {
    "global_risk_shock": Window(0 * H, 4 * H),
    "critical_vulnerability": Window(0 * H, 72 * H),
    "us_cpi": Window(24 * H, 4 * H), "us_core_cpi": Window(24 * H, 4 * H),
    "us_pce": Window(24 * H, 4 * H), "us_core_pce": Window(24 * H, 4 * H),
    "us_nfp": Window(24 * H, 4 * H), "us_unemployment": Window(24 * H, 4 * H),
    "us_gdp": Window(24 * H, 4 * H), "us_ppi": Window(12 * H, 2 * H),
    "us_retail_sales": Window(12 * H, 2 * H), "us_ism_pmi": Window(12 * H, 2 * H),
    "fomc_decision": Window(48 * H, 24 * H), "fed_speech": Window(2 * H, 2 * H),
    "treasury_auction": Window(4 * H, 2 * H), "boj_decision": Window(48 * H, 24 * H),
    "ecb_decision": Window(24 * H, 12 * H), "boe_decision": Window(24 * H, 12 * H),
    "rate_decision_other": Window(24 * H, 12 * H), "inflation_release_other": Window(12 * H, 4 * H),
    "liquidity_fixture": Window(12 * H, 12 * H), "central_bank_statement": Window(0 * H, 12 * H),
    "exchange_outage": Window(0 * H, 24 * H),
    "exchange_deposit_withdrawal_disruption": Window(0 * H, 24 * H),
    "exchange_insolvency": Window(0 * H, 14 * D), "listing": Window(72 * H, 48 * H),
    "delisting": Window(72 * H, 72 * H), "risk_parameter_change": Window(24 * H, 48 * H),
    "stablecoin_depeg": Window(0 * H, 72 * H), "etf_flow": Window(0 * H, 24 * H),
    "treasury_flow": Window(0 * H, 48 * H), "custody_disruption": Window(0 * H, 72 * H),
    "exchange_maintenance": Window(2 * H, 6 * H), "chain_halt": Window(0 * H, 72 * H),
    "governance_proposal": Window(0 * H, 7 * D), "governance_result": Window(0 * H, 72 * H),
    "token_unlock": Window(72 * H, 48 * H), "protocol_upgrade": Window(48 * H, 48 * H),
    "treasury_action": Window(0 * H, 72 * H), "validator_issue": Window(0 * H, 48 * H),
    "regulatory_action": Window(0 * H, 7 * D), "lawsuit": Window(0 * H, 7 * D),
    "partnership": Window(0 * H, 48 * H), "tokenomics_change": Window(0 * H, 7 * D),
    "exploit_suspected": Window(0 * H, 72 * H), "exploit_confirmed": Window(0 * H, 7 * D),
    "bridge_exploit": Window(0 * H, 7 * D), "stolen_funds_movement": Window(0 * H, 72 * H),
    "key_compromise": Window(0 * H, 7 * D), "emergency_pause": Window(0 * H, 72 * H),
}  # fmt: skip

# Unscheduled observations of the same logical event are linked when Prism saw them within
# this long of the event's first observation (and they share category + primary entity).
LINK_WINDOW: dict[Category, timedelta] = {
    Category.MACRO: 12 * H, Category.MARKET_STRUCTURE: 48 * H, Category.PROTOCOL: 72 * H,
    Category.SECURITY: 7 * D,
}  # fmt: skip

# --------------------------------------------------------------------------- materiality v1

# Base importance of the event type (0-100), before confidence, scope and asset relevance.
BASE_IMPORTANCE: dict[str, int] = {
    "global_risk_shock": 80, "critical_vulnerability": 80,
    "us_cpi": 90, "us_core_cpi": 85, "fomc_decision": 95, "us_nfp": 85, "us_pce": 75,
    "us_core_pce": 80, "us_unemployment": 70, "us_gdp": 65, "us_ppi": 55, "us_retail_sales": 50,
    "us_ism_pmi": 50, "fed_speech": 40, "treasury_auction": 35, "boj_decision": 80,
    "ecb_decision": 65, "boe_decision": 55, "rate_decision_other": 40,
    "inflation_release_other": 40, "liquidity_fixture": 40, "central_bank_statement": 45,
    "exchange_outage": 55, "exchange_deposit_withdrawal_disruption": 35,
    "exchange_insolvency": 95, "listing": 50, "delisting": 60, "risk_parameter_change": 45,
    "stablecoin_depeg": 85, "etf_flow": 45, "treasury_flow": 40, "custody_disruption": 70,
    "exchange_maintenance": 15, "chain_halt": 85, "governance_proposal": 30,
    "governance_result": 40, "token_unlock": 45, "protocol_upgrade": 45, "treasury_action": 40,
    "validator_issue": 50, "regulatory_action": 65, "lawsuit": 55, "partnership": 25,
    "tokenomics_change": 50, "exploit_suspected": 60, "exploit_confirmed": 85,
    "bridge_exploit": 85, "stolen_funds_movement": 50, "key_compromise": 80,
    "emergency_pause": 70,
}  # fmt: skip

CONFIDENCE_WEIGHT = {Confidence.DENIED: 0.0, Confidence.UNCONFIRMED: 0.4,
                     Confidence.REPORTED: 0.7, Confidence.CONFIRMED: 0.9,
                     Confidence.OFFICIAL: 1.0}  # fmt: skip
LINK_WEIGHT = {"direct": 1.0, "ecosystem": 0.6, "market_wide": 0.5, "none": 0.0}
SCOPE_BONUS = {"systemic": 10, "exchange": 5, "asset": 0}


def loss_bonus(loss_usd: float | None) -> int:
    """Security incidents: +0..15 by estimated loss (>=$1M +5, >=$10M +10, >=$100M +15)."""
    if not loss_usd or loss_usd <= 0:
        return 0
    return 15 if loss_usd >= 1e8 else 10 if loss_usd >= 1e7 else 5 if loss_usd >= 1e6 else 0


def materiality(subcategory: str, confidence: Confidence, *, link: str = "direct",
                scope: str = "asset", loss_usd: float | None = None) -> dict:  # fmt: skip
    """Deterministic 0-100 score with its components (``materiality_v1``).

    ``link`` is the event's relevance to the asset being asked about (``direct`` when no
    asset is in question). The components are returned so a reader can see why."""
    base = BASE_IMPORTANCE[subcategory]
    raw = min(100, base + SCOPE_BONUS.get(scope, 0) + loss_bonus(loss_usd))
    score = round(raw * CONFIDENCE_WEIGHT[confidence] * LINK_WEIGHT[link])
    version = (
        "work_materiality_v1"
        if subcategory in {"global_risk_shock", "critical_vulnerability"}
        else MATERIALITY_VERSION
    )
    return {"version": version, "score": int(score), "base": base,
            "scope": scope, "loss_bonus": loss_bonus(loss_usd),
            "confidence_weight": CONFIDENCE_WEIGHT[confidence],
            "link": link, "link_weight": LINK_WEIGHT[link],
            "meaning": "potential market relevance only; not a direction"}  # fmt: skip


def severity(score: int) -> str:
    return "high" if score >= 70 else "medium" if score >= 40 else "low"


# Sensitivities of scheduled macro events (descriptive tags, not directions).
MACRO_SENSITIVITY: dict[str, tuple[str, ...]] = {
    "us_cpi": ("usd", "global_rates"), "us_core_cpi": ("usd", "global_rates"),
    "us_pce": ("usd", "global_rates"), "us_core_pce": ("usd", "global_rates"),
    "us_nfp": ("usd", "global_rates"), "us_unemployment": ("usd", "global_rates"),
    "us_gdp": ("usd",), "us_ppi": ("usd",), "us_retail_sales": ("usd",), "us_ism_pmi": ("usd",),
    "fomc_decision": ("usd", "global_rates"), "fed_speech": ("usd",),
    "treasury_auction": ("usd", "global_rates"), "boj_decision": ("jpy", "global_rates"),
    "ecb_decision": ("eur",), "boe_decision": ("gbp",), "liquidity_fixture": ("usd",),
}  # fmt: skip
TIER1_MACRO = frozenset({"us_cpi", "us_core_cpi", "fomc_decision", "us_nfp", "us_pce",
                         "us_core_pce", "boj_decision", "ecb_decision"})  # fmt: skip


def taxonomy() -> dict:
    """Machine-readable dump (for ``market context taxonomy`` and docs)."""
    return {"version": TAXONOMY_VERSION, "materiality_version": MATERIALITY_VERSION,
            "relevance_version": RELEVANCE_VERSION,
            "categories": {c.value: sorted(s for s, k in SUBCATEGORIES.items() if k == c)
                           for c in Category},
            "confidence": [c.value for c in Confidence],
            "source_types": {s.value: SOURCE_TIER[s] for s in SourceType},
            "tier1_macro": sorted(TIER1_MACRO)}  # fmt: skip
