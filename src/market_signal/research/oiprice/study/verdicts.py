"""The frozen verdict policy ``phase19_verdicts_v1``: Phase 18's ladder, read per direction.

Each member is ONE two-sided test. Its two directions get verdicts read from the sign, never
assumed: ``positive`` is continuation (price-oriented members), up (long-oriented) or larger
moves (volatility); ``negative`` is reversal / down / smaller.

- ``INSUFFICIENT``: a preregistered sample or coverage gate failed.
- ``ROBUST ENOUGH FOR NEXT RESEARCH STAGE``: q <= 0.10 in its family with this sign, the
  statistic >= its floor, net > 0 after costs (direction positions), positive-asset share
  >= 60%, every leave-one-asset-out keeps the sign, a PLATEAU neighbourhood, and the other
  venue agrees. Phase 19 has no testable second venue, so this rung is unreachable here.
- ``PROMISING — NEEDS VALIDATION``: as above without the plateau or the agreement.
- ``WEAK / EXPLORATORY``: q <= 0.10 but a substantive gate fails, or one-sided p <= 0.05
  without correction.
- ``REJECTED``: q <= 0.10 with the opposite sign, or the upper 95% bound is below the floor.
- ``NO EVIDENCE``: otherwise.

Floors: 10 bps (direction: net excess; volatility: |log return| excess), 0.03 mean Spearman.
Nothing here can produce "validated".
"""

from __future__ import annotations

from market_signal.research.relative.study.verdicts import (
    INSUFFICIENT,
    NO_EVIDENCE,
    PROMISING,
    REJECTED,
    ROBUST,
    VERDICTS,
    WEAK,
)
from market_signal.research.relative.study.verdicts import verdict as _phase18


def verdict(s: dict, other: dict | None, plateau: str | None, floor: float, kind: str,
            st) -> tuple[str, list[str]]:  # fmt: skip
    """``kind='volatility'`` skips the position (net > 0) gate: magnitude is not a trade."""
    return _phase18(s, other, plateau, floor, kind, st)


__all__ = ["INSUFFICIENT", "NO_EVIDENCE", "PROMISING", "REJECTED", "ROBUST", "VERDICTS", "WEAK",
           "verdict"]  # fmt: skip
