"""Versioned co-pilot policy: is a CURRENT Strategy Lab signal worth showing to a human?

The policy reads an evidence view (built from governed Lab records) and a live-signal
context, and returns ALERT or SUPPRESS with explicit, deterministic reasons. It never
modifies evidence. Changing any threshold or rule is a new version, hence a new
``policy_id``; decisions record the policy that made them.

Priorities are alert-priority labels for a human reader, not trading recommendations:

- ``WATCH``: historically interesting; evidence exploratory or limited.
- ``STRONG_WATCH``: interesting with stronger supporting research (see ``priority``).

The policy is deliberately less strict than a future automated-execution policy would be:
FDR survival, supportive validation and mature forward evidence are shown as caveats, not
required. Co-pilot eligibility is never auto-trader eligibility.
"""

from __future__ import annotations

from typing import Literal

from market_signal.research.lab.common import LabModel, Name, Number, PositiveInt, Probability
from market_signal.research.lab.common import content_id as _content_id

Decision = Literal["ALERT", "SUPPRESS"]
Priority = Literal["WATCH", "STRONG_WATCH"]


class CopilotPolicy(LabModel):
    """Every field that affects a decision or a priority is part of the identity."""

    name: Name = "copilot_policy"
    version: PositiveInt = 1
    consumer: Literal["perps_copilot"] = "perps_copilot"
    # --- blocking rules (a fired signal is SUPPRESSED if any fails)
    eligible_tiers: tuple[str, ...] = ("EXPLORATORY", "RESEARCH_SUPPORTED")
    min_independent_events: PositiveInt = 30
    min_assets_with_events: PositiveInt = 3
    min_excess: Number = 0.0  # primary-horizon net excess must be strictly above this
    max_asset_event_share: Probability = 0.5
    block_dominated_by_one_asset: bool = True
    block_isolated_spike: bool = True
    adverse_full_research: tuple[str, ...] = ("FULL_RESEARCH_INCONSISTENT",)
    adverse_validation: tuple[str, ...] = ("VALIDATION_ADVERSE",)
    # forward evidence blocks only when it is mature AND points the other way
    adverse_forward_levels: tuple[str, ...] = ("MATURE",)
    block_retired_strategy: bool = True  # every forward tracking of the strategy stopped
    max_bar_interval_hours: Number = 24.0  # data gap inside the lookback -> broken data
    # --- priority rules (only for ALERTs)
    strong_tiers: tuple[str, ...] = ("RESEARCH_SUPPORTED",)
    strong_validation: tuple[str, ...] = ("VALIDATION_SUPPORTIVE",)
    strong_full_research: tuple[str, ...] = ("FULL_RESEARCH_CONSISTENT",)
    strong_neighbourhood: tuple[str, ...] = ("plateau",)
    strong_min_assets: PositiveInt = 5
    strong_min_positive_asset_share: Probability = 0.6
    downgrade_validation: tuple[str, ...] = ("VALIDATION_MIXED",)
    downgrade_forward_levels: tuple[str, ...] = ("DEVELOPING", "MATURE")
    # --- presentation
    fdr_caveat_q: Probability = 0.10  # q above this is shown as "did not survive", never blocks

    @property
    def policy_id(self) -> str:
        return _content_id("copolicy_", self.model_dump(mode="python"))


# Every released policy, reproducible by version. Never edit an entry; add a new one.
POLICIES = {1: CopilotPolicy()}


def get_policy(version: int | None = None) -> CopilotPolicy:
    v = version or max(POLICIES)
    if v not in POLICIES:
        raise ValueError(f"unknown co-pilot policy version {v}; known: {sorted(POLICIES)}")
    return POLICIES[v]


def policy_by_id(policy_id: str) -> CopilotPolicy:
    for p in POLICIES.values():
        if p.policy_id == policy_id:
            return p
    raise ValueError(f"co-pilot policy {policy_id} is not a released policy in this code")


# --------------------------------------------------------------------------- evaluation


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def _forward_adverse(fwd: dict | None) -> bool:
    """Forward evidence pointing against the tested direction (net excess at or below 0)."""
    if not fwd or fwd.get("excess_mean") is None:
        return False
    return fwd["excess_mean"] <= 0 or fwd.get("direction_vs_historical") == "opposite"


def evaluate(policy: CopilotPolicy, evidence: dict | None, context: dict) -> dict:
    """Pure decision for one fired signal.

    ``evidence``: the view built by ``engine.evidence_view`` (None if no usable profile).
    ``context``: ``semantics_ok``/``semantics_detail``, ``max_interval_hours``,
    ``duplicate_of`` (an earlier ALERT for the same strategy/symbol/bar) and ``retired``.

    Returns ``decision``, ``priority``, ``checks`` (every blocking rule with pass/fail and
    detail), ``priority_reasons``, ``caveats`` and ``blocked_by``.
    """
    checks: list[dict] = []

    def check(rule: str, passed: bool, detail: str) -> None:
        checks.append({"rule": rule, "passed": bool(passed), "detail": detail})

    check("versions_compatible", context.get("semantics_ok", False),
          context.get("semantics_detail") or "strategy, compiler and evidence versions match")  # fmt: skip
    gap = context.get("max_interval_hours")
    check("data_quality", gap is not None and gap <= policy.max_bar_interval_hours,
          "daily bars contiguous through the signal bar" if gap is not None
          and gap <= policy.max_bar_interval_hours
          else f"largest gap between bars in the lookback is {gap} h (max "
          f"{policy.max_bar_interval_hours:g} h)")  # fmt: skip
    dup = context.get("duplicate_of")
    check("signal_new", dup is None,
          "first time this strategy/asset/bar is surfaced" if dup is None
          else f"already surfaced as {dup}")  # fmt: skip
    check("strategy_not_retired", not (policy.block_retired_strategy and context.get("retired")),
          "every prospective tracking of the strategy is stopped" if context.get("retired")
          else "strategy not retired")  # fmt: skip

    caveats: list[str] = []
    if evidence is None:
        check("evidence_available", False, "no usable evidence profile for this strategy")
        return _result("SUPPRESS", None, checks, [], caveats)
    check("evidence_available", True, f"profile {evidence['profile_id']}")
    tier = evidence["tier"]
    check("tier_eligible", tier in policy.eligible_tiers,
          f"evidence tier {tier}" + ("" if tier in policy.eligible_tiers
                                     else f" (needs {' or '.join(policy.eligible_tiers)})"))  # fmt: skip
    n, assets = evidence["sample"]["independent_events"], evidence["assets"]["assets_with_events"]
    check("sample_adequate",
          (n or 0) >= policy.min_independent_events
          and (assets or 0) >= policy.min_assets_with_events,
          f"{n} independent events on {assets} assets (min {policy.min_independent_events} on "
          f"{policy.min_assets_with_events})")  # fmt: skip
    ex = evidence["effect"]["excess_mean"]
    check("effect_positive", ex is not None and ex > policy.min_excess,
          f"{evidence['primary_horizon']} expected-direction net excess {_pct(ex)}")  # fmt: skip
    a = evidence["assets"]
    share = a.get("max_asset_event_share")
    dominated = policy.block_dominated_by_one_asset and a.get("dominated_by_one_asset")
    check("breadth_ok", not dominated and (share is None or share <= policy.max_asset_event_share),
          f"{a.get('positive')}/{assets} assets positive; largest asset share "
          f"{'n/a' if share is None else f'{share:.0%}'}"
          + ("; pooled sign flips without the top asset" if a.get("dominated_by_one_asset") else ""))  # fmt: skip
    nb = evidence["neighbourhood"]
    check("not_isolated_spike", not (policy.block_isolated_spike and nb.get("isolated_spike")),
          f"parameter neighbourhood {nb.get('label') or 'n/a'}")  # fmt: skip
    fr = evidence.get("full_research")
    fs = fr["status"] if fr else None
    check("full_research_not_adverse", fs not in policy.adverse_full_research,
          f"full research {fs or 'not run'}")  # fmt: skip
    val = evidence.get("validation")
    vs = val["status"] if val else None
    check("validation_not_adverse", vs not in policy.adverse_validation,
          f"validation {vs or 'not run'}")  # fmt: skip
    fwd = evidence.get("forward")
    level = fwd["maturity"] if fwd else None
    adverse_fwd = _forward_adverse(fwd)
    check("forward_not_adverse_mature", not (level in policy.adverse_forward_levels and adverse_fwd),
          f"forward {level or 'not tracked'}"
          + (" and pointing against the tested direction" if adverse_fwd else ""))  # fmt: skip

    # --- caveats: shown, never blocking
    st = evidence["statistics"]
    if not st.get("fdr_survivor"):
        q = st.get("q")
        caveats.append("did not survive family (BH) correction"
                       + (f": q {q:.2f}" if q is not None else ""))  # fmt: skip
    if fs is None:
        caveats.append("full research not run")
    elif fs not in policy.strong_full_research:
        caveats.append(f"full research {fs.removeprefix('FULL_RESEARCH_').lower()}")
    if vs in (None, "VALIDATION_INSUFFICIENT", "VALIDATION_ERROR"):
        caveats.append("validation not mature / insufficient")
    elif vs in policy.downgrade_validation:
        caveats.append("validation mixed")
    if level in (None, "TOO_EARLY", "EARLY"):
        caveats.append("forward evidence too early" if fwd else "not prospectively tracked")
    elif adverse_fwd:
        caveats.append(f"forward evidence {level.lower()} and adverse")
    if nb.get("label") not in policy.strong_neighbourhood:
        caveats.append(f"parameter neighbourhood {nb.get('label') or 'n/a'}")

    if any(not c["passed"] for c in checks):
        return _result("SUPPRESS", None, checks, [], caveats)

    # --- priority: explicit rules, no weighted score
    reasons, downgrades = [], []
    if tier in policy.strong_tiers:
        reasons.append(f"evidence tier {tier}")
    if vs in policy.strong_validation:
        reasons.append("supportive independent validation")
    broad = (assets or 0) >= policy.strong_min_assets and (
        a.get("positive_share") or 0
    ) >= policy.strong_min_positive_asset_share
    if (
        fs in policy.strong_full_research
        and nb.get("label") in policy.strong_neighbourhood
        and broad
    ):
        reasons.append("consistent full research, plateau neighbourhood and broad asset support")
    if vs in policy.downgrade_validation:
        downgrades.append("validation mixed")
    if level in policy.downgrade_forward_levels and adverse_fwd:
        downgrades.append(f"forward evidence {level.lower()} and adverse")
    strong = bool(reasons) and not downgrades
    reasons = (
        reasons
        if strong
        else [*(f"held at WATCH: {d}" for d in downgrades)]
        or ["eligible exploratory evidence without the stronger supporting properties"]
    )
    return _result("ALERT", "STRONG_WATCH" if strong else "WATCH", checks, reasons, caveats)


def _result(decision, priority, checks, reasons, caveats) -> dict:
    return {
        "decision": decision,
        "priority": priority,
        "checks": checks,
        "blocked_by": [c["rule"] for c in checks if not c["passed"]],
        "priority_reasons": reasons,
        "caveats": caveats,
    }
