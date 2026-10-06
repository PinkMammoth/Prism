"""The frozen verdict policy ``phase18_verdicts_v1`` (Phase 17's ladder, two-sided members).

Each member is ONE two-sided test; its two directions (continuation = the oriented sign,
reversal = the opposite) get verdicts read from the sign, never assumed:

- ``INSUFFICIENT``: a preregistered sample gate failed (rarity is itself a finding).
- ``ROBUST ENOUGH FOR NEXT RESEARCH STAGE``: q <= target in its family, the direction's
  statistic is positive, the substantive gates hold (statistic >= floor; net > 0 where the
  member is a position; positive-asset share >= 60% where per-asset effects exist; every
  leave-one-asset-out keeps the sign where computed), a PLATEAU neighbourhood, and the
  other venue agrees (same direction, one-sided p <= ``cross_venue_p``).
- ``PROMISING — NEEDS VALIDATION``: as above without the plateau or the agreement.
- ``WEAK / EXPLORATORY``: q <= target but a substantive gate fails, or q > target with
  one-sided p <= 0.05 in this direction.
- ``REJECTED``: q <= target with the OPPOSITE sign, or the upper 95% bound of this
  direction's statistic is below the floor (a useful effect is excluded).
- ``NO EVIDENCE``: everything else.

Floors: 10 bps of net excess (events, contrasts, spreads), 0.03 of mean Spearman (IC),
0.03 of probability (bucket persistence). Nothing here can produce "validated".
"""

from __future__ import annotations

from market_signal.research.structure.study.verdicts import (
    INSUFFICIENT,
    NO_EVIDENCE,
    PROMISING,
    REJECTED,
    ROBUST,
    VERDICTS,
    WEAK,
    cross_venue_label,
)


def verdict(s: dict, other: dict | None, plateau: str | None, floor: float, kind: str,
            st) -> tuple[str, list[str]]:  # fmt: skip
    """(verdict, reasons) for one member read in one direction (``analysis.directional``)."""
    if not s.get("testable"):
        return INSUFFICIENT, ["sample gate"]
    stat = s.get("stat")
    q = s.get("q_value")
    if q is not None and q <= st.q:
        if stat is not None and stat > 0:
            gates = {"statistic >= floor": stat >= floor}
            if kind in ("event", "contrast", "spread"):
                gates["net > 0"] = (s.get("net_mean") or 0) > 0
            if s.get("positive_asset_share") is not None:
                gates["positive-asset share"] = (
                    s["positive_asset_share"] >= st.min_positive_asset_share
                )
            if s.get("loo_all_same_sign") is not None:
                gates["sign survives every leave-one-asset-out"] = bool(s["loo_all_same_sign"])
            failed = [k for k, ok in gates.items() if not ok]
            if failed:
                return WEAK, ["q <= target but fails: " + ", ".join(failed)]
            agree = bool(other and other.get("testable") and (other.get("stat") or 0) > 0
                         and (other.get("p_value") or 1) <= st.cross_venue_p)  # fmt: skip
            if plateau == "PLATEAU" and agree:
                return ROBUST, ["q <= target, substantive gates, plateau, cross-venue agreement"]
            return PROMISING, [
                f"q <= target and substantive gates; plateau={plateau}, cross_venue={agree}"
            ]
        return REJECTED, ["the opposite direction is significant after correction"]
    hi = (s.get("ci95") or [None, None])[1]
    if hi is not None and hi < floor:
        return REJECTED, [f"upper 95% bound {hi:.5f} < floor {floor}"]
    if (s.get("p_value") or 1) <= 0.05 and (stat or 0) > 0:
        return WEAK, ["one-sided p <= 0.05 but not significant after correction"]
    return NO_EVIDENCE, ["not significant after correction; a useful effect is not excluded"]


__all__ = [
    "INSUFFICIENT",
    "NO_EVIDENCE",
    "PROMISING",
    "REJECTED",
    "ROBUST",
    "VERDICTS",
    "WEAK",
    "cross_venue_label",
    "verdict",
]  # fmt: skip
