"""Prospective forward tracking of enrolled Strategy Lab strategies (Phase 8).

This measures whether historical signal behaviour persists on bars Prism genuinely saw
live. It is NOT the simulated-execution paper trader: there is no sizing, portfolio,
fill, margin, risk limit or execution rule here, and nothing is ever acted on.

Rules that keep it prospective:
- A strategy is tracked only after an explicit enrollment that freezes the evidence
  profile that justified it, the plan's costs/funding/horizons and the semantic versions.
- Daily bar T (Prism's ``close_time``, UTC midnight for crypto perps) can be evaluated only
  while ``bar_close <= evaluated_at < bar_close + 1 day``: from the moment it is complete
  until the next bar completes. A database CHECK enforces the same window. A bar whose
  window passed unobserved (PC off, data not ingested) is never evaluated later; it is a
  coverage gap, derived on read from the absence of an evaluation.
- Every evaluation (signal, no signal, or inputs incomplete) is written once per tracking,
  symbol and bar. Re-running gives the same answer (no-op) or raises ``ForwardConflict``.
- Signals come from the Phase 3 compiler on a snapshot of rows with ``close_time <= T``
  (funding by minute-snapped availability), so cooldown/edge state is rebuilt only from
  data available at T.
- Outcomes use Phase 4 conventions (entry at T+1 open, exit at T+h close, side-signed,
  frozen fee + slippage, causal daily funding; missing funding is never zero). An outcome
  row is written once, only when final: ``resolved`` or ``unavailable`` after a bounded
  wait. ``pending`` is the absence of a row.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import uuid4

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from market_signal.backtest.events import decluster
from market_signal.models.domain import AssetClass, utcnow
from market_signal.perps.backtest import PerpCosts, side_forward_returns
from market_signal.research.lab import features as lab_features
from market_signal.research.lab.common import (
    LabModel,
    Name,
    PositiveInt,
    Symbol,
    Text,
    canonical_json,
    content_id,
)
from market_signal.research.lab.compiler import (
    COMPILER_VERSION,
    Snapshot,
    build_inputs,
    compile_strategy,
    required_features,
)
from market_signal.research.lab.datasets import SeriesSelection, capture_dataset, snapshot_rows
from market_signal.research.lab.evidence import (
    EvidencePolicy,
    EvidenceProfile,
    EvidenceSource,
    record_profiles,
)
from market_signal.research.lab.ledger import Ledger, LedgerError
from market_signal.research.lab.policy import CausalFundingPolicy, Horizon, ScreenAssetCosts
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.vocabulary import VOCABULARY_VERSION

EVALUATOR_VERSION = "lab_forward_evaluator_v1"
RESOLVER_VERSION = "lab_forward_resolver_v1"
SUMMARY_VERSION = "lab_forward_summary_v1"
FORWARD_BUILDER_VERSION = "lab_evidence_forward_builder_v1"
ENROLLABLE_TIERS = ("EXPLORATORY", "RESEARCH_SUPPORTED")
# One daily bar interval: bar T is observable until bar T+1 completes (also a DB CHECK).
GRACE = timedelta(days=1)
# After the exit bar's close, missing bars/funding become a final 'unavailable' outcome.
OUTCOME_WAIT_DAYS = 7
# Resolver context before T: lab_funding_day estimates cadence over a trailing 7 days.
FUNDING_CONTEXT = timedelta(days=8)
# Snapshot cutoff after a bar close: funding availability is judged to the minute.
AVAILABILITY_SLACK = timedelta(minutes=1)
STATUSES = ("active", "paused", "stopped")
FORWARD_LIMITATIONS = (
    "Forward evidence is descriptive: no significance test is computed on prospective "
    "samples, and it never changes the historical tier.",
    "Forward outcomes are analytical (T+1 open to T+h close), not simulated fills.",
    "Coverage depends on when Prism was running; unobserved bars are gaps, never backfilled.",
)


class ForwardError(LedgerError):
    pass


class ForwardConflict(ForwardError):
    """A recomputed evaluation differs from the one already recorded for that bar."""


def semantics() -> dict[str, str]:
    """The versions whose change would make evaluations incomparable."""
    return {
        "compiler_version": COMPILER_VERSION,
        "vocabulary_version": VOCABULARY_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "resolver_version": RESOLVER_VERSION,
    }


class TrackingDefinition(LabModel):
    """Frozen enrollment. Any change (strategy, horizons, label...) is a new tracking ID."""

    schema_version: Literal["1"] = "1"
    label: Name | None = None  # distinguishes a deliberate re-enrollment of the same config
    continues: str | None = None  # explicit continuation of an earlier tracking
    strategy_id: str
    enrollment_profile_id: str
    enrollment_tier: Literal["EXPLORATORY", "RESEARCH_SUPPORTED"]
    evidence_policy_id: str
    family: str | None
    family_version: int | None
    params: dict | None
    plan_id: str
    dataset_id: str
    experiment_id: str
    batch_id: str | None
    analysis_id: str | None
    market: Literal["perp"]
    side: Literal["long", "short"]
    source: Text
    timeframe: Literal["1d"] = "1d"
    assets: Annotated[tuple[Symbol, ...], Field(min_length=1)]
    costs: Annotated[tuple[ScreenAssetCosts, ...], Field(min_length=1)]
    funding: CausalFundingPolicy
    horizons: Annotated[tuple[Horizon, ...], Field(min_length=1, max_length=8)]
    primary_horizon: str
    lookback_days: Annotated[int, Field(strict=True, ge=1, le=5000)]
    grace: Literal["one_bar_interval_v1"] = "one_bar_interval_v1"
    entry: Literal["next_bar_open"] = "next_bar_open"
    horizon_exit: Literal["horizon_bar_close"] = "horizon_bar_close"
    return_model: Literal["perp_notional_v1"] = "perp_notional_v1"
    outcome_wait_days: Literal[7] = OUTCOME_WAIT_DAYS
    semantics: dict[str, str]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        labels = [h.label for h in self.horizons]
        if len(set(labels)) != len(labels):
            raise ValueError("duplicate horizon labels")
        if self.primary_horizon not in labels:
            raise ValueError("the primary horizon must be tracked")
        if any(h.bars > 250 for h in self.horizons):
            raise ValueError("forward horizons are at most 250 bars")
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("duplicate assets")
        if {c.symbol for c in self.costs} != set(self.assets):
            raise ValueError("every tracked asset needs exactly its frozen cost assumption")
        return self

    @property
    def tracking_id(self) -> str:
        return content_id("tracking_", self.model_dump(mode="python"))

    def cost(self, symbol: str) -> ScreenAssetCosts:
        return next(c for c in self.costs if c.symbol == symbol)


class MaturityPolicy(LabModel):
    """Sample maturity from independent resolved primary-horizon signal outcomes.

    Maturity says how much forward evidence exists, never whether it is good: a MATURE
    negative sample is as informative as a MATURE positive one.
    """

    name: Name = "lab_forward_maturity"
    version: PositiveInt = 1
    early: PositiveInt = 10
    developing: PositiveInt = 30
    mature: PositiveInt = 100
    mature_min_observed_days: PositiveInt = 180

    def level(self, independent: int, observed_days: int) -> str:
        if independent >= self.mature and observed_days >= self.mature_min_observed_days:
            return "MATURE"
        if independent >= self.developing:
            return "DEVELOPING"
        return "EARLY" if independent >= self.early else "TOO_EARLY"


MATURITY_POLICIES = {1: MaturityPolicy()}


# --------------------------------------------------------------------------- helpers


def _ts(value) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(value) -> str | None:
    return None if value is None else _ts(value).isoformat()


def _num(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if np.isfinite(x) else None


def _rows(ledger: Ledger, sql: str, args: list) -> list[dict]:
    cursor = ledger.store.con.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def _require_tables(ledger: Ledger) -> None:
    if not ledger.store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='lab_forward_trackings'"
    ).fetchone():
        raise ForwardError("forward tables are absent; open Store writable once to migrate")


def _snapshot(store, selections: tuple[SeriesSelection, ...]) -> Snapshot:
    """Read rows into memory (fingerprinted); nothing is retained in the ledger."""
    capture = capture_dataset(store, selections)
    blobs = dict(capture.blobs)
    rows = tuple(snapshot_rows(s, blobs[s.sha256]) for s in capture.manifest.series)
    return Snapshot(capture.manifest.dataset_id, capture.manifest, rows)


def _selections(source: str, symbol: str, start, end) -> tuple[SeriesSelection, ...]:
    return tuple(
        SeriesSelection(
            kind=kind,
            symbol=symbol,
            source=source,
            timeframe="1d" if kind == "perp_bars" else None,
            start=_ts(start).to_pydatetime(),
            end=_ts(end).to_pydatetime(),
        )
        for kind in ("perp_bars", "perp_funding")
    )


def newest_bar(store, source: str, symbol: str, now) -> pd.Timestamp | None:
    """Close of the newest completed daily perp bar stored at ``now`` (None if none)."""
    row = store.con.execute(
        "SELECT max(close_time) FROM perp_bars WHERE coin=? AND source=? AND "
        "timeframe='1d' AND close_time<=?",
        [symbol, source, _ts(now).to_pydatetime()],
    ).fetchone()
    return None if row[0] is None else _ts(row[0])


def in_live_window(bar, now) -> bool:
    """Bar T is live from its close until the next bar completes (one bar interval)."""
    return _ts(bar) <= _ts(now) < _ts(bar) + GRACE


def live_snapshot(store, source: str, symbol: str, bar, lookback_days: int) -> Snapshot:
    """Rows available at bar T's close: bars with close_time <= T, funding to the minute."""
    bar = _ts(bar)
    start = bar - pd.Timedelta(days=lookback_days)
    return _snapshot(store, _selections(source, symbol, start, bar + AVAILABILITY_SLACK))


def funding_ready(snap: Snapshot, symbol: str, bar) -> bool:
    """Has funding through the bar close been ingested (judged to the minute)?"""
    funding = snap.find("perp_funding", symbol)[1]
    return any(_ts(r["time"]).round("min") >= _ts(bar) for r in funding)


def _input_id(snapshot: Snapshot) -> tuple[str, str]:
    manifest = snapshot.manifest.canonical_json()
    return content_id("fwdinput_", json.loads(manifest)), manifest


def _put_input(ledger: Ledger, input_id: str, manifest: str, now) -> None:
    if not ledger.store.con.execute(
        "SELECT 1 FROM lab_forward_inputs WHERE input_id=?", [input_id]
    ).fetchone():
        ledger.store.con.execute(
            "INSERT INTO lab_forward_inputs VALUES (?,?,?)", [input_id, manifest, now]
        )


def _definition(row: dict) -> TrackingDefinition:
    d = TrackingDefinition.model_validate_json(row["definition"])
    if d.tracking_id != row["tracking_id"]:
        raise ForwardError("stored tracking definition does not match its ID")
    return d


def _status_events(ledger: Ledger, tracking_id: str) -> list[dict]:
    return _rows(
        ledger,
        "SELECT status, reason, recorded_at FROM lab_forward_status WHERE tracking_id=? "
        "ORDER BY recorded_at, event_id",
        [tracking_id],
    )


def _status_at(events: list[dict], when) -> str | None:
    status = None
    for e in events:
        if _ts(e["recorded_at"]) <= _ts(when):
            status = e["status"]
    return status


def trackings(ledger: Ledger) -> list[dict]:
    _require_tables(ledger)
    out = []
    for row in _rows(
        ledger, "SELECT * FROM lab_forward_trackings ORDER BY enrolled_at, tracking_id", []
    ):
        events = _status_events(ledger, row["tracking_id"])
        out.append({**row, "definition": _definition(row), "events": events,
                    "status": events[-1]["status"] if events else None})  # fmt: skip
    return out


def _tracking(ledger: Ledger, tracking_id: str) -> dict:
    hit = [t for t in trackings(ledger) if t["tracking_id"] == tracking_id]
    if not hit:
        raise ForwardError(f"unknown tracking {tracking_id}")
    return hit[0]


# --------------------------------------------------------------------------- enrollment


def _profile(ledger: Ledger, profile_id: str) -> EvidenceProfile:
    row = ledger.store.con.execute(
        "SELECT payload FROM lab_evidence_profiles WHERE profile_id=?", [profile_id]
    ).fetchone()
    if row is None:
        raise ForwardError(f"unknown evidence profile {profile_id}")
    profile = EvidenceProfile.model_validate_json(row[0])
    if profile.profile_id != profile_id:
        raise ForwardError("stored profile does not match its ID")
    return profile


def build_definition(
    ledger: Ledger,
    profile_id: str,
    *,
    horizons: tuple[str, ...] = (),
    label: str | None = None,
    continues: str | None = None,
) -> TrackingDefinition:
    """Pure read: the frozen tracking definition an enrollment would record."""
    from market_signal.research.lab.policy import ScreenPlan

    profile = _profile(ledger, profile_id)
    # 4 = Phase 9 extension, 5 = Phase 11 corroboration extension; enroll from history
    if profile.profile_schema in ("3", "4", "5"):
        raise ForwardError("enroll from a historical profile, not an extended one")
    if profile.tier not in ENROLLABLE_TIERS:
        raise ForwardError(
            f"profile tier {profile.tier} is not trackable; forward tracking starts from "
            f"{' or '.join(ENROLLABLE_TIERS)} profiles"
        )
    screen = next((s for s in profile.sources if s.stage == "fast_screen"), None)
    if screen is None:
        raise ForwardError("profile cites no fast_screen source")
    fdr = next((s for s in profile.sources if s.stage == "batch_fdr"), None)
    plan = ledger.get_plan(screen.records["plan_id"])
    if not isinstance(plan, ScreenPlan) or plan.market != "perp" or plan.funding is None:
        raise ForwardError("Phase 8 tracks daily perp strategies screened under a v2 plan")
    strategy_id = profile.subject["strategy_id"]
    definition = ledger.get_strategy(strategy_id)
    experiment = ledger.get_experiment(screen.records["experiment_id"])
    chosen = tuple(horizons) or tuple(h.label for h in plan.horizons)
    by_label = {h.label: h for h in plan.horizons}
    unknown = [h for h in chosen if h not in by_label]
    if unknown:
        raise ForwardError(f"horizons {unknown} are not in the plan ({sorted(by_label)})")
    keys = required_features(definition)
    warmup = max(lab_features.warmup(k, AssetClass.CRYPTO) for k in keys) + (
        1 if any(c.op.startswith("crosses") for c in definition.conditions) else 0
    )
    if warmup + definition.cooldown_bars + 2 > plan.warmup_days:
        raise ForwardError(
            f"plan warmup ({plan.warmup_days} days) cannot cover the strategy's warmup "
            f"({warmup}) plus cooldown ({definition.cooldown_bars}) for live evaluation"
        )
    if continues is not None:
        prior = _tracking(ledger, continues)
        if prior["strategy_id"] != strategy_id:
            raise ForwardError("a continuation must track the same strategy")
    subject = profile.subject
    return TrackingDefinition(
        label=label,
        continues=continues,
        strategy_id=strategy_id,
        enrollment_profile_id=profile_id,
        enrollment_tier=profile.tier,
        evidence_policy_id=profile.policy_id,
        family=subject.get("family"),
        family_version=subject.get("family_version"),
        params=subject.get("params"),
        plan_id=plan.plan_id,
        dataset_id=screen.records["dataset_id"],
        experiment_id=experiment.experiment_id,
        batch_id=fdr.records.get("batch_id") if fdr else None,
        analysis_id=fdr.records.get("analysis_id") if fdr else None,
        market="perp",
        side=definition.side,
        source=plan.source,
        assets=tuple(sorted(experiment.assets)),
        costs=tuple(
            sorted((c for c in plan.costs if c.symbol in experiment.assets), key=lambda c: c.symbol)
        ),
        funding=plan.funding,
        horizons=tuple(by_label[h] for h in sorted(set(chosen), key=lambda x: by_label[x].bars)),
        primary_horizon=plan.primary_horizon,
        lookback_days=plan.warmup_days,
        semantics=semantics(),
    )


def enroll(
    ledger: Ledger,
    profile_id: str,
    *,
    reason: str,
    origin: str,
    software: SoftwareIdentity,
    horizons: tuple[str, ...] = (),
    label: str | None = None,
    continues: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Freeze a tracking definition and start it. Nothing before ``enrolled_at`` is evaluated."""
    _require_tables(ledger)
    if not reason or not reason.strip():
        raise ForwardError("an enrollment needs a recorded reason")
    d = build_definition(ledger, profile_id, horizons=horizons, label=label, continues=continues)
    now = _ts(now or utcnow())
    existing = ledger.store.con.execute(
        "SELECT enrolled_at FROM lab_forward_trackings WHERE tracking_id=?", [d.tracking_id]
    ).fetchone()
    if existing:
        raise ForwardError(
            f"{d.tracking_id} is already enrolled (since {_iso(existing[0])}); a different "
            "configuration or --label is a new tracking"
        )
    others = [t["tracking_id"] for t in trackings(ledger)
              if t["strategy_id"] == d.strategy_id and t["status"] != "stopped"]  # fmt: skip
    out = {
        "tracking_id": d.tracking_id,
        "enrolled_at": now.isoformat(),
        "first_evaluable_bar_close": _first_bar(now).isoformat(),
        "definition": d.model_dump(mode="json"),
        "other_open_trackings_of_strategy": others,
        "dry_run": dry_run,
        "note": "evidence collection only: enrollment grants no trading or alert status",
    }
    if dry_run:
        return out
    software_id = ledger.register_software(software)
    with ledger.store.transaction():
        ledger.store.con.execute(
            "INSERT INTO lab_forward_trackings VALUES (?,?,?,?,?,?,?,?,?)",
            [d.tracking_id, d.strategy_id, profile_id, d.enrollment_tier, now.to_pydatetime(),
             reason.strip(), origin, software_id, canonical_json(d.model_dump(mode="python"))],
        )  # fmt: skip
        ledger.store.con.execute(
            "INSERT INTO lab_forward_status VALUES (?,?,?,?,?)",
            ["fwdstatus_" + uuid4().hex, d.tracking_id, "active", "enrolled",
             now.to_pydatetime()],
        )  # fmt: skip
    return out


def set_status(
    ledger: Ledger, tracking_id: str, status: str, *, reason: str, now: datetime | None = None
) -> dict:
    """Append a status event: active <-> paused; either -> stopped (terminal)."""
    if status not in STATUSES:
        raise ForwardError(f"status must be one of {STATUSES}")
    if not reason or not reason.strip():
        raise ForwardError("a status change needs a reason")
    t = _tracking(ledger, tracking_id)
    now = _ts(now or utcnow())
    if t["status"] == "stopped":
        raise ForwardError("a stopped tracking is final; enroll a new one (e.g. with --label)")
    if t["status"] == status:
        raise ForwardError(f"tracking is already {status}")
    if t["events"] and now < _ts(t["events"][-1]["recorded_at"]):
        raise ForwardError("status events must be recorded in time order")
    ledger.store.con.execute(
        "INSERT INTO lab_forward_status VALUES (?,?,?,?,?)",
        ["fwdstatus_" + uuid4().hex, tracking_id, status, reason.strip(), now.to_pydatetime()],
    )
    return {"tracking_id": tracking_id, "status": status, "recorded_at": now.isoformat()}


def _first_bar(enrolled_at) -> pd.Timestamp:
    """First daily close strictly after enrollment (crypto daily bars close at UTC midnight)."""
    t = _ts(enrolled_at)
    return t.floor("D") + pd.Timedelta(days=1)


# --------------------------------------------------------------------------- check


def _answer(row: dict) -> tuple:
    return (row["status"], bool(row["eligible"]), bool(row["active"]), bool(row["fired"]))


def check(
    ledger: Ledger,
    *,
    software: SoftwareIdentity,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Evaluate each active tracking's newest completed bar if it is inside its window.

    Never evaluates an older bar: if the newest stored bar's window has passed, nothing is
    recorded and the bar remains a coverage gap. Idempotent; a different answer for an
    already-recorded bar is collected as a conflict and raised after the run is logged.
    """
    _require_tables(ledger)
    started = _ts(utcnow())
    now = _ts(now or started)
    current = semantics()
    notes, records, conflicts = [], [], []
    snapshots: dict = {}
    for t in trackings(ledger):
        d: TrackingDefinition = t["definition"]
        tid = t["tracking_id"]
        status = _status_at(t["events"], now)
        if status != "active":
            notes.append({"tracking_id": tid, "note": f"not active ({status})"})
            continue
        if d.semantics != current:
            notes.append({"tracking_id": tid, "note": "semantics changed since enrollment "
                          f"({d.semantics} -> {current}); not evaluated. Enroll a new tracking, "
                          "optionally with --continues, to resume under the new semantics"})  # fmt: skip
            continue
        definition = ledger.get_strategy(d.strategy_id)
        for symbol in d.assets:
            note = {"tracking_id": tid, "symbol": symbol}
            bar = newest_bar(ledger.store, d.source, symbol, now)
            if bar is None:
                notes.append({**note, "note": "no completed bars stored"})
                continue
            note["bar_close"] = bar.isoformat()
            if bar <= _ts(t["enrolled_at"]):
                notes.append({**note, "note": "newest bar closed before enrollment"})
                continue
            if not in_live_window(bar, now):
                notes.append({**note, "note": "newest stored bar is outside its evaluation "
                              "window; missed bars are never backfilled (run an update first)"})  # fmt: skip
                continue
            key = (d.source, symbol, bar, d.lookback_days)
            if key not in snapshots:
                snapshots[key] = live_snapshot(ledger.store, d.source, symbol, bar, d.lookback_days)
            snap = snapshots[key]
            if not funding_ready(snap, symbol, bar):
                notes.append({**note, "note": "funding through the bar close is not ingested "
                              "yet; will retry inside the window"})  # fmt: skip
                continue
            compiled = compile_strategy(definition, snap, symbol)
            if compiled.signal.index[-1] != bar:
                raise ForwardError("compiled frame does not end at the signal bar")
            i = -1
            eligible = bool(compiled.eligible.iloc[i])
            active = bool(compiled.active.iloc[i])
            fired = bool(compiled.signal.iloc[i])
            status_ = "signal" if fired else "no_signal" if eligible else "ineligible"
            input_id, manifest = _input_id(snap)
            meta = compiled.metadata.model_dump(mode="python")
            payload = {
                "evaluator_version": EVALUATOR_VERSION,
                "semantics": current,
                "tracking_id": tid,
                "strategy_id": d.strategy_id,
                "enrollment_profile_id": d.enrollment_profile_id,
                "enrollment_tier": d.enrollment_tier,
                "symbol": symbol,
                "source": d.source,
                "side": d.side,
                "signal_bar": {"open": _iso(compiled.inputs["ts"].iloc[i]), "close": bar.isoformat()},
                "data_cutoff": {"bars_close_time_lte": bar.isoformat(),
                                "funding_available_at_minute_lte": bar.isoformat(),
                                "snapshot_end_exclusive": (bar + AVAILABILITY_SLACK).isoformat(),
                                "lookback_days": d.lookback_days},
                "latency_seconds": (now - bar).total_seconds(),
                "features": {k: _num(v) for k, v in compiled.features.iloc[i].items()},
                "conditions": {k: bool(v) for k, v in compiled.conditions.iloc[i].items()},
                "compile": meta,
                "input_id": input_id,
            }  # fmt: skip
            rec = {
                "tracking_id": tid, "symbol": symbol, "bar_close": bar, "status": status_,
                "eligible": eligible, "active": active, "fired": fired,
                "close": _num(compiled.inputs["close"].iloc[i]),
                "stop": _num(compiled.stop.iloc[i]) if fired else None,
                "input_id": input_id, "manifest": manifest, "payload": payload,
            }  # fmt: skip
            old = _rows(
                ledger,
                "SELECT evaluation_id, status, eligible, active, fired FROM lab_forward_evaluations "
                "WHERE tracking_id=? AND symbol=? AND bar_close=?",
                [tid, symbol, bar.to_pydatetime()],
            )
            if old:
                if _answer(old[0]) == _answer(rec):
                    notes.append({**note, "note": f"already recorded ({status_}); unchanged"})
                else:
                    conflicts.append({**note, "recorded": _answer(old[0]), "now": _answer(rec)})
                continue
            records.append(rec)
            notes.append({**note, "note": f"evaluated: {status_}"})
    summary = {
        "evaluator_version": EVALUATOR_VERSION,
        "now": now.isoformat(),
        "recorded": len(records),
        "signals": sum(r["fired"] for r in records),
        "conflicts": [
            {**c, "recorded": list(c["recorded"]), "now": list(c["now"])} for c in conflicts
        ],
        "notes": notes,
        "dry_run": dry_run,
    }
    if not dry_run:
        software_id = ledger.register_software(software)
        run_id = "fwdrun_" + uuid4().hex
        with ledger.store.transaction():
            ledger.store.con.execute(
                "INSERT INTO lab_forward_runs VALUES (?,?,?,?,?,?)",
                [run_id, "check", now.to_pydatetime(), max(now, _ts(utcnow())).to_pydatetime(),
                 software_id, canonical_json(summary)],
            )  # fmt: skip
            for r in records:
                _put_input(ledger, r["input_id"], r["manifest"], now.to_pydatetime())
                ledger.store.con.execute(
                    "INSERT INTO lab_forward_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ["fwdeval_" + uuid4().hex, r["tracking_id"], r["symbol"],
                     r["bar_close"].to_pydatetime(), now.to_pydatetime(), run_id, r["status"],
                     r["eligible"], r["active"], r["fired"], r["close"], r["stop"],
                     r["input_id"], software_id,
                     canonical_json({**r["payload"], "software_id": software_id})],
                )  # fmt: skip
        summary["run_id"] = run_id
    if conflicts:
        raise ForwardConflict(
            f"{len(conflicts)} recomputed evaluation(s) differ from the recorded ones; nothing "
            f"was overwritten: {summary['conflicts']}"
        )
    return summary


# --------------------------------------------------------------------------- resolve


def _outcome(
    store,
    d: TrackingDefinition,
    symbol: str,
    bar: pd.Timestamp,
    horizon: Horizon,
    now: pd.Timestamp,
    cache: dict,
) -> dict | None:
    """Final outcome for (signal bar, horizon), or None while still pending."""
    exit_close = bar + pd.Timedelta(days=horizon.bars)
    if now < exit_close:
        return None
    key = (d.source, symbol, bar, exit_close)
    if key not in cache:
        cache[key] = _snapshot(
            store,
            _selections(d.source, symbol, bar - FUNDING_CONTEXT, exit_close + AVAILABILITY_SLACK),
        )
    snap = cache[key]
    input_id, manifest = _input_id(snap)
    cost = d.cost(symbol)
    base = {
        "resolver_version": RESOLVER_VERSION,
        "horizon": horizon.label,
        "bars": horizon.bars,
        "side": d.side,
        "signal_bar_close": bar.isoformat(),
        "entry_rule": "open of bar T+1 (opens at the signal bar close)",
        "exit_rule": "close of bar T+h",
        "exit_bar_close": exit_close.isoformat(),
        "return_model": d.return_model,
        "funding_policy": d.funding.model_dump(mode="python"),
        "fee_bps": cost.fee_bps,
        "slippage_bps": cost.slippage_bps,
        "input_id": input_id,
    }
    final = now >= exit_close + pd.Timedelta(days=d.outcome_wait_days)

    def missing(reason: str) -> dict | None:
        if not final:
            return None
        return {"status": "unavailable", "gross": None, "net": None, "exit_close": exit_close,
                "input_id": input_id, "manifest": manifest,
                "payload": {**base, "status": "unavailable", "reason": reason}}  # fmt: skip

    try:
        frame, _, _ = build_inputs(snap, symbol, "perp", AssetClass.CRYPTO, True)
    except ValueError as exc:  # CompileError: no rows / no funding series
        return missing(f"inputs unavailable: {exc}")
    closes = pd.DatetimeIndex(frame["close_time"])
    pos = np.flatnonzero(closes == bar)
    if len(pos) != 1:
        return missing("signal bar not in stored bars")
    i = int(pos[0])
    expected = [bar + pd.Timedelta(days=k) for k in range(horizon.bars + 1)]
    window = list(closes[i : i + horizon.bars + 1])
    if window != expected:
        return missing("bars T+1..T+h are missing or not contiguous")
    sign = 1 if d.side == "long" else -1
    costs = PerpCosts(cost.fee_bps, cost.slippage_bps)
    net = side_forward_returns(frame, {horizon.label: horizon.bars}, sign, costs)
    value = _num(net[f"ret_{horizon.label}"].iloc[i])
    entry = float(frame["open"].iloc[i + 1])
    exit_price = float(frame["close"].iloc[i + horizon.bars])
    if value is None:
        return missing("funding missing or published late for a day in T+1..T+h")
    gross = sign * (exit_price / entry - 1)
    funding = net[f"funding_{horizon.label}"].iloc[i]  # side-signed funding paid / entry
    payload = {
        **base,
        "status": "resolved",
        "entry_bar_open": _iso(frame["ts"].iloc[i + 1]),
        "entry_price": entry,
        "exit_price": exit_price,
        "gross": gross,
        "round_trip_cost": 2 * costs.per_side,
        "funding_paid": _num(funding),
        "net": value,
    }
    return {"status": "resolved", "gross": gross, "net": value, "exit_close": exit_close,
            "input_id": input_id, "manifest": manifest, "payload": payload}  # fmt: skip


def resolve(
    ledger: Ledger,
    *,
    software: SoftwareIdentity,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Write final entries/outcomes for recorded evaluations. Pending ones stay pending.

    Signals get an entry record and outcomes; evaluated no-signal bars with complete inputs
    get outcomes too (the prospective same-asset/side baseline). Stopped and paused
    trackings keep resolving what was already recorded; nothing new is observed here.
    """
    _require_tables(ledger)
    now = _ts(now or utcnow())
    defs = {t["tracking_id"]: t["definition"] for t in trackings(ledger)}
    evals = _rows(
        ledger,
        "SELECT evaluation_id, tracking_id, symbol, bar_close, fired FROM lab_forward_evaluations "
        "WHERE eligible ORDER BY bar_close, evaluation_id",
        [],
    )
    have_out = {(r[0], r[1]) for r in ledger.store.con.execute(
        "SELECT evaluation_id, horizon FROM lab_forward_outcomes").fetchall()}  # fmt: skip
    have_entry = {r[0] for r in ledger.store.con.execute(
        "SELECT evaluation_id FROM lab_forward_entries").fetchall()}  # fmt: skip
    cache: dict = {}
    entries, outcomes, pending = [], [], 0
    for e in evals:
        d = defs[e["tracking_id"]]
        bar = _ts(e["bar_close"])
        if e["fired"] and e["evaluation_id"] not in have_entry:
            hit = ledger.store.con.execute(
                "SELECT ts, open, close_time FROM perp_bars WHERE coin=? AND source=? AND "
                "timeframe='1d' AND ts=? AND close_time<=?",
                [e["symbol"], d.source, bar.to_pydatetime(), now.to_pydatetime()],
            ).fetchone()
            if hit is not None and _num(hit[1]) is not None:
                entries.append((e["evaluation_id"], "entered", _ts(hit[0]), float(hit[1]),
                                {"entry_bar_open": _iso(hit[0]), "entry_bar_close": _iso(hit[2]),
                                 "entry_price": float(hit[1]), "rule": "open of bar T+1",
                                 "note": "analytical reference price, not a simulated fill"}))  # fmt: skip
            elif now >= bar + pd.Timedelta(days=1 + d.outcome_wait_days):
                entries.append((e["evaluation_id"], "unavailable", None, None,
                                {"reason": "bar T+1 never became available", "rule": "open of bar T+1"}))  # fmt: skip
        for h in d.horizons:
            if (e["evaluation_id"], h.label) in have_out:
                continue
            out = _outcome(ledger.store, d, e["symbol"], bar, h, now, cache)
            if out is None:
                pending += 1
                continue
            outcomes.append((e["evaluation_id"], h.label, out))
    summary = {
        "resolver_version": RESOLVER_VERSION,
        "now": now.isoformat(),
        "entries": len(entries),
        "resolved": sum(o["status"] == "resolved" for _, _, o in outcomes),
        "unavailable": sum(o["status"] == "unavailable" for _, _, o in outcomes),
        "pending": pending,
        "dry_run": dry_run,
    }
    if dry_run:
        return summary
    software_id = ledger.register_software(software)
    run_id = "fwdrun_" + uuid4().hex
    with ledger.store.transaction():
        ledger.store.con.execute(
            "INSERT INTO lab_forward_runs VALUES (?,?,?,?,?,?)",
            [run_id, "resolve", now.to_pydatetime(), max(now, _ts(utcnow())).to_pydatetime(),
             software_id, canonical_json(summary)],
        )  # fmt: skip
        for evaluation_id, status, entry_open, price, payload in entries:
            ledger.store.con.execute(
                "INSERT INTO lab_forward_entries VALUES (?,?,?,?,?,?)",
                [evaluation_id, status, None if entry_open is None else entry_open.to_pydatetime(),
                 price, now.to_pydatetime(), canonical_json({**payload, "run_id": run_id})],
            )  # fmt: skip
        for evaluation_id, label, o in outcomes:
            _put_input(ledger, o["input_id"], o["manifest"], now.to_pydatetime())
            ledger.store.con.execute(
                "INSERT INTO lab_forward_outcomes VALUES (?,?,?,?,?,?,?,?,?,?)",
                [evaluation_id, label, o["status"], o["exit_close"].to_pydatetime(), o["gross"],
                 o["net"], o["input_id"], software_id, now.to_pydatetime(),
                 canonical_json({**o["payload"], "run_id": run_id, "software_id": software_id})],
            )  # fmt: skip
    summary["run_id"] = run_id
    return summary


# --------------------------------------------------------------------------- coverage


def coverage(ledger: Ledger, tracking_id: str, now: datetime | None = None) -> dict:
    """Per expected (symbol, daily bar): evaluated / pending / paused / gap.

    Expected bars are daily closes after enrollment up to ``now`` (and until a stop).
    A gap is a bar whose evaluation window passed without an evaluation; its reason says
    whether any check ran inside the window (and what it noted) or none ran at all.
    """
    t = _tracking(ledger, tracking_id)
    d: TrackingDefinition = t["definition"]
    now = _ts(now or utcnow())
    evaluated = {(r["symbol"], _ts(r["bar_close"])): r["status"] for r in _rows(
        ledger, "SELECT symbol, bar_close, status FROM lab_forward_evaluations WHERE tracking_id=?",
        [tracking_id])}  # fmt: skip
    runs = _rows(ledger, "SELECT started_at, summary FROM lab_forward_runs WHERE kind='check'", [])
    runs = [(_ts(r["started_at"]), json.loads(r["summary"])) for r in runs]
    first = _first_bar(t["enrolled_at"])
    days = []
    bar = first
    while bar <= now:
        days.append(bar)
        bar += pd.Timedelta(days=1)
    per_symbol = {}
    totals = defaultdict(int)
    for symbol in d.assets:
        rows, gaps = [], []
        for bar in days:
            status = _status_at(t["events"], bar)
            if (symbol, bar) in evaluated:
                state, reason = "evaluated", evaluated[(symbol, bar)]
            elif status in ("paused", "stopped"):
                state, reason = status, None
            elif now < bar + GRACE:
                state, reason = "pending_window", None
            else:
                inside = [s for at, s in runs if bar <= at < bar + GRACE]
                said = [n["note"] for s in inside for n in s.get("notes", [])
                        if n.get("tracking_id") == tracking_id and n.get("symbol") == symbol]  # fmt: skip
                state = "gap"
                reason = ("check ran but did not evaluate: " + "; ".join(sorted(set(said)))
                          if said else "check ran; tracking not evaluated" if inside
                          else "no check ran inside the window")  # fmt: skip
                gaps.append({"bar_close": bar.isoformat(), "reason": reason})
            rows.append((bar, state, reason))
            totals[state] += 1
        per_symbol[symbol] = {
            "evaluated": sum(s == "evaluated" for _, s, _ in rows),
            "gaps": gaps,
            "paused_or_stopped": sum(s in ("paused", "stopped") for _, s, _ in rows),
            "pending_window": sum(s == "pending_window" for _, s, _ in rows),
        }
    expected = totals["evaluated"] + totals["gap"]
    return {
        "tracking_id": tracking_id,
        "first_bar_close": first.isoformat(),
        "through": now.isoformat(),
        "expected_evaluations": expected,  # excludes paused/stopped and open windows
        "evaluated": totals["evaluated"],
        "gaps": totals["gap"],
        "paused_or_stopped": totals["paused"] + totals["stopped"],
        "pending_window": totals["pending_window"],
        "coverage_share": _num(totals["evaluated"] / expected) if expected else None,
        "by_symbol": per_symbol,
    }


# --------------------------------------------------------------------------- summary


def _digest(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "hit_rate": None, "min": None,
                "q25": None, "q75": None, "max": None}  # fmt: skip
    a = np.asarray(values, float)
    return {"n": len(a), "mean": _num(a.mean()), "median": _num(np.median(a)),
            "hit_rate": _num((a > 0).mean()), "min": _num(a.min()),
            "q25": _num(np.quantile(a, 0.25)), "q75": _num(np.quantile(a, 0.75)),
            "max": _num(a.max())}  # fmt: skip


def forward_summary(
    ledger: Ledger,
    tracking_id: str,
    *,
    as_of: datetime | None = None,
    maturity_version: int | None = None,
) -> dict:
    """Descriptive forward evidence from immutable rows recorded up to ``as_of``.

    No p-values: prospective samples start tiny and a test would invite retuning.
    Comparisons with the enrollment profile are signs and differences only.
    """
    t = _tracking(ledger, tracking_id)
    d: TrackingDefinition = t["definition"]
    as_of = _ts(as_of or utcnow())
    mpol = MATURITY_POLICIES[maturity_version or max(MATURITY_POLICIES)]
    profile = _profile(ledger, d.enrollment_profile_id)
    evals = _rows(
        ledger,
        "SELECT evaluation_id, symbol, bar_close, status, fired FROM lab_forward_evaluations "
        "WHERE tracking_id=? AND evaluated_at<=? ORDER BY bar_close, symbol",
        [tracking_id, as_of.to_pydatetime()],
    )
    by_id = {e["evaluation_id"]: e for e in evals}
    outs = [o for o in _rows(
        ledger,
        "SELECT o.evaluation_id, o.horizon, o.status, o.net, o.gross FROM lab_forward_outcomes o "
        "JOIN lab_forward_evaluations e USING (evaluation_id) WHERE e.tracking_id=? "
        "AND o.recorded_at<=?", [tracking_id, as_of.to_pydatetime()]) if o["evaluation_id"] in by_id]  # fmt: skip
    entries = [r for r in _rows(
        ledger, "SELECT evaluation_id, status FROM lab_forward_entries WHERE recorded_at<=?",
        [as_of.to_pydatetime()]) if r["evaluation_id"] in by_id]  # fmt: skip
    cov = coverage(ledger, tracking_id, as_of)
    hist_rows = {r["horizon"]: r for r in profile.horizons.get("rows", [])}
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    rows = []
    signals = [e for e in evals if e["fired"]]
    for h in d.horizons:
        res = [o for o in outs if o["horizon"] == h.label]
        sig = [o for o in res if by_id[o["evaluation_id"]]["fired"]]
        resolved = [o for o in sig if o["status"] == "resolved"]
        base = defaultdict(list)
        for o in res:
            if o["status"] == "resolved" and not by_id[o["evaluation_id"]]["fired"]:
                base[by_id[o["evaluation_id"]]["symbol"]].append(o["net"])
        # independent events per asset: keep the first, then >= h bars after the last kept
        independent = []
        for symbol in sorted({by_id[o["evaluation_id"]]["symbol"] for o in resolved}):
            mine = sorted((o for o in resolved if by_id[o["evaluation_id"]]["symbol"] == symbol),
                          key=lambda o: by_id[o["evaluation_id"]]["bar_close"])  # fmt: skip
            day = np.array(
                [(_ts(by_id[o["evaluation_id"]]["bar_close"]) - epoch).days for o in mine]
            )
            keep = set(decluster(day, h.bars).tolist())
            independent += [o for o, x in zip(mine, day, strict=True) if x in keep]
        excess = [o["net"] - float(np.mean(base[by_id[o["evaluation_id"]]["symbol"]]))
                  for o in independent if base[by_id[o["evaluation_id"]]["symbol"]]]  # fmt: skip
        net = _stats([o["net"] for o in independent])
        hist = hist_rows.get(h.label, {})
        f_ex = _num(np.mean(excess)) if excess else None
        h_ex = hist.get("excess_mean")
        rows.append({
            "horizon": h.label,
            "bars": h.bars,
            "primary": h.label == d.primary_horizon,
            "signals_recorded": len(signals),
            "signals_resolved": len(resolved),
            "signals_unavailable": sum(o["status"] == "unavailable" for o in sig),
            "signals_pending": len(signals) - len(sig),
            "independent_resolved": len(independent),
            "net": net,
            "gross_mean": _num(np.mean([o["gross"] for o in independent])) if independent else None,
            "baseline_bars": sum(len(v) for v in base.values()),
            "baseline_mean": _num(np.mean([x for v in base.values() for x in v]))
            if base else None,
            "excess_mean": f_ex,
            "excess_events": len(excess),
            "historical": {"excess_mean": _num(h_ex), "net_mean": _num(hist.get("net_mean")),
                           "independent_events": hist.get("independent_events")},
            "direction_vs_historical": None if f_ex is None or h_ex is None or h_ex == 0
            else "same" if np.sign(f_ex) == np.sign(h_ex) else "opposite",
            "excess_difference": _num(f_ex - h_ex) if f_ex is not None and h_ex is not None else None,
        })  # fmt: skip
    primary = next(r for r in rows if r["primary"])
    observed_days = len({_ts(e["bar_close"]) for e in evals})
    payload = {
        "summary_version": SUMMARY_VERSION,
        "tracking_id": tracking_id,
        "strategy_id": d.strategy_id,
        "enrollment_profile_id": d.enrollment_profile_id,
        "enrollment_tier": d.enrollment_tier,
        "semantics": d.semantics,
        "as_of": as_of.isoformat(),
        "observation": {
            "enrolled_at": _iso(t["enrolled_at"]),
            "first_bar_close": cov["first_bar_close"],
            "first_evaluated_bar": _iso(evals[0]["bar_close"]) if evals else None,
            "last_evaluated_bar": _iso(evals[-1]["bar_close"]) if evals else None,
            "observed_days": observed_days,
            "expected_evaluations": cov["expected_evaluations"],
            "evaluated": cov["evaluated"],
            "gap_evaluations": cov["gaps"],
            "paused_or_stopped": cov["paused_or_stopped"],
            "coverage_share": cov["coverage_share"],
            "evaluated_signal": sum(e["status"] == "signal" for e in evals),
            "evaluated_no_signal": sum(e["status"] == "no_signal" for e in evals),
            "evaluated_inputs_incomplete": sum(e["status"] == "ineligible" for e in evals),
        },
        "entries": {
            "recorded": sum(r["status"] == "entered" for r in entries),
            "unavailable": sum(r["status"] == "unavailable" for r in entries),
        },
        "horizons": rows,
        "maturity": {
            "policy": f"{mpol.name}_v{mpol.version}",
            "thresholds": mpol.model_dump(mode="python"),
            "level": mpol.level(primary["independent_resolved"], observed_days),
            "basis": "independent resolved primary-horizon signal outcomes and observed days",
            "note": "maturity is sample size, not quality; a mature negative sample is evidence",
        },
        "records": {
            "evaluations": len(evals),
            "evaluations_digest": _digest([e["evaluation_id"] for e in evals]),
            "outcomes": len(outs),
            "outcomes_digest": _digest([f"{o['evaluation_id']}:{o['horizon']}" for o in outs]),
        },
        "note": "Descriptive prospective evidence. No significance test; the historical tier "
        "is unchanged by forward outcomes.",
    }
    return payload


def record_forward_evidence(
    ledger: Ledger,
    tracking_id: str,
    *,
    software: SoftwareIdentity,
    as_of: datetime | None = None,
) -> dict:
    """Append a forward summary and a NEW profile extending the enrollment profile.

    The enrollment profile and every historical record are only read. The new profile
    keeps the historical tier, cites the extra ``paper_forward`` source and ``extends``
    the enrollment profile (profile schema 3).
    """
    t = _tracking(ledger, tracking_id)
    d: TrackingDefinition = t["definition"]
    payload = forward_summary(ledger, tracking_id, as_of=as_of)
    summary_id = content_id("fwdsummary_", payload)
    base = _profile(ledger, d.enrollment_profile_id)
    policy_row = ledger.store.con.execute(
        "SELECT payload FROM lab_evidence_policies WHERE policy_id=?", [base.policy_id]
    ).fetchone()
    policy = EvidencePolicy.model_validate_json(policy_row[0])
    software_id = ledger.register_software(software)
    source = EvidenceSource(
        stage="paper_forward",
        records={"tracking_id": tracking_id, "summary_id": summary_id, "as_of": payload["as_of"],
                 "evaluations_digest": payload["records"]["evaluations_digest"],
                 "outcomes_digest": payload["records"]["outcomes_digest"]},
        versions={**d.semantics, "summary_version": SUMMARY_VERSION,
                  "maturity_policy": payload["maturity"]["policy"]},
    )  # fmt: skip
    data = base.model_dump(mode="python")
    data.update(
        profile_schema="3",
        builder={"version": FORWARD_BUILDER_VERSION, "software_id": software_id},
        sources=(*data["sources"], source.model_dump(mode="python")),
        extends=base.profile_id,
        forward={"summary_id": summary_id, **{k: payload[k] for k in (
            "as_of", "tracking_id", "enrollment_tier", "observation", "entries", "horizons",
            "maturity", "note")}},
        limitations=(*base.limitations, *FORWARD_LIMITATIONS),
    )  # fmt: skip
    profile = EvidenceProfile.model_validate(data)
    with ledger.store.transaction():
        if not ledger.store.con.execute(
            "SELECT 1 FROM lab_forward_summaries WHERE summary_id=?", [summary_id]
        ).fetchone():
            ledger.store.con.execute(
                "INSERT INTO lab_forward_summaries VALUES (?,?,?,?,?)",
                [summary_id, tracking_id, _ts(payload["as_of"]).to_pydatetime(),
                 canonical_json(payload), utcnow()],
            )  # fmt: skip
    out = record_profiles(ledger, [profile], policy)
    return {"summary_id": summary_id, "profile_id": profile.profile_id,
            "extends": base.profile_id, "tier": profile.tier, "new_profiles": out["new"],
            "maturity": payload["maturity"]["level"]}  # fmt: skip


# --------------------------------------------------------------------------- inspection


def show(ledger: Ledger, tracking_id: str, now: datetime | None = None, limit: int = 30) -> dict:
    t = _tracking(ledger, tracking_id)
    now = _ts(now or utcnow())
    evals = _rows(
        ledger,
        "SELECT evaluation_id, symbol, bar_close, evaluated_at, status, close, stop "
        "FROM lab_forward_evaluations WHERE tracking_id=? ORDER BY bar_close DESC, symbol",
        [tracking_id],
    )
    signals = []
    for e in evals:
        if e["status"] != "signal":
            continue
        outs = _rows(ledger, "SELECT horizon, status, gross, net, exit_bar_close FROM "
                     "lab_forward_outcomes WHERE evaluation_id=?", [e["evaluation_id"]])  # fmt: skip
        entry = _rows(ledger, "SELECT status, entry_bar_open, entry_price FROM lab_forward_entries "
                      "WHERE evaluation_id=?", [e["evaluation_id"]])  # fmt: skip
        done = {o["horizon"] for o in outs}
        signals.append({**e, "entry": entry[0] if entry else {"status": "pending"},
                        "outcomes": outs + [{"horizon": h.label, "status": "pending"}
                                            for h in t["definition"].horizons
                                            if h.label not in done]})  # fmt: skip
    return {
        "tracking_id": tracking_id,
        "strategy_id": t["strategy_id"],
        "enrollment_profile_id": t["profile_id"],
        "enrollment_tier": t["profile_tier"],
        "enrolled_at": t["enrolled_at"],
        "reason": t["reason"],
        "origin": t["origin"],
        "software_id": t["software_id"],
        "status": t["status"],
        "status_history": t["events"],
        "definition": t["definition"].model_dump(mode="json"),
        "coverage": coverage(ledger, tracking_id, now),
        "signals": signals,
        "recent_evaluations": evals[:limit],
        "summary": forward_summary(ledger, tracking_id, as_of=now),
    }


def list_trackings(ledger: Ledger) -> list[dict]:
    out = []
    for t in trackings(ledger):
        d: TrackingDefinition = t["definition"]
        counts = ledger.store.con.execute(
            "SELECT count(*), count(*) FILTER (WHERE fired), max(bar_close) "
            "FROM lab_forward_evaluations WHERE tracking_id=?", [t["tracking_id"]]).fetchone()  # fmt: skip
        out.append({
            "tracking_id": t["tracking_id"], "strategy_id": t["strategy_id"],
            "family": d.family, "params": d.params, "side": d.side, "label": d.label,
            "enrollment_tier": d.enrollment_tier, "status": t["status"],
            "enrolled_at": t["enrolled_at"], "evaluations": counts[0], "signals": counts[1],
            "last_evaluated_bar": counts[2],
        })  # fmt: skip
    return out


# --------------------------------------------------------------------------- candidates


SELECTION_RULE = "plateau_centrality_v1"


def candidates(ledger: Ledger, analysis_id: str, policy_id: str) -> dict:
    """Enrollable profiles of one batch analysis, with a deterministic representative rule.

    ``plateau_centrality_v1``: per family and side, among EXPLORATORY/RESEARCH_SUPPORTED
    profiles, prefer neighbourhood ``plateau`` members, else ``mixed``; isolated spikes and
    members without testable neighbours are never suggested. Within that group pick the
    most agreeing testable neighbours, then most testable neighbours, then the name.
    Effect size, p and q are deliberately not used: the rule must not pick the
    historically best variant.
    """
    from market_signal.research.lab.evidence import load_profiles

    profiles = [p for p in load_profiles(ledger, analysis_id=analysis_id)
                if p["policy_id"] == policy_id]  # fmt: skip
    groups = defaultdict(list)
    for p in profiles:
        if p["tier"] in ENROLLABLE_TIERS and p.get("profile_schema") not in ("3", "5"):
            s = p["subject"]
            groups[(str(s["family"] or s["ledger_family"]), s["side"])].append(p)
    out = []
    for (family, side), group in sorted(groups.items()):

        def rank(p):
            nb = p["neighbourhood"]
            return (-(nb.get("same_direction") or 0), -(nb.get("testable_neighbours") or 0),
                    p["subject"]["name"])  # fmt: skip

        pick = None
        for label in ("plateau", "mixed"):
            pool = [p for p in group if p["neighbourhood"].get("label") == label
                    and not p["neighbourhood"].get("isolated_spike")]  # fmt: skip
            if pool:
                pick = sorted(pool, key=rank)[0]
                break
        out.append({
            "family": family, "side": side,
            "suggested": None if pick is None else {
                "profile_id": pick["profile_id"], "name": pick["subject"]["name"],
                "strategy_id": pick["subject"]["strategy_id"], "tier": pick["tier"],
                "neighbourhood": pick["neighbourhood"].get("label"),
                "agreeing_neighbours": pick["neighbourhood"].get("same_direction"),
                "testable_neighbours": pick["neighbourhood"].get("testable_neighbours")},
            "members": [{"profile_id": p["profile_id"], "name": p["subject"]["name"],
                         "tier": p["tier"], "neighbourhood": p["neighbourhood"].get("label")}
                        for p in sorted(group, key=rank)],
        })  # fmt: skip
    return {"rule": SELECTION_RULE, "analysis_id": analysis_id, "policy_id": policy_id,
            "note": "suggestions ignore effect size, p and q; enrollment is still a manual choice",
            "groups": out}  # fmt: skip
