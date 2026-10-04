"""Cross-venue historical corroboration of frozen Lab strategies (Phase 11).

> Binance historical corroboration is supporting historical evidence, not independent
> validation, because parts of the Binance history have been used in prior Prism research.

The question is narrow: *does the same frozen strategy show broadly similar behaviour on
another major perp venue, in earlier market regimes?* It is answered with the machinery
that produced the original evidence, not a second backtester:

- the corroboration plan is the strategy's source (discovery) plan with only the venue
  (``source``), the venue's per-asset costs and one ``corroboration`` period changed
  (``check_plan_compatibility``); horizons, the primary horizon, funding, statistics,
  warmup, entry/exit and gates are identical;
- the look is a Phase 2 experiment (role ``corroboration``) evaluated by the Phase 4
  ``run_screen``, so its start is permanently recorded in ``lab_inspections`` and
  ``Ledger.exposures`` shows it; each registered Phase 6 neighbour gets its own experiment;
- walk-forward, parameter-neighbour sensitivity, breadth and horizon summaries reuse the
  Phase 9 adapter / Phase 7 evidence functions; predeclared chronological regime blocks
  reuse Prism's ``window_excess``.

Rules:

- **Never independent.** Registrations and results carry ``independent = False`` (model,
  policy and a database CHECK). Recorded prior exposure of the venue's history (legacy
  ``research_runs`` and Lab experiments) is captured at registration; what is not recorded
  (which human design decisions those results influenced) is stated as unknown.
- **Earlier regimes only.** The period must end no later than the source discovery period
  starts, so no market day contributes an outcome to both venues' samples. Any later
  reserved validation/holdout period is therefore untouched.
- **Frozen strategy.** The registration cites the immutable strategy ID and the neighbours
  recorded in its profile. Nothing is tuned; neighbours are descriptive only.
- **Looked at once.** A second run needs an explicit rerun link and reason; results are
  append-only.
- **No pooling.** Venue statistics are reported side by side; no combined sample,
  statistic or p-value is computed.
- **No tier effect.** The schema-5 profile copies the extended profile's tier unchanged.
  Corroboration can never produce RESEARCH_SUPPORTED or VALIDATED, and it carries no
  consumer/approval field of any kind.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Annotated, Literal, Self
from uuid import uuid4

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from market_signal.backtest.events import baseline_bars
from market_signal.backtest.robustness import window_excess
from market_signal.models.domain import utcnow
from market_signal.research.lab import validation as v9
from market_signal.research.lab.adapter import (
    ADAPTER_VERSION,
    FULL_RESEARCH_POLICIES,
    evaluate,
    frozen_walk_forward,
    neighbour_sensitivity,
    parity,
    primary_row,
)
from market_signal.research.lab.common import (
    LabModel,
    Name,
    Number,
    PositiveInt,
    Probability,
    Symbol,
    Text,
    canonical_json,
    content_id,
)
from market_signal.research.lab.compiler import COMPILER_VERSION, Snapshot
from market_signal.research.lab.datasets import SeriesSelection, capture_dataset
from market_signal.research.lab.evidence import (
    EvidenceProfile,
    EvidenceSource,
    asset_summary,
    horizon_summary,
    record_profiles,
)
from market_signal.research.lab.ledger import Ledger, LedgerError, ordered_now
from market_signal.research.lab.policy import Period, ScreenAssetCosts, ScreenPlan
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.screen import SCREEN_VERSION, ScreenResult, ScreenWorkspace
from market_signal.research.lab.vocabulary import VOCABULARY_VERSION

CORROBORATION_VERSION = "lab_cross_venue_corroboration_v1"
EXTENSION_BUILDER_VERSION = "lab_corroboration_extension_builder_v1"
STAGE = "cross_venue_corroboration"
ROLE = "corroboration"
SUPPORTED_VENUES = ("binance",)
STATUSES = ("CROSS_VENUE_CORROBORATIVE", "CROSS_VENUE_MIXED", "CROSS_VENUE_ADVERSE",
            "CROSS_VENUE_INSUFFICIENT", "CROSS_VENUE_ERROR")  # fmt: skip
EVIDENCE_CLASS = "historically exposed, non-independent cross-venue evidence"
STATEMENT = (
    "Binance historical corroboration is supporting historical evidence, not independent "
    "validation, because parts of the Binance history have been used in prior Prism research."
)
LIMITATIONS = (
    "Cross-venue corroboration re-tests the frozen strategy on another venue's earlier "
    "history that prior Prism research has already used: historically exposed, "
    "non-independent evidence. It is not validation and never satisfies a validation rule.",
    "Venue differences (fees, slippage, funding cadence and level, quote currency, listing "
    "dates and asset composition) can change outcomes as well as strategy quality.",
    "Venue statistics are reported side by side and never pooled.",
)


class CorroborationError(LedgerError):
    pass


def semantics() -> dict[str, str]:
    """Versions whose change would make corroboration results incomparable."""
    return {
        "screen_version": SCREEN_VERSION,
        "compiler_version": COMPILER_VERSION,
        "vocabulary_version": VOCABULARY_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "corroboration_version": CORROBORATION_VERSION,
    }


# --------------------------------------------------------------------------- policy


class CorroborationPolicy(LabModel):
    """Versioned, predeclared corroboration rules. Descriptive statuses, never actions.

    Sample thresholds equal the discovery evidence policy's. Walk-forward uses the Phase 9
    full-research policy's 6-month blocks; the regime split is ``regime_blocks`` equal
    calendar-length chronological blocks of the period, fixed before any outcome.
    """

    name: Name = "lab_cross_venue_corroboration_policy"
    version: PositiveInt = 1
    evidence_class: Literal["historically_exposed_non_independent"] = (
        "historically_exposed_non_independent"
    )
    satisfies_independent_validation: Literal[False] = False
    tier_effect: Literal["none"] = "none"
    min_independent_events: PositiveInt = 30
    min_assets_with_events: PositiveInt = 3
    corroborative_min_effect: Number = 0.0
    corroborative_min_positive_asset_share: Probability = 0.6
    corroborative_walk_forward: tuple[str, ...] = ("consistent", "mixed")
    corroborative_regimes: tuple[str, ...] = ("broadly_persistent", "unstable")
    adverse_max_effect: Number = 0.0
    adverse_max_positive_asset_share: Probability = 0.5
    regime_blocks: Literal[3] = 3
    regime_min_events: PositiveInt = 5
    similar_magnitude_ratio: tuple[Number, Number] = (0.5, 2.0)

    @property
    def policy_id(self) -> str:
        return content_id("xvpolicy_", self.model_dump(mode="python"))


POLICIES = {1: CorroborationPolicy()}


def _policy_by_id(policy_id: str) -> CorroborationPolicy:
    for p in POLICIES.values():
        if p.policy_id == policy_id:
            return p
    raise CorroborationError(f"policy {policy_id} is not a released version in this software")


# --------------------------------------------------------------------------- plan


def venue_costs(perps_cfg: dict, venue: str, symbols) -> tuple[ScreenAssetCosts, ...]:
    """The venue's per-side fee and per-coin slippage from Prism's perp cost policy.

    ``perps.backtest.perp_costs`` is the same resolution legacy venue research used
    (``config/perps.yaml``: ``venues.<venue>.taker_fee_bps`` plus ``costs.slippage_bps``).
    Stop slippage is not used: the event study has no stops.
    """
    from market_signal.perps.backtest import perp_costs

    if venue not in SUPPORTED_VENUES:
        raise CorroborationError(f"venue {venue!r} is not supported (v1: {SUPPORTED_VENUES})")
    if not ((perps_cfg.get("venues") or {}).get(venue) or {}).get("enabled"):
        raise CorroborationError(f"venue {venue!r} is not enabled in config/perps.yaml")
    out = []
    for s in sorted(symbols):
        c = perp_costs(perps_cfg, s, venue)
        out.append(ScreenAssetCosts(symbol=s, fee_bps=c.fee_bps, slippage_bps=c.slippage_bps))
    return tuple(out)


def corroboration_plan(
    source: ScreenPlan, venue: str, start: datetime, end: datetime, costs
) -> ScreenPlan:
    """The source plan with only venue, venue costs and one corroboration period changed."""
    name = f"{source.name}_xv_{venue}_{start:%Y%m%d}_{end:%Y%m%d}"
    if len(name) > 80:
        raise CorroborationError(f"derived plan name {name!r} is too long")
    data = {**source.model_dump(mode="python"), "name": name, "version": 1, "source": venue,
            "costs": [c.model_dump(mode="python") for c in costs],
            "periods": [{"role": ROLE, "start": start, "end": end}]}  # fmt: skip
    plan = ScreenPlan.model_validate(data)
    check_plan_compatibility(source, plan)
    return plan


_ALLOWED_TO_DIFFER = {"name", "version", "source", "costs", "periods"}


def check_plan_compatibility(source: ScreenPlan, plan: ScreenPlan) -> None:
    """Methodology (horizons, primary, funding, statistics, warmup, gates, entry/exit) is
    the source plan's exactly; only the venue, its costs and the period differ."""
    a, b = source.model_dump(mode="python"), plan.model_dump(mode="python")
    differ = sorted(k for k in a if k not in _ALLOWED_TO_DIFFER and a[k] != b[k])
    if differ:
        raise CorroborationError(
            f"corroboration plan differs from the source plan in {differ}; only the venue, "
            "its costs and the corroboration period may differ"
        )
    if plan.source == source.source:
        raise CorroborationError("corroboration needs a different venue from the source plan")
    if [p.role for p in plan.periods] != [ROLE]:
        raise CorroborationError("a corroboration plan declares exactly one corroboration period")


# --------------------------------------------------------------------------- coverage


def _ts(value) -> pd.Timestamp:
    return v9._ts(value)


def coverage(store, venue: str, symbols, data_start, start, end) -> dict[str, dict]:
    """Stored venue history per asset: timestamps and counts only (no prices or outcomes).

    An asset is usable iff the venue stores daily bars AND funding closing inside the
    period. Listing dates are reported, never back-filled: an asset listed after the
    period starts simply has no eligible bars before its features warm up.
    """
    out = {}
    for s in sorted(symbols):
        bars = store.con.execute(
            "SELECT count(*), min(close_time), max(close_time), "
            "count(*) FILTER (WHERE close_time >= ?) FROM perp_bars WHERE coin=? AND "
            "source=? AND timeframe='1d' AND close_time >= ? AND close_time < ?",
            [start, s, venue, data_start, end],
        ).fetchone()
        gaps = store.con.execute(
            "SELECT count(*) FROM (SELECT epoch(close_time) - lag(epoch(close_time)) OVER "
            "(ORDER BY close_time) AS d FROM perp_bars WHERE coin=? AND source=? AND "
            "timeframe='1d' AND close_time >= ? AND close_time < ?) WHERE d <> 86400",
            [s, venue, data_start, end],
        ).fetchone()[0]
        fund = store.con.execute(
            "SELECT count(*), min(available_at), max(available_at), "
            "count(*) FILTER (WHERE available_at >= ?) FROM perp_funding WHERE coin=? AND "
            "source=? AND available_at >= ? AND available_at < ?",
            [start, s, venue, data_start, end],
        ).fetchone()
        per_day = store.con.execute(
            "SELECT n, count(*) AS days, min(d) AS first FROM (SELECT "
            "date_trunc('day', time AT TIME ZONE 'UTC') AS d, count(*) AS n FROM perp_funding "
            "WHERE coin=? AND source=? AND available_at >= ? AND available_at < ? GROUP BY d) "
            "GROUP BY n ORDER BY days DESC",
            [s, venue, start, end],
        ).fetchall()
        modal = per_day[0][0] if per_day else None
        irregular = [{"settlements_per_day": int(n), "days": int(d), "first": str(f)[:10]}
                     for n, d, f in per_day[1:]]  # fmt: skip
        usable = bool(bars[3]) and bool(fund[3])
        out[s] = {
            "usable": usable,
            "reason": None if usable else f"no stored {venue} perp bars and funding in the period",
            "bars": int(bars[0]),
            "bars_in_period": int(bars[3]),
            "first_bar_close": _ts(bars[1]).isoformat() if bars[1] else None,
            "last_bar_close": _ts(bars[2]).isoformat() if bars[2] else None,
            "bar_gaps": int(gaps),
            "funding_settlements": int(fund[0]),
            "funding_in_period": int(fund[3]),
            "first_funding_available": _ts(fund[1]).isoformat() if fund[1] else None,
            "modal_settlements_per_day": int(modal) if modal else None,
            "irregular_settlement_days": irregular,
        }
    return out


# --------------------------------------------------------------------------- exposure


def _overlap(a0, a1, b0, b1):
    lo, hi = max(_ts(a0), _ts(b0)), min(_ts(a1), _ts(b1))
    return (lo, hi) if lo < hi else None


def recorded_exposure(ledger: Ledger, reg: CorroborationRegistration, *, now) -> dict:
    """Prior recorded use of this venue's history in the window, before registration.

    Two sources are observable: legacy Prism ``research_runs`` whose config names the venue
    and a period, and Lab experiments on plans with this venue as ``source``. Neither tells
    us which human design decisions those results influenced; that is stated, not guessed.
    """
    con = ledger.store.con
    p = reg.period
    window = (p.start - timedelta(days=reg.warmup_days), p.end)
    strategy_at = con.execute(
        "SELECT recorded_at FROM lab_strategies WHERE strategy_id=?", [reg.strategy_id]
    ).fetchone()[0]
    legacy = []
    if con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='research_runs'"
    ).fetchone():
        for run_id, name, created, config, summary in con.execute(
            "SELECT run_id, name, created_at, config, summary FROM research_runs "
            "ORDER BY created_at, run_id"
        ).fetchall():
            try:
                cfg = json.loads(config or "{}")
            except ValueError:
                continue
            period = cfg.get("period") if isinstance(cfg, dict) else None
            if cfg.get("venue") != reg.venue or not period or len(period) != 2:
                continue
            ov = _overlap(period[0] + "T00:00:00+00:00", period[1] + "T00:00:00+00:00", *window)
            if ov is None or _ts(created) >= _ts(now):
                continue
            verdict = ((json.loads(summary or "{}") or {}).get("verdict") or {}).get("verdict")
            legacy.append({
                "run_id": run_id, "name": name, "recorded_at": _ts(created).isoformat(),
                "strategy": cfg.get("strategy"), "params": cfg.get("params"),
                "period": period, "window_note": cfg.get("window_note"), "verdict": verdict,
                "overlap": [ov[0].isoformat(), ov[1].isoformat()],
                "before_strategy_registered": _ts(created) < _ts(strategy_at),
            })  # fmt: skip
    lab = {"strategy": [], "other": []}
    for r in v9._rows(
        ledger,
        "SELECT i.inspection_id, i.recorded_at, i.kind, e.experiment_id, e.strategy_id, e.role, "
        "e.period_start, e.period_end FROM lab_inspections i JOIN lab_experiments e "
        "USING (experiment_id) JOIN lab_plans pl ON pl.plan_id = e.plan_id "
        "WHERE json_extract_string(pl.payload, '$.source') = ? AND e.period_start < ? "
        "AND e.period_end > ? AND i.recorded_at < ? ORDER BY i.recorded_at",
        [reg.venue, window[1], window[0], _ts(now).to_pydatetime()],
    ):
        item = {"inspection_id": r["inspection_id"], "experiment_id": r["experiment_id"],
                "strategy_id": r["strategy_id"], "role": r["role"], "kind": r["kind"],
                "recorded_at": _ts(r["recorded_at"]).isoformat()}  # fmt: skip
        lab["strategy" if r["strategy_id"] == reg.strategy_id else "other"].append(item)
    # Share of the corroboration PERIOD (where signals and outcomes live) that legacy runs
    # covered: the union of their overlaps, clipped to the period.
    span = (_ts(p.end) - _ts(p.start)).total_seconds()
    clipped = [_overlap(x["overlap"][0], x["overlap"][1], p.start, p.end) for x in legacy]
    covered = []
    for a, b in sorted(c for c in clipped if c):
        if covered and a <= covered[-1][1]:
            covered[-1] = (covered[-1][0], max(covered[-1][1], b))
        else:
            covered.append((a, b))
    return {
        "independent": False,
        "classification": "historically_exposed_cross_venue",
        "statement": STATEMENT if reg.venue == "binance" else EVIDENCE_CLASS,
        "data_region": [window[0].isoformat(), window[1].isoformat()],
        "legacy_research_runs": legacy,
        "legacy_share_of_period": sum((b - a).total_seconds() for a, b in covered) / span,
        "lab_exposures_before_registration": lab,
        "strategy_registered_at": _ts(strategy_at).isoformat(),
        "unrecorded": "which human design decisions (family design, parameter grids, this "
        "strategy's selection) were influenced by earlier results on this venue is not "
        "recorded; exposure here means 'these results existed before registration', no more",
        "note": "empty lists would mean no RECORDED exposure; direct market-table access "
        "cannot be observed. Corroboration is never independent regardless.",
    }


# --------------------------------------------------------------------------- registration


class CorroborationRegistration(LabModel):
    """Frozen corroboration registration; any change is a new registration (new ID)."""

    schema_version: Literal["1"] = "1"
    label: Name | None = None
    independent: Literal[False] = False
    evidence_class: Literal["historically_exposed_non_independent"] = (
        "historically_exposed_non_independent"
    )
    strategy_id: str
    strategy_name: str
    side: Literal["long", "short"]
    ledger_family: str
    family: str | None
    family_version: int | None
    params: dict | None
    base_profile_id: str  # the profile the corroboration profile extends (schema 1/2/4)
    source_profile_id: str  # the historical (schema 1/2) profile
    historical_tier: str
    evidence_policy_id: str
    source_venue: str
    source_plan_id: str
    source_dataset_id: str
    source_experiment_id: str
    source_result_id: str
    source_role: Literal["discovery", "development"]
    source_period: Period
    batch_id: str | None
    batch_run_id: str | None
    analysis_id: str | None
    neighbour_strategy_ids: tuple[str, ...]
    venue: Literal["binance"]
    plan_id: str
    period: Period
    warmup_days: Annotated[int, Field(strict=True, ge=0, le=5000)]
    assets: Annotated[tuple[Symbol, ...], Field(min_length=1)]
    excluded_assets: tuple[tuple[Symbol, Text], ...]
    full_research_policy_id: str
    policy_id: str
    semantics: dict[str, str]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.period.role != ROLE:
            raise ValueError("the period must be a plan-declared corroboration period")
        if self.period.end > self.source_period.start:
            raise ValueError("the corroboration period must end before the source period starts")
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("duplicate assets")
        if set(self.assets) & {s for s, _ in self.excluded_assets}:
            raise ValueError("an asset cannot be both included and excluded")
        if self.venue == self.source_venue:
            raise ValueError("corroboration needs a different venue")
        return self

    @property
    def registration_id(self) -> str:
        return content_id("xvenue_", self.model_dump(mode="python"))

    @property
    def data_start(self) -> datetime:
        return self.period.start - timedelta(days=self.warmup_days)


def _require_tables(ledger: Ledger) -> None:
    if not ledger.store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='lab_corroboration_runs'"
    ).fetchone():
        raise CorroborationError("Phase 11 tables are absent; open Store writable once to migrate")


def _resolve_profiles(ledger: Ledger, profile_id: str) -> tuple[EvidenceProfile, EvidenceProfile]:
    """(base to extend, historical source). Base: a historical or Phase 9 profile."""
    base = v9._profile(ledger, profile_id)
    if base.profile_schema in ("1", "2"):
        hist = base
    elif base.profile_schema == "4":
        hist = v9._profile(ledger, base.extends)
        if hist.profile_schema not in ("1", "2"):
            raise CorroborationError("the schema-4 profile does not extend a historical profile")
    else:
        raise CorroborationError(
            "corroborate a historical (schema 1/2) or full-research (schema 4) profile, not a "
            f"schema {base.profile_schema} profile"
        )
    return base, hist


def build_registration(
    ledger: Ledger,
    profile_id: str,
    *,
    venue: str,
    start: datetime,
    end: datetime,
    perps_cfg: dict,
    label: str | None = None,
) -> tuple[CorroborationRegistration, ScreenPlan, dict]:
    """Pure read: (registration, venue plan, coverage) a ``register`` call would record."""
    from market_signal.research.lab.evidence import gather

    base, hist = _resolve_profiles(ledger, profile_id)
    screen_src = next((s for s in hist.sources if s.stage == "fast_screen"), None)
    fdr = next((s for s in hist.sources if s.stage == "batch_fdr"), None)
    if screen_src is None:
        raise CorroborationError("profile cites no fast_screen source")
    source_plan = ledger.get_plan(screen_src.records["plan_id"])
    if not isinstance(source_plan, ScreenPlan) or source_plan.market != "perp":
        raise CorroborationError("Phase 11 corroborates daily perp strategies with a v2 plan")
    experiment = ledger.get_experiment(screen_src.records["experiment_id"])
    if experiment.role not in ("discovery", "development"):
        raise CorroborationError("the historical evidence must come from discovery/development")
    src_period = source_plan.period(experiment.role)
    strategy_id = hist.subject["strategy_id"]
    if experiment.strategy_id != strategy_id or base.subject["strategy_id"] != strategy_id:
        raise CorroborationError("profiles and source experiment cite different strategies")
    definition = ledger.get_strategy(strategy_id)  # immutable by ID: the frozen strategy
    neighbours = tuple(sorted(hist.neighbourhood.get("neighbour_strategy_ids") or ()))
    if fdr is not None:
        records, _ = gather(ledger, fdr.records["batch_id"], fdr.records["run_id"])
        members = {r["strategy_id"] for r in records}
        if strategy_id not in members or not set(neighbours) <= members:
            raise CorroborationError("strategy and neighbours must be members of the source batch")
    start, end = _ts(start).to_pydatetime(), _ts(end).to_pydatetime()
    if end > src_period.start:
        raise CorroborationError(
            f"the corroboration period must end by {src_period.start.date()} (the source "
            f"{experiment.role} period start), so it adds earlier regimes and no market day "
            "contributes an outcome to both venues"
        )
    data_start = start - timedelta(days=source_plan.warmup_days)
    cov = coverage(ledger.store, venue, experiment.assets, data_start, start, end)
    usable = tuple(s for s in sorted(cov) if cov[s]["usable"])
    excluded = tuple((s, cov[s]["reason"]) for s in sorted(cov) if not cov[s]["usable"])
    policy = POLICIES[max(POLICIES)]
    if len(usable) < policy.min_assets_with_events:
        raise CorroborationError(
            f"only {len(usable)} assets have {venue} history in the period "
            f"(policy needs {policy.min_assets_with_events}); breadth cannot be assessed"
        )
    plan = corroboration_plan(source_plan, venue, start, end,
                              venue_costs(perps_cfg, venue, usable))  # fmt: skip
    subject = hist.subject
    reg = CorroborationRegistration(
        label=label,
        strategy_id=strategy_id,
        strategy_name=subject["name"],
        side=definition.side,
        ledger_family=subject["ledger_family"],
        family=subject.get("family"),
        family_version=subject.get("family_version"),
        params=subject.get("params"),
        base_profile_id=base.profile_id,
        source_profile_id=hist.profile_id,
        historical_tier=hist.tier,
        evidence_policy_id=hist.policy_id,
        source_venue=source_plan.source,
        source_plan_id=source_plan.plan_id,
        source_dataset_id=screen_src.records["dataset_id"],
        source_experiment_id=experiment.experiment_id,
        source_result_id=screen_src.records["result_id"],
        source_role=experiment.role,
        source_period=src_period,
        batch_id=fdr.records.get("batch_id") if fdr else None,
        batch_run_id=fdr.records.get("run_id") if fdr else None,
        analysis_id=fdr.records.get("analysis_id") if fdr else None,
        neighbour_strategy_ids=neighbours,
        venue=venue,
        plan_id=plan.plan_id,
        period=plan.period(ROLE),
        warmup_days=plan.warmup_days,
        assets=usable,
        excluded_assets=excluded,
        full_research_policy_id=FULL_RESEARCH_POLICIES[max(FULL_RESEARCH_POLICIES)].policy_id,
        policy_id=policy.policy_id,
        semantics=semantics(),
    )
    return reg, plan, cov


def _venue_semantics(plan: ScreenPlan, source: ScreenPlan) -> dict:
    return {
        "costs_per_side_bps": {c.symbol: {"fee": c.fee_bps, "slippage": c.slippage_bps}
                               for c in plan.costs},
        "source_costs_per_side_bps": {c.symbol: {"fee": c.fee_bps, "slippage": c.slippage_bps}
                                      for c in source.costs},
        "cost_source": "perps.backtest.perp_costs(config/perps.yaml, coin, venue), frozen in the plan",
        "funding": plan.funding.model_dump(mode="python") if plan.funding else None,
        "funding_note": "same Lab causal policy on both venues: each daily bar sums the settled "
        "rates in (open, close] with the cadence inferred from the trailing 7 days, so Binance's "
        "8-hourly settlements (3/day; briefly shorter intervals in volatile episodes) and "
        "Hyperliquid's hourly settlements are both daily funding fractions of notional",
        "contract_note": "Binance USD-M linear USDT-margined perps (USDT quote, last-price "
        "candles) versus Hyperliquid USDC-margined perps; daily bars open 00:00 UTC on both",
        "interpretation": "a difference in outcome may reflect venue market structure, cost or "
        "funding differences and different asset composition, not only strategy quality",
    }  # fmt: skip


def register(
    ledger: Ledger,
    profile_id: str,
    *,
    venue: str,
    start: datetime,
    end: datetime,
    perps_cfg: dict,
    reason: str,
    origin: str,
    software: SoftwareIdentity,
    label: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Freeze a registration (explicit only). ``dry_run`` shows everything, writes nothing."""
    _require_tables(ledger)
    if not reason or not reason.strip():
        raise CorroborationError("a registration needs a recorded reason")
    reg, plan, cov = build_registration(ledger, profile_id, venue=venue, start=start, end=end,
                                        perps_cfg=perps_cfg, label=label)  # fmt: skip
    now = _ts(now or utcnow())
    existing = ledger.store.con.execute(
        "SELECT registered_at FROM lab_corroboration_registrations WHERE registration_id=?",
        [reg.registration_id],
    ).fetchone()
    if existing:
        raise CorroborationError(
            f"{reg.registration_id} is already registered (since {_ts(existing[0]).isoformat()});"
            " a different configuration or --label is a new registration"
        )
    exposure = recorded_exposure(ledger, reg, now=now)
    source_plan = ledger.get_plan(reg.source_plan_id)
    out = {
        "registration_id": reg.registration_id,
        "registered_at": now.isoformat(),
        "evidence_class": EVIDENCE_CLASS,
        "independent": False,
        "definition": json.loads(canonical_json(reg.model_dump(mode="python"))),
        "strategy_definition": json.loads(ledger.get_strategy(reg.strategy_id).canonical_json()),
        "plan_id": plan.plan_id,
        "period": [reg.period.start.isoformat(), reg.period.end.isoformat()],
        "data_region_with_warmup": [reg.data_start.isoformat(), reg.period.end.isoformat()],
        "source_period": [reg.source_period.start.isoformat(), reg.source_period.end.isoformat()],
        "assets": {"included": list(reg.assets),
                   "excluded": [{"symbol": s, "reason": r} for s, r in reg.excluded_assets]},
        "coverage": cov,
        "venue_semantics": _venue_semantics(plan, source_plan),
        "exposure": exposure,
        "dry_run": dry_run,
        "note": "research only: registration grants no status of any kind",
    }  # fmt: skip
    if dry_run:
        return out
    software_id = ledger.register_software(software)
    ledger.register_plan(plan)
    policy = _policy_by_id(reg.policy_id)
    with ledger.store.transaction():
        if not ledger.store.con.execute(
            "SELECT 1 FROM lab_research_policies WHERE policy_id=?", [policy.policy_id]
        ).fetchone():
            ledger.store.con.execute(
                "INSERT INTO lab_research_policies VALUES (?,?,?)",
                [policy.policy_id, canonical_json(policy.model_dump(mode="python")), utcnow()],
            )
        ledger.store.con.execute(
            "INSERT INTO lab_corroboration_registrations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [reg.registration_id, reg.strategy_id, reg.base_profile_id, reg.venue, plan.plan_id,
             now.to_pydatetime(), reason.strip(), origin, software_id, False,
             canonical_json(reg.model_dump(mode="python")), canonical_json(exposure)],
        )  # fmt: skip
    return out


def get_registration(ledger: Ledger, registration_id: str):
    _require_tables(ledger)
    rows = v9._rows(
        ledger,
        "SELECT * FROM lab_corroboration_registrations WHERE registration_id=?",
        [registration_id],
    )
    if not rows:
        raise CorroborationError(f"unknown corroboration registration {registration_id}")
    row = rows[0]
    reg = CorroborationRegistration.model_validate_json(row["definition"])
    if reg.registration_id != registration_id:
        raise CorroborationError("stored registration does not match its ID")
    row["exposure"] = json.loads(row["exposure"])
    return reg, row


# --------------------------------------------------------------------------- runs


def _runs(ledger: Ledger, registration_id: str) -> list[dict]:
    return v9._rows(
        ledger,
        "SELECT r.*, x.result_id, x.status, x.completed_at FROM lab_corroboration_runs r "
        "LEFT JOIN lab_corroboration_results x USING (run_id) WHERE r.registration_id=? "
        "ORDER BY r.attempt",
        [registration_id],
    )


def _result(ledger: Ledger, result_id: str) -> dict:
    rows = v9._rows(
        ledger,
        "SELECT x.*, r.registration_id, r.attempt, r.experiment_id FROM "
        "lab_corroboration_results x JOIN lab_corroboration_runs r USING (run_id) "
        "WHERE x.result_id=?",
        [result_id],
    )
    if not rows:
        raise CorroborationError(f"unknown corroboration result {result_id}")
    return {**rows[0], "payload": json.loads(rows[0]["payload"])}


def latest_result(ledger: Ledger, registration_id: str) -> dict | None:
    done = [r for r in _runs(ledger, registration_id) if r["result_id"]]
    return _result(ledger, done[-1]["result_id"]) if done else None


def _check_semantics(reg: CorroborationRegistration) -> None:
    if reg.semantics != semantics():
        raise CorroborationError(
            f"semantic versions differ from the registration ({reg.semantics} vs "
            f"{semantics()}); register again so incomparable results are never mixed"
        )


def _prepare(ledger, reg, strategy_id, dataset_id, software, *, rerun_of=None, rerun_reason=None):
    """Phase 2 preregistration on the corroboration role (committed; no exposure yet)."""
    prior = ledger.store.con.execute(
        "SELECT experiment_id FROM lab_experiments WHERE strategy_id=? AND plan_id=? AND "
        "dataset_id=? AND role=? AND assets=? ORDER BY attempt DESC LIMIT 1",
        [strategy_id, reg.plan_id, dataset_id, ROLE, canonical_json(list(reg.assets))],
    ).fetchone()
    if prior and rerun_of is None:
        if not ledger.inspect_experiment(prior[0])["results"]:
            raise CorroborationError(f"attempt {prior[0]} is unfinished; rerun explicitly")
        return prior[0], False
    exp = ledger.preregister(
        v9._submission(ledger, reg, strategy_id), reg.plan_id, dataset_id, role=ROLE,
        assets=reg.assets, software=software, origin=f"lab_corroboration:{reg.registration_id}",
        rerun_of=rerun_of, rerun_reason=rerun_reason,
    )  # fmt: skip
    return exp.experiment_id, True


def _metrics(ledger, experiment_id, software, needs_screen) -> dict:
    inspected = v9._look(ledger, experiment_id, software, needs_screen)
    result = inspected["results"][0]
    if result["status"] == "errored":
        raise CorroborationError(f"corroboration screen errored: {result['error']}")
    return result


def run(
    ledger: Ledger,
    registration_id: str,
    *,
    software: SoftwareIdentity,
    rerun_of: str | None = None,
    rerun_reason: str | None = None,
) -> dict:
    """One governed look at the venue period for the frozen strategy (and its neighbours)."""
    reg, row = get_registration(ledger, registration_id)
    _check_semantics(reg)
    policy = _policy_by_id(reg.policy_id)
    fr_policy = v9._policy_by_id(FULL_RESEARCH_POLICIES, reg.full_research_policy_id)
    prior = _runs(ledger, registration_id)
    if prior and not (rerun_of and rerun_reason and rerun_reason.strip()):
        raise CorroborationError(
            f"corroboration was already run for this registration ({prior[-1]['run_id']}); a "
            "rerun needs --rerun-of and --rerun-reason and never replaces the earlier result"
        )
    if rerun_of is not None and rerun_of not in {r["run_id"] for r in prior}:
        raise CorroborationError("rerun_of must be an earlier run of this registration")
    if rerun_reason and not rerun_of:
        raise CorroborationError("rerun_reason requires rerun_of")
    plan = ledger.get_plan(reg.plan_id)
    check_plan_compatibility(ledger.get_plan(reg.source_plan_id), plan)
    selections = tuple(
        SeriesSelection(kind=kind, symbol=s, source=reg.venue,
                        timeframe="1d" if kind == "perp_bars" else None,
                        start=reg.data_start, end=reg.period.end)
        for s in reg.assets for kind in ("perp_bars", "perp_funding")
    )  # fmt: skip
    dataset_id = ledger.register_dataset(capture_dataset(ledger.store, selections))
    exp_rerun = None
    if prior:
        prev = ledger.get_experiment(prior[-1]["experiment_id"])
        if prev.dataset_id == dataset_id:
            exp_rerun = prev.experiment_id
    # Order: preregister (committed, no exposure) -> run row citing it -> start + screen.
    experiment_id, needs_screen = _prepare(
        ledger, reg, reg.strategy_id, dataset_id, software, rerun_of=exp_rerun,
        rerun_reason=rerun_reason if exp_rerun else None,
    )  # fmt: skip
    software_id = ledger.register_software(software)
    run_id = "xvrun_" + uuid4().hex
    attempt = (prior[-1]["attempt"] + 1) if prior else 1
    with ledger.store.transaction():
        ledger.store.con.execute(
            "INSERT INTO lab_corroboration_runs VALUES (?,?,?,?,?,?,?,?)",
            [run_id, registration_id, attempt, software_id, utcnow(), experiment_id, rerun_of,
             rerun_reason.strip() if rerun_reason else None],
        )  # fmt: skip
    payload = {
        "evidence_class": EVIDENCE_CLASS,
        "independent": False,
        "independence": {
            "independent": False,
            "satisfies_independent_validation": False,
            "reason": row["exposure"]["statement"],
            "recorded_exposure_at_registration": row["exposure"],
        },
        "first_look": not prior,
        "prior_runs": [r["run_id"] for r in prior],
        "venue": reg.venue,
        "period": [reg.period.start.isoformat(), reg.period.end.isoformat()],
        "assets": {"included": list(reg.assets),
                   "excluded": [{"symbol": s, "reason": r} for s, r in reg.excluded_assets]},
    }  # fmt: skip
    try:
        payload.update(_evaluate(ledger, reg, plan, dataset_id, experiment_id, software,
                                 needs_screen, policy, fr_policy))  # fmt: skip
    except Exception as exc:  # recorded, never hidden
        payload.update(status="CROSS_VENUE_ERROR", **v9._error_payload(exc))
    payload["provenance"] = {
        "registration_id": registration_id, "run_id": run_id, "attempt": attempt,
        "software_id": software.software_id, "strategy_id": reg.strategy_id,
        "plan_id": reg.plan_id, "dataset_id": dataset_id, "experiment_id": experiment_id,
        "policy_id": reg.policy_id, "full_research_policy_id": reg.full_research_policy_id,
        "semantics": semantics(),
    }  # fmt: skip
    status = payload["status"]
    if status not in STATUSES:
        raise CorroborationError(f"unknown status {status}")
    result_id = "xvresult_" + uuid4().hex
    with ledger.store.transaction():
        started = ledger.store.con.execute(
            "SELECT started_at FROM lab_corroboration_runs WHERE run_id=?", [run_id]
        ).fetchone()[0]
        ledger.store.con.execute(
            "INSERT INTO lab_corroboration_results VALUES (?,?,?,?,?)",
            [result_id, run_id, ordered_now(started), status, canonical_json(payload)],
        )
    return {"run_id": run_id, "result_id": result_id, "attempt": attempt, "status": status,
            "reasons": payload.get("reasons"), "experiment_id": experiment_id,
            "error": payload.get("error")}  # fmt: skip


def _evaluate(ledger, reg, plan, dataset_id, experiment_id, software, needs_screen, policy,
              fr_policy) -> dict:  # fmt: skip
    """Governed screen of the target (+ neighbours), then the reused deeper summaries."""
    governed = _metrics(ledger, experiment_id, software, needs_screen)
    metrics = governed["metrics"]
    snapshot = Snapshot.from_ledger(ledger, dataset_id)
    if any(s.selection.source != reg.venue for s in snapshot.manifest.series):
        raise CorroborationError("the corroboration dataset must contain only venue rows")
    ws = ScreenWorkspace(snapshot)
    result = evaluate(ledger.get_strategy(reg.strategy_id), plan, snapshot, ROLE, reg.assets, ws)
    par = parity(result.metrics, metrics)  # pure recomputation == the governed result
    if not par["exact"]:
        raise CorroborationError(f"recomputation differs from the governed result {par}")
    primary = plan.primary_horizon
    agg = primary_row(metrics, primary)
    ev_policy = v9._evidence_policy(ledger, reg.evidence_policy_id)
    records = v9._batch_records(ledger, reg)
    target = {**records[reg.strategy_id], "metrics": metrics}
    rows = []
    neighbours = []
    for sid in reg.neighbour_strategy_ids:
        exp_id, fresh = _prepare(ledger, reg, sid, dataset_id, software)
        m = _metrics(ledger, exp_id, software, fresh)["metrics"] or {}
        neighbours.append({**records[sid], "metrics": m})
        a = primary_row(m, primary)
        rows.append({"strategy_id": sid, "name": records[sid].get("name"), "experiment_id": exp_id,
                     "newly_evaluated": fresh, "triage": m.get("triage"),
                     "independent_events": a.get("independent_events"),
                     "excess_mean": a.get("excess_mean")})  # fmt: skip
    sensitivity = neighbour_sensitivity(target, neighbours, plan, ROLE, ev_policy, fr_policy)
    assets = asset_summary(metrics["per_asset"], agg, primary, ev_policy)
    wf = frozen_walk_forward(result, plan, ROLE, fr_policy)
    regimes = regime_blocks(result, plan, policy)
    horizons = horizon_summary(metrics["aggregate"], {h.label: h.bars for h in plan.horizons},
                               primary, ev_policy.horizon_dead_band)  # fmt: skip
    event_study = {
        "horizon": primary,
        "independent_events": agg.get("independent_events"),
        "assets_with_events": agg.get("assets_with_events"),
        "excess_mean": agg.get("excess_mean"),
        "excess_median": agg.get("excess_median"),
        "net_mean": agg.get("net_mean"),
        "net_median": agg.get("net_median"),
        "gross_mean": agg.get("gross_mean"),
        "hit_rate": agg.get("hit_rate"),
        "raw_p_random_entry_descriptive": agg.get("p_value_random_entry"),
        "screen_triage_under_plan_gates": metrics.get("triage"),
    }
    per_asset = [
        {k: r.get(k) for k in ("symbol", "independent_events", "excess_mean", "net_mean",
                               "net_median", "hit_rate", "raw_signals")}
        for r in metrics["per_asset"] if r["horizon"] == primary
    ]  # fmt: skip
    status, checks, reasons = classify(event_study, assets, wf, regimes, policy)
    base = v9._profile(ledger, reg.base_profile_id)
    historical = next(
        r["metrics"] for r in ledger.inspect_experiment(reg.source_experiment_id)["results"]
        if r["result_id"] == reg.source_result_id
    )  # fmt: skip
    comparison = compare(base, historical, reg, event_study, assets, wf, regimes, per_asset,
                         policy)  # fmt: skip
    return {
        "status": status,
        "checks": checks,
        "reasons": reasons,
        "event_study": event_study,
        "per_asset": per_asset,
        "assets_summary": assets,
        "horizons": horizons,
        "walk_forward": wf,
        "regimes": regimes,
        "sensitivity": {**{k: sensitivity.get(k) for k in (
            "status", "label", "rows", "prism_plateau_verdict", "testable_neighbours",
            "same_direction_share", "neighbour_excess_std", "neighbour_excess_range",
            "knife_edge", "winner_selection")},
            "experiments": rows,
            "note": "descriptive parameter-neighbour context only; no neighbour can replace "
            "the frozen strategy and the status never depends on these rows"},
        "comparison": comparison,
        "corroboration_experiment": {"experiment_id": experiment_id,
                                     "result_id": governed["result_id"], "parity": par},
        "compile_digests": {s: m["compile_digest"] for s, m in metrics["assets"].items()},
    }  # fmt: skip


# --------------------------------------------------------------------------- summaries


def regime_blocks(result: ScreenResult, plan: ScreenPlan, policy: CorroborationPolicy) -> dict:
    """The frozen strategy on ``policy.regime_blocks`` equal-length chronological blocks.

    Boundaries are a mechanical split of the period (fixed before any outcome). Each block
    uses Prism's ``window_excess``: block-local same-asset baselines and independent events
    selected by signal time, exactly like a walk-forward fold.
    """
    period = plan.period(ROLE)
    start, end = _ts(period.start), _ts(period.end)
    days = (end - start).days
    cuts = [start + pd.Timedelta(days=round(days * i / policy.regime_blocks))
            for i in range(policy.regime_blocks + 1)]  # fmt: skip
    cuts[-1] = end
    primary = plan.primary_horizon
    bars = {h.label: h.bars for h in plan.horizons}[primary]
    events = result.events
    base = baseline_bars(list(result.asset_events))
    gap = {ae.symbol: bars for ae in result.asset_events}
    names = ("early", "middle", "late") if policy.regime_blocks == 3 else None
    rows = []
    for i, (a, b) in enumerate(pairwise(cuts)):
        w = window_excess(events, base, primary, a, b, gap) if not events.empty else events
        n = len(w)
        ex = float(w["w_excess"].mean()) if n else None
        rows.append({
            "block": names[i] if names else str(i + 1), "start": a.isoformat(),
            "end": b.isoformat(), "independent_events": n,
            "assets_with_events": int(w["symbol"].nunique()) if n else 0,
            "excess_mean": ex if ex is None or np.isfinite(ex) else None,
            "excess_median": float(w["w_excess"].median()) if n else None,
            "net_mean": float(w["ret"].mean()) if n else None,
            "hit_rate": float((w["ret"] > 0).mean()) if n else None,
            "adequate": n >= policy.regime_min_events,
        })  # fmt: skip
    adequate = [r for r in rows if r["adequate"] and r["excess_mean"] is not None]
    positive = sum(r["excess_mean"] > 0 for r in adequate)
    if len(adequate) < 2:
        label = "insufficient"
    elif positive == len(adequate):
        label = "broadly_persistent"
    elif positive == 0:
        label = "adverse"
    elif positive == 1:
        label = "concentrated"
    else:
        label = "unstable"
    return {
        "definition": f"{policy.regime_blocks} equal-length chronological blocks of the period; "
        "block-local baselines (Prism window_excess); boundaries fixed before any outcome",
        "horizon": primary,
        "blocks": rows,
        "adequate_blocks": len(adequate),
        "positive_adequate_blocks": positive,
        "label": label,
    }


def classify(event_study, assets, wf, regimes, policy) -> tuple[str, dict, list[str]]:
    """CROSS_VENUE_* status (descriptive). p-values never decide it."""
    n = event_study["independent_events"] or 0
    k = event_study["assets_with_events"] or 0
    effect, share = event_study["excess_mean"], assets.get("positive_share")
    checks = {
        "min_independent_events": n >= policy.min_independent_events,
        "min_assets_with_events": k >= policy.min_assets_with_events,
    }
    if not all(checks.values()):
        return (
            "CROSS_VENUE_INSUFFICIENT",
            checks,
            [
                f"needs >= {policy.min_independent_events} independent events on >= "
                f"{policy.min_assets_with_events} assets (have {n} on {k}); too few events is "
                "never an adverse finding"
            ],
        )
    broad = (share or 0) >= policy.corroborative_min_positive_asset_share
    if (effect is not None and effect > policy.corroborative_min_effect and broad
            and not assets["dominated_by_one_asset"]
            and wf["label"] in policy.corroborative_walk_forward
            and regimes["label"] in policy.corroborative_regimes):  # fmt: skip
        return (
            "CROSS_VENUE_CORROBORATIVE",
            checks,
            [
                "expected-direction effect on the venue's earlier history, broad across assets "
                f"and not confined to one period (walk-forward {wf['label']}, regimes "
                f"{regimes['label']})"
            ],
        )
    if (effect is None or effect <= policy.adverse_max_effect) and (
        share or 0
    ) <= policy.adverse_max_positive_asset_share:
        return "CROSS_VENUE_ADVERSE", checks, ["effect absent or reversed on most assets"]
    why = []
    if effect is None or effect <= policy.corroborative_min_effect:
        why.append("pooled excess not in the expected direction")
    if not broad:
        why.append(f"only {share} of assets positive")
    if assets["dominated_by_one_asset"]:
        why.append("sign depends on one asset")
    if wf["label"] not in policy.corroborative_walk_forward:
        why.append(f"walk-forward {wf['label']}")
    if regimes["label"] not in policy.corroborative_regimes:
        why.append(f"regimes {regimes['label']}")
    return "CROSS_VENUE_MIXED", checks, why


def _sign(x) -> int:
    return 0 if x is None or x == 0 else (1 if x > 0 else -1)


def compare(base: EvidenceProfile, historical_metrics: dict, reg, event_study, assets, wf,
            regimes, per_asset, policy) -> dict:  # fmt: skip
    """Source venue (discovery/full research) versus this venue, side by side. Nothing pooled."""
    primary = base.evaluation["primary_horizon"]
    hl_rows = {r["symbol"]: r for r in historical_metrics.get("per_asset", [])
               if r["horizon"] == primary and r["independent_events"]}  # fmt: skip
    bn_rows = {r["symbol"]: r for r in per_asset if r["independent_events"]}
    common = sorted(set(hl_rows) & set(bn_rows))
    weighted = [(hl_rows[s]["excess_mean"], hl_rows[s]["independent_events"]) for s in common
                if hl_rows[s]["excess_mean"] is not None]  # fmt: skip
    hl_common = (
        (sum(e * n for e, n in weighted) / sum(n for _, n in weighted)) if weighted else None
    )
    fr = base.full_research or {}
    he, be = base.effect.get("excess_mean"), event_study["excess_mean"]
    ratio = (be / he) if he and be is not None else None
    lo, hi = policy.similar_magnitude_ratio
    magnitude = ("unavailable" if ratio is None else "opposite_sign" if ratio < 0
                 else "similar" if lo <= ratio <= hi else "smaller" if ratio < lo else "larger")  # fmt: skip
    hl_wf = (fr.get("walk_forward") or {}).get("label")

    def breadth(a):
        return "asset_specific" if a.get("concentrated") else "broad"

    return {
        "horizon": primary,
        "source_venue_discovery": {
            "venue": reg.source_venue,
            "period": [reg.source_period.start.isoformat(), reg.source_period.end.isoformat()],
            "excess_mean": he, "net_mean": base.effect.get("net_mean"),
            "hit_rate": base.effect.get("hit_rate"),
            "independent_events": base.sample.get("independent_events"),
            "assets": sorted(hl_rows), "assets_with_events": base.sample.get("assets_with_events"),
            "positive_asset_share": base.assets.get("positive_share"),
            "breadth": breadth(base.assets), "raw_p": base.statistics.get("raw_p"),
            "q": base.statistics.get("q"), "tier": base.tier,
            "full_research_status": fr.get("status"), "walk_forward_label": hl_wf,
            "walk_forward_positive_share": (fr.get("walk_forward") or {}).get("positive_share"),
        },
        "corroboration_venue": {
            "venue": reg.venue,
            "period": [reg.period.start.isoformat(), reg.period.end.isoformat()],
            "excess_mean": be, "net_mean": event_study["net_mean"],
            "net_median": event_study["net_median"], "hit_rate": event_study["hit_rate"],
            "independent_events": event_study["independent_events"],
            "assets": sorted(bn_rows), "assets_with_events": event_study["assets_with_events"],
            "positive_asset_share": assets.get("positive_share"), "breadth": breadth(assets),
            "raw_p_descriptive": event_study["raw_p_random_entry_descriptive"],
            "walk_forward_label": wf.get("label"),
            "walk_forward_positive_share": wf.get("positive_share"),
            "regimes_label": regimes["label"],
        },
        "same_sign": _sign(he) != 0 and _sign(he) == _sign(be),
        "both_expected_direction": _sign(he) > 0 and _sign(be) > 0,
        "effect_ratio": ratio,
        "magnitude": magnitude,
        "breadth": {"source": breadth(base.assets), "corroboration": breadth(assets)},
        "walk_forward_pattern": {"source": hl_wf, "corroboration": wf.get("label"),
                                 "same_label": hl_wf is not None and hl_wf == wf.get("label")},
        "asset_composition": {
            "source_only": sorted(set(hl_rows) - set(bn_rows)),
            "corroboration_only": sorted(set(bn_rows) - set(hl_rows)),
            "common": common,
            "source_excess_on_common_assets": hl_common,
            "note": "venue samples cover different asset sets; compare common assets too",
        },
        "per_asset": [{"symbol": s, "source_excess": hl_rows[s]["excess_mean"],
                       "corroboration_excess": bn_rows[s]["excess_mean"],
                       "same_sign": _sign(hl_rows[s]["excess_mean"]) == _sign(bn_rows[s]["excess_mean"])}
                      for s in common],
        "pooled": False,
        "note": "distinct evidence sources: no combined sample, statistic or p-value. Positive "
        "excess = the strategy's expected direction (side-adjusted).",
    }  # fmt: skip


# --------------------------------------------------------------------------- evidence


def extend_profile(
    ledger: Ledger,
    registration_id: str,
    *,
    software: SoftwareIdentity,
    result_id: str | None = None,
) -> dict:
    """Append a NEW schema-5 profile extending the registered base profile. Tier unchanged."""
    reg, _ = get_registration(ledger, registration_id)
    base = v9._profile(ledger, reg.base_profile_id)
    res = _result(ledger, result_id) if result_id else latest_result(ledger, registration_id)
    if res is None:
        raise CorroborationError("run the corroboration before extending the profile")
    if res["registration_id"] != registration_id:
        raise CorroborationError(f"{res['result_id']} is not a result of this registration")
    policy = _policy_by_id(reg.policy_id)
    p = res["payload"]
    status = res["status"]
    tier = base.tier  # policy.tier_effect == "none": corroboration never changes a tier
    line = (f"cross-venue corroboration ({reg.venue}, historically exposed, not independent): "
            f"{status}")  # fmt: skip
    supporting = (
        (*base.supporting, line) if status == "CROSS_VENUE_CORROBORATIVE" else base.supporting
    )
    limiting = base.limiting if status == "CROSS_VENUE_CORROBORATIVE" else (*base.limiting, line)
    es, cmp_ = p.get("event_study") or {}, p.get("comparison") or {}
    block = {
        "status": status,
        "result_id": res["result_id"],
        "evidence_class": EVIDENCE_CLASS,
        "independent": False,
        "satisfies_independent_validation": False,
        "venue": reg.venue,
        "period": [reg.period.start.isoformat(), reg.period.end.isoformat()],
        "assets": p.get("assets"),
        "exposure": {k: (p.get("independence") or {}).get("recorded_exposure_at_registration",
                                                         {}).get(k)
                     for k in ("statement", "legacy_share_of_period", "unrecorded")},
        "first_look": p.get("first_look"),
        "reasons": p.get("reasons"),
        "sample": {"independent_events": es.get("independent_events"),
                   "assets_with_events": es.get("assets_with_events")},
        "effect": {k: es.get(k) for k in ("horizon", "excess_mean", "net_mean", "net_median",
                                          "hit_rate")},
        "breadth": {k: (p.get("assets_summary") or {}).get(k) for k in (
            "positive_share", "max_asset_event_share", "dominated_by_one_asset", "concentrated")},
        "walk_forward": {k: (p.get("walk_forward") or {}).get(k) for k in (
            "label", "adequate_folds", "positive_adequate_folds", "positive_share")},
        "regimes": {k: (p.get("regimes") or {}).get(k) for k in (
            "label", "adequate_blocks", "positive_adequate_blocks")},
        "neighbours": {k: (p.get("sensitivity") or {}).get(k) for k in (
            "label", "testable_neighbours", "same_direction_share")},
        "comparison": {k: cmp_.get(k) for k in ("same_sign", "effect_ratio", "magnitude",
                                                 "breadth", "walk_forward_pattern", "pooled")},
        "tier_effect": policy.tier_effect,
        "policy_id": reg.policy_id,
    }  # fmt: skip
    source = EvidenceSource(
        stage=STAGE,
        records={k: v for k, v in {
            "registration_id": registration_id, "run_id": res["run_id"],
            "result_id": res["result_id"], "experiment_id": res["experiment_id"],
            "dataset_id": (p.get("provenance") or {}).get("dataset_id"),
            "plan_id": reg.plan_id}.items() if v},
        versions={**reg.semantics, "corroboration_policy": reg.policy_id,
                  "full_research_policy": reg.full_research_policy_id},
    )  # fmt: skip
    data = base.model_dump(mode="python")
    data.update(
        profile_schema="5",
        builder={"version": EXTENSION_BUILDER_VERSION,
                 "software_id": ledger.register_software(software)},
        sources=tuple(s.model_dump(mode="python") for s in (*base.sources, source)),
        extends=base.profile_id,
        forward=None,
        tier=tier,
        components={**base.components, STAGE: status.lower()},
        supporting=supporting,
        limiting=limiting,
        limitations=(*base.limitations, *LIMITATIONS),
        corroboration=block,
    )  # fmt: skip
    profile = EvidenceProfile.model_validate(json.loads(canonical_json(data)))
    if profile.tier != base.tier:
        raise CorroborationError("corroboration cannot change a tier")
    out = record_profiles(ledger, [profile], v9._evidence_policy(ledger, base.policy_id))
    return {"profile_id": profile.profile_id, "extends": base.profile_id, "tier": tier,
            "tier_changed": False, "new_profiles": out["new"], "corroboration": status}  # fmt: skip


# --------------------------------------------------------------------------- inspection


def list_registrations(ledger: Ledger) -> list[dict]:
    _require_tables(ledger)
    out = []
    for row in v9._rows(
        ledger,
        "SELECT * FROM lab_corroboration_registrations ORDER BY registered_at, registration_id",
        [],
    ):
        reg = CorroborationRegistration.model_validate_json(row["definition"])
        latest = latest_result(ledger, row["registration_id"])
        out.append({
            "registration_id": row["registration_id"], "strategy": reg.strategy_name,
            "strategy_id": reg.strategy_id, "venue": reg.venue,
            "period": [reg.period.start, reg.period.end], "assets": list(reg.assets),
            "independent": False, "registered_at": row["registered_at"],
            "latest": latest["status"] if latest else None,
        })  # fmt: skip
    return out


def show(ledger: Ledger, registration_id: str) -> dict:
    reg, row = get_registration(ledger, registration_id)
    latest = latest_result(ledger, registration_id)
    profiles = [
        {"profile_id": r[0], "tier": r[1]}
        for r in ledger.store.con.execute(
            "SELECT profile_id, tier FROM lab_evidence_profiles WHERE strategy_id=? AND "
            "json_extract_string(payload, '$.profile_schema')='5' AND "
            "json_extract_string(payload, '$.extends')=? ORDER BY recorded_at",
            [reg.strategy_id, reg.base_profile_id],
        ).fetchall()
    ]
    return {
        "registration_id": registration_id,
        "registered_at": row["registered_at"],
        "reason": row["reason"],
        "evidence_class": EVIDENCE_CLASS,
        "independent": False,
        "definition": json.loads(canonical_json(reg.model_dump(mode="python"))),
        "exposure": row["exposure"],
        "runs": _runs(ledger, registration_id),
        "latest": None if latest is None else {"status": latest["status"],
                                               "result_id": latest["result_id"],
                                               **latest["payload"]},
        "extended_profiles": profiles,
    }  # fmt: skip
