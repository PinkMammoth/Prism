"""Trade, request and account records for counterfactual sizing replay.

Causality is structural: ``SizingRequest`` holds only what was known at the entry decision.
``TradeRecord`` additionally carries the frozen outcome (exit quote, MFE/MAE, realized
funding, exit reason) that the replay uses as an evaluation target, never as a sizing input.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields, replace

INVALIDATION_CLASSES = (
    "EXPLICIT_INVALIDATION",
    "DERIVABLE_INVALIDATION",
    "EXPIRY_ONLY",
    "UNSUITABLE_FOR_RISK_NORMALIZED_SIZING",
)


@dataclass(frozen=True)
class SizingRequest:
    """Everything the sizer may see. No outcome, no confidence score, no LLM opinion."""

    asset: str
    side: str  # long | short
    cohort: str  # CORE | SATELLITE
    hypothesis_id: str
    family: str
    decision_at: str
    bid: float
    ask: float
    invalidation_price: float | None
    target_price: float | None
    emergency_price: float | None
    causal_volatility: float | None  # fraction; the thesis' frozen volatility move
    slippage_bps: float  # frozen per-asset (core) or conservative satellite classification
    funding_rate_hourly: float  # predicted, causal
    max_hold_minutes: int
    size_decimals: int
    venue_max_leverage: float
    depth_usd: float | None = None
    regime_ratio: float | None = None
    invalidation_class: str = "EXPLICIT_INVALIDATION"

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class OpenExposure:
    trade_id: str
    asset: str
    side: str
    cohort: str
    hypothesis_id: str
    notional: float
    margin: float
    risk: float  # intended loss at invalidation, dollars


@dataclass(frozen=True)
class FamilyEvidence:
    """Causal (closed before the decision) outcome summary for one hypothesis."""

    samples: int
    mean_r: float
    lower_r: float  # pessimistic one-sided bound on mean R
    second_moment_r: float


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    peak_equity: float
    open: tuple[OpenExposure, ...] = ()
    halted: bool = False
    evidence: dict = field(default_factory=dict)  # hypothesis_id -> FamilyEvidence

    @property
    def drawdown(self) -> float:
        if not (self.peak_equity > 0 and math.isfinite(self.peak_equity)):
            return 0.0
        return max(0.0, 1 - self.equity / self.peak_equity)


@dataclass(frozen=True)
class TradeRecord:
    # identity / provenance
    source: str
    run_id: str
    trade_id: str
    hypothesis_id: str
    family: str
    asset: str
    side: str
    cohort: str
    signal_at: str
    decision_at: str
    priority: str  # frozen admission order key; ties never fall back to row order
    # causal entry state
    entry_bid: float
    entry_ask: float
    invalidation_price: float | None
    target_price: float | None
    emergency_price: float | None
    causal_volatility: float | None
    slippage_bps: float
    fee_bps: float
    funding_rate_hourly: float
    max_hold_minutes: int
    size_decimals: int
    venue_max_leverage: float
    depth_usd: float | None
    regime_ratio: float | None
    invalidation_class: str
    # frozen outcome (evaluation only)
    entry_at: str
    exit_at: str
    exit_bid: float
    exit_ask: float
    exit_reason: str
    exit_detail: str | None
    mfe: float  # signed favourable move vs entry mid, fraction
    mae: float  # signed adverse move vs entry mid, fraction (<= 0)
    funding_paid_fraction: float  # signed cost per unit notional (+ = paid)
    hold_minutes: float
    # recorded source sizing (reconciliation only)
    recorded_units: float | None = None
    recorded_notional: float | None = None
    recorded_net_pnl: float | None = None
    recorded_equity_at_decision: float | None = None
    synthetic: bool = False

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1

    def request(self) -> SizingRequest:
        return SizingRequest(
            asset=self.asset,
            side=self.side,
            cohort=self.cohort,
            hypothesis_id=self.hypothesis_id,
            family=self.family,
            decision_at=self.decision_at,
            bid=self.entry_bid,
            ask=self.entry_ask,
            invalidation_price=self.invalidation_price,
            target_price=self.target_price,
            emergency_price=self.emergency_price,
            causal_volatility=self.causal_volatility,
            slippage_bps=self.slippage_bps,
            funding_rate_hourly=self.funding_rate_hourly,
            max_hold_minutes=self.max_hold_minutes,
            size_decimals=self.size_decimals,
            venue_max_leverage=self.venue_max_leverage,
            depth_usd=self.depth_usd,
            regime_ratio=self.regime_ratio,
            invalidation_class=self.invalidation_class,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> TradeRecord:
        names = {f.name for f in fields(cls)}
        unknown = set(d) - names
        if unknown:
            raise ValueError(f"unknown trade record fields: {sorted(unknown)}")
        return cls(**d)

    def with_(self, **kw) -> TradeRecord:
        return replace(self, **kw)


OUTCOME_FIELDS = frozenset(
    {
        "entry_at",
        "exit_at",
        "exit_bid",
        "exit_ask",
        "exit_reason",
        "exit_detail",
        "mfe",
        "mae",
        "funding_paid_fraction",
        "hold_minutes",
        "recorded_units",
        "recorded_notional",
        "recorded_net_pnl",
        "recorded_equity_at_decision",
    }
)
