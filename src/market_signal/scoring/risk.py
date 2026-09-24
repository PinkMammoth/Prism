"""Position / risk model (conservative, no leverage).

TRADE:       position_fraction = portfolio_risk / distance_to_invalidation
             portfolio_risk    = tier budget (0.5% / 0.75% / 1.0%) × regime multiplier
INVESTMENT:  position_fraction = tier fraction (5% / 7.5% / 10%) × regime multiplier;
             no tight technical stop — thesis-based exits.
Both are capped by max_position_fraction and never exceed 100% gross exposure.
"""

from __future__ import annotations

from dataclasses import dataclass

TIERS = ("actionable", "strong", "exceptional")

INVESTMENT_EXITS = [
    "thesis invalidated (the reason for owning it is no longer true)",
    "fundamentals deteriorate (e.g. TTM revenue/earnings growth turns negative in filings)",
    "valuation becomes excessive (e.g. P/E back above its 5y 80th percentile; HYPE yield < 3%)",
    "a substantially better opportunity exists (score gap >= 15 points)",
    "portfolio concentration becomes excessive (position > max_position_fraction)",
]


@dataclass(frozen=True)
class SizingSuggestion:
    kind: str
    tier: str
    regime_multiplier: float
    portfolio_risk: float | None  # TRADE only
    stop_distance: float | None  # fraction of entry price
    position_fraction: float
    capped: bool
    note: str


def suggest_size(
    kind: str,
    tier: str,
    regime_multiplier: float,
    entry: float | None,
    stop: float | None,
    cfg: dict,
    current_gross_exposure: float = 0.0,
) -> SizingSuggestion | None:
    if tier not in TIERS:
        return None
    max_pos = float(cfg["max_position_fraction"])
    room = max(float(cfg["max_gross_exposure"]) - current_gross_exposure, 0.0)
    if kind == "INVESTMENT":
        frac = float(cfg["investment_fraction"][tier]) * regime_multiplier
        capped = frac > min(max_pos, room)
        return SizingSuggestion(kind, tier, regime_multiplier, None, None, min(frac, max_pos, room), capped,
                                "thesis-based exits; no tight technical stop")  # fmt: skip
    if entry is None or stop is None or not (entry > stop > 0):
        return SizingSuggestion(
            kind,
            tier,
            regime_multiplier,
            None,
            None,
            0.0,
            False,
            "no valid invalidation level: do not size",
        )
    risk = float(cfg["trade_risk"][tier]) * regime_multiplier
    dist = (entry - stop) / entry
    frac = risk / dist
    capped = frac > min(max_pos, room)
    return SizingSuggestion(kind, tier, regime_multiplier, risk, dist, min(frac, max_pos, room), capped,
                            "capped by max position / exposure" if capped else "risk-based")  # fmt: skip
