"""The preregistered candidate pool (``incubation_pool_rule`` v1) and its direction audit.

Eligibility is frozen logic, never performance: every perp variant of every v1 Lab catalogue
family, both sides, on the prospective venue. No strategy is included or excluded because it
looked good or bad historically (Phase 20 lifetime states are context, not a filter).

- Families: the nine v1 catalogue families (trend, breakout, pullback, mean reversion,
  volatility compression, volume breakout and two funding families). Their long and short
  rules are authored as mirrors in the family files themselves; nothing is fabricated here.
- Phase 17/18/19 structural, relative-strength and OI studies are NOT catalogue families and
  are excluded: their hypotheses were falsified or insufficient, and Phase 21 promotes none.
- A member that cannot produce outcomes on the venue (data insufficiency) stays in the pool
  and is reported as such, never silently dropped.

The pool ID hashes the rule and every member's strategy ID.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

from market_signal.research.lab.common import LabModel, Name, PositiveInt, content_id
from market_signal.research.lifecycle.study import FamilyRef, StrategyRef

V1_FAMILIES = ("ma_trend", "donchian_breakout", "trend_pullback", "rsi_exhaustion",
               "ma_distance_reversion", "vol_compression_breakout", "volume_breakout",
               "funding_extreme_fade", "funding_momentum_exhaustion")  # fmt: skip
STYLE = {  # descriptive grouping for the balance audit (fixed by family, not by results)
    "ma_trend": "trend", "donchian_breakout": "breakout", "trend_pullback": "pullback",
    "rsi_exhaustion": "mean_reversion", "ma_distance_reversion": "mean_reversion",
    "vol_compression_breakout": "breakout", "volume_breakout": "breakout",
    "funding_extreme_fade": "funding", "funding_momentum_exhaustion": "funding",
}  # fmt: skip


class PoolRule(LabModel):
    name: Literal["incubation_pool_rule"] = "incubation_pool_rule"
    version: PositiveInt = 1
    source: Literal["lab_family_catalogue"] = "lab_family_catalogue"
    families: tuple[FamilyRef, ...] = tuple(FamilyRef(family=f, version=1) for f in V1_FAMILIES)
    market: Literal["perp"] = "perp"
    sides: tuple[Literal["long", "short"], ...] = ("long", "short")
    variants: Literal["all_grid_variants"] = "all_grid_variants"
    selection_by_performance: Literal[False] = False
    excluded: tuple[Name, ...] = ("phase17_structure", "phase18_relative_strength",
                                  "phase19_oi_price")  # fmt: skip


def members(rule: PoolRule, catalogue_dir: Path) -> tuple[StrategyRef, ...]:
    """Every perp variant of the rule's families, in catalogue order."""
    from market_signal.research.lab.families import generate, load_catalogue

    cat = load_catalogue(catalogue_dir)
    out = []
    for ref in rule.families:
        for v in generate(cat[(ref.family, ref.version)], rule.market):
            if v.side in rule.sides:
                out.append(StrategyRef(name=v.name, strategy_id=v.strategy_id, family=v.family,
                                       version=v.version, side=v.side, params=dict(v.params),
                                       definition=v.hypothesis.definition.model_dump(mode="json")))  # fmt: skip
    return tuple(out)


def pool_id(rule: PoolRule, refs: tuple[StrategyRef, ...]) -> str:
    return content_id("incpool_", {"rule": rule.model_dump(mode="json"),
                                   "members": sorted(r.strategy_id for r in refs)})  # fmt: skip


def balance(refs: tuple[StrategyRef, ...]) -> dict:
    """Long/short counts overall, by family and by style; and whether every family is
    mirrored (same number of long and short variants)."""
    by_family: dict[str, Counter] = {}
    for r in refs:
        by_family.setdefault(r.family, Counter())[r.side] += 1
    by_style: dict[str, Counter] = {}
    for fam, c in by_family.items():
        by_style.setdefault(STYLE.get(fam, "other"), Counter()).update(c)
    sides = Counter(r.side for r in refs)
    return {
        "strategies": len(refs),
        "long": sides["long"], "short": sides["short"],
        "by_family": {f: {"long": c["long"], "short": c["short"]} for f, c in by_family.items()},
        "by_style": {s: {"long": c["long"], "short": c["short"]} for s, c in by_style.items()},
        "asymmetric_families": sorted(f for f, c in by_family.items() if c["long"] != c["short"]),
        "construction": "each family file authors its long and short rule as mirrors "
        "(thresholds reflected: 1 - tail, 100 - level, crosses_below for crosses_above)",
    }  # fmt: skip
