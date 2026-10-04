"""Structured strategy families: economic hypotheses with bounded, deterministic variants.

A family is a versioned, immutable template: a rationale, the markets and sides it applies
to, a small explicit parameter grid with constraints, and per-side condition templates.
Generation is a pure function of the family: it enumerates the constrained grid in a fixed
order and emits ordinary Phase 1 ``Hypothesis`` documents, so variants get the usual
canonical strategy IDs. Families never reference datasets or plans; a Phase 5 batch does.

Grids are preregistered research spaces, not optimisation searches run after looking at
results. A grid larger than the family's ``max_variants`` is an invalid family (there is
no sampling or silent truncation), and a batch larger than the plan's Monte Carlo
resolution supports is refused.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, model_validator

from market_signal.research.lab.common import (
    LabModel,
    Name,
    Number,
    PositiveInt,
    Text,
    UTCDateTime,
    canonical_json,
    content_id,
)
from market_signal.research.lab.spec import Hypothesis, StrategyDefinition, _UniqueKeyLoader
from market_signal.research.lab.vocabulary import parse_feature

GENERATOR = "lab_family_grid_v1"
ParamName = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,23}$")]
ParamValue = (
    Annotated[int, Field(strict=True)] | Annotated[float, Field(strict=True, allow_inf_nan=False)]
)
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_OPS = Literal["gt", "ge", "lt", "le", "crosses_above", "crosses_below"]
_COMPARE = {"lt": float.__lt__, "le": float.__le__, "gt": float.__gt__, "ge": float.__ge__}


class Constraint(LabModel):
    """``left op factor * right``; ``right`` is a parameter or a constant."""

    left: ParamName
    op: Literal["lt", "le", "gt", "ge"]
    right: ParamName | Number
    factor: Annotated[Number, Field(gt=0)] = 1.0

    def holds(self, params: dict) -> bool:
        right = params[self.right] if isinstance(self.right, str) else self.right
        return _COMPARE[self.op](float(params[self.left]), float(self.factor * right))


class FeatureOperand(LabModel):
    feature: Annotated[str, Field(min_length=1, max_length=64)]


class ParamOperand(LabModel):
    """A numeric threshold ``offset + scale * param`` (e.g. RSI 100 - level for shorts)."""

    param: ParamName
    scale: Number = 1.0
    offset: Number = 0.0


class ConditionTemplate(LabModel):
    left: Annotated[str, Field(min_length=1, max_length=64)]  # feature template
    op: _OPS
    right: Number | FeatureOperand | ParamOperand


class SideTemplate(LabModel):
    hypothesis: Text  # variant sentence; placeholders {param} and {side}
    conditions: Annotated[tuple[ConditionTemplate, ...], Field(min_length=1, max_length=4)]


class ExitTemplate(LabModel):
    # The ATR meaning is chosen per market (perp_sma_14 / wilder_14), as the spec requires.
    stop_atr: Annotated[Number, Field(gt=0, le=10)]
    max_hold_bars: Annotated[int, Field(strict=True, ge=1, le=10_000)]
    target_r: Annotated[Number, Field(gt=0, le=20)] | None = None


class StrategyFamily(LabModel):
    schema_version: Literal["1"] = "1"
    family: Name
    version: PositiveInt
    title: Text
    rationale: Text
    markets: Annotated[tuple[Literal["spot", "perp"], ...], Field(min_length=1, max_length=2)]
    authored_at: UTCDateTime  # fixed: makes generated hypothesis documents reproducible
    parameters: Annotated[
        dict[ParamName, tuple[ParamValue, ...]], Field(min_length=1, max_length=4)
    ]
    constraints: Annotated[tuple[Constraint, ...], Field(max_length=6)] = ()
    name: Annotated[str, Field(min_length=1, max_length=80)]  # e.g. ma_trend_{fast}_{slow}_{side}
    cooldown_bars: Annotated[int, Field(strict=True, ge=0, le=10_000)]
    exit: ExitTemplate
    max_variants: Annotated[int, Field(strict=True, ge=1, le=64)]
    generator: Literal["lab_family_grid_v1"] = GENERATOR
    long: SideTemplate | None = None
    short: SideTemplate | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(set(self.markets)) != len(self.markets):
            raise ValueError("duplicate markets")
        if self.long is None and self.short is None:
            raise ValueError("a family needs at least one side template")
        for name, values in self.parameters.items():
            if not 1 <= len(values) <= 10 or len(set(values)) != len(values):
                raise ValueError(f"parameter {name} needs 1-10 distinct values")
        used = set(_PLACEHOLDER.findall(self.name))
        if "side" not in used or not set(self.parameters) <= used:
            raise ValueError("the name template must contain {side} and every parameter")
        for c in self.constraints:
            for p in (c.left, c.right):
                if isinstance(p, str) and p not in self.parameters:
                    raise ValueError(f"constraint references unknown parameter {p}")
        # Generation must succeed for every market: invalid features, conditions, names
        # or a grid above max_variants make the family itself invalid.
        for market in self.markets:
            generate(self, market)
        return self

    def canonical_json(self) -> str:
        data = self.model_dump(mode="python")
        data["parameters"] = {k: sorted(v) for k, v in data["parameters"].items()}
        data["constraints"] = sorted(data["constraints"], key=canonical_json)
        for side in ("long", "short"):
            if data[side] is not None:
                data[side]["conditions"] = sorted(data[side]["conditions"], key=canonical_json)
        return canonical_json(data)

    @property
    def family_id(self) -> str:
        return content_id("family_", json.loads(self.canonical_json()))

    def sides(self, market: str) -> tuple[str, ...]:
        # Prism's spot engine is long-only; a family's short template applies to perps.
        out = [s for s in ("long", "short") if getattr(self, s) is not None]
        return tuple(s for s in out if not (market == "spot" and s == "short"))


# --------------------------------------------------------------------------- generation


def _substitute(template: str, values: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            raise ValueError(f"template placeholder {{{key}}} is not a parameter")
        return values[key]

    out = _PLACEHOLDER.sub(replace, template)
    if "{" in out or "}" in out:
        raise ValueError(f"malformed template {template!r}")
    return out


def name_token(value) -> str:
    """Deterministic name-safe rendering: 20 -> 20, 0.95 -> p95, 1.5 -> 1p5, -2 -> m2."""
    if isinstance(value, int):
        return str(value).replace("-", "m")
    text = f"{value:g}"
    text = text.replace("0.", "p", 1) if abs(value) < 1 else text.replace(".", "p")
    return text.replace("-", "m")


def _text(value) -> str:
    return str(value) if isinstance(value, int) else f"{value:g}"


def _feature(template: str, params: dict) -> str:
    rendered = {}
    for key in _PLACEHOLDER.findall(template):
        value = params.get(key)
        if not isinstance(value, int):
            raise ValueError(f"feature template {template!r} needs integer parameter {key}")
        rendered[key] = str(value)
    name = _substitute(template, rendered)
    parse_feature(name)  # vocabulary and canonical spelling
    return name


@dataclass(frozen=True)
class Variant:
    family: str
    version: int
    family_id: str
    market: str
    side: str
    params: tuple[tuple[str, int | float], ...]
    hypothesis: Hypothesis

    @property
    def name(self) -> str:
        return self.hypothesis.name

    @property
    def strategy_id(self) -> str:
        return self.hypothesis.strategy_id

    @property
    def complexity(self) -> dict:
        refs = {r.name for c in self.hypothesis.definition.conditions for r in c.references()}
        return {
            "conditions": len(self.hypothesis.definition.conditions),
            "unique_features": len(refs),
            "free_parameters": len(self.params),
        }

    def summary(self) -> dict:
        return {
            "name": self.name,
            "strategy_id": self.strategy_id,
            "side": self.side,
            "params": dict(self.params),
            "complexity": self.complexity,
            "conditions": [
                f"{c.left.name} {c.op} "
                + (c.right.name if hasattr(c.right, "name") else _text(c.right))
                for c in self.hypothesis.definition.conditions
            ],
        }


def assignments(family: StrategyFamily) -> list[dict]:
    """The constrained grid in a fixed order: parameters by name, values ascending."""
    names = sorted(family.parameters)
    out = []
    for combo in itertools.product(*(sorted(family.parameters[n]) for n in names)):
        params = dict(zip(names, combo, strict=True))
        if all(c.holds(params) for c in family.constraints):
            out.append(params)
    return out


def generate(family: StrategyFamily, market: str) -> tuple[Variant, ...]:
    """Deterministic variants for one market. Raises if the family cannot apply to it."""
    if market not in family.markets:
        raise ValueError(f"family {family.family} does not apply to {market}")
    family_id = content_id("family_", json.loads(family.canonical_json()))
    grid = assignments(family)
    if not grid:
        raise ValueError(f"family {family.family}: constraints leave no parameter assignment")
    variants, seen_ids, seen_names = [], {}, set()
    for side in family.sides(market):
        template: SideTemplate = getattr(family, side)
        for params in grid:
            conditions = []
            for c in template.conditions:
                if isinstance(c.right, FeatureOperand):
                    right = {"name": _feature(c.right.feature, params), "timeframe": "1d"}
                elif isinstance(c.right, ParamOperand):
                    right = float(c.right.offset + c.right.scale * params[c.right.param])
                    right = round(right, 12) + 0.0  # 100 - 30 stays 70.0, not 69.999...
                else:
                    right = float(c.right)
                conditions.append(
                    {"left": {"name": _feature(c.left, params), "timeframe": "1d"}, "op": c.op,
                     "right": right}
                )  # fmt: skip
            definition = StrategyDefinition.model_validate(
                {
                    "market": market,
                    "side": side,
                    "trigger_timeframe": "1d",
                    "conditions": conditions,
                    "cooldown_bars": family.cooldown_bars,
                    "exit": {
                        "atr": "perp_sma_14" if market == "perp" else "wilder_14",
                        **family.exit.model_dump(mode="python"),
                    },
                }
            )
            tokens = {k: name_token(v) for k, v in params.items()} | {"side": side}
            texts = {k: _text(v) for k, v in params.items()} | {"side": side}
            name = _substitute(family.name, tokens)
            lineage = {
                "generator": family.generator,
                "family": family.family,
                "version": family.version,
                "family_id": family_id,
                "market": market,
                "side": side,
                "params": params,
            }
            hypothesis = Hypothesis.model_validate(
                {
                    "name": name,
                    "hypothesis": f"{_substitute(template.hypothesis, texts)} Family rationale "
                    f"({family.family} v{family.version}): {family.rationale.strip()}",
                    "source": canonical_json(lineage),
                    "created_at": family.authored_at,
                    "definition": definition.model_dump(mode="json"),
                }
            )
            if name in seen_names:
                raise ValueError(f"family {family.family}: duplicate variant name {name}")
            if hypothesis.strategy_id in seen_ids:
                raise ValueError(
                    f"family {family.family}: {name} and {seen_ids[hypothesis.strategy_id]} "
                    "are the same strategy (an unused parameter?)"
                )
            seen_names.add(name)
            seen_ids[hypothesis.strategy_id] = name
            variants.append(
                Variant(family.family, family.version, family_id, market, side,
                        tuple(sorted(params.items())), hypothesis)
            )  # fmt: skip
    if len(variants) > family.max_variants:
        raise ValueError(
            f"family {family.family} v{family.version} generates {len(variants)} {market} "
            f"variants, above its max_variants={family.max_variants}; narrow the grid in a "
            "new version (no sampling or truncation)"
        )
    return tuple(variants)


# --------------------------------------------------------------------------- catalogue


def load_family(path: Path) -> StrategyFamily:
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    family = StrategyFamily.model_validate(raw)
    expected = f"{family.family}.v{family.version}.yaml"
    if path.name != expected:
        raise ValueError(f"{path.name}: family files are named {expected}")
    return family


def load_catalogue(directory: Path) -> dict[tuple[str, int], StrategyFamily]:
    out = {}
    for path in sorted(directory.glob("*.yaml")):
        family = load_family(path)
        out[(family.family, family.version)] = family
    if not out:
        raise ValueError(f"no strategy families found in {directory}")
    return out


def catalogue_summary(catalogue: dict) -> list[dict]:
    rows = []
    for (name, version), f in sorted(catalogue.items()):
        rows.append(
            {
                "family": name,
                "version": version,
                "family_id": f.family_id,
                "title": f.title,
                "markets": list(f.markets),
                "sides": {m: list(f.sides(m)) for m in f.markets},
                "parameters": {k: sorted(v) for k, v in sorted(f.parameters.items())},
                "variants": {m: len(generate(f, m)) for m in f.markets},
            }
        )
    return rows


# --------------------------------------------------------------------------- batch planning


class FamilyRef(LabModel):
    family: Name
    version: PositiveInt


class FamilyBatchRequest(LabModel):
    """Strict YAML for ``market lab batch generate``: families -> a Phase 5 manifest."""

    name: Name
    description: Text
    plan: str
    dataset: str
    role: Literal["discovery", "development"]
    correction: dict = Field(default_factory=dict)
    survivor: dict
    families: Annotated[tuple[FamilyRef, ...], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def unique_families(self) -> Self:
        names = [f.family for f in self.families]
        if len(set(names)) != len(names):
            raise ValueError("each family may appear once (one version) per batch")
        return self


def load_request(path: Path) -> FamilyBatchRequest:
    raw = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    return FamilyBatchRequest.model_validate(raw)


def max_supported_members(random_entry_samples: int, q: float) -> int:
    """Largest m for which the smallest attainable Monte Carlo p, 1/(draws + 1), is at most
    BH's first threshold q/m (the Phase 5 freeze rule), and within the batch cap."""
    return min(math.floor(q * (random_entry_samples + 1) + 1e-9), 1000)


def plan_family_batch(ledger, request: FamilyBatchRequest, catalogue: dict) -> dict:
    """Dry run: everything needed to inspect a proposed batch, without writing or results.

    Returns the would-be manifest (as data), variants per family, registration state, and
    every reason the Phase 5 freeze would refuse it. Reads only definitions and the
    preregistration log, never screen outcomes.
    """
    from market_signal.research.lab.batch import BatchManifest, CorrectionPolicy
    from market_signal.research.lab.policy import ScreenPlan

    plan = ledger.get_plan(request.plan)
    if not isinstance(plan, ScreenPlan):
        raise ValueError("family batches need a schema v2 screen plan")
    ledger.get_dataset(request.dataset)  # must exist
    correction = CorrectionPolicy.model_validate(request.correction)
    families, members, problems = [], [], []
    for ref in request.families:
        family = catalogue.get((ref.family, ref.version))
        if family is None:
            raise ValueError(f"unknown family {ref.family} v{ref.version}")
        if plan.market not in family.markets:
            raise ValueError(
                f"family {ref.family} does not apply to the plan's {plan.market} market"
            )
        variants = generate(family, plan.market)
        rows = []
        for v in variants:
            row = v.summary()
            state = ledger.store.con.execute(
                "SELECT family_id FROM lab_strategies WHERE strategy_id=?", [v.strategy_id]
            ).fetchone()
            row["registered"] = state is not None
            if state is not None and state[0] != family.family:
                problems.append(f"{v.name} is already registered in family {state[0]}")
            seen = ledger.store.con.execute(
                "SELECT experiment_id FROM lab_experiments WHERE strategy_id=? AND plan_id=? "
                "AND dataset_id=? AND role=? LIMIT 1",
                [v.strategy_id, request.plan, request.dataset, request.role],
            ).fetchone()
            if seen:
                problems.append(f"{v.name} was already preregistered on these inputs ({seen[0]})")
            rows.append(row)
            members.append(v)
        families.append(
            {
                "family": family.family,
                "version": family.version,
                "family_id": family.family_id,
                "sides": list(family.sides(plan.market)),
                "parameters": {k: sorted(v) for k, v in sorted(family.parameters.items())},
                "constraints": [c.model_dump(mode="python") for c in family.constraints],
                "variants": len(variants),
                "members": rows,
            }
        )
    ids = [v.strategy_id for v in members]
    if len(set(ids)) != len(ids):
        problems.append("two families generate the same strategy; a batch needs distinct members")
    m = len(ids)
    supported = max_supported_members(plan.statistics.random_entry_samples, correction.q)
    if m > supported:
        problems.append(
            f"{m} members exceed the {supported} that random_entry_samples="
            f"{plan.statistics.random_entry_samples} supports at q={correction.q:g}"
        )
    if m < 2:
        problems.append("a batch needs at least two members")
    manifest = None
    if not problems:
        manifest = BatchManifest.model_validate(
            {
                "name": request.name,
                "description": request.description,
                "plan": request.plan,
                "dataset": request.dataset,
                "role": request.role,
                "primary_horizon": plan.primary_horizon,
                "correction": correction.model_dump(mode="python"),
                "survivor": request.survivor,
                "members": sorted(ids),
            }
        )
    return {
        "request": request.model_dump(mode="python"),
        "market": plan.market,
        "primary_horizon": plan.primary_horizon,
        "families": families,
        "total_variants": m,
        "statistics": {
            "correction_q": correction.q,
            "random_entry_samples": plan.statistics.random_entry_samples,
            "smallest_attainable_p": 1 / (plan.statistics.random_entry_samples + 1),
            "max_supported_members": supported,
            "compatible": not problems,
        },
        "problems": problems,
        "new_registrations": sum(not r["registered"] for f in families for r in f["members"]),
        "manifest": manifest.model_dump(mode="python") if manifest else None,
        "_variants": members,
    }


def register_family_batch(ledger, request: FamilyBatchRequest, catalogue: dict, *, origin: str):
    """Register missing variants (one submission each) and return the batch manifest.

    Refuses if the dry run reports any problem. Variants already registered in the same
    family are reused, never resubmitted, so repeated generation adds no records. The
    batch is NOT frozen or run here.
    """
    from market_signal.research.lab.batch import BatchManifest

    report = plan_family_batch(ledger, request, catalogue)
    if report["problems"]:
        raise ValueError("cannot generate batch: " + "; ".join(report["problems"]))
    submitted = 0
    for v in report["_variants"]:
        if ledger.store.con.execute(
            "SELECT 1 FROM lab_strategies WHERE strategy_id=?", [v.strategy_id]
        ).fetchone():
            continue
        receipt = ledger.submit(v.hypothesis.model_dump_json(), family_id=v.family, origin=origin)
        if not receipt.accepted:
            raise ValueError(f"{v.name} was rejected by the ledger: {receipt.error}")
        submitted += 1
    return BatchManifest.model_validate(report["manifest"]), submitted
