"""Causal input and immutable thesis values. No network or execution transports."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import pandas as pd
from pydantic import Field

from market_signal.paper.v2.data import ts
from market_signal.research.lab.common import LabModel, content_id

from . import spec


@dataclass(frozen=True)
class ExecutableQuote:
    asset: str
    at: str
    available_at: str
    bid: float
    ask: float
    received_at: str | None = None
    kind: str = "live_book"
    high: float | None = None
    low: float | None = None
    interval_start: str | None = None
    session_id: str | None = None

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    def valid(self, now, *, fresh=True):
        return (
            all(math.isfinite(v) and v > 0 for v in (self.bid, self.ask))
            and self.bid <= self.ask
            and ts(self.at)
            <= ts(self.received_at or self.available_at)
            <= ts(self.available_at)
            <= ts(now)
            and (
                not fresh
                or ts(now) - ts(self.received_at or self.available_at)
                <= pd.Timedelta(seconds=spec.EXECUTION["quote_max_age_seconds"])
            )
            and (
                not fresh
                or ts(now) - ts(self.at)
                <= pd.Timedelta(seconds=spec.EXECUTION["quote_max_age_seconds"])
            )
        )


@dataclass(frozen=True)
class StateUpdate:
    asset: str
    observed_at: str
    available_at: str
    values: dict


class ExecutionThesis(LabModel):
    version: Literal["execution_thesis_v1"] = "execution_thesis_v1"
    hypothesis_id: str
    asset: str
    side: Literal["long", "short"]
    trigger_at: str
    evidence_available_at: str
    frozen_at: str
    context_snapshot: dict
    positioning_state: dict
    microstructure_state: dict
    entry_reference_price: float = Field(gt=0, allow_inf_nan=False)
    objective: dict
    invalidation: dict
    expiry: dict
    max_hold_minutes: int = Field(gt=0, le=480)
    execution_policy_id: str
    thesis_policy_version: str = spec.THESIS_VERSION
    exit_policy_version: str = spec.EXIT_VERSION
    research_horizons_minutes: tuple[int, ...]
    episode_id: str
    source_latency: dict

    @property
    def thesis_id(self):
        return content_id("executionthesis_", self.model_dump(mode="json"))


def sign(side):
    return 1 if side == "long" else -1


def fill(q, side, *, entry):
    buy = (side == "long") == entry
    px = q.ask if buy else q.bid
    return px * (1 + (1 if buy else -1) * spec.EXECUTION["slippage_bps"][q.asset] / 10000)


def economics(thesis, q, funding_rate=0.0):
    side = thesis.side
    target_move = sign(side) * (thesis.objective["price"] / q.mid - 1)
    spread = (q.ask - q.bid) / q.mid
    fees = 2 * spec.EXECUTION["fee_bps"] / 10000
    slip = 2 * spec.EXECUTION["slippage_bps"][q.asset] / 10000
    # At most one first settlement estimate per hour within the maximum backstop.
    settlements = math.ceil(thesis.max_hold_minutes / 60)
    adverse_funding = max(0.0, sign(side) * funding_rate) * settlements
    costs = spread + fees + slip + adverse_funding
    return {
        "target_move": target_move,
        "expected_round_trip_cost": costs,
        "fees": fees,
        "spread": spread,
        "slippage": slip,
        "adverse_funding_reserve": adverse_funding,
        "break_even_move": costs,
        "economic": target_move >= spec.EXECUTION["economic_cost_multiple"] * costs,
    }


def build_thesis(h, observation, q, geometry, now) -> ExecutionThesis:
    """Levels are fixed from known structure. Economics never enlarges a target to pass."""
    p = spec.mapping(h)
    s = sign(h.side)
    ref = q.mid
    low, high = geometry["local_low"], geometry["local_high"]
    spread = q.ask - q.bid
    invalidation = low - spread if s > 0 else high + spread
    risk_distance = s * (ref - invalidation)
    if not (0 < low <= high and risk_distance > 0):
        raise ValueError("INVALID_GEOMETRY")
    family = p["objective"]
    if family == "event_reaction":
        invalidation = geometry["pre_event_price"]
        if s * (ref - invalidation) <= 0:
            raise ValueError("INVALID_GEOMETRY")
    if family == "structural":
        target = high if s > 0 else low
    elif family == "risk_multiple":
        target = ref + s * risk_distance
    elif family == "event_reaction":
        target = ref * (1 + s * geometry["event_reaction_move"])
    else:
        target = ref * (1 + s * geometry["volatility_move"] * 0.5)
    ctx = observation.context or {}
    ev = observation.evidence or {}
    latency = ev.get("source_latency", {})
    # Scientific horizons are the original primary plus shared comparison horizons.
    return ExecutionThesis(
        hypothesis_id=h.hypothesis_id,
        asset=observation.asset,
        side=h.side,
        trigger_at=observation.signal_at,
        evidence_available_at=observation.available_at,
        frozen_at=ts(now).isoformat(),
        context_snapshot=ctx,
        positioning_state=ev.get("positioning", ctx.get("positioning") or {}),
        microstructure_state=geometry.get("microstructure", {}),
        entry_reference_price=ref,
        objective={"family": family, "price": target, "definition": p},
        invalidation={
            "price": invalidation,
            "family": "prior_structure",
            "state_rule": p["state_rule"],
            "emergency_price": ref * (1 - s * spec.EXECUTION["emergency_adverse_move"]),
        },
        expiry={
            "reaction_window_minutes": p["expiry_minutes"],
            "minimum_progress_target_fraction": 0.25,
        },
        max_hold_minutes=p["max_hold_minutes"],
        execution_policy_id=spec.policy_id(),
        research_horizons_minutes=tuple(
            sorted(
                set(
                    (
                        *(
                            spec.REACTION_HORIZONS
                            if h.source_key in spec.EVENT_CONTRACTS
                            else spec.baseline.HORIZONS
                        ),
                        h.horizon_minutes,
                    )
                )
            )
        ),
        episode_id=ev.get("episode_id")
        or content_id(
            "episode_",
            {
                "hypothesis": h.hypothesis_id,
                "asset": observation.asset,
                "signal_at": observation.signal_at,
            },
        ),
        source_latency=latency,
    )
