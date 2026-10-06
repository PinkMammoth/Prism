"""Paper auto-trader engine: governed signals -> promotion -> risk -> simulated orders -> account.

A paper run is a stateful simulated perp account with a frozen identity: cohort, promotion
policy, risk policy, execution model, exit policy, maturity policy, engine version and
cycle order. Its whole history is the append-only ``paper_events`` ledger; the account
(cash, positions, orders, marks) is always ``account.replay`` of those events.

Prospective by construction:

- Only bars closing strictly after the run's ``created_at`` are processed. No earlier signal
  can ever become an order (also a DB CHECK on ``paper_events``).
- An entry is decided from data available at bar T's close and must be recorded within the
  execution model's entry window after T closes; it fills at T+1's stored open. A signal
  first seen after the window is recorded and REJECTED (``missed_execution_window``), never
  filled retrospectively.
- Bars are processed strictly in time order, each exactly once (``CYCLE_ORDER``). One
  cycle's events are committed in one transaction under idempotent event keys, so a crash or
  a re-run can neither duplicate a fill nor double-charge funding.

Consumer neutrality: the engine reads ``lab_*`` tables (strategies, evidence, forward
tracking) and market tables, and writes only ``paper_*`` tables. It does not import or
depend on the co-pilot, and nothing in ``research/`` imports it.

Safety: execution goes through ``PaperExecutionAdapter`` only (see ``execution.py``). No
module here imports a network client; Telegram is an injected callable used after commit.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import uuid4

import pandas as pd
from pydantic import Field, model_validator

from market_signal.models.domain import utcnow
from market_signal.paper import render
from market_signal.paper.account import (
    EPS,
    TERMINAL_STATUSES,
    AccountState,
    ImpossibleState,
    apply,
    replay,
)
from market_signal.paper.execution import PaperExecutionAdapter, PaperOrder, require_paper_adapter
from market_signal.paper.policy import (
    AssetCosts,
    AutotraderPolicy,
    ExecutionModel,
    ExitPolicy,
    PaperMaturityPolicy,
    RiskPolicy,
    evaluate_promotion,
    exit_policy,
    maturity_policy,
    promotion_policy,
    risk_policy,
)
from market_signal.paper.risk import allocate
from market_signal.research.lab import forward as fwd
from market_signal.research.lab.common import (
    LabModel,
    Name,
    PositiveInt,
    Symbol,
    Text,
    UTCDateTime,
    canonical_json,
    content_id,
)
from market_signal.research.lab.compiler import COMPILER_VERSION, compile_strategy
from market_signal.research.lab.evidence import EvidenceProfile
from market_signal.research.lab.ledger import Ledger, LedgerError
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.vocabulary import VOCABULARY_VERSION

ENGINE_VERSION = "paper_engine_v1"
EVIDENCE_STAGE = "paper_execution"
SUMMARY_VERSION = "paper_execution_summary_v1"
BAR = timedelta(days=1)
# Frozen daily event order for one bar close B (part of every run's identity).
CYCLE_ORDER = (
    "validate_bar_data",
    "fill_entry_orders_at_open",
    "liquidation_check_on_bar_range",
    "accrue_settled_funding",
    "mark_at_close",
    "scheduled_exits_at_close",
    "account_state_and_kill_switches",
    "read_signals",
    "intents_promotion_and_risk",
    "submit_entry_orders",
    "persist_then_notify",
)
NOTIFY_WINDOW = timedelta(days=1)  # older unsent notifications are never sent late

Sender = Callable[[str], None]


class PaperError(LedgerError):
    pass


def semantics() -> dict[str, str]:
    return {"compiler_version": COMPILER_VERSION, "vocabulary_version": VOCABULARY_VERSION}


class CohortMember(LabModel):
    """One enrolled strategy, frozen from its active Phase 8 tracking definition."""

    strategy_id: str
    strategy_name: Text
    side: Literal["long", "short"]
    source: Text
    assets: Annotated[tuple[Symbol, ...], Field(min_length=1)]
    lookback_days: Annotated[int, Field(strict=True, ge=1, le=5000)]
    primary_horizon: str
    horizon_bars: PositiveInt
    baseline_profile_id: str
    baseline_tier: Literal["EXPLORATORY", "RESEARCH_SUPPORTED"]
    tracking_id: str

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1


class PaperRunDefinition(LabModel):
    """Frozen paper account identity. Any change is a new run (``continues`` = lineage)."""

    schema_version: Literal["1"] = "1"
    mode: Literal["paper"] = "paper"
    label: Name | None = None
    continues: str | None = None
    created_at: UTCDateTime
    cohort: Annotated[tuple[CohortMember, ...], Field(min_length=1)]
    promotion_policy_id: str
    risk_policy_id: str
    execution_model_id: str
    exit_policy_id: str
    maturity_policy_id: str
    engine_version: Literal["paper_engine_v1"] = ENGINE_VERSION
    cycle_order: tuple[str, ...] = CYCLE_ORDER
    semantics: dict[str, str]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        ids = [m.strategy_id for m in self.cohort]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate cohort strategies")
        if self.cycle_order != CYCLE_ORDER:
            raise ValueError("cycle order is frozen by the engine version")
        return self

    @property
    def run_id(self) -> str:
        return content_id("paperrun_", self.model_dump(mode="python"))

    @property
    def first_bar(self) -> pd.Timestamp:
        """First processable close: the next UTC midnight strictly after creation."""
        return _ts(self.created_at).floor("D") + BAR


class RunContext:
    """A loaded run: definition, its frozen policies and the paper adapter."""

    def __init__(self, run_id: str, definition: PaperRunDefinition, promotion: AutotraderPolicy,
                 risk: RiskPolicy, execution: ExecutionModel, exit_: ExitPolicy,
                 maturity: PaperMaturityPolicy):  # fmt: skip
        self.run_id, self.definition = run_id, definition
        self.promotion, self.risk, self.execution = promotion, risk, execution
        self.exit, self.maturity = exit_, maturity
        self.adapter = require_paper_adapter(PaperExecutionAdapter(execution))

    @property
    def window(self) -> timedelta:
        return timedelta(hours=self.execution.max_entry_latency_hours)


# --------------------------------------------------------------------------- helpers


def _ts(value) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(value) -> str:
    return _ts(value).isoformat()


def _rows(store, sql: str, args: list) -> list[dict]:
    cursor = store.con.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def _require_tables(store) -> None:
    if not store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='paper_events'"
    ).fetchone():
        raise PaperError("paper tables are absent; open Store writable once to migrate")


def _put(store, table: str, id_column: str, id_: str, payload: dict, now, kind: str | None = None):
    # Table/column identifiers are internal constants, never submitted values.
    row = store.con.execute(f"SELECT payload FROM {table} WHERE {id_column}=?", [id_]).fetchone()
    if row:
        if canonical_json(json.loads(row[0])) != canonical_json(payload):
            raise PaperError(f"{id_} already exists with different content")
        return
    values = [id_, kind, canonical_json(payload), _ts(now).to_pydatetime()]
    if kind is None:
        values.pop(1)
    store.con.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(values))})", values)


def _put_software(store, software: SoftwareIdentity, now) -> str:
    data = software.model_dump(mode="python")
    data["packages"] = sorted(data["packages"])
    _put(store, "paper_software", "software_id", software.software_id, data, now)
    return software.software_id


def _num(x) -> float | None:
    return fwd._num(x)


def _h(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- evidence view


def _profile(store, profile_id: str) -> EvidenceProfile:
    row = store.con.execute(
        "SELECT payload FROM lab_evidence_profiles WHERE profile_id=?", [profile_id]
    ).fetchone()
    if row is None:
        raise PaperError(f"unknown evidence profile {profile_id}")
    p = EvidenceProfile.model_validate_json(row[0])
    if p.profile_id != profile_id:
        raise PaperError("stored profile does not match its ID")
    return p


def evidence_chain(store, baseline_profile_id: str, as_of) -> list[str]:
    """The baseline and each newest profile extending the previous one, as recorded by ``as_of``.

    Point-in-time: a profile recorded after the decision time is invisible to it. Forward
    (schema 3) extensions are skipped because forward evidence is read from the tracking, so
    the chain is historical -> full research/validation (4) -> corroboration (5).
    """
    chain = [baseline_profile_id]
    while True:
        row = store.con.execute(
            "SELECT profile_id FROM lab_evidence_profiles WHERE "
            "json_extract_string(payload, '$.extends')=? AND "
            "json_extract_string(payload, '$.profile_schema') IN ('4','5') AND recorded_at <= ? "
            "ORDER BY recorded_at DESC, profile_id DESC LIMIT 1",
            [chain[-1], _ts(as_of).to_pydatetime()],
        ).fetchone()
        if row is None or row[0] in chain:
            return chain
        chain.append(row[0])


def _forward_view(ledger: Ledger, member: CohortMember, now) -> tuple[dict | None, bool]:
    """Read-only forward state of the member's frozen Phase 8 tracking."""
    try:
        hit = [t for t in fwd.trackings(ledger) if t["tracking_id"] == member.tracking_id]
    except LedgerError:
        return None, False
    if not hit:
        return None, False
    status = fwd._status_at(hit[0]["events"], now)
    try:
        s = fwd.forward_summary(ledger, member.tracking_id, as_of=now)
    except Exception as exc:  # display only; an unusable summary never invents a level
        return {"tracking_id": member.tracking_id, "status": status, "maturity": None,
                "error": str(exc)}, status == "active"  # fmt: skip
    prim = next(h for h in s["horizons"] if h["primary"])
    return {
        "tracking_id": member.tracking_id,
        "status": status,
        "maturity": s["maturity"]["level"],
        "independent_resolved": prim["independent_resolved"],
        "signals_recorded": prim["signals_recorded"],
        "excess_mean": prim["excess_mean"],
        "direction_vs_historical": prim["direction_vs_historical"],
    }, status == "active"


def evidence_view(ledger: Ledger, member: CohortMember, now) -> tuple[dict, dict]:
    """(evidence view, promotion context) for one cohort member. Lab records are only read."""
    store = ledger.store
    chain = evidence_chain(store, member.baseline_profile_id, now)
    base = _profile(store, chain[0])
    p = _profile(store, chain[-1])
    if p.subject.get("strategy_id") != member.strategy_id:
        raise PaperError("evidence profile belongs to another strategy")
    forward, active = _forward_view(ledger, member, now)
    versions: dict[str, set] = {}
    for s in p.sources:
        for k in ("compiler_version", "vocabulary_version"):
            if k in s.versions:
                versions.setdefault(k, set()).add(s.versions[k])
    view = {
        "profile_id": p.profile_id,
        "profile_schema": p.profile_schema,
        "chain": chain,
        "tier": p.tier,
        "historical_tier": base.tier,
        "primary_horizon": p.evaluation.get("primary_horizon"),
        "effect": {k: p.effect.get(k) for k in ("excess_mean", "net_mean", "hit_rate")},
        "sample": {k: p.sample.get(k) for k in ("independent_events", "assets_with_events")},
        "assets": {k: p.assets.get(k) for k in ("assets_with_events", "positive_share",
                                                "max_asset_event_share", "dominated_by_one_asset")},
        "neighbourhood": {k: p.neighbourhood.get(k) for k in ("label", "isolated_spike")},
        "statistics": {k: p.statistics.get(k) for k in ("raw_p", "q", "fdr_survivor")},
        "full_research_status": (p.full_research or {}).get("status"),
        "validation_status": (p.validation or {}).get("status"),
        "corroboration_status": (p.corroboration or {}).get("status"),
        "forward": forward if forward and forward.get("maturity") else None,
        "source_versions": {k: sorted(v) for k, v in versions.items()},
    }  # fmt: skip
    view = json.loads(canonical_json(view))
    ok, detail = True, "compiler/vocabulary versions match"
    cur = semantics()
    for k, v in cur.items():
        have = view["source_versions"].get(k)
        if have and have != [v]:
            ok, detail = False, f"evidence was produced under {k} {have}; live signals use {v}"
    return view, {"semantics_ok": ok, "semantics_detail": detail, "tracking_active": active}


# --------------------------------------------------------------------------- runs


def _member(ledger: Ledger, strategy: str) -> CohortMember:
    """Freeze a cohort member from the strategy's ACTIVE Phase 8 tracking (name or ID)."""
    hits = [t for t in fwd.trackings(ledger) if t["status"] == "active"]
    for t in hits:
        d: fwd.TrackingDefinition = t["definition"]
        name = ledger.store.con.execute(
            "SELECT json_extract_string(payload, '$.subject.name') FROM lab_evidence_profiles "
            "WHERE profile_id=?",
            [d.enrollment_profile_id],
        ).fetchone()[0]
        if strategy in (d.strategy_id, name):
            h = next(x for x in d.horizons if x.label == d.primary_horizon)
            return CohortMember(
                strategy_id=d.strategy_id, strategy_name=name or d.strategy_id, side=d.side,
                source=d.source, assets=d.assets, lookback_days=d.lookback_days,
                primary_horizon=d.primary_horizon, horizon_bars=h.bars,
                baseline_profile_id=d.enrollment_profile_id, baseline_tier=d.enrollment_tier,
                tracking_id=t["tracking_id"],
            )  # fmt: skip
    raise PaperError(f"{strategy} has no active Phase 8 forward tracking; paper cohorts are "
                     "frozen from active trackings only")  # fmt: skip


def _execution_model(ledger: Ledger, cohort: tuple[CohortMember, ...]) -> ExecutionModel:
    costs: dict[str, tuple[float, float]] = {}
    for m in cohort:
        d = next(
            t["definition"] for t in fwd.trackings(ledger) if t["tracking_id"] == m.tracking_id
        )
        for c in d.costs:
            pair = (float(c.fee_bps), float(c.slippage_bps))
            if costs.setdefault(c.symbol, pair) != pair:
                raise PaperError(f"cohort plans disagree on the cost of {c.symbol}")
    return ExecutionModel(costs=tuple(AssetCosts(symbol=s, fee_bps=f, slippage_bps=sl)
                                      for s, (f, sl) in sorted(costs.items())))  # fmt: skip


def runs(store) -> list[dict]:
    _require_tables(store)
    out = []
    for r in _rows(store, "SELECT * FROM paper_runs ORDER BY created_at, run_id", []):
        d = PaperRunDefinition.model_validate_json(r["definition"])
        if d.run_id != r["run_id"]:
            raise PaperError("stored run definition does not match its ID")
        out.append({**r, "definition": d})
    return out


def load_run(store, run_id: str) -> RunContext:
    hit = [r for r in runs(store) if r["run_id"] == run_id]
    if not hit:
        raise PaperError(f"unknown paper run {run_id}")
    d: PaperRunDefinition = hit[0]["definition"]

    def pol(model, policy_id):
        row = store.con.execute("SELECT payload FROM paper_policies WHERE policy_id=?",
                                [policy_id]).fetchone()  # fmt: skip
        if row is None:
            raise PaperError(f"policy {policy_id} is not recorded")
        obj = model.model_validate_json(row[0])
        if obj.policy_id != policy_id:
            raise PaperError(f"stored policy {policy_id} does not match its ID")
        return obj

    return RunContext(run_id, d, pol(AutotraderPolicy, d.promotion_policy_id),
                      pol(RiskPolicy, d.risk_policy_id), pol(ExecutionModel, d.execution_model_id),
                      pol(ExitPolicy, d.exit_policy_id), pol(PaperMaturityPolicy, d.maturity_policy_id))  # fmt: skip


def events(store, run_id: str) -> list[dict]:
    out = _rows(store, "SELECT * FROM paper_events WHERE run_id=? ORDER BY seq", [run_id])
    for e in out:
        e["payload"] = json.loads(e["payload"])
        for k in ("market_time", "run_created_at", "recorded_at"):
            e[k] = None if e[k] is None else _iso(e[k])
    return out


def account(store, run_id: str) -> AccountState:
    return replay(events(store, run_id))


def build_run(
    ledger: Ledger,
    strategies: list[str],
    *,
    now,
    label: str | None = None,
    continues: str | None = None,
    promotion: AutotraderPolicy | None = None,
    risk: RiskPolicy | None = None,
    exit_: ExitPolicy | None = None,
) -> tuple[PaperRunDefinition, dict]:
    """Pure read: the frozen run a creation would record, and each member's eligibility."""
    if not strategies:
        raise PaperError("a paper run needs at least one enrolled strategy")
    promotion, risk, exit_ = (
        promotion or promotion_policy(),
        risk or risk_policy(),
        exit_ or exit_policy(),
    )
    maturity = maturity_policy()
    cohort = tuple(sorted((_member(ledger, s) for s in dict.fromkeys(strategies)),
                          key=lambda m: m.strategy_name))  # fmt: skip
    execution = _execution_model(ledger, cohort)
    for m in cohort:
        if m.horizon_bars != exit_.horizon_bars:
            raise PaperError(f"{m.strategy_name}: primary horizon {m.primary_horizon} is not the "
                             f"exit policy's {exit_.horizon_bars} bars")  # fmt: skip
        missing = [a for a in m.assets if risk.maintenance_rate(a) is None]
        if missing:
            raise PaperError(f"risk policy has no frozen maintenance rate for {missing}")
    d = PaperRunDefinition(
        label=label, continues=continues, created_at=_ts(now).to_pydatetime(), cohort=cohort,
        promotion_policy_id=promotion.policy_id, risk_policy_id=risk.policy_id,
        execution_model_id=execution.policy_id, exit_policy_id=exit_.policy_id,
        maturity_policy_id=maturity.policy_id, semantics=semantics(),
    )  # fmt: skip
    eligibility = {}
    for m in cohort:
        view, ctx = evidence_view(ledger, m, now)
        eligibility[m.strategy_name] = {
            "evidence": view,
            "result": evaluate_promotion(promotion, view, {**ctx, "enrolled": True}),
        }
    policies = {"promotion": promotion, "risk": risk, "execution": execution, "exit": exit_,
                "maturity": maturity}  # fmt: skip
    return d, {"eligibility": eligibility, "policies": policies}


def create_run(
    ledger: Ledger,
    strategies: list[str],
    *,
    reason: str,
    origin: str,
    software: SoftwareIdentity,
    now: datetime | None = None,
    label: str | None = None,
    continues: str | None = None,
    promotion: AutotraderPolicy | None = None,
    risk: RiskPolicy | None = None,
    exit_: ExitPolicy | None = None,
    dry_run: bool = False,
) -> dict:
    """Create a paper account. Its clock starts now: no earlier bar is ever processed."""
    store = ledger.store
    _require_tables(store)
    if not reason or not reason.strip():
        raise PaperError("a paper run needs a recorded reason")
    now = _ts(now or utcnow())
    d, info = build_run(ledger, strategies, now=now, label=label, continues=continues,
                        promotion=promotion, risk=risk, exit_=exit_)  # fmt: skip
    blocked = {k: v["result"]["blocked_by"] for k, v in info["eligibility"].items()
               if v["result"]["decision"] != "PAPER_ELIGIBLE"}  # fmt: skip
    existing = runs(store)
    open_runs = [r["run_id"] for r in existing
                 if account(store, r["run_id"]).status not in TERMINAL_STATUSES]  # fmt: skip
    pol = info["policies"]
    out = {
        "run_id": d.run_id,
        "mode": "paper",
        "created_at": now.isoformat(),
        "first_processable_bar_close": d.first_bar.isoformat(),
        "starting_equity": pol["risk"].starting_equity,
        "currency": pol["risk"].currency,
        "cohort": [m.model_dump(mode="json") for m in d.cohort],
        "policies": {k: {"policy_id": v.policy_id, **v.model_dump(mode="json")} for k, v in pol.items()},
        "engine_version": ENGINE_VERSION,
        "cycle_order": list(CYCLE_ORDER),
        "eligibility": {k: {"decision": v["result"]["decision"], "blocked_by": v["result"]["blocked_by"],
                            "caveats": v["result"]["caveats"], "profile_id": v["evidence"]["profile_id"]}
                        for k, v in info["eligibility"].items()},
        "open_runs": open_runs,
        "dry_run": dry_run,
        "note": "simulated account only: no order can reach an exchange; no historical signal "
                "is traded",
    }  # fmt: skip
    refused = []
    if blocked:
        refused.append(f"cohort members not paper-eligible under {pol['promotion'].name} "
                       f"v{pol['promotion'].version}: {blocked}")  # fmt: skip
    if open_runs:
        refused.append(f"a paper run is still open ({open_runs}); stop it first")
    if continues is not None:
        if continues not in {r["run_id"] for r in existing}:
            refused.append(f"unknown run to continue: {continues}")
        elif account(store, continues).status not in TERMINAL_STATUSES:
            refused.append("only a stopped or killed run can be continued")
    if any(r["run_id"] == d.run_id for r in existing):
        refused.append(f"{d.run_id} already exists")
    if refused:
        out["refused"] = refused
        if not dry_run:
            raise PaperError("; ".join(refused))
    if dry_run:
        return out
    with store.transaction():
        software_id = _put_software(store, software, now)
        for kind, p in pol.items():
            _put(store, "paper_policies", "policy_id", p.policy_id, p.model_dump(mode="python"),
                 now, kind=kind)  # fmt: skip
        store.con.execute(
            "INSERT INTO paper_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [d.run_id, now.to_pydatetime(), "paper", label, continues, d.promotion_policy_id,
             d.risk_policy_id, d.execution_model_id, d.exit_policy_id, d.maturity_policy_id,
             ENGINE_VERSION, reason.strip(), origin, software_id,
             canonical_json(d.model_dump(mode="python"))],
        )  # fmt: skip
        _insert(store, d.run_id, now, None, [_event(d.run_id, 1, "run_created", "run_created",
                                                    None, {"created_at": now.isoformat(),
                                                          "starting_equity": pol["risk"].starting_equity,
                                                          "currency": pol["risk"].currency,
                                                          "reason": reason.strip()})], now)  # fmt: skip
    return out


def _event(run_id: str, seq: int, event_type: str, key: str, market_time, payload: dict) -> dict:
    return {
        "event_id": content_id("paperevt_", {"run_id": run_id, "key": key}),
        "run_id": run_id,
        "seq": seq,
        "event_key": key,
        "event_type": event_type,
        "market_time": None if market_time is None else _iso(market_time),
        "payload": json.loads(canonical_json(payload)),
    }


def _insert(store, run_id: str, run_created_at, cycle_id: str | None, new: list[dict], now) -> None:
    for e in new:
        store.con.execute(
            "INSERT INTO paper_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            [e["event_id"], run_id, e["seq"], e["event_key"], e["event_type"],
             None if e["market_time"] is None else _ts(e["market_time"]).to_pydatetime(),
             _ts(run_created_at).to_pydatetime(), _ts(now).to_pydatetime(), cycle_id,
             canonical_json(e["payload"])],
        )  # fmt: skip


def set_status(
    store, run_id: str, status: str, *, reason: str, now: datetime | None = None
) -> dict:
    """Manual administration: PAUSED (no new entries) <-> ACTIVE; STOPPED (terminal).

    Existing positions keep being managed to their scheduled exits in every status.
    """
    if status not in ("ACTIVE", "PAUSED", "STOPPED"):
        raise PaperError("manual status must be ACTIVE (resume), PAUSED or STOPPED")
    if not reason or not reason.strip():
        raise PaperError("a status change needs a reason")
    ctx = load_run(store, run_id)
    now = _ts(now or utcnow())
    state = account(store, run_id)
    if state.terminal:
        raise PaperError(f"run is {state.status}: final; create a new run (--continues)")
    if state.status == status:
        raise PaperError(f"run is already {status}")
    payload = {"status": status, "previous": state.status, "actor": "manual",
               "reason": reason.strip(), "at": now.isoformat()}  # fmt: skip
    e = _event(run_id, state.seq + 1, "status_changed",
               f"status:{state.seq + 1}:{status}", None, payload)  # fmt: skip
    with store.transaction():
        _insert(store, run_id, ctx.definition.created_at, None, [e], now)
    return {
        "run_id": run_id,
        "status": status,
        "previous": state.status,
        "recorded_at": now.isoformat(),
    }


# --------------------------------------------------------------------------- market data


def _bar(store, source: str, symbol: str, close) -> dict | None:
    rows = _rows(store, "SELECT open, high, low, close FROM perp_bars WHERE coin=? AND source=? "
                 "AND timeframe='1d' AND close_time=?", [symbol, source, _ts(close).to_pydatetime()])  # fmt: skip
    if not rows:
        return None
    r = {k: float(v) for k, v in rows[0].items()}
    return r if all(v > 0 for v in r.values()) else None


def _funding_ready(store, source: str, symbol: str, close) -> bool:
    row = store.con.execute("SELECT max(time) FROM perp_funding WHERE coin=? AND source=?",
                            [symbol, source]).fetchone()  # fmt: skip
    return row[0] is not None and _ts(row[0]).round("min") >= _ts(close)


def _settlements(store, source: str, symbol: str, start, end) -> list[dict]:
    """Settled funding rates in (start, end], timestamps snapped to the minute."""
    rows = _rows(store, "SELECT time, funding_rate FROM perp_funding WHERE coin=? AND source=? "
                 "AND time > ? AND time < ? ORDER BY time",
                 [symbol, source, (_ts(start) - timedelta(minutes=1)).to_pydatetime(),
                  (_ts(end) + timedelta(minutes=1)).to_pydatetime()])  # fmt: skip
    out = []
    for r in rows:
        t = _ts(r["time"]).round("min")
        if _ts(start) < t <= _ts(end) and r["funding_rate"] is not None:
            out.append({"time": t.isoformat(), "rate": float(r["funding_rate"])})
    return out


def _beyond(price: float, level: float, side: int) -> bool:
    """Has the price reached ``level`` adversely for a position on ``side``?"""
    return price <= level if side > 0 else price >= level


def _snapshot_funding_ready(snap, symbol: str, bar) -> bool:
    """``forward.funding_ready`` (same answer: minute rounding is monotonic), O(1) conversions."""
    rows = snap.find("perp_funding", symbol)[1]
    return bool(rows) and _ts(max(r["time"] for r in rows)).round("min") >= _ts(bar)


def _signal(
    ledger: Ledger, m: CohortMember, symbol: str, bar, snapshots: dict | None = None
) -> dict:
    """The Phase 3 compiler on data available at bar T's close (Phase 8 live snapshot)."""
    store = ledger.store
    key = (m.source, symbol, _iso(bar), m.lookback_days)
    snapshots = {} if snapshots is None else snapshots
    if key not in snapshots:
        snapshots[key] = fwd.live_snapshot(store, m.source, symbol, bar, m.lookback_days)
    snap = snapshots[key]
    if not _snapshot_funding_ready(snap, symbol, bar):
        return {"state": "DATA_MISSING", "note": "funding through the bar close not ingested"}
    compiled = compile_strategy(ledger.get_strategy(m.strategy_id), snap, symbol)
    if compiled.signal.index[-1] != _ts(bar):
        return {"state": "DATA_MISSING", "note": "no stored bar at this close"}
    fired = bool(compiled.signal.iloc[-1])
    eligible = bool(compiled.eligible.iloc[-1])
    return {
        "state": "SIGNAL" if fired else "NO_SIGNAL" if eligible else "INELIGIBLE_BAR",
        "fired": fired,
        "close": _num(compiled.inputs["close"].iloc[-1]),
        "features": {k: _num(v) for k, v in compiled.features.iloc[-1].items()},
        "conditions": {k: bool(v) for k, v in compiled.conditions.iloc[-1].items()},
        "compile_digest": compiled.metadata.digest,
        "input_id": fwd._input_id(snap)[0],
        "max_interval_hours": compiled.metadata.max_interval_hours,
    }


# --------------------------------------------------------------------------- the cycle


class _Cycle:
    """One engine pass over every unprocessed bar of one run (pure until ``commit``)."""

    def __init__(self, ledger: Ledger, ctx: RunContext, now, cycle_id: str):
        self.ledger, self.store, self.ctx = ledger, ledger.store, ctx
        self.now, self.cycle_id = _ts(now), cycle_id
        self.prior = events(self.store, ctx.run_id)
        self.keys = {e["event_key"] for e in self.prior}
        self.state = replay(self.prior)
        self.new: list[dict] = []
        self.notes: list[dict] = []
        self._evidence: dict[str, tuple] = {}

    # -- event emission: apply immediately, so the cycle's state IS the replay
    def emit(
        self, event_type: str, key: str, market_time, payload: dict, once: bool = False
    ) -> bool:
        if key in self.keys:
            if once:
                return False
            raise ImpossibleState(f"event {key} would be recorded twice")
        e = _event(self.ctx.run_id, self.state.seq + 1, event_type, key, market_time, payload)
        apply(self.state, e)
        self.keys.add(key)
        self.new.append(e)
        return True

    def run(self) -> dict:
        d = self.ctx.definition
        if self.state.run_id is None:
            raise PaperError("run has no run_created event")
        bar = _ts(self.state.last_bar) + BAR if self.state.last_bar else d.first_bar
        processed = []
        while bar <= self.now:
            if self.state.terminal and self.state.flat:
                self.notes.append({"note": f"run {self.state.status} and flat: nothing to manage"})
                break
            if not self._ready(bar):
                break
            self._process(bar)
            processed.append(bar.isoformat())
            bar += BAR
        return {"bars_processed": processed, "notes": self.notes}

    # -- 1. validate data
    def _ready(self, bar) -> bool:
        late = self.now - bar > self.ctx.window
        need_bar = {p["symbol"] for p in self.state.positions.values()}
        missing = []
        for s in sorted(need_bar):
            src = self._source(s)
            if _bar(self.store, src, s, bar) is None or not _funding_ready(self.store, src, s, bar):
                missing.append(s)
        orders_missing = [o for o in self.state.entry_orders()
                          if _bar(self.store, self._source(o["order"]["symbol"]), o["order"]["symbol"], bar) is None]  # fmt: skip
        if missing:
            self.notes.append({"bar_close": bar.isoformat(), "state": "WAITING_FOR_DATA",
                               "open_position_assets_missing": missing})  # fmt: skip
            if late:
                self.emit("data_issue", f"unmarked:{bar.isoformat()}", bar,
                          {"kind": "open_positions_unmarked", "bar_close": bar.isoformat(),
                           "symbols": missing, "effect": "bar not processed; no entries; exits and "
                           "marks wait for data (never invented)"}, once=True)  # fmt: skip
            return False
        if orders_missing and not late:
            self.notes.append({"bar_close": bar.isoformat(), "state": "WAITING_FOR_DATA",
                               "entry_fill_bar_missing": [o["order"]["symbol"] for o in orders_missing]})  # fmt: skip
            return False
        cohort_missing = [s for s in self._cohort_assets()
                          if _bar(self.store, self._source(s), s, bar) is None
                          or not _funding_ready(self.store, self._source(s), s, bar)]  # fmt: skip
        # Wait only for assets that are lagging (yesterday's bar exists). An asset missing for
        # longer is broken/stale: the bar proceeds without it (its signals are DATA_MISSING),
        # so one dead market cannot block entries on every other asset.
        lagging = [s for s in cohort_missing
                   if _bar(self.store, self._source(s), s, bar - BAR) is not None]  # fmt: skip
        if lagging and not late:
            self.notes.append({"bar_close": bar.isoformat(), "state": "WAITING_FOR_DATA",
                               "signal_assets_lagging": lagging})  # fmt: skip
            return False
        if cohort_missing:
            self.emit("data_issue", f"signal_data_missing:{bar.isoformat()}", bar,
                      {"kind": "signal_data_missing", "bar_close": bar.isoformat(),
                       "symbols": cohort_missing, "stale": sorted(set(cohort_missing) - set(lagging)),
                       "effect": "no signal evaluated for these assets on this bar"}, once=True)  # fmt: skip
        return True

    def _cohort_assets(self) -> list[str]:
        return sorted({a for m in self.ctx.definition.cohort for a in m.assets})

    def _source(self, symbol: str) -> str:
        return next(m.source for m in self.ctx.definition.cohort if symbol in m.assets)

    def _process(self, bar) -> None:
        ctx, st = self.ctx, self.state
        b = bar.isoformat()
        late = self.now - bar > ctx.window
        bars = {s: _bar(self.store, self._source(s), s, bar) for s in self._cohort_assets()}

        # 2. entry orders fill at this bar's open (submitted after the previous close)
        for o in sorted(st.entry_orders(), key=lambda o: o["order"]["order_id"]):
            order = PaperOrder.model_validate(o["order"])
            if order.fill_bar_close != b:
                raise ImpossibleState(f"order {order.order_id} was due at {order.fill_bar_close}")
            px = bars.get(order.symbol)
            if px is None:
                self.emit("order_expired", f"expired:{order.order_id}", bar,
                          {"order_id": order.order_id, "symbol": order.symbol,
                           "reason": "fill bar not stored within the entry window"})  # fmt: skip
                continue
            fill = ctx.adapter.fill(order, px["open"])
            margin = fill["notional"] / o["leverage"]
            if margin + fill["fee"] > st.cash + EPS:
                self.emit("order_rejected", f"rejected:{order.order_id}", bar,
                          {"order_id": order.order_id, "symbol": order.symbol,
                           "reason": "insufficient cash at fill"})  # fmt: skip
                continue
            self.emit("order_filled", f"filled:{order.order_id}", bar,
                      {"order_id": order.order_id, "symbol": order.symbol, "fill": fill})  # fmt: skip
            pos = {
                "position_id": "paperpos_" + _h(ctx.run_id, order.order_id),
                "order_id": order.order_id, "intent_id": o["intent_id"],
                "strategy_id": o["strategy_id"], "strategy": o["strategy"],
                "symbol": order.symbol, "side": order.position_side,
                "signal_bar": o["signal_bar"], "entry_time": (bar - BAR).isoformat(),
                "entry_bar_close": b, "exit_bar": o["exit_bar"], "units": fill["units"],
                "entry_ref": fill["reference_price"], "entry_fill": fill["fill_price"],
                "notional": fill["notional"], "leverage": o["leverage"], "margin": margin,
                "maintenance_rate": o["maintenance_rate"], "entry_fee": fill["fee"],
            }  # fmt: skip
            pos["liquidation_price_at_entry"] = st.liquidation_price({**pos, "margin_bal": margin})
            self.emit("position_opened", f"opened:{pos['position_id']}", bar, pos)

        # 3. liquidation on the bar's adverse extreme (conservative: high/low only ever hurt)
        for s in sorted(st.positions):
            p, px = st.positions[s], bars[s]
            liq = st.liquidation_price(p)
            adverse = px["low"] if p["side"] > 0 else px["high"]
            if _beyond(adverse, liq, p["side"]):
                ref = px["open"] if _beyond(px["open"], liq, p["side"]) else liq
                self._liquidate(p, bar, ref, liq, "bar range reached the liquidation price")

        # 4. funding settled inside this bar, at this bar's close price (research convention)
        for s in sorted(st.positions):
            p, px = st.positions[s], bars[s]
            rows = _settlements(self.store, self._source(s), s, bar - BAR, bar)
            amount = sum(p["side"] * p["units"] * px["close"] * r["rate"] for r in rows)
            expected = ctx.execution.settlements_per_bar
            self.emit("funding_accrued", f"funding:{p['position_id']}:{b}", bar, {
                "position_id": p["position_id"], "symbol": s, "bar_close": b,
                "price": px["close"], "settlements": rows, "present": len(rows),
                "expected": expected, "missing": max(expected - len(rows), 0), "amount": amount,
                "sign": "paid" if amount > 0 else "received" if amount < 0 else "none",
            })  # fmt: skip
            value = st.position_value(p, px["close"])
            if value <= p["maintenance_rate"] * p["units"] * px["close"]:
                self._liquidate(p, bar, px["close"], st.liquidation_price(p),
                                "funding exhausted the isolated margin")  # fmt: skip

        # 5. marks happen in the account snapshot below; 6. scheduled exits at the close
        for s in sorted(st.positions):
            p = st.positions[s]
            if _ts(p["exit_bar"]) == bar:
                self._exit(p, bar, bars[s]["close"], late)
            elif _ts(p["exit_bar"]) < bar:
                raise ImpossibleState(f"position {p['position_id']} missed its exit bar")

        # 7. account state and kill switches
        prices = {s: bars[s]["close"] for s in st.positions}
        equity = st.equity(prices)
        prev = st.marks[-1]["equity"] if st.marks else st.starting_equity
        peak = max(st.peak_equity, equity)
        notional = {s: p["units"] * prices[s] for s, p in st.positions.items()}
        by_strategy: dict[str, float] = {}
        for s, p in st.positions.items():
            by_strategy[p["strategy"]] = by_strategy.get(p["strategy"], 0.0) + notional[s]
        gross = sum(notional.values())
        mark = {
            "bar_close": b, "cash": st.cash, "equity": equity, "peak_equity": peak,
            "drawdown": 1 - equity / peak if peak > 0 else 0.0, "day_pnl": equity - prev,
            "day_return": equity / prev - 1 if prev > 0 else None, "prices": prices,
            "gross_notional": gross, "exposure": gross / equity if equity > 0 else None,
            "per_asset_notional": notional, "per_strategy_notional": by_strategy,
            "open_positions": len(st.positions),
            "unrealised": {s: st.position_value(p, prices[s]) - p["margin"] - p["entry_fee"]
                           for s, p in st.positions.items()},
            "processed_at": self.now.isoformat(), "latency_hours": (self.now - bar).total_seconds() / 3600,
        }  # fmt: skip
        self.emit("account_mark", f"mark:{b}", bar, mark)
        broken = [s for s, p in st.positions.items() if p["margin_bal"] < -EPS]
        if (equity <= 0 or broken) and not st.terminal:
            self._kill(bar, "impossible_state", f"equity {equity:.2f}; negative margin {broken}")
        elif not st.terminal and mark["drawdown"] >= ctx.risk.max_drawdown_kill_fraction:
            self._kill(bar, "drawdown_kill", f"drawdown {mark['drawdown']:.2%} >= "
                       f"{ctx.risk.max_drawdown_kill_fraction:.0%} of peak {peak:.2f}")  # fmt: skip
        if not st.terminal and mark["day_pnl"] <= -ctx.risk.daily_loss_halt_fraction * prev:
            self.emit("kill_switch", f"kill:daily_loss_halt:{b}", bar, {
                "kind": "daily_loss_halt", "bar_close": b, "effect": "no new entries on this bar",
                "detail": f"bar-to-bar equity change {mark['day_pnl']:.2f} <= "
                          f"-{ctx.risk.daily_loss_halt_fraction:.0%} of {prev:.2f}"})  # fmt: skip

        # 8-10. signals -> intents -> promotion + risk -> entry orders (never once terminal)
        if not st.terminal:
            self._entries(bar, bars, late)

    def _close_payload(self, p: dict, bar, reason: str, exit_ref: float, exit_fill: float,
                       exit_fee: float, proceeds: float, **extra) -> dict:  # fmt: skip
        side, u = p["side"], p["units"]
        gross = side * u * (exit_ref - p["entry_ref"])
        price_pnl = side * u * (exit_fill - p["entry_fill"])
        net = proceeds - p["margin"] - p["entry_fee"]
        return {
            "position_id": p["position_id"], "symbol": p["symbol"], "strategy": p["strategy"],
            "strategy_id": p["strategy_id"], "side": side, "reason": reason,
            "exit_time": _iso(bar), "exit_ref": exit_ref, "exit_fill": exit_fill,
            "exit_fee": exit_fee, "proceeds": proceeds, "gross_pnl": gross,
            "slippage_cost": gross - price_pnl if reason != "liquidation" else 0.0,
            "fees": p["entry_fee"] + exit_fee, "funding": p["funding_paid"], "net_pnl": net,
            "return_on_notional": net / p["notional"], "return_on_margin": net / p["margin"],
            "bars_held": round((_ts(bar) - _ts(p["entry_time"])) / BAR),
            "funding_missing_settlements": p["funding_missing"], **extra,
        }  # fmt: skip

    def _liquidate(self, p: dict, bar, ref: float, liq: float, why: str) -> None:
        st = self.state
        equity_at_ref = st.position_value(p, ref)
        self.emit("liquidation", f"liquidation:{p['position_id']}", bar, {
            "position_id": p["position_id"], "symbol": p["symbol"], "reference_price": ref,
            "liquidation_price": liq, "margin_lost": p["margin_bal"], "why": why,
            "loss_beyond_margin": max(-equity_at_ref, 0.0),
            "model": self.ctx.risk.liquidation_model,
            "caveat": "approximate isolated-margin model; not Hyperliquid liquidation parity",
        })  # fmt: skip
        self.emit("position_closed", f"closed:{p['position_id']}", bar,
                  self._close_payload(p, bar, "liquidation", ref, ref, 0.0, 0.0))  # fmt: skip

    def _exit(self, p: dict, bar, close: float, late: bool) -> None:
        ctx = self.ctx
        self.emit("exit_intent", f"exit_intent:{p['position_id']}", bar,
                  {"position_id": p["position_id"], "symbol": p["symbol"], "reason": "time_exit",
                   "rule": ctx.exit.rule, "horizon_bars": ctx.exit.horizon_bars})  # fmt: skip
        order = PaperOrder(order_id="paperord_" + _h(ctx.run_id, p["position_id"], "exit"),
                           purpose="exit", symbol=p["symbol"], side=-p["side"],
                           position_side=p["side"], reduce_only=True, reference="exit_bar_close",
                           fill_bar_close=bar.isoformat(), units=p["units"])  # fmt: skip
        problem = ctx.adapter.validate(order)
        if problem:
            raise ImpossibleState(f"exit order invalid: {problem}")
        self.emit("order_submitted", f"submitted:{order.order_id}", bar,
                  {"order": order.model_dump(mode="json"), "position_id": p["position_id"]})  # fmt: skip
        fill = ctx.adapter.fill(order, close)
        self.emit("order_filled", f"filled:{order.order_id}", bar,
                  {"order_id": order.order_id, "symbol": p["symbol"], "fill": fill})  # fmt: skip
        value = self.state.position_value(p, fill["fill_price"])
        self.emit("position_closed", f"closed:{p['position_id']}", bar,
                  self._close_payload(p, bar, "time_exit", close, fill["fill_price"], fill["fee"],
                                      value - fill["fee"], late_processing=late,
                                      processed_at=self.now.isoformat()))  # fmt: skip

    def _kill(self, bar, kind: str, detail: str) -> None:
        b = bar.isoformat()
        self.emit("kill_switch", f"kill:{kind}:{b}", bar,
                  {"kind": kind, "bar_close": b, "detail": detail,
                   "effect": "no new entries ever; open positions run to their scheduled exits"})  # fmt: skip
        self.emit("status_changed", f"status:{self.state.seq + 1}:KILLED", None,
                  {"status": "KILLED", "previous": self.state.status, "actor": "automatic",
                   "reason": f"{kind}: {detail}", "at": self.now.isoformat()})  # fmt: skip

    def evidence(self, m: CohortMember) -> tuple[dict | None, dict]:
        if m.strategy_id not in self._evidence:
            try:
                view, ctx = evidence_view(self.ledger, m, self.now)
            except Exception as exc:  # unusable evidence makes the strategy ineligible
                view, ctx = None, {"semantics_ok": False, "tracking_active": False,
                                   "semantics_detail": f"{type(exc).__name__}: {exc}"}  # fmt: skip
            if self.ctx.definition.semantics != semantics():
                ctx = {**ctx, "semantics_ok": False,
                       "semantics_detail": f"run semantics {self.ctx.definition.semantics} != {semantics()}"}  # fmt: skip
            result = evaluate_promotion(self.ctx.promotion, view, {**ctx, "enrolled": True})
            self._evidence[m.strategy_id] = (view, result)
        return self._evidence[m.strategy_id]

    def _entries(self, bar, bars: dict, late: bool) -> None:
        ctx, st = self.ctx, self.state
        b = bar.isoformat()
        states, cands, snapshots = [], [], {}
        for m in ctx.definition.cohort:
            for s in m.assets:
                if bars.get(s) is None or not _funding_ready(self.store, m.source, s, bar):
                    states.append(
                        {"strategy": m.strategy_name, "symbol": s, "state": "DATA_MISSING"}
                    )
                    continue
                try:
                    sig = _signal(self.ledger, m, s, bar, snapshots)
                except Exception as exc:
                    sig = {"state": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
                states.append({"strategy": m.strategy_name, "symbol": s, "state": sig["state"],
                               **({"note": sig["note"]} if "note" in sig else {}),
                               **({"error": sig["error"]} if "error" in sig else {})})  # fmt: skip
                if sig["state"] != "SIGNAL":
                    continue
                self.emit("signal_consumed", f"signal:{m.strategy_id}:{s}:{b}", bar,
                          {"strategy_id": m.strategy_id, "strategy": m.strategy_name, "symbol": s,
                           "side": m.side, "signal_bar": b, "signal": sig,
                           "data_cutoff": {"bars_close_time_lte": b, "lookback_days": m.lookback_days}})  # fmt: skip
                view, promo = self.evidence(m)
                intent_id = content_id("paperintent_", {"run_id": ctx.run_id, "strategy_id": m.strategy_id,
                                                        "symbol": s, "signal_bar": b})  # fmt: skip
                self.emit("intent_created", f"intent:{intent_id}", bar, {
                    "intent_id": intent_id, "strategy_id": m.strategy_id, "strategy": m.strategy_name,
                    "symbol": s, "side": m.sign, "signal_bar": b, "kind": "open",
                    "promotion_policy_id": ctx.promotion.policy_id, "promotion": promo,
                    "evidence_profile_id": (view or {}).get("profile_id"), "evidence": view,
                })  # fmt: skip
                gap = sig.get("max_interval_hours")
                cands.append({"intent_id": intent_id, "strategy_id": m.strategy_id, "member": m,
                              "symbol": s, "side": m.sign, "live": not late, "promotion": promo,
                              "data_ok": gap is not None and gap <= ctx.risk.max_bar_interval_hours})  # fmt: skip
        self.emit("signals_evaluated", f"signals:{b}", bar,
                  {"bar_close": b, "states": states, "within_entry_window": not late,
                   "latency_hours": (self.now - bar).total_seconds() / 3600})  # fmt: skip
        if not cands:
            return
        prices = {s: bars[s]["close"] for s in st.positions}
        snapshot = {
            "status": st.status, "halted": b in st.halted_bars,
            "equity": st.marks[-1]["equity"], "cash": st.cash,
            "positions": {s: {"side": p["side"], "strategy_id": p["strategy_id"],
                              "notional": p["units"] * prices[s]} for s, p in st.positions.items()},
            "orders": {o["order"]["symbol"]: {"side": o["order"]["position_side"],
                                              "strategy_id": o["strategy_id"],
                                              "notional": o["order"]["target_notional"],
                                              "reserved_cash": o["reserved_cash"]}
                       for o in st.entry_orders()},
        }  # fmt: skip
        decisions = allocate(ctx.risk, ctx.execution, snapshot,
                             [{k: v for k, v in c.items() if k != "member"} for c in cands],
                             run_id=ctx.run_id, bar_close=b)  # fmt: skip
        for c in sorted(cands, key=lambda c: decisions[c["intent_id"]]["rank"]):
            d = decisions[c["intent_id"]]
            self.emit("risk_decision", f"risk:{c['intent_id']}", bar, {
                "intent_id": c["intent_id"], "strategy": c["member"].strategy_name,
                "symbol": c["symbol"], "risk_policy_id": ctx.risk.policy_id, **d,
                "snapshot": {k: snapshot[k] for k in ("status", "halted", "equity", "cash")},
            })  # fmt: skip
            if d["decision"] != "ACCEPTED":
                continue
            m: CohortMember = c["member"]
            order = PaperOrder(order_id="paperord_" + _h(ctx.run_id, c["intent_id"]), purpose="entry",
                               symbol=c["symbol"], side=m.sign, position_side=m.sign,
                               reduce_only=False, reference="next_bar_open",
                               fill_bar_close=(bar + BAR).isoformat(), target_notional=d["notional"])  # fmt: skip
            problem = ctx.adapter.validate(order)
            fee = d["notional"] * ctx.execution.cost(c["symbol"]).fee_bps / 1e4
            self.emit("order_submitted", f"submitted:{order.order_id}", bar, {
                "order": order.model_dump(mode="json"), "intent_id": c["intent_id"],
                "strategy_id": m.strategy_id, "strategy": m.strategy_name, "signal_bar": b,
                "exit_bar": (bar + m.horizon_bars * BAR).isoformat(),
                "leverage": ctx.risk.leverage, "maintenance_rate": ctx.risk.maintenance_rate(c["symbol"]),
                "reserved_cash": d["notional"] / ctx.risk.leverage + fee,
                "execution_model_id": ctx.execution.policy_id,
            })  # fmt: skip
            if problem:
                self.emit("order_rejected", f"rejected:{order.order_id}", bar,
                          {"order_id": order.order_id, "symbol": c["symbol"], "reason": problem})  # fmt: skip


def _commit(store, ctx: RunContext, cyc: _Cycle, software: SoftwareIdentity, started, status: str,
            summary: dict) -> None:  # fmt: skip
    now = _ts(utcnow())
    with store.transaction():
        software_id = _put_software(store, software, now)
        store.con.execute(
            "INSERT INTO paper_cycles VALUES (?,?,?,?,?,?,?,?)",
            [cyc.cycle_id, ctx.run_id, _ts(cyc.now).to_pydatetime(), max(now, _ts(started)).to_pydatetime(),
             status, software_id, len(cyc.new), canonical_json(summary)],
        )  # fmt: skip
        _insert(store, ctx.run_id, ctx.definition.created_at, cyc.cycle_id, cyc.new, cyc.now)


def _error_streak(store, run_id: str) -> int:
    n = 0
    for (status,) in store.con.execute(
        "SELECT status FROM paper_cycles WHERE run_id=? ORDER BY started_at DESC, finished_at DESC",
        [run_id],
    ).fetchall():
        if status != "error":
            break
        n += 1
    return n


def cycle(
    ledger: Ledger,
    run_id: str,
    *,
    software: SoftwareIdentity,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Process every bar of one run that closed since its last processed bar.

    ``dry_run`` computes the same events and writes nothing.
    """
    store = ledger.store
    _require_tables(store)
    ctx = load_run(store, run_id)
    now = _ts(now or utcnow())
    started = _ts(utcnow())
    cyc = _Cycle(ledger, ctx, now, "papercycle_" + uuid4().hex)
    try:
        out = cyc.run()
    except Exception as exc:
        err = {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(limit=5)}
        summary = {
            "engine_version": ENGINE_VERSION,
            "now": now.isoformat(),
            "status": "error",
            **err,
        }
        if dry_run:
            return {**summary, "dry_run": True}
        cyc.new = []
        _commit(store, ctx, cyc, software, started, "error", summary)
        summary["cycle_id"] = cyc.cycle_id
        streak = _error_streak(store, run_id)
        state = account(store, run_id)
        if streak >= ctx.risk.max_consecutive_error_cycles and state.status == "ACTIVE":
            e = _event(run_id, state.seq + 1, "status_changed", f"status:{state.seq + 1}:PAUSED", None,
                       {"status": "PAUSED", "previous": "ACTIVE", "actor": "automatic",
                        "reason": f"repeated_errors: {streak} consecutive failed cycles",
                        "at": now.isoformat()})  # fmt: skip
            with store.transaction():
                _insert(store, run_id, ctx.definition.created_at, cyc.cycle_id, [e], now)
            summary["auto_paused"] = True
        summary["error_streak"] = streak
        return summary
    st = cyc.state
    summary = {
        "engine_version": ENGINE_VERSION,
        "run_id": run_id,
        "now": now.isoformat(),
        "status": "ok",
        "run_status": st.status,
        **out,
        "events": dict(Counter(e["event_type"] for e in cyc.new)),
        "cash": st.cash,
        "equity": st.marks[-1]["equity"] if st.marks else st.starting_equity,
        "open_positions": sorted(st.positions),
        "pending_orders": sorted(o["order"]["symbol"] for o in st.entry_orders()),
    }
    if dry_run:
        return {**summary, "dry_run": True, "would_record": cyc.new}
    # invariant: what we are about to commit replays to the same account
    check = replay(cyc.prior + cyc.new)
    if abs(check.cash - st.cash) > EPS or check.seq != st.seq:
        raise ImpossibleState("cycle state differs from the replay of its events")
    _commit(store, ctx, cyc, software, started, "ok", summary)
    summary["cycle_id"] = cyc.cycle_id
    return summary


def run_all(
    ledger: Ledger,
    *,
    software: SoftwareIdentity,
    sender_factory: Callable[[], Sender] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
    alerts: bool = True,
) -> dict:
    """Cycle every run that is open or still holds positions/orders, then notify (optional)."""
    store = ledger.store
    _require_tables(store)
    now = _ts(now or utcnow())
    results = []
    for r in runs(store):
        st = account(store, r["run_id"])
        if st.terminal and st.flat:
            continue
        results.append(cycle(ledger, r["run_id"], software=software, now=now, dry_run=dry_run))
    out = {"now": now.isoformat(), "runs": results, "dry_run": dry_run}
    if not dry_run:
        out["notifications"] = notify(store, sender_factory or _no_sender, now) if alerts else []
    return out


# --------------------------------------------------------------------------- notifications


NOTIFY_EVENTS = ("position_opened", "position_closed", "kill_switch", "status_changed")


def _no_sender() -> Sender:
    raise PaperError("no message sender configured")


def _pending_notifications(store, now) -> list[dict]:
    """Paper events/cycle errors to report: recent, never sent, no attempt of unknown outcome."""
    since = (_ts(now) - NOTIFY_WINDOW).to_pydatetime()
    subjects = []
    for e in _rows(store, "SELECT * FROM paper_events WHERE recorded_at >= ? ORDER BY run_id, seq",
                   [since]):  # fmt: skip
        if e["event_type"] not in NOTIFY_EVENTS:
            continue
        e["payload"] = json.loads(e["payload"])
        if e["event_type"] == "status_changed" and (
            e["payload"]["actor"] != "automatic" or e["payload"]["status"] == "KILLED"
        ):
            continue  # manual changes are the operator's own; a kill is the kill_switch message
        subjects.append((e["event_id"], e["run_id"], render.render_event(e)))
    for c in _rows(store, "SELECT * FROM paper_cycles WHERE status='error' AND started_at >= ? "
                   "ORDER BY started_at", [since]):  # fmt: skip
        prev = store.con.execute(
            "SELECT status FROM paper_cycles WHERE run_id=? AND started_at < ? "
            "ORDER BY started_at DESC LIMIT 1", [c["run_id"], c["started_at"]]).fetchone()  # fmt: skip
        if prev is None or prev[0] != "error":  # report the start of an error streak once
            subjects.append(
                (c["cycle_id"], c["run_id"], render.render_error(json.loads(c["summary"])))
            )
    out = []
    for sid, run_id, text in subjects:
        states: dict[int, set] = {}
        for a, s in store.con.execute(
            "SELECT attempt, status FROM paper_notifications WHERE subject_id=?", [sid]
        ).fetchall():
            states.setdefault(a, set()).add(s)
        if any("sent" in v for v in states.values()) or any(
            v == {"attempted"} for v in states.values()
        ):
            continue
        out.append({"subject_id": sid, "run_id": run_id, "text": text,
                    "attempt": max(states, default=0) + 1})  # fmt: skip
    return out


def notify(store, sender_factory: Callable[[], Sender], now) -> list[dict]:
    """Send PAPER notifications after events are committed; delivery never affects trading."""
    pending = _pending_notifications(store, now)
    if not pending:
        return []
    groups = [pending] if len(pending) > render.MAX_SINGLE else [[p] for p in pending]
    try:
        sender, setup_error = sender_factory(), None
    except Exception as exc:
        sender, setup_error = None, f"{type(exc).__name__}: {exc}"
    results = []
    for group in groups:
        text = (
            group[0]["text"]
            if len(group) == 1
            else render.render_digest([p["text"] for p in group])
        )
        digest = render.sha256(text)

        def mark(status: str, error: str | None = None, group=group, digest=digest) -> None:
            for p in group:
                store.con.execute(
                    "INSERT INTO paper_notifications VALUES (?,?,?,?,?,?,?,?,?)",
                    ["papernote_" + uuid4().hex, p["run_id"], p["subject_id"], p["attempt"], status,
                     "telegram", digest, error, max(_ts(now), _ts(utcnow())).to_pydatetime()],
                )  # fmt: skip

        mark("attempted")
        error = setup_error
        if sender is not None:
            try:
                sender(text)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        mark("failed" if error else "sent", error)
        results.append({"subjects": [p["subject_id"] for p in group],
                        "status": "failed" if error else "sent", "error": error})  # fmt: skip
    return results


# --------------------------------------------------------------------------- reading


def status(store, run_id: str | None = None) -> list[dict]:
    out = []
    for r in runs(store):
        if run_id and r["run_id"] != run_id:
            continue
        ctx = load_run(store, r["run_id"])
        st = account(store, r["run_id"])
        last = st.marks[-1] if st.marks else None
        out.append({
            "run_id": r["run_id"], "mode": "paper", "status": st.status,
            "created_at": _iso(r["created_at"]), "label": r["label"], "continues": r["continues"],
            "cohort": [m.strategy_name for m in ctx.definition.cohort],
            "starting_equity": st.starting_equity, "currency": ctx.risk.currency,
            "cash": st.cash, "equity": last["equity"] if last else st.starting_equity,
            "last_processed_bar": st.last_bar,
            "first_processable_bar": ctx.definition.first_bar.isoformat(),
            "open_positions": len(st.positions), "pending_entry_orders": len(st.entry_orders()),
            "closed_trades": len(st.closed), "drawdown": last["drawdown"] if last else 0.0,
            "policies": {"promotion": ctx.promotion.policy_id, "risk": ctx.risk.policy_id,
                         "execution": ctx.execution.policy_id, "exit": ctx.exit.policy_id,
                         "maturity": ctx.maturity.policy_id},
            "engine_version": ctx.definition.engine_version, "events": st.seq,
            "last_cycle": _last_cycle(store, r["run_id"]),
        })  # fmt: skip
    return out


def _last_cycle(store, run_id: str) -> dict | None:
    rows = _rows(store, "SELECT cycle_id, started_at, status, events_written FROM paper_cycles "
                 "WHERE run_id=? ORDER BY started_at DESC LIMIT 1", [run_id])  # fmt: skip
    return {**rows[0], "started_at": _iso(rows[0]["started_at"])} if rows else None


def positions(store, run_id: str) -> dict:
    st = account(store, run_id)
    return {
        "run_id": run_id, "as_of_bar": st.last_bar,
        "open": [{k: p[k] for k in ("position_id", "strategy", "symbol", "side", "signal_bar",
                                    "entry_time", "exit_bar", "units", "entry_fill", "notional",
                                    "leverage", "margin", "margin_bal", "funding_paid",
                                    "last_price")}
                 | {"liquidation_price": st.liquidation_price(p),
                    "unrealised": st.position_value(p, p["last_price"]) - p["margin"] - p["entry_fee"]}
                 for p in st.positions.values()],
        "pending_entry_orders": [{"order_id": o["order"]["order_id"], "symbol": o["order"]["symbol"],
                                  "strategy": o["strategy"], "fill_bar_close": o["order"]["fill_bar_close"],
                                  "target_notional": o["order"]["target_notional"]}
                                 for o in st.entry_orders()],
    }  # fmt: skip


def trades(store, run_id: str) -> list[dict]:
    keys = (
        "position_id",
        "strategy",
        "symbol",
        "side",
        "signal_bar",
        "entry_time",
        "exit_time",
        "reason",
        "entry_ref",
        "entry_fill",
        "exit_ref",
        "exit_fill",
        "notional",
        "gross_pnl",
        "slippage_cost",
        "fees",
        "funding",
        "net_pnl",
        "return_on_notional",
        "bars_held",
    )
    return [{k: t.get(k) for k in keys} for t in account(store, run_id).closed]


def list_events(
    store, run_id: str, *, limit: int = 100, event_type: str | None = None
) -> list[dict]:
    out = [e for e in events(store, run_id) if event_type is None or e["event_type"] == event_type]
    return [{k: v for k, v in e.items() if k != "run_created_at"} for e in out[-limit:]]


# --------------------------------------------------------------------------- evidence


def paper_summary(store, run_id: str) -> dict:
    """``paper_execution`` evidence: what the simulated trading system did, from the ledger.

    Descriptive only: no significance test is computed. Maturity counts closed trades and
    observed days; it never implies profitability.
    """
    ctx = load_run(store, run_id)
    evs = events(store, run_id)
    st = replay(evs)
    closed = st.closed
    marks = st.marks
    equity = marks[-1]["equity"] if marks else st.starting_equity
    net = [t["net_pnl"] for t in closed]
    unrealised = sum(st.position_value(p, p["last_price"]) - p["margin"] - p["entry_fee"]
                     for p in st.positions.values())  # fmt: skip
    max_dd = max((m["drawdown"] for m in marks), default=0.0)
    expo = [m["exposure"] for m in marks if m["exposure"] is not None]

    def contribution(key: str) -> dict:
        out: dict[str, dict] = {}
        for t in closed:
            g = out.setdefault(t[key], {"trades": 0, "net_pnl": 0.0, "wins": 0})
            g["trades"] += 1
            g["net_pnl"] += t["net_pnl"]
            g["wins"] += t["net_pnl"] > 0
        return out

    rejected: Counter = Counter()
    accepted = 0
    for e in evs:
        if e["event_type"] == "risk_decision":
            if e["payload"]["decision"] == "ACCEPTED":
                accepted += 1
            else:
                for reason in e["payload"]["reasons"]:
                    rejected[reason.split(":")[0]] += 1
    funding_missing = sum(
        e["payload"]["missing"] for e in evs if e["event_type"] == "funding_accrued"
    )
    reconciled = abs(st.starting_equity + sum(net) + unrealised - equity) < 1e-6 if marks else True
    summary = {
        "stage": EVIDENCE_STAGE,
        "summary_version": SUMMARY_VERSION,
        "run_id": run_id,
        "mode": "paper",
        "status": st.status,
        "cohort": [m.strategy_name for m in ctx.definition.cohort],
        "policies": {"promotion": ctx.promotion.policy_id, "risk": ctx.risk.policy_id,
                     "execution": ctx.execution.policy_id, "exit": ctx.exit.policy_id},
        "observation": {"created_at": _iso(ctx.definition.created_at),
                        "first_bar": marks[0]["bar_close"] if marks else None,
                        "last_bar": st.last_bar, "observed_days": len(marks)},
        "account": {"starting_equity": st.starting_equity, "equity": equity, "cash": st.cash,
                    "return_on_starting_equity": equity / st.starting_equity - 1,
                    "max_drawdown": max_dd, "open_positions": len(st.positions),
                    "unrealised_pnl": unrealised,
                    "avg_exposure": sum(expo) / len(expo) if expo else None,
                    "max_exposure": max(expo) if expo else None},
        "trades": {"closed": len(closed), "wins": sum(x > 0 for x in net),
                   "win_rate": sum(x > 0 for x in net) / len(net) if net else None,
                   "gross_pnl": sum(t["gross_pnl"] for t in closed),
                   "slippage_cost": sum(t["slippage_cost"] for t in closed),
                   "fees": sum(t["fees"] for t in closed),
                   "funding_paid": sum(t["funding"] for t in closed),
                   "net_pnl": sum(net),
                   "liquidations": sum(t["reason"] == "liquidation" for t in closed),
                   "late_exits": sum(bool(t.get("late_processing")) for t in closed),
                   "avg_bars_held": sum(t["bars_held"] for t in closed) / len(closed) if closed else None,
                   "by_strategy": contribution("strategy"), "by_asset": contribution("symbol")},
        "decisions": {"signals_consumed": st.counts["signal_consumed"],
                      "intents": st.counts["intent_created"], "accepted": accepted,
                      "rejected_by_reason": dict(rejected),
                      "orders_filled": st.counts["order_filled"],
                      "orders_expired": st.counts["order_expired"],
                      "orders_rejected": st.counts["order_rejected"]},
        "data": {"funding_missing_settlements": funding_missing,
                 "data_issues": st.counts["data_issue"],
                 "kill_switch_events": st.counts["kill_switch"]},
        "maturity": {"policy": ctx.maturity.policy_id,
                     "level": ctx.maturity.level(len(closed), len(marks))},
        "reconciles_with_ledger": reconciled,
        "records": {"events": len(evs), "last_seq": st.seq,
                    "events_digest": hashlib.sha256("".join(e["event_id"] for e in evs).encode()).hexdigest()},
        "limitations": [
            "Descriptive only: no significance test on paper samples.",
            "Paper fills are simulated (stored opens/closes +- frozen slippage); no order book.",
            "Paper evidence never changes a Strategy Lab tier and never authorises real trading.",
        ],
    }  # fmt: skip
    return json.loads(canonical_json(summary))


def record_evidence(store, run_id: str, now: datetime | None = None) -> dict:
    """Append the current ``paper_execution`` summary (content-addressed, idempotent)."""
    now = _ts(now or utcnow())
    s = paper_summary(store, run_id)
    sid = content_id("paperevidence_", s)
    if not store.con.execute("SELECT 1 FROM paper_evidence WHERE summary_id=?", [sid]).fetchone():
        store.con.execute("INSERT INTO paper_evidence VALUES (?,?,?,?,?)",
                          [sid, run_id, EVIDENCE_STAGE, now.to_pydatetime(), canonical_json(s)])  # fmt: skip
    return {"summary_id": sid, **s}
