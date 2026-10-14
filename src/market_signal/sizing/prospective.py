"""DISABLED prospective sizing-cohort design and runtime adapter. Never registered or wired.

Future comparison: ``dynamic_lab_fixed_v1`` vs ``dynamic_lab_risk_sizing_v1`` on the same
signal stream, differing only in the sizing policy. Activation requires a separate, reviewed
phase that registers new run identities; this module refuses to size at runtime until then.

LLM boundary: a future LLM may supply a structured thesis (asset, side, levels, context). It
can never supply dollar size, leverage, account risk or liquidation tolerance; those fields do
not exist on ``SizingRequest`` and are always computed here deterministically.
"""

from __future__ import annotations

import math
from copy import deepcopy
from functools import cache

from market_signal.research.lab.common import content_id

from .engine import SizingDecision, size
from .model import AccountSnapshot, OpenExposure, SizingRequest
from .policy import baseline, drawdown_throttled

ENABLED = False  # module constant; there is no environment or config override by design
REGISTERED = False


def candidate():
    """The policy the evidence supports carrying into a prospective paper cohort."""
    return drawdown_throttled(0.005, leverage_cap=2.0)


# Fields a prospective sizing cohort must persist at entry, because the current frozen thesis
# does not: without them the replay cannot reproduce the sizing decision exactly.
REQUIRED_ENTRY_RECORD = (
    "sizing_policy_id",
    "sizing_explanation",
    "volatility_move",
    "regime_ratio",
    "predicted_funding_rate_hourly",
    "venue_max_leverage",
    "size_decimals",
    "depth_usd",
    "equity_at_sizing",
    "peak_equity_at_sizing",
    "open_exposures_at_sizing",
)


@cache
def _design():
    control, sized = baseline(), candidate()
    return {
        "version": "prospective_sizing_cohort_design_v1",
        "enabled": ENABLED,
        "registered": REGISTERED,
        "cohorts": {
            "dynamic_lab_fixed_v1": {
                "sizing_policy": control.identity(),
                "role": "CONTROL",
            },
            "dynamic_lab_risk_sizing_v1": {
                "sizing_policy": sized.identity(),
                "role": "TREATMENT",
            },
        },
        "same_signal_stream": "both cohorts consume one frozen signal/thesis stream; sizing "
        "and size-induced admission are the only differences",
        "starting_equity_usd": 100.0,
        "required_entry_record": REQUIRED_ENTRY_RECORD,
        "ledger": "separate paper run identities; never the six-asset or Phase 30 cohorts",
        "minimum_evidence_before_conclusion": {
            "closed_trades_per_cohort": 200,
            "calendar_days": 60,
            "report": "paired by signal id; R distributions, drawdown, skipped trades",
        },
        "llm_boundary": "an LLM may propose a structured thesis only; size/leverage/risk "
        "are computed by this deterministic policy",
    }


def design() -> dict:
    return deepcopy(_design())


def design_id() -> str:
    return content_id("sizingcohortdesign_", design())


def request_from_thesis(
    thesis: dict,
    quote,
    *,
    cohort: str,
    predicted_funding_rate_hourly: float,
    size_decimals: int,
    venue_max_leverage: float,
    volatility_move: float | None,
    depth_usd: float | None = None,
    regime_ratio: float | None = None,
    slippage_bps: float,
) -> SizingRequest:
    """Pure translation of a frozen ExecutionThesis payload + fill quote into a request."""
    inv = thesis.get("invalidation") or {}
    obj = thesis.get("objective") or {}
    return SizingRequest(
        asset=thesis["asset"],
        side=thesis["side"],
        cohort=cohort,
        hypothesis_id=thesis["hypothesis_id"],
        family=str(obj.get("family", "")),
        decision_at=quote.at,
        bid=quote.bid,
        ask=quote.ask,
        invalidation_price=inv.get("price"),
        target_price=obj.get("price"),
        emergency_price=inv.get("emergency_price"),
        causal_volatility=volatility_move
        if volatility_move is not None and math.isfinite(volatility_move)
        else None,
        slippage_bps=slippage_bps,
        funding_rate_hourly=predicted_funding_rate_hourly,
        max_hold_minutes=int(thesis["max_hold_minutes"]),
        size_decimals=size_decimals,
        venue_max_leverage=venue_max_leverage,
        depth_usd=depth_usd,
        regime_ratio=regime_ratio,
    )


def runtime_size(
    request: SizingRequest,
    equity: float,
    peak_equity: float,
    open_exposures: tuple[OpenExposure, ...],
) -> SizingDecision:
    """Disabled integration point. Raises until a reviewed activation phase flips ENABLED."""
    if not (ENABLED and REGISTERED):
        raise RuntimeError("prospective sizing cohort is not activated (research only)")
    return size(request, AccountSnapshot(equity, peak_equity, open_exposures), candidate())
