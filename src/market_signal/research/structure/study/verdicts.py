"""Cross-venue labels and the frozen verdict policy ``phase17_verdicts_v1``.

Verdicts, per primary-family hypothesis (venue, architecture, level kind, rung, direction):

- ``INSUFFICIENT``: a preregistered sample gate failed (too few independent events or
  assets, or too few evaluable paths). Rarity is itself a finding; gates are never relaxed.
- ``ROBUST ENOUGH FOR NEXT RESEARCH STAGE``: q <= target in its family, the substantive
  gates (excess >= economic floor, net mean > 0, positive-asset share, sign survives
  leaving out the largest asset), a PLATEAU parameter neighbourhood, and the other venue
  agrees (same sign, raw one-sided p <= ``cross_venue_p``).
- ``PROMISING — NEEDS VALIDATION``: q <= target and the substantive gates, but no plateau
  or no cross-venue agreement.
- ``WEAK / EXPLORATORY``: q <= target but a substantive gate fails, or q > target with raw
  p <= 0.05 and positive excess.
- ``REJECTED``: the OPPOSITE direction is significant (q <= target, positive excess), or
  the upper 95% bound of the excess is below the economic floor (an economically useful
  effect is excluded).
- ``NO EVIDENCE``: everything else.

Nothing here can produce "validated": the study is EXPLORATORY by definition.
"""

from __future__ import annotations

ROBUST = "ROBUST ENOUGH FOR NEXT RESEARCH STAGE"
PROMISING = "PROMISING — NEEDS VALIDATION"
WEAK = "WEAK / EXPLORATORY"
REJECTED = "REJECTED"
NO_EVIDENCE = "NO EVIDENCE"
INSUFFICIENT = "INSUFFICIENT"
VERDICTS = (ROBUST, PROMISING, WEAK, REJECTED, NO_EVIDENCE, INSUFFICIENT)
OTHER = {"reversal": "continuation", "continuation": "reversal"}


def _sig(s: dict | None, p: float) -> bool:
    return bool(s and s.get("p_value") is not None and s["p_value"] <= p)


def cross_venue_label(a: dict | None, b: dict | None, a_opp: dict | None, b_opp: dict | None,
                      p: float = 0.10) -> str:  # fmt: skip
    """SIMILAR / MIXED / OPPOSITE / INSUFFICIENT for one hypothesis on two venues, from raw
    one-sided p-values (descriptive; venues are never pooled)."""
    if not (a and b and a.get("testable") and b.get("testable")):
        return "INSUFFICIENT"
    sa, sb = _sig(a, p), _sig(b, p)
    na, nb = _sig(a_opp, p), _sig(b_opp, p)
    if (sa and nb) or (sb and na):
        return "OPPOSITE"
    if (sa and sb) or (na and nb) or not (sa or sb or na or nb):
        return "SIMILAR"
    return "MIXED"


def verdict(
    s: dict, opp: dict | None, plateau: str | None, other: dict | None, st
) -> tuple[str, list[str]]:
    """(verdict, reasons) for one hypothesis. ``other`` = the same hypothesis on the other
    venue (None if not run); ``plateau`` = the sensitivity verdict (None if not run)."""
    q = st.q
    if not s.get("testable"):
        return INSUFFICIENT, [s.get("untestable_reason") or "sample gate"]
    reasons = []
    ex = s.get("excess_mean")
    if s.get("q_value") is not None and s["q_value"] <= q:
        lla = s.get("leave_largest_asset_out") or {}
        gates = {
            "excess >= floor": ex is not None and ex >= st.economic_floor,
            "net mean > 0": (s.get("net_mean") or 0) > 0,
            "positive-asset share": (s.get("positive_asset_share") or 0) >= st.min_positive_asset_share,
            "sign survives leave-largest-asset-out": bool(lla.get("sign_survives")),
        }  # fmt: skip
        failed = [k for k, ok in gates.items() if not ok]
        if failed:
            return WEAK, ["q <= target but fails: " + ", ".join(failed)]
        robust = plateau == "PLATEAU"
        agree = bool(other and other.get("testable") and other.get("excess_mean") is not None
                     and other["excess_mean"] > 0 and (other.get("p_value") or 1) <= st.cross_venue_p)  # fmt: skip
        if robust and agree:
            return ROBUST, ["q <= target, substantive gates, plateau, cross-venue agreement"]
        reasons.append(f"q <= target and substantive gates; plateau={plateau}, cross_venue={agree}")
        return PROMISING, reasons
    if (
        opp
        and opp.get("q_value") is not None
        and opp["q_value"] <= q
        and (opp.get("excess_mean") or 0) > 0
    ):
        return REJECTED, ["the opposite direction is significant after correction"]
    hi = (s.get("excess_ci95") or [None, None])[1]
    if hi is not None and hi < st.economic_floor:
        return REJECTED, [
            f"upper 95% bound of excess {hi:.5f} < economic floor {st.economic_floor}"
        ]
    if (s.get("p_value") or 1) <= 0.05 and (ex or 0) > 0:
        return WEAK, ["raw p <= 0.05 but not significant after correction"]
    return NO_EVIDENCE, ["not significant after correction; a useful effect is not excluded"]
