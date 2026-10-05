"""Consumer-neutral evidence profiles over governed Strategy Lab results.

A profile summarises what the recorded research says about one strategy: sample, effect,
asset breadth, horizon behaviour, parameter-neighbourhood and family context, raw and
family-corrected statistics, and limitations. It assigns a human-readable tier under a
versioned ``EvidencePolicy``. It never decides what any application does with it:
alerting, execution, sizing and radar surfacing belong to separate, independently
versioned consumer policies that do not exist yet.

Evidence *stages* (where it came from) are kept apart from the *tier* (how it reads).
Phase 7 profiles cite ``fast_screen`` and ``batch_fdr`` sources only. Later stages
(full research, validation, paper/live forward) are added by NEW profiles that list the
extra sources and ``extends`` the earlier profile; nothing is rewritten. ``VALIDATED``
is reserved and cannot be produced without a ``validation`` source.

EXPLORATORY does not mean a strategy has demonstrated persistent alpha. It means the
historical pattern is sufficiently interesting to merit human inspection.
RESEARCH_SUPPORTED is not equivalent to production validation.
"""

from __future__ import annotations

import json
from itertools import pairwise
from typing import Annotated, Literal, Self

import numpy as np
from pydantic import Field, model_validator

from market_signal.models.domain import utcnow
from market_signal.research.lab.common import (
    LabModel,
    Name,
    Number,
    PositiveInt,
    Probability,
    canonical_json,
    content_id,
)

PROFILE_SCHEMA = "2"  # schema 1 profiles (Phase 7) remain readable with their original IDs
# Version of the code that derives profile fields. Part of schema-2 identity: a change to
# how profiles are computed must bump it, giving new profiles instead of a collision.
EVIDENCE_BUILDER_VERSION = "lab_evidence_builder_v2"
Tier = Literal[
    "UNAVAILABLE",
    "INSUFFICIENT",
    "NEGATIVE",
    "INCONCLUSIVE",
    "EXPLORATORY",
    "RESEARCH_SUPPORTED",
    "VALIDATED",  # reserved: requires a governed validation stage (not implemented)
]
Stage = Literal[
    "fast_screen",
    "batch_fdr",
    "full_research",
    "validation",
    "paper_forward",
    "live_forward",
    # Phase 11: another venue's earlier history. Historically exposed, non-independent
    # cross-venue evidence; it never satisfies a rule that requires `validation`.
    "cross_venue_corroboration",
]
STANDARD_LIMITATIONS = (
    "Historical, in-sample-for-this-batch evidence from a fast screen; not full research.",
    "Horizons other than the plan's primary horizon are descriptive only.",
    "Neighbourhood, asset and horizon summaries are descriptive robustness context, "
    "not significance tests.",
    "Variants in a family are correlated; family counts overstate independent evidence.",
)


class ExploratoryRules(LabModel):
    min_independent_events: PositiveInt = 30
    min_assets_with_events: PositiveInt = 3
    min_effect: Number = 0.002  # pooled independent net excess at the primary horizon
    min_positive_asset_share: Probability = 0.6
    max_asset_event_share: Probability = 0.5
    max_raw_p: Probability = 0.25  # a loose sanity bound, not the defining criterion
    min_neighbour_support: Probability = 0.5  # share of testable neighbours agreeing
    strong_effect: Number = 0.01  # may stand in for absent/weak neighbourhood support


class ResearchSupportedRules(LabModel):
    require_fdr_survivor: Literal[True] = True
    require_neighbourhood_support: Literal[True] = True


class NegativeRules(LabModel):
    max_effect: Number = 0.0  # pooled excess at or below this...
    max_positive_asset_share: Probability = 0.5  # ...and few assets agreeing


class HorizonDeadBand(LabModel):
    """A non-primary horizon whose |excess| is below max(absolute, relative x |primary
    excess|) counts as flat: it neither agrees nor reverses (descriptive wording only)."""

    relative: Probability = 0.25
    absolute: Annotated[Number, Field(ge=0)] = 0.001


class EvidencePolicy(LabModel):
    """Versioned tier rules. Changing anything creates a new policy and new profiles.

    v1 (Phase 7) has no horizon dead band: any opposite-signed horizon is a reversal.
    v2 (Phase 7.1, the default) adds ``horizon_dead_band``; tier rules are otherwise equal.
    """

    name: Name = "lab_evidence_policy"
    version: PositiveInt = 2
    plateau_share: Probability = 0.67
    exploratory: ExploratoryRules = ExploratoryRules()
    research_supported: ResearchSupportedRules = ResearchSupportedRules()
    negative: NegativeRules = NegativeRules()
    horizon_dead_band: HorizonDeadBand | None = HorizonDeadBand()

    @model_validator(mode="after")
    def v1_is_strict(self) -> Self:
        if self.version == 1 and self.horizon_dead_band is not None:
            raise ValueError("evidence policy v1 has no horizon dead band")
        return self

    @property
    def policy_id(self) -> str:
        data = self.model_dump(mode="python")
        if data["horizon_dead_band"] is None:
            del data["horizon_dead_band"]  # keeps the v1 identity exactly as recorded
        return content_id("evpolicy_", data)


# Every released policy, reproducible by version. Never edit an entry; add a new one.
POLICIES = {1: EvidencePolicy(version=1, horizon_dead_band=None), 2: EvidencePolicy()}


class ReportPolicy(LabModel):
    """Versioned thresholds for the read-time batch report's descriptive wording."""

    name: Name = "lab_evidence_report_policy"
    version: PositiveInt = 1
    broad_directional_share: Probability = 0.67
    min_adequate_variants: PositiveInt = 2

    @property
    def report_policy_id(self) -> str:
        return content_id("evreport_", self.model_dump(mode="python"))


REPORT_POLICIES = {1: ReportPolicy()}  # v1 = the thresholds Phase 7 used in code


class EvidenceSource(LabModel):
    """One governed record a profile relies on. Future stages use the same shape."""

    stage: Stage
    records: dict[str, str]  # e.g. experiment_id, result_id, batch_id, analysis_id
    versions: dict[str, str] = Field(default_factory=dict)


class EvidenceProfile(LabModel):
    # "3": a profile that extends an earlier one with a paper_forward summary (Phase 8)
    # "4": a profile that extends a historical one with full research / validation (Phase 9)
    # "5": a profile that extends a historical or schema-4 profile with a descriptive
    #      cross-venue corroboration block (Phase 11); its tier equals the extended one's
    profile_schema: Literal["1", "2", "3", "4", "5"] = PROFILE_SCHEMA
    policy_id: str
    # schema 2: {"version": EVIDENCE_BUILDER_VERSION, "software_id": ...}; absent in schema 1
    builder: dict | None = None
    subject: dict
    sources: Annotated[tuple[EvidenceSource, ...], Field(min_length=1)]
    extends: str | None = None  # an earlier profile this one adds evidence to
    evaluation: dict
    statistics: dict
    sample: dict
    effect: dict
    assets: dict
    horizons: dict
    neighbourhood: dict
    family_context: dict
    components: dict[str, str]
    tier: Tier
    supporting: tuple[str, ...]
    limiting: tuple[str, ...]
    limitations: tuple[str, ...]
    # schema 3 only: descriptive prospective evidence (never changes the tier)
    forward: dict | None = None
    # schema 4 only: deeper research on the discovery data, and independent validation on a
    # reserved untouched period. Kept as separate blocks; never pooled with each other or
    # with forward evidence.
    full_research: dict | None = None
    validation: dict | None = None
    # schema 5 only: historically exposed, NON-independent evidence from another venue.
    # Never pooled with the blocks above and never a substitute for `validation`.
    corroboration: dict | None = None

    @model_validator(mode="after")
    def reserved_tier(self) -> Self:
        if self.tier == "VALIDATED" and not any(s.stage == "validation" for s in self.sources):
            raise ValueError("VALIDATED requires a governed validation-stage source")
        if self.profile_schema == "1" and self.builder is not None:
            raise ValueError("schema 1 profiles carry no builder provenance")
        if self.profile_schema != "1" and not (self.builder and self.builder.get("version")):
            raise ValueError("schema 2+ profiles record their builder version")
        # Schema 1/2 may already cite later stages (Phase 7 contract); only schema 3 carries
        # a forward block, and it must cite the paper_forward source behind it.
        has_forward_source = any(s.stage == "paper_forward" for s in self.sources)
        if (self.profile_schema == "3") != (self.forward is not None) or (
            self.forward is not None and not has_forward_source
        ):
            raise ValueError(
                "a forward block belongs to schema 3 profiles citing a paper_forward source"
            )
        if self.profile_schema == "3" and self.extends is None:
            raise ValueError("schema 3 profiles extend an earlier profile")
        stages = {s.stage for s in self.sources}
        if self.profile_schema == "4":
            if self.extends is None or self.full_research is None:
                raise ValueError("schema 4 profiles extend a profile with a full_research block")
            if "full_research" not in stages or (self.validation is not None) != (
                "validation" in stages
            ):
                raise ValueError("schema 4 blocks must cite their full_research/validation sources")
            if self.tier == "VALIDATED":
                # Phase 9 can run validation, but VALIDATED is reserved for a future standard
                # (prospective forward and final-holdout evidence); no Phase 9 policy emits it.
                raise ValueError("VALIDATED is reserved: schema 4 profiles cannot carry it")
        elif self.profile_schema == "5":
            # Copies the extended profile's blocks (schema 4 or none) and adds corroboration.
            if self.extends is None or self.corroboration is None:
                raise ValueError("schema 5 profiles extend a profile with a corroboration block")
            if "cross_venue_corroboration" not in stages:
                raise ValueError("a corroboration block must cite its corroboration source")
            if (self.full_research is not None) != ("full_research" in stages) or (
                self.validation is not None
            ) != ("validation" in stages):
                raise ValueError("schema 5 blocks must cite their full_research/validation sources")
            if self.corroboration.get("independent") is not False:
                raise ValueError("cross-venue corroboration is never independent evidence")
            # The builder copies the extended profile's tier unchanged (policy tier_effect
            # "none"); the model can only rule out the reserved tier.
            if self.tier == "VALIDATED":
                raise ValueError("VALIDATED is reserved: schema 5 profiles cannot carry it")
        elif self.full_research is not None or self.validation is not None:
            raise ValueError("full_research/validation blocks belong to schema 4 profiles")
        if self.corroboration is not None and self.profile_schema != "5":
            raise ValueError("a corroboration block belongs to schema 5 profiles")
        if "cross_venue_corroboration" in stages and self.profile_schema != "5":
            raise ValueError("cross_venue_corroboration sources belong to schema 5 profiles")
        return self

    def payload(self) -> dict:
        """Stored form. Older payloads omit later blocks so they stay byte-identical."""
        data = self.model_dump(mode="python")
        for block in ("forward", "full_research", "validation", "corroboration"):
            if data[block] is None:
                data.pop(block)
        return data

    @property
    def profile_id(self) -> str:
        # Inputs determine every derived field: identity = schema + policy + cited records.
        identity = {
            "profile_schema": self.profile_schema,
            "policy_id": self.policy_id,
            "strategy_id": self.subject["strategy_id"],
            "sources": sorted(
                (s.model_dump(mode="python") for s in self.sources), key=canonical_json
            ),
            "extends": self.extends,
        }
        if self.profile_schema != "1":  # schema 1 identities stay exactly as recorded
            identity["builder_version"] = self.builder["version"]
        return content_id("evidence_", identity)


# --------------------------------------------------------------------------- helpers


def _num(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if np.isfinite(x) else None


def _median(values) -> float | None:
    vals = [v for v in values if v is not None]
    return _num(np.median(vals)) if vals else None


def _sign(x) -> int:
    return 0 if x is None or x == 0 else (1 if x > 0 else -1)


# --------------------------------------------------------------------------- components


def asset_summary(per_asset: list[dict], aggregate: dict, primary: str, policy) -> dict:
    rows = [r for r in per_asset if r["horizon"] == primary and r["independent_events"]]
    excess = {r["symbol"]: r["excess_mean"] for r in rows if r["excess_mean"] is not None}
    counts = {r["symbol"]: r["independent_events"] for r in rows}
    total = sum(counts.values())
    pooled = (
        sum(excess[s] * counts[s] for s in excess) / sum(counts[s] for s in excess)
        if excess
        else None
    )
    # Pooled excess without the largest single contributor: does the sign survive?
    loo = None
    if len(excess) > 1:
        top = max(excess, key=lambda s: abs(excess[s] * counts[s]))
        rest = [s for s in excess if s != top]
        loo = sum(excess[s] * counts[s] for s in rest) / sum(counts[s] for s in rest)
    share = aggregate.get("max_asset_event_share")
    dominated = pooled is not None and loo is not None and _sign(loo) != _sign(pooled)
    best = max(excess, key=excess.get) if excess else None
    worst = min(excess, key=excess.get) if excess else None
    return {
        "horizon": primary,
        "assets_with_events": len(rows),
        "positive": sum(v > 0 for v in excess.values()),
        "negative": sum(v < 0 for v in excess.values()),
        "positive_share": _num(np.mean([v > 0 for v in excess.values()])) if excess else None,
        "median_asset_excess": _median(excess.values()),
        "best": {"symbol": best, "excess": _num(excess[best])} if best else None,
        "worst": {"symbol": worst, "excess": _num(excess[worst])} if worst else None,
        "max_asset_event_share": _num(share),
        "events_by_asset": {s: counts[s] for s in sorted(counts)},
        "pooled_excess_without_top_contributor": _num(loo),
        "dominated_by_one_asset": bool(dominated),
        "concentrated": bool(
            dominated or (share is not None and share > policy.exploratory.max_asset_event_share)
        ),
        "total_independent_events": total,
    }


def horizon_summary(
    aggregate: list[dict],
    horizons: dict[str, int],
    primary: str,
    dead_band: HorizonDeadBand | None = None,
) -> dict:
    """All horizons, ordered by bars. Only the primary horizon carries a test.

    With a dead band, small opposite-signed horizons are flat (sign 0) and are excluded
    from consistency; without one (policy v1) every nonzero sign counts.
    """
    p_value = _num(next((a.get("excess_mean") for a in aggregate if a["horizon"] == primary), None))
    band = max(dead_band.absolute, dead_band.relative * abs(p_value or 0.0)) if dead_band else 0.0

    def signed(x) -> int:
        return 0 if x is None or abs(x) < band else _sign(x)

    rows = []
    for label, bars in sorted(horizons.items(), key=lambda kv: kv[1]):
        agg = next((a for a in aggregate if a["horizon"] == label), {})
        rows.append(
            {
                "horizon": label,
                "bars": bars,
                "primary": label == primary,
                "excess_mean": _num(agg.get("excess_mean")),
                "net_mean": _num(agg.get("net_mean")),
                "independent_events": agg.get("independent_events"),
                "sign": signed(_num(agg.get("excess_mean"))),
                # Phase 4 tests the primary horizon only; others never carry a p-value.
                "raw_p": _num(agg.get("p_value_random_entry")) if label == primary else None,
            }
        )
    signs = [r["sign"] for r in rows if r["excess_mean"] is not None]
    if dead_band:
        signs = [x for x in signs if x != 0]  # flat horizons neither agree nor disagree
    p_sign = next((r["sign"] for r in rows if r["primary"]), 0)
    consistency = _num(np.mean([s == p_sign for s in signs])) if signs and p_sign else None
    values = [(r["bars"], r["excess_mean"]) for r in rows if r["excess_mean"] is not None]
    shape = "unavailable"
    if len(values) >= 2:
        if len({signed(v) for _, v in values} - {0}) > 1:
            shape = "reverses"
        else:
            mags = [abs(v) for _, v in values]
            if all(b >= a for a, b in pairwise(mags)):
                shape = "strengthens"
            elif all(b <= a for a, b in pairwise(mags)):
                shape = "decays"
            else:
                shape = "mixed"
    largest = max(values, key=lambda bv: abs(bv[1])) if values else None
    return {
        "primary": primary,
        "rows": rows,
        "sign_consistency_with_primary": consistency,
        "shape": shape,
        "largest_effect_horizon_descriptive": next(
            (r["horizon"] for r in rows if largest and r["bars"] == largest[0]), None
        ),
        "dead_band": {"threshold": _num(band), **dead_band.model_dump()} if dead_band else None,
        "note": "only the primary horizon was preregistered for testing; others are descriptive",
    }


def neighbours(target: dict, peers: list[dict]) -> list[dict]:
    """Same family/version/market/side, differing by one adjacent step in one parameter.

    Steps use the sorted distinct values each parameter takes among those peers (the
    batch's members of that family-side), so constraint-removed grid points are skipped.
    """
    lin = target["lineage"]
    if lin is None:
        return []
    group = [
        p
        for p in peers
        if p["lineage"] is not None
        and all(p["lineage"][k] == lin[k] for k in ("family_id", "market", "side"))
    ]
    grids = {k: sorted({p["lineage"]["params"][k] for p in group}) for k in lin["params"]}
    out = []
    for p in group:
        if p["strategy_id"] == target["strategy_id"]:
            continue
        diff = [k for k in lin["params"] if p["lineage"]["params"][k] != lin["params"][k]]
        if len(diff) == 1:
            k = diff[0]
            i, j = grids[k].index(lin["params"][k]), grids[k].index(p["lineage"]["params"][k])
            if abs(i - j) == 1:
                out.append(p)
    return out


def _testable(member: dict) -> bool:
    return member["phase4_triage"] in ("WEAK", "INTERESTING")


def neighbourhood_summary(target: dict, peers: list[dict], policy: EvidencePolicy) -> dict:
    if target["lineage"] is None:
        return {"available": False, "reason": "no structured-family lineage"}
    near = neighbours(target, peers)
    testable = [n for n in near if _testable(n) and n["excess_mean"] is not None]
    t_effect = target["excess_mean"]
    agree = [n for n in testable if _sign(n["excess_mean"]) == _sign(t_effect) != 0]
    share = len(agree) / len(testable) if testable else None
    effects = [n["excess_mean"] for n in testable]
    median = _median(effects)
    label = "no_testable_neighbours"
    if testable:
        if share >= policy.plateau_share and len(testable) >= 2:
            label = "plateau"
        elif share < policy.exploratory.min_neighbour_support:
            label = "isolated"
        else:
            label = "mixed"
    return {
        "available": True,
        "definition": "same family/version/market/side, one adjacent step in one parameter",
        "neighbours": len(near),
        "testable_neighbours": len(testable),
        "same_direction": len(agree),
        "support_share": _num(share),
        "median_neighbour_excess": median,
        "target_excess": _num(t_effect),
        "target_minus_median": _num(t_effect - median)
        if t_effect is not None and median is not None
        else None,
        "neighbour_excess_range": [_num(min(effects)), _num(max(effects))] if effects else None,
        "label": label,
        "isolated_spike": label == "isolated" and _sign(t_effect) > 0,
        "neighbour_strategy_ids": sorted(n["strategy_id"] for n in near),
    }


def family_summary(target: dict, peers: list[dict]) -> dict:
    lin = target["lineage"]
    if lin is None:
        group = [p for p in peers if p["family_id"] == target["family_id"]]
        key = {"ledger_family": target["family_id"]}
    else:
        group = [
            p
            for p in peers
            if p["lineage"] is not None
            and all(p["lineage"][k] == lin[k] for k in ("family_id", "market", "side"))
        ]
        key = {"family": lin["family"], "version": lin["version"], "side": lin["side"]}
    testable = [p for p in group if _testable(p) and p["excess_mean"] is not None]
    effects = sorted((p["excess_mean"] for p in testable), reverse=True)
    rank = (
        effects.index(target["excess_mean"]) + 1
        if _testable(target) and target["excess_mean"] in effects
        else None
    )
    return {
        **key,
        "variants": len(group),
        "testable": len(testable),
        "positive_share": _num(np.mean([e > 0 for e in effects])) if effects else None,
        "median_excess": _median(effects),
        "excess_std": _num(np.std(effects, ddof=1)) if len(effects) > 1 else None,
        "target_rank_by_excess": rank,
        "passing_substantive_gates": sum(p["substantive"] for p in group),
        "fdr_survivors": sum(p["batch_status"] == "FDR_SURVIVOR" for p in group),
    }


# --------------------------------------------------------------------------- tiering


def assign_tier(ctx: dict, policy: EvidencePolicy) -> tuple[str, dict, list, list]:
    """Tier plus named categorical components and supporting/limiting reasons.

    Uses only primary-horizon statistics and the robustness summaries; non-primary
    horizons add limitations but never change statistical status.
    """
    r = policy.exploratory
    stats, sample, assets, nb, hz = (
        ctx["statistics"], ctx["sample"], ctx["assets"], ctx["neighbourhood"], ctx["horizons"],
    )  # fmt: skip
    effect = ctx["effect"]["excess_mean"]
    support, limit = [], []
    components = {
        "sample": "unavailable",
        "effect": "unavailable",
        "breadth": "unavailable",
        "concentration": "unavailable",
        "neighbourhood": nb.get("label", "unavailable") if nb.get("available") else "unavailable",
        "horizon": hz["shape"],
        "statistics": "fdr_survivor"
        if stats["fdr_survivor"]
        else (
            "raw_only" if stats["raw_p"] is not None and stats["raw_p"] <= r.max_raw_p else "weak"
        ),
    }
    if hz["shape"] == "reverses":
        limit.append("effect sign reverses across horizons (descriptive)")
    if stats["phase4_triage"] == "ERROR":
        return "UNAVAILABLE", components, support, ["screen errored: no evidence either way"]
    adequate = (
        effect is not None
        and stats["phase4_triage"] in ("WEAK", "INTERESTING")
        and (sample["independent_events"] or 0) >= r.min_independent_events
        and (sample["assets_with_events"] or 0) >= r.min_assets_with_events
    )
    components["sample"] = "adequate" if adequate else "insufficient"
    if not adequate:
        limit.append(
            f"needs >= {r.min_independent_events} independent events on >= "
            f"{r.min_assets_with_events} assets (Phase 4: {stats['phase4_triage']})"
        )
        return "INSUFFICIENT", components, support, limit
    components["effect"] = (
        "strong" if effect >= r.strong_effect else "meaningful" if effect >= r.min_effect
        else "small" if effect > 0 else "adverse"
    )  # fmt: skip
    share = assets["positive_share"] or 0.0
    components["breadth"] = "broad" if share >= r.min_positive_asset_share else "narrow"
    components["concentration"] = "concentrated" if assets["concentrated"] else "spread"
    checks = {
        "effect": effect >= r.min_effect,
        "breadth": share >= r.min_positive_asset_share,
        "concentration": not assets["concentrated"],
        "raw_p": stats["raw_p"] is not None and stats["raw_p"] <= r.max_raw_p,
        "neighbourhood_or_strong": (
            nb.get("available")
            and nb.get("support_share") is not None
            and nb["support_share"] >= r.min_neighbour_support
        )
        or effect >= r.strong_effect,
    }
    labels = {
        "effect": f"pooled net excess {effect:.4f} vs minimum {r.min_effect}",
        "breadth": f"{share:.0%} of assets agree (minimum {r.min_positive_asset_share:.0%})",
        "concentration": "no single asset dominates the pooled result",
        "raw_p": f"raw p {stats['raw_p']} (loose bound {r.max_raw_p}; uncorrected)",
        "neighbourhood_or_strong": "adjacent parameter variants agree, or the effect is strong",
    }
    for key, ok in checks.items():
        (support if ok else limit).append(("" if ok else "not met: ") + labels[key])
    if nb.get("isolated_spike"):
        limit.append("isolated spike: adjacent parameter variants do not agree")
    if stats["q"] is not None and not stats["fdr_survivor"]:
        limit.append(f"not an FDR survivor (q = {stats['q']:.3f}, target {stats['q_target']})")
    if all(checks.values()):
        neighbourhood_ok = (
            nb.get("available")
            and nb.get("label") in ("plateau", "mixed")
            and (nb["support_share"] or 0) >= r.min_neighbour_support
        )
        if stats["fdr_survivor"] and neighbourhood_ok:
            support.append("FDR survivor with substantive gates and neighbourhood support")
            return "RESEARCH_SUPPORTED", components, support, limit
        return "EXPLORATORY", components, support, limit
    n = policy.negative
    if effect <= n.max_effect and share <= n.max_positive_asset_share:
        return "NEGATIVE", components, support, limit
    return "INCONCLUSIVE", components, support, limit


# --------------------------------------------------------------------------- building


def build_profiles(
    records: list[dict], analysis: dict, policy: EvidencePolicy, *, builder_software_id: str
) -> list:
    """Pure: profiles for every member of one batch analysis (peers give context).

    ``builder_software_id`` identifies the code that derived the profile (provenance only;
    the builder *version* is what enters identity).
    """
    out = []
    for target in records:
        metrics = target["metrics"]
        primary = analysis["primary_horizon"]
        aggregate = metrics.get("aggregate", [])
        agg_primary = next((a for a in aggregate if a["horizon"] == primary), {})
        horizons = metrics.get("provenance", {}).get("horizons", {}) or {
            a["horizon"]: None for a in aggregate
        }
        statistics = {
            "primary_horizon": primary,
            "test": analysis["correction"]["test"],
            "raw_p": target["raw_p"],
            "q": target["q"],
            "q_target": analysis["correction"]["q"],
            "correction_method": analysis["correction"]["method"],
            "correction_family_size": analysis["counts"]["correction_family"],
            "preregistered_in_batch": analysis["counts"]["preregistered"],
            "fdr_survivor": target["batch_status"] == "FDR_SURVIVOR",
            "batch_status": target["batch_status"],
            "phase4_triage": target["phase4_triage"],
        }
        ctx = {
            "statistics": statistics,
            "sample": {
                "independent_events": agg_primary.get("independent_events"),
                "evaluable_events": agg_primary.get("evaluable_events"),
                "raw_signals": agg_primary.get("raw_signals"),
                "assets_with_events": agg_primary.get("assets_with_events"),
            },
            "effect": {
                "metric": "pooled independent net excess vs same-asset/side eligible baseline",
                "horizon": primary,
                "excess_mean": _num(agg_primary.get("excess_mean")),
                "net_mean": _num(agg_primary.get("net_mean")),
                "gross_mean": _num(agg_primary.get("gross_mean")),
                "hit_rate": _num(agg_primary.get("hit_rate")),
            },
            "assets": asset_summary(metrics.get("per_asset", []), agg_primary, primary, policy),
            "horizons": horizon_summary(aggregate, horizons, primary, policy.horizon_dead_band)
            if aggregate
            else {"primary": primary, "rows": [], "shape": "unavailable"},
            "neighbourhood": neighbourhood_summary(target, records, policy),
            "family_context": family_summary(target, records),
        }
        tier, components, support, limit = assign_tier(ctx, policy)
        lin = target["lineage"]
        out.append(
            EvidenceProfile(
                policy_id=policy.policy_id,
                builder={"version": EVIDENCE_BUILDER_VERSION, "software_id": builder_software_id},
                subject={
                    "strategy_id": target["strategy_id"],
                    "name": target["name"],
                    "market": target["market"],
                    "side": target["side"],
                    "ledger_family": target["family_id"],
                    "family": lin["family"] if lin else None,
                    "family_version": lin["version"] if lin else None,
                    "family_definition_id": lin["family_id"] if lin else None,
                    "params": lin["params"] if lin else None,
                },
                sources=(
                    EvidenceSource(
                        stage="fast_screen",
                        records={
                            "experiment_id": target["experiment_id"],
                            "result_id": target["result_id"],
                            "plan_id": analysis["plan_id"],
                            "dataset_id": analysis["dataset_id"],
                            "software_id": target["software_id"],
                        },
                        versions={k: target["versions"][k] for k in sorted(target["versions"])},
                    ),
                    EvidenceSource(
                        stage="batch_fdr",
                        records={
                            "batch_id": analysis["batch_id"],
                            "run_id": analysis["run_id"],
                            "analysis_id": analysis["analysis_id"],
                        },
                        versions={"analysis_version": analysis["analysis_version"]},
                    ),
                ),
                evaluation={
                    "market": target["market"],
                    "timeframe": "1d",
                    "role": analysis["role"],
                    "primary_horizon": primary,
                    "horizons": horizons,
                    "effect_metric": ctx["effect"]["metric"],
                },
                components=components,
                tier=tier,
                supporting=tuple(support),
                limiting=tuple(limit),
                limitations=STANDARD_LIMITATIONS,
                **ctx,
            )
        )
    return out


def gather(ledger, batch_id: str, run_id: str | None = None) -> tuple[list[dict], dict]:
    """Read one completed batch analysis and its cited screen records (read-only)."""
    from market_signal.research.lab.batch import inspect_batch

    batch = inspect_batch(ledger, batch_id)
    analyses = [a for a in batch["analyses"] if a["status"] == "completed"]
    if run_id:
        analyses = [a for a in analyses if a["run_id"] == run_id]
    if not analyses:
        raise ValueError("no completed batch analysis to profile")
    row = analyses[-1]
    analysis = {**row["payload"], "analysis_id": row["analysis_id"]}
    submissions = batch["batch"]["submissions"]
    records = []
    for m in analysis["members"]:
        result = next(
            r
            for r in ledger.inspect_experiment(m["experiment_id"])["results"]
            if r["result_id"] == m["result_id"]
        )
        hyp = ledger.store.con.execute(
            "SELECT h.payload FROM lab_submissions s JOIN lab_hypotheses h USING (hypothesis_id) "
            "WHERE s.submission_id=?",
            [submissions[m["strategy_id"]]],
        ).fetchone()
        payload = json.loads(hyp[0])
        lineage = None
        try:
            source = json.loads(payload["source"])
            if isinstance(source, dict) and source.get("generator") == "lab_family_grid_v1":
                lineage = source
        except (TypeError, ValueError):
            pass
        prov = (result["metrics"] or {}).get("provenance", {})
        records.append(
            {
                **m,
                "name": payload["name"],
                "market": payload["definition"]["market"],
                "lineage": lineage,
                "metrics": result["metrics"] or {},
                "software_id": prov.get("software_id", analysis["software_id"]),
                "versions": {
                    k: prov[k]
                    for k in ("screen_version", "compiler_version", "vocabulary_version")
                    if k in prov
                },
                "substantive": all(
                    (m.get("gate_checks") or {}).get(g) is True
                    for g in (
                        "min_independent_events",
                        "min_assets_with_events",
                        "min_pooled_excess",
                        "min_positive_asset_share",
                    )
                ),
            }
        )
    return records, analysis


def _content(payload: dict) -> str:
    # The builder's software ID is provenance of the first recording: an identical profile
    # rebuilt by later software is the same evidence, not a collision.
    data = json.loads(json.dumps(payload))
    if data.get("builder"):
        data["builder"].pop("software_id", None)
    return canonical_json(data)


def record_profiles(ledger, profiles: list[EvidenceProfile], policy: EvidencePolicy) -> dict:
    """Append profiles (idempotent by identity); never update an existing one.

    Same identity with different derived content raises: the computation changed without
    a builder/policy/schema version bump.
    """
    with ledger.store.transaction():
        if not ledger.store.con.execute(
            "SELECT 1 FROM lab_evidence_policies WHERE policy_id=?", [policy.policy_id]
        ).fetchone():
            ledger.store.con.execute(
                "INSERT INTO lab_evidence_policies VALUES (?,?,?)",
                [policy.policy_id, canonical_json(policy.model_dump(mode="python")), utcnow()],
            )
        new = 0
        for p in profiles:
            payload = canonical_json(p.payload())
            row = ledger.store.con.execute(
                "SELECT payload FROM lab_evidence_profiles WHERE profile_id=?", [p.profile_id]
            ).fetchone()
            if row:
                if _content(json.loads(row[0])) != _content(json.loads(payload)):
                    raise ValueError("evidence profile identity collision")
                continue
            analysis_id = next(
                s.records["analysis_id"] for s in p.sources if s.stage == "batch_fdr"
            )
            ledger.store.con.execute(
                "INSERT INTO lab_evidence_profiles VALUES (?,?,?,?,?,?,?)",
                [p.profile_id, p.policy_id, p.subject["strategy_id"], analysis_id, p.tier,
                 payload, utcnow()],
            )  # fmt: skip
            new += 1
    return {"profiles": len(profiles), "new": new, "policy_id": policy.policy_id}


def load_profiles(ledger, *, analysis_id=None, strategy_id=None) -> list[dict]:
    sql, args = "SELECT profile_id, payload FROM lab_evidence_profiles WHERE 1=1", []
    if analysis_id:
        sql += " AND analysis_id=?"
        args.append(analysis_id)
    if strategy_id:
        sql += " AND strategy_id=?"
        args.append(strategy_id)
    rows = ledger.store.con.execute(sql + " ORDER BY recorded_at, profile_id", args).fetchall()
    return [{"profile_id": r[0], **json.loads(r[1])} for r in rows]


# --------------------------------------------------------------------------- batch report


TIER_ORDER = ("RESEARCH_SUPPORTED", "EXPLORATORY", "INCONCLUSIVE", "NEGATIVE", "INSUFFICIENT",
              "UNAVAILABLE")  # fmt: skip


def batch_report(profiles: list[dict], report_policy: ReportPolicy | None = None) -> dict:
    """Descriptive families/variants view. Sorted by objective fields, never 'best trade'.

    Computed at read time; its wording thresholds come from a versioned ``ReportPolicy``
    (cited in the output), so an old batch is always described by an explicit version.
    """
    rp = report_policy or REPORT_POLICIES[max(REPORT_POLICIES)]
    families = {}
    for p in profiles:
        s = p["subject"]
        key = (s["family"] or s["ledger_family"], s["family_version"], s["side"])
        families.setdefault(key, []).append(p)
    fam_rows = []
    for (family, version, side), group in sorted(families.items(), key=lambda kv: str(kv[0])):
        effects = [
            g["effect"]["excess_mean"] for g in group if g["effect"]["excess_mean"] is not None
        ]
        tiers = {t: sum(g["tier"] == t for g in group) for t in TIER_ORDER}
        adequate = [g for g in group if g["components"].get("sample") == "adequate"]
        fam_rows.append(
            {
                "family": family,
                "version": version,
                "side": side,
                "variants": len(group),
                "tiers": {k: v for k, v in tiers.items() if v},
                "positive_effect_share": _num(np.mean([e > 0 for e in effects])) if effects else None,
                "median_excess": _median(effects),
                # plateaus are sign-agnostic agreement; report which way they point
                "positive_plateaus": sum(g["neighbourhood"].get("label") == "plateau"
                                         and (g["effect"]["excess_mean"] or 0) > 0 for g in group),
                "adverse_plateaus": sum(g["neighbourhood"].get("label") == "plateau"
                                        and (g["effect"]["excess_mean"] or 0) < 0 for g in group),
                "isolated_spikes": [g["subject"]["name"] for g in group
                                    if g["neighbourhood"].get("isolated_spike")],
                "pattern": (
                    "broad_directional" if effects
                    and np.mean([e > 0 for e in effects]) >= rp.broad_directional_share
                    and len(adequate) >= rp.min_adequate_variants
                    else "uniformly_weak_or_adverse" if effects and all(e <= 0 for e in effects)
                    else "mixed" if effects else "insufficient"
                ),
            }
        )  # fmt: skip
    variants = sorted(
        profiles,
        key=lambda p: (
            TIER_ORDER.index(p["tier"]) if p["tier"] in TIER_ORDER else -1,
            str(p["subject"]["family"] or p["subject"]["ledger_family"]),
            p["subject"]["name"],
        ),
    )
    return {
        "note": "Descriptive evidence; not a ranking of trades. Only the primary horizon is tested.",
        "report_policy": {"report_policy_id": rp.report_policy_id, **rp.model_dump(mode="python")},
        "evidence_policy_ids": sorted({p["policy_id"] for p in profiles}),
        "tiers": {t: sum(p["tier"] == t for p in profiles) for t in TIER_ORDER},
        "families": fam_rows,
        "asset_specific": [p["subject"]["name"] for p in profiles if p["assets"].get("concentrated")
                           and p["components"].get("sample") == "adequate"],
        "horizon_specific": [p["subject"]["name"] for p in profiles
                             if p["horizons"].get("shape") == "reverses"
                             and p["components"].get("sample") == "adequate"],
        "variants": [
            {"name": p["subject"]["name"], "strategy_id": p["subject"]["strategy_id"],
             "tier": p["tier"], "excess": p["effect"]["excess_mean"],
             "raw_p": p["statistics"]["raw_p"], "q": p["statistics"]["q"],
             "neighbourhood": p["neighbourhood"].get("label"),
             "breadth": p["components"].get("breadth"), "horizon": p["horizons"].get("shape")}
            for p in variants
        ],
    }  # fmt: skip
