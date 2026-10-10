"""Phase 28A definitions and submission policy. No trading dependencies."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field

from market_signal.context.model import Short
from market_signal.context.taxonomy import SUBCATEGORIES
from market_signal.research.lab.common import LabModel, UTCDateTime

LEGACY_PROVIDER = "chatgpt_work_v1"
PROVIDER_IDS = (
    "chatgpt_work_crypto_breaking_v1",
    "chatgpt_work_exchange_structure_v1",
    "chatgpt_work_macro_risk_v1",
    "chatgpt_work_regulatory_institutional_v1",
    "chatgpt_work_technical_tailrisk_v1",
)
ALLOWED = {
    PROVIDER_IDS[0]: {
        "exploit_suspected",
        "exploit_confirmed",
        "bridge_exploit",
        "stolen_funds_movement",
        "key_compromise",
        "emergency_pause",
        "stablecoin_depeg",
        "chain_halt",
        "validator_issue",
        "treasury_action",
        "tokenomics_change",
        "token_unlock",
        "governance_result",
    },
    PROVIDER_IDS[1]: {
        "exchange_outage",
        "exchange_deposit_withdrawal_disruption",
        "exchange_insolvency",
        "listing",
        "delisting",
        "risk_parameter_change",
        "custody_disruption",
    },
    PROVIDER_IDS[2]: {
        "us_cpi",
        "us_core_cpi",
        "us_pce",
        "us_core_pce",
        "us_nfp",
        "us_unemployment",
        "us_gdp",
        "us_ppi",
        "us_retail_sales",
        "us_ism_pmi",
        "fomc_decision",
        "boj_decision",
        "ecb_decision",
        "boe_decision",
        "rate_decision_other",
        "inflation_release_other",
        "liquidity_fixture",
        "central_bank_statement",
        "global_risk_shock",
    },
    PROVIDER_IDS[3]: {"regulatory_action", "lawsuit", "treasury_flow"},
    PROVIDER_IDS[4]: {"critical_vulnerability"},
}


class MonitorEvidence(LabModel):
    """Sender assertions, never a materiality override or verified truth."""

    policy_version: Literal["work_sensor_policy_v1"]
    novelty: Literal["new", "update", "corroboration"]
    information_time: UTCDateTime = Field(
        description="Time of this new fact/publication, not old episode start."
    )
    immediate_impact: Literal[True]
    impact_reason: Short
    evidence: Literal["primary", "direct_evidence", "credible_reporting"]


def provider(sender: str) -> str:
    if (
        sender.startswith("chatgpt_work_")
        and sender not in PROVIDER_IDS
        and any(
            word in sender
            for word in (
                "crypto_breaking",
                "exchange_structure",
                "macro_risk",
                "regulatory_institutional",
                "technical_tailrisk",
            )
        )
    ):
        raise ValueError("unknown monitor version")
    return sender if sender in PROVIDER_IDS else LEGACY_PROVIDER


def check(sub, received: datetime) -> None:
    pid = provider(sub.sender_version)
    if pid == LEGACY_PROVIDER:
        if sub.monitor is not None:
            raise ValueError("monitor evidence requires a registered specialist")
        return
    evidence, item = sub.monitor, sub.item
    if evidence is None:
        raise ValueError("specialist requires monitor evidence")
    if item.subcategory not in ALLOWED[pid]:
        raise ValueError("event belongs to another monitor or is ordinary news")
    if not item.factual_claims or item.scheduled:
        raise ValueError("specialists require facts and report developments, not schedules")
    if not received - timedelta(hours=2) <= evidence.information_time <= item.first_seen_at:
        raise ValueError("new information must be within the two-hour breaking window")
    if item.published_at and item.published_at > item.first_seen_at:
        raise ValueError("discovery precedes publication")
    text = "\n".join(
        (
            item.title,
            item.summary,
            *item.factual_claims,
            evidence.impact_reason,
            *(str(v) for v in item.attributes.facts.values()),
        )
    )
    if re.search(
        r"(?im)(?:^|[.!?]\s+)(?:short|long|buy|sell)\s+[A-Z0-9]+\b|"
        r"\b(?:you should|recommend(?:ed)? (?:buy|sell|short|long)|set (?:a )?(?:stop|tp)|"
        r"go (?:long|short)|position size|take.profit|stop.loss|"
        r"(?:open|enter|close|exit) (?:a |the )?(?:long|short|position|trade)|"
        r"(?:place|submit|execute) (?:a |the )?(?:\w+ )?(?:buy|sell|order)|"
        r"allocate \d+(?:\.\d+)?%)",
        text,
    ):
        raise ValueError("monitor text contains a trade instruction")


def definitions() -> dict:
    from market_signal.config import get_settings

    path = get_settings().paths.root / "config/context/work_monitors.v1.yaml"
    config = yaml.safe_load(Path(path).read_text())
    for pid, monitor in config["monitors"].items():
        monitor["allowed_pairs"] = [f"{SUBCATEGORIES[s].value}/{s}" for s in sorted(ALLOWED[pid])]
        monitor["proposed_rrule"] = (
            f"RRULE:FREQ=MINUTELY;INTERVAL={monitor['proposed_interval_minutes']}"
        )
        monitor["installation_state"] = "definition_only_not_a_saved_schedule"
    return config


def prompt(config: dict, pid: str) -> str:
    m = config["monitors"][pid]
    pairs = ", ".join(m["allowed_pairs"])
    return (
        f"Provider/sender_version: {pid}.\n{config['base_policy'].strip()}\n\n"
        f"Allowed category/subcategory pairs: {pairs}.\n{m['prompt'].strip()}"
    )
