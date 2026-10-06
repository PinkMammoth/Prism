"""Co-pilot engine: watchlist -> live signals -> evidence -> policy -> decisions -> Telegram.

Signals use exactly the Phase 3 compiler on the Phase 8 live window: bar T (a completed
daily perp bar) is evaluated only while ``T <= now < T + 1 day``, on a snapshot of rows
available at T's close (bars with ``close_time <= T``, funding to the minute), so
edge-trigger/cooldown state is rebuilt only from data available at T. Older bars are
never evaluated: there are no "you missed this yesterday" alerts.

Records (append-only, co-pilot tables only):

- a watch freezes the strategy, its historical baseline profile, the co-pilot policy and
  the semantic versions; changing the policy means stopping the watch and registering a
  new one, which only evaluates bars that close after its registration (no replay);
- a decision (ALERT or SUPPRESS) is written once per policy, strategy, symbol and signal
  bar, for every fired signal, with the evidence view, every rule result and the message;
- non-firing evaluations (no signal, waiting for data, outside the window, errors) are
  listed in the run summary rather than as one row per day;
- deliveries are separate attempts: ``attempted`` before sending, then ``sent`` or
  ``failed``. A failed attempt is retried while the bar is live; an attempt with no
  outcome (crash mid-send) is never retried automatically, so retries cannot duplicate.

Lab tables are only read. Nothing here sizes, approves or places an order.
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

import pandas as pd
from pydantic import Field, model_validator

from market_signal.copilot import render
from market_signal.copilot.policy import CopilotPolicy, evaluate, get_policy, policy_by_id
from market_signal.models.domain import utcnow
from market_signal.research.lab import forward as fwd
from market_signal.research.lab.common import (
    LabModel,
    Name,
    Symbol,
    Text,
    canonical_json,
    content_id,
)
from market_signal.research.lab.compiler import COMPILER_VERSION, compile_strategy
from market_signal.research.lab.evidence import EvidenceProfile
from market_signal.research.lab.ledger import Ledger, LedgerError, ordered_now
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.vocabulary import VOCABULARY_VERSION

ENGINE_VERSION = "copilot_engine_v1"
STATUSES = ("active", "paused", "stopped")
CHANNEL = "telegram"

Sender = Callable[[str], None]


class CopilotError(LedgerError):
    pass


def semantics() -> dict[str, str]:
    """Versions whose change makes a live signal incomparable with its evidence."""
    return {"compiler_version": COMPILER_VERSION, "vocabulary_version": VOCABULARY_VERSION}


class WatchDefinition(LabModel):
    """Frozen co-pilot registration. Any change (policy, baseline, label) is a new watch."""

    schema_version: Literal["1"] = "1"
    consumer: Literal["perps_copilot"] = "perps_copilot"
    label: Name | None = None
    strategy_id: str
    strategy_name: Text
    baseline_profile_id: str
    baseline_tier: Literal["EXPLORATORY", "RESEARCH_SUPPORTED"]
    evidence_policy_id: str
    policy_id: str
    market: Literal["perp"]
    side: Literal["long", "short"]
    source: Text
    timeframe: Literal["1d"] = "1d"
    assets: Annotated[tuple[Symbol, ...], Field(min_length=1)]
    primary_horizon: str
    lookback_days: Annotated[int, Field(strict=True, ge=1, le=5000)]
    semantics: dict[str, str]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("duplicate assets")
        return self

    @property
    def watch_id(self) -> str:
        return content_id("copwatch_", self.model_dump(mode="python"))


# --------------------------------------------------------------------------- helpers


def _ts(value) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _rows(store, sql: str, args: list) -> list[dict]:
    cursor = store.con.execute(sql, args)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def _require_tables(store) -> None:
    if not store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='copilot_decisions'"
    ).fetchone():
        raise CopilotError("co-pilot tables are absent; open Store writable once to migrate")


def _put(store, table: str, id_column: str, id_: str, payload: dict, now) -> None:
    # Table/column identifiers are internal constants, never submitted values.
    row = store.con.execute(f"SELECT payload FROM {table} WHERE {id_column}=?", [id_]).fetchone()
    if row:
        if canonical_json(json.loads(row[0])) != canonical_json(payload):
            raise CopilotError(f"{id_} already exists with different content")
        return
    store.con.execute(
        f"INSERT INTO {table} VALUES (?,?,?)",
        [id_, canonical_json(payload), _ts(now).to_pydatetime()],
    )


def _put_software(store, software: SoftwareIdentity, now) -> str:
    data = software.model_dump(mode="python")
    data["packages"] = sorted(data["packages"])
    _put(store, "copilot_software", "software_id", software.software_id, data, now)
    return software.software_id


def _profile(store, profile_id: str) -> EvidenceProfile:
    row = store.con.execute(
        "SELECT payload FROM lab_evidence_profiles WHERE profile_id=?", [profile_id]
    ).fetchone()
    if row is None:
        raise CopilotError(f"unknown evidence profile {profile_id}")
    p = EvidenceProfile.model_validate_json(row[0])
    if p.profile_id != profile_id:
        raise CopilotError("stored profile does not match its ID")
    return p


# --------------------------------------------------------------------------- watchlist


def build_watch(
    ledger: Ledger, profile_id: str, *, policy: CopilotPolicy, label: str | None = None
) -> WatchDefinition:
    """Pure read: the frozen watch a registration would record.

    The baseline must be a historical (schema 1/2) EXPLORATORY/RESEARCH_SUPPORTED profile
    of a daily perp strategy; the universe, venue, lookback and primary horizon come from
    the plan that screened it (the same derivation Phase 8 uses, read-only).
    """
    d = fwd.build_definition(ledger, profile_id)  # validates tier, plan and live warmup
    profile = _profile(ledger.store, profile_id)
    return WatchDefinition(
        label=label,
        strategy_id=d.strategy_id,
        strategy_name=profile.subject.get("name") or d.strategy_id,
        baseline_profile_id=profile_id,
        baseline_tier=d.enrollment_tier,
        evidence_policy_id=d.evidence_policy_id,
        policy_id=policy.policy_id,
        market="perp",
        side=d.side,
        source=d.source,
        assets=d.assets,
        primary_horizon=d.primary_horizon,
        lookback_days=d.lookback_days,
        semantics=semantics(),
    )


def _status_events(store, watch_id: str) -> list[dict]:
    return _rows(
        store,
        "SELECT status, reason, recorded_at FROM copilot_watch_status WHERE watch_id=? "
        "ORDER BY recorded_at, event_id",
        [watch_id],
    )


def _status_at(events: list[dict], when) -> str | None:
    status = None
    for e in events:
        if _ts(e["recorded_at"]) <= _ts(when):
            status = e["status"]
    return status


def watches(store) -> list[dict]:
    _require_tables(store)
    out = []
    for row in _rows(store, "SELECT * FROM copilot_watchlist ORDER BY registered_at, watch_id", []):
        d = WatchDefinition.model_validate_json(row["definition"])
        if d.watch_id != row["watch_id"]:
            raise CopilotError("stored watch definition does not match its ID")
        events = _status_events(store, row["watch_id"])
        out.append({**row, "definition": d, "events": events,
                    "status": events[-1]["status"] if events else None})  # fmt: skip
    return out


def _watch(store, watch_id: str) -> dict:
    hit = [w for w in watches(store) if w["watch_id"] == watch_id]
    if not hit:
        raise CopilotError(f"unknown watch {watch_id}")
    return hit[0]


def register_watch(
    ledger: Ledger,
    profile_id: str,
    *,
    reason: str,
    origin: str,
    software: SoftwareIdentity,
    policy: CopilotPolicy | None = None,
    label: str | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Add a strategy to the co-pilot watchlist. Only bars closing after ``now`` alert."""
    store = ledger.store
    _require_tables(store)
    if not reason or not reason.strip():
        raise CopilotError("a watch registration needs a recorded reason")
    policy = policy or get_policy()
    d = build_watch(ledger, profile_id, policy=policy, label=label)
    now = _ts(now or utcnow())
    existing = watches(store)
    if any(w["watch_id"] == d.watch_id for w in existing):
        raise CopilotError(f"{d.watch_id} is already registered; a different policy or --label "
                           "is a new watch")  # fmt: skip
    open_ = [w["watch_id"] for w in existing
             if w["strategy_id"] == d.strategy_id and w["status"] != "stopped"]  # fmt: skip
    if open_:
        raise CopilotError(f"strategy already has an open watch {open_}; stop it first (a policy "
                           "change is a new watch that never replays earlier bars)")  # fmt: skip
    out = {
        "watch_id": d.watch_id,
        "registered_at": now.isoformat(),
        "first_alertable_bar_close": (now.floor("D") + pd.Timedelta(days=1)).isoformat(),
        "definition": d.model_dump(mode="json"),
        "policy": policy.model_dump(mode="json"),
        "dry_run": dry_run,
        "note": "human alerts only: a watch grants no trading, sizing or execution status",
    }
    if dry_run:
        return out
    with store.transaction():
        software_id = _put_software(store, software, now)
        _put(store, "copilot_policies", "policy_id", policy.policy_id,
             policy.model_dump(mode="python"), now)  # fmt: skip
        store.con.execute(
            "INSERT INTO copilot_watchlist VALUES (?,?,?,?,?,?,?,?,?)",
            [d.watch_id, d.strategy_id, profile_id, policy.policy_id, now.to_pydatetime(),
             reason.strip(), origin, software_id, canonical_json(d.model_dump(mode="python"))],
        )  # fmt: skip
        store.con.execute(
            "INSERT INTO copilot_watch_status VALUES (?,?,?,?,?)",
            ["copstatus_" + uuid4().hex, d.watch_id, "active", "registered", now.to_pydatetime()],
        )
    return out


def set_watch_status(
    store, watch_id: str, status: str, *, reason: str, now: datetime | None = None
) -> dict:
    """Append a status event: active <-> paused; either -> stopped (terminal)."""
    if status not in STATUSES:
        raise CopilotError(f"status must be one of {STATUSES}")
    if not reason or not reason.strip():
        raise CopilotError("a status change needs a reason")
    w = _watch(store, watch_id)
    if now is None and w["events"]:
        # Host clock: absorb the small backward steps WSL makes (ledger.ordered_now, <= 5 s). A
        # clamped time would tie the previous event, and ties sort by a random event_id, so the
        # event goes 1 us after it. A larger regression is still refused below.
        last = _ts(w["events"][-1]["recorded_at"])
        now = _ts(ordered_now(last.to_pydatetime()))
        if now == last:  # clamped (or an exact tie); an earlier time stays earlier
            now = last + pd.Timedelta(microseconds=1)
    now = _ts(now or utcnow())
    if w["status"] == "stopped":
        raise CopilotError("a stopped watch is final; register a new one")
    if w["status"] == status:
        raise CopilotError(f"watch is already {status}")
    if w["events"] and now < _ts(w["events"][-1]["recorded_at"]):
        raise CopilotError("status events must be recorded in time order")
    store.con.execute(
        "INSERT INTO copilot_watch_status VALUES (?,?,?,?,?)",
        ["copstatus_" + uuid4().hex, watch_id, status, reason.strip(), now.to_pydatetime()],
    )
    return {"watch_id": watch_id, "status": status, "recorded_at": now.isoformat()}


def list_watches(store) -> list[dict]:
    out = []
    for w in watches(store):
        d: WatchDefinition = w["definition"]
        counts = store.con.execute(
            "SELECT count(*) FILTER (WHERE decision='ALERT'), count(*) FILTER "
            "(WHERE decision='SUPPRESS'), max(bar_close) FROM copilot_decisions WHERE watch_id=?",
            [w["watch_id"]],
        ).fetchone()
        out.append({
            "watch_id": w["watch_id"], "strategy": d.strategy_name, "strategy_id": d.strategy_id,
            "side": d.side, "assets": list(d.assets), "baseline_profile_id": d.baseline_profile_id,
            "baseline_tier": d.baseline_tier, "policy_id": d.policy_id, "status": w["status"],
            "registered_at": w["registered_at"], "alerts": counts[0], "suppressed": counts[1],
            "last_decision_bar": counts[2],
        })  # fmt: skip
    return out


# --------------------------------------------------------------------------- evidence


def _forward_view(ledger: Ledger, d: WatchDefinition, now) -> tuple[dict | None, bool]:
    """Read-only forward summary of the strategy's tracking, and whether it is retired.

    Prefers an open tracking enrolled from the watch's baseline. ``retired`` is True when
    trackings exist and every one is stopped. Nothing is written.
    """
    try:
        mine = [t for t in fwd.trackings(ledger) if t["strategy_id"] == d.strategy_id]
    except LedgerError:
        return None, False
    if not mine:
        return None, False
    status = {t["tracking_id"]: fwd._status_at(t["events"], now) for t in mine}
    retired = all(s == "stopped" for s in status.values())
    ranked = sorted(mine, key=lambda t: (status[t["tracking_id"]] == "stopped",
                                         t["profile_id"] != d.baseline_profile_id,
                                         -_ts(t["enrolled_at"]).value))  # fmt: skip
    t = ranked[0]
    try:
        s = fwd.forward_summary(ledger, t["tracking_id"], as_of=now)
    except Exception as exc:  # display only; never blocks a decision
        return {"tracking_id": t["tracking_id"], "error": str(exc), "maturity": None}, retired
    prim = next(h for h in s["horizons"] if h["primary"])
    return {
        "tracking_id": t["tracking_id"],
        "status": status[t["tracking_id"]],
        "maturity": s["maturity"]["level"],
        "maturity_policy": s["maturity"]["policy"],
        "horizon": prim["horizon"],
        "signals_recorded": prim["signals_recorded"],
        "signals_resolved": prim["signals_resolved"],
        "independent_resolved": prim["independent_resolved"],
        "excess_mean": prim["excess_mean"],
        "direction_vs_historical": prim["direction_vs_historical"],
        "observed_days": s["observation"]["observed_days"],
        "evaluations_digest": s["records"]["evaluations_digest"],
        "outcomes_digest": s["records"]["outcomes_digest"],
    }, retired


def latest_extension(store, d: WatchDefinition) -> str | None:
    """Newest schema-4 (full research / validation) profile extending the baseline."""
    row = store.con.execute(
        "SELECT profile_id FROM lab_evidence_profiles WHERE strategy_id=? AND "
        "json_extract_string(payload, '$.profile_schema')='4' AND "
        "json_extract_string(payload, '$.extends')=? ORDER BY recorded_at DESC, profile_id DESC "
        "LIMIT 1",
        [d.strategy_id, d.baseline_profile_id],
    ).fetchone()
    return row[0] if row else None


def evidence_view(ledger: Ledger, d: WatchDefinition, now) -> tuple[dict, bool]:
    """The governed evidence chain as the co-pilot reads it (historical, FDR, full research,
    validation, forward) — kept as separate blocks, never pooled into one score."""
    store = ledger.store
    base = _profile(store, d.baseline_profile_id)
    ext_id = latest_extension(store, d)
    p = _profile(store, ext_id) if ext_id else base
    if p.subject["strategy_id"] != d.strategy_id:
        raise CopilotError("evidence profile belongs to another strategy")
    st, nb, a = p.statistics, p.neighbourhood, p.assets
    forward, retired = _forward_view(ledger, d, now)
    versions = {}
    for s in p.sources:
        for k in ("compiler_version", "vocabulary_version"):
            if k in s.versions:
                versions.setdefault(k, set()).add(s.versions[k])
    view = {
        "profile_id": p.profile_id,
        "profile_schema": p.profile_schema,
        "baseline_profile_id": base.profile_id,
        "evidence_policy_id": p.policy_id,
        "subject": {k: p.subject.get(k) for k in ("name", "family", "ledger_family", "params", "side")},
        "stages": sorted({s.stage for s in p.sources}),
        "tier": p.tier,
        "historical_tier": base.tier,
        "primary_horizon": p.evaluation.get("primary_horizon"),
        "effect": {k: p.effect.get(k) for k in ("excess_mean", "net_mean", "gross_mean", "hit_rate")},
        "sample": {k: p.sample.get(k) for k in ("independent_events", "assets_with_events", "raw_signals")},
        "assets": {k: a.get(k) for k in ("assets_with_events", "positive", "negative", "positive_share",
                                         "max_asset_event_share", "dominated_by_one_asset",
                                         "concentrated", "events_by_asset")},
        "neighbourhood": {k: nb.get(k) for k in ("label", "isolated_spike", "testable_neighbours",
                                                 "same_direction", "support_share")},
        "statistics": {k: st.get(k) for k in ("raw_p", "q", "q_target", "fdr_survivor", "batch_status",
                                              "correction_family_size", "correction_method")},
        "full_research": None if p.full_research is None else {
            "status": p.full_research["status"], "result_id": p.full_research.get("result_id"),
            "reasons": p.full_research.get("reasons"),
            "walk_forward": p.full_research.get("walk_forward"),
            "sensitivity": {k: (p.full_research.get("sensitivity") or {}).get(k)
                            for k in ("label", "testable_neighbours", "knife_edge")},
        },
        "validation": None if p.validation is None else {
            "status": p.validation["status"], "result_id": p.validation.get("result_id"),
            "sample": p.validation.get("sample"), "reasons": p.validation.get("reasons"),
            "window": [str(x) for x in p.validation.get("window") or []],
        },
        "forward": forward if forward and forward.get("maturity") else None,
        "source_versions": {k: sorted(v) for k, v in versions.items()},
        "limiting": list(p.limiting),
    }  # fmt: skip
    return json.loads(canonical_json(view)), retired


def _semantics_check(d: WatchDefinition, ev: dict | None) -> tuple[bool, str]:
    cur = semantics()
    if d.semantics != cur:
        return False, f"semantics changed since the watch was registered ({d.semantics} -> {cur})"
    if ev is not None:
        for k, v in cur.items():
            have = ev["source_versions"].get(k)
            if have and have != [v]:
                return False, f"evidence was produced under {k} {have}, live signals use {v}"
    return True, "strategy, compiler and evidence versions match"


# --------------------------------------------------------------------------- candidates


def _previous_alert(store, strategy_id: str, symbol: str, bar) -> str | None:
    row = store.con.execute(
        "SELECT decision_id FROM copilot_decisions WHERE strategy_id=? AND symbol=? AND "
        "bar_close=? AND decision='ALERT' ORDER BY evaluated_at LIMIT 1",
        [strategy_id, symbol, _ts(bar).to_pydatetime()],
    ).fetchone()
    return row[0] if row else None


def _forward_evaluation(store, tracking_id: str | None, symbol: str, bar) -> str | None:
    if not tracking_id:
        return None
    row = store.con.execute(
        "SELECT status FROM lab_forward_evaluations WHERE tracking_id=? AND symbol=? AND bar_close=?",
        [tracking_id, symbol, _ts(bar).to_pydatetime()],
    ).fetchone()
    return row[0] if row else None


def candidates(ledger: Ledger, *, now: datetime | None = None) -> list[dict]:
    """Read-only: every watched strategy/asset's current state and what the policy says.

    ``state``: SIGNAL, NO_SIGNAL, INELIGIBLE_BAR (features undefined), WAITING_FOR_DATA,
    OUTSIDE_WINDOW (newest bar no longer live: stale data), BEFORE_REGISTRATION, NO_BARS,
    WATCH_PAUSED/WATCH_STOPPED, ERROR. For SIGNAL the policy decision is final input to
    ``run``; for other states ``if_fired`` shows what the policy would decide (inspection
    only, never recorded or sent).
    """
    store = ledger.store
    _require_tables(store)
    now = _ts(now or utcnow())
    out, snapshots = [], {}
    for w in watches(store):
        d: WatchDefinition = w["definition"]
        policy = policy_by_id(d.policy_id)
        base = {"watch_id": w["watch_id"], "strategy": d.strategy_name, "strategy_id": d.strategy_id,
                "side": d.side, "policy_id": d.policy_id, "policy_version": policy.version}  # fmt: skip
        status = _status_at(w["events"], now)
        if status != "active":
            out.append({**base, "symbol": None, "state": f"WATCH_{(status or 'unknown').upper()}"})
            continue
        try:
            ev, retired = evidence_view(ledger, d, now)
        except Exception as exc:  # unusable evidence suppresses; it is never guessed
            ev, retired = None, False
            ev_error = f"{type(exc).__name__}: {exc}"
        else:
            ev_error = None
        sem_ok, sem_detail = _semantics_check(d, ev)
        definition = ledger.get_strategy(d.strategy_id)
        for symbol in d.assets:
            c = {**base, "symbol": symbol}
            try:
                bar = fwd.newest_bar(store, d.source, symbol, now)
                if bar is None:
                    out.append({**c, "state": "NO_BARS"})
                    continue
                c["bar_close"] = bar.isoformat()
                # Not alertable: the bar closed before registration, or is no longer live.
                # Still evaluated below so inspection shows the signal state.
                closed = (
                    "BEFORE_REGISTRATION" if bar <= _ts(w["registered_at"])
                    else None if fwd.in_live_window(bar, now) else "OUTSIDE_WINDOW"
                )  # fmt: skip
                key = (d.source, symbol, bar, d.lookback_days)
                if key not in snapshots:
                    snapshots[key] = fwd.live_snapshot(
                        store, d.source, symbol, bar, d.lookback_days
                    )
                snap = snapshots[key]
                if not fwd.funding_ready(snap, symbol, bar):
                    out.append({**c, "state": closed or "WAITING_FOR_DATA",
                                "note": "funding through the bar close is not ingested yet"})  # fmt: skip
                    continue
                compiled = compile_strategy(definition, snap, symbol)
                if compiled.signal.index[-1] != bar:
                    raise CopilotError("compiled frame does not end at the signal bar")
                fired = bool(compiled.signal.iloc[-1])
                eligible = bool(compiled.eligible.iloc[-1])
                signal = {
                    "fired": fired, "eligible": eligible, "active": bool(compiled.active.iloc[-1]),
                    "close": fwd._num(compiled.inputs["close"].iloc[-1]),
                    "features": {k: fwd._num(v) for k, v in compiled.features.iloc[-1].items()},
                    "conditions": {k: bool(v) for k, v in compiled.conditions.iloc[-1].items()},
                    "definition_conditions": [x.model_dump(mode="json") for x in definition.conditions],
                    "cooldown_bars": definition.cooldown_bars,
                    "input_id": fwd._input_id(snap)[0],
                    "compile_digest": compiled.metadata.digest,
                    "max_interval_hours": compiled.metadata.max_interval_hours,
                    "data_cutoff": {"bars_close_time_lte": bar.isoformat(),
                                    "funding_available_at_minute_lte": bar.isoformat(),
                                    "lookback_days": d.lookback_days},
                    "latency_seconds": (now - bar).total_seconds(),
                }  # fmt: skip
                tracking = (ev or {}).get("forward") or {}
                signal["forward_tracker_recorded"] = _forward_evaluation(
                    store, tracking.get("tracking_id"), symbol, bar
                )
                context = {
                    "semantics_ok": sem_ok and ev_error is None,
                    "semantics_detail": sem_detail if ev_error is None else ev_error,
                    "max_interval_hours": signal["max_interval_hours"],
                    "duplicate_of": _previous_alert(store, d.strategy_id, symbol, bar),
                    "retired": retired,
                }
                result = evaluate(policy, ev, context)
                state = closed or (
                    "SIGNAL" if fired else "NO_SIGNAL" if eligible else "INELIGIBLE_BAR"
                )
                c.update(state=state, signal=signal, evidence=ev, context=context)
                c["result" if state == "SIGNAL" else "if_fired"] = result
                out.append(c)
            except Exception as exc:
                out.append({**c, "state": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc(limit=3)})  # fmt: skip
    return out


def _record(c: dict, now) -> dict:
    """The audit record of one fired signal (what ``render`` and ``show`` read)."""
    return {
        "engine_version": ENGINE_VERSION,
        "render_version": render.RENDER_VERSION,
        "watch_id": c["watch_id"], "policy_id": c["policy_id"], "policy_version": c["policy_version"],
        "strategy_id": c["strategy_id"], "strategy": c["strategy"], "symbol": c["symbol"],
        "side": c["side"], "bar_close": c["bar_close"], "evaluated_at": _ts(now).isoformat(),
        "semantics": semantics(), "signal": c["signal"], "evidence": c["evidence"],
        "context": c["context"], "result": c["result"],
    }  # fmt: skip


# --------------------------------------------------------------------------- run


def _pending_deliveries(store, now) -> list[dict]:
    """ALERTs still live with no successful delivery and no attempt of unknown outcome."""
    rows = _rows(
        store,
        "SELECT decision_id, bar_close, payload FROM copilot_decisions WHERE decision='ALERT' "
        "AND bar_close <= ? AND bar_close > ? ORDER BY bar_close, symbol, decision_id",
        [_ts(now).to_pydatetime(), (_ts(now) - fwd.GRACE).to_pydatetime()],
    )
    out = []
    for r in rows:
        states = _rows(store, "SELECT attempt, status FROM copilot_deliveries WHERE decision_id=?",
                       [r["decision_id"]])  # fmt: skip
        by_attempt: dict[int, set] = {}
        for s in states:
            by_attempt.setdefault(s["attempt"], set()).add(s["status"])
        if any("sent" in v for v in by_attempt.values()):
            continue
        if any(v == {"attempted"} for v in by_attempt.values()):
            continue  # outcome unknown (crash mid-send): never resend automatically
        out.append({**r, "attempt": max(by_attempt, default=0) + 1,
                    "record": json.loads(r["payload"])})  # fmt: skip
    return out


def _deliver(store, pending: list[dict], sender_factory: Callable[[], Sender], now) -> list[dict]:
    if not pending:
        return []
    now = _ts(now)
    if len(pending) > render.MAX_SINGLE:
        messages = [("digest", pending, render.render_digest([p["record"] for p in pending]))]
    else:
        messages = [("single", [p], p["record"]["message"]["text"]) for p in pending]
    try:
        sender, setup_error = sender_factory(), None
    except Exception as exc:  # e.g. Telegram not configured: recorded as a failed delivery
        sender, setup_error = None, f"{type(exc).__name__}: {exc}"
    results = []
    for kind, group, text in messages:
        digest = render.sha256(text)

        def mark(status: str, error: str | None = None, group=group, kind=kind, digest=digest):
            for p in group:
                store.con.execute(
                    "INSERT INTO copilot_deliveries VALUES (?,?,?,?,?,?,?,?,?)",
                    ["copdelivery_" + uuid4().hex, p["decision_id"], p["attempt"], status, CHANNEL,
                     kind, digest, error, max(now, _ts(utcnow())).to_pydatetime()],
                )  # fmt: skip

        mark("attempted")
        error = setup_error
        if sender is not None:
            try:
                sender(text)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        mark("failed" if error else "sent", error)
        results.append({"kind": kind, "decisions": [p["decision_id"] for p in group],
                        "status": "failed" if error else "sent", "error": error})  # fmt: skip
    return results


def _no_sender() -> Sender:
    raise CopilotError("no message sender configured")


def run(
    ledger: Ledger,
    *,
    software: SoftwareIdentity,
    sender_factory: Callable[[], Sender] | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """Evaluate live signals, record new decisions, deliver new/failed live ALERTs.

    Idempotent: a strategy/asset/bar already decided under its policy is left alone, and a
    sent alert is never sent again. ``dry_run`` writes nothing and sends nothing; it
    returns the decisions and the rendered messages.
    """
    store = ledger.store
    _require_tables(store)
    now = _ts(now or utcnow())
    cands = candidates(ledger, now=now)
    new, notes = [], []
    for c in cands:
        note = {k: c.get(k) for k in ("watch_id", "strategy", "symbol", "bar_close", "state")}
        if c.get("error"):
            note["error"] = c["error"]
        if c["state"] == "SIGNAL":
            done = store.con.execute(
                "SELECT decision_id, decision FROM copilot_decisions WHERE policy_id=? AND "
                "strategy_id=? AND symbol=? AND bar_close=?",
                [
                    c["policy_id"],
                    c["strategy_id"],
                    c["symbol"],
                    _ts(c["bar_close"]).to_pydatetime(),
                ],
            ).fetchone()
            if done:
                note["note"] = f"already decided ({done[1]}, {done[0]}); unchanged"
            else:
                rec = _record(c, now)
                if c.get("evidence") is not None:  # no evidence: SUPPRESS, nothing to render
                    text = render.render_alert(rec)
                    rec["message"] = {"render_version": render.RENDER_VERSION,
                                      "sha256": render.sha256(text), "text": text}  # fmt: skip
                new.append(rec)
                note["decision"] = rec["result"]["decision"]
                note["priority"] = rec["result"]["priority"]
                note["blocked_by"] = rec["result"]["blocked_by"]
        notes.append(note)
    summary = {
        "engine_version": ENGINE_VERSION,
        "now": now.isoformat(),
        "decisions": len(new),
        "alerts": sum(r["result"]["decision"] == "ALERT" for r in new),
        "suppressed": sum(r["result"]["decision"] == "SUPPRESS" for r in new),
        "errors": sum(c["state"] == "ERROR" for c in cands),
        "notes": notes,
        "dry_run": dry_run,
    }
    if dry_run:
        alerts = [r for r in new if r["result"]["decision"] == "ALERT"]
        summary["would_send"] = (
            [render.render_digest(alerts)] if len(alerts) > render.MAX_SINGLE
            else [r["message"]["text"] for r in alerts]
        )  # fmt: skip
        summary["new_decisions"] = new
        return summary
    run_id = "coprun_" + uuid4().hex
    with store.transaction():
        software_id = _put_software(store, software, now)
        store.con.execute(
            "INSERT INTO copilot_runs VALUES (?,?,?,?,?)",
            [run_id, now.to_pydatetime(), max(now, _ts(utcnow())).to_pydatetime(), software_id,
             canonical_json(summary)],
        )  # fmt: skip
        for r in new:
            res = r["result"]
            store.con.execute(
                "INSERT INTO copilot_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ["copdecision_" + uuid4().hex, r["watch_id"], r["policy_id"], r["strategy_id"],
                 r["symbol"], _ts(r["bar_close"]).to_pydatetime(), now.to_pydatetime(), run_id,
                 res["decision"], res["priority"], (r["evidence"] or {}).get("profile_id"),
                 software_id, canonical_json({**r, "run_id": run_id, "software_id": software_id})],
            )  # fmt: skip
    summary["run_id"] = run_id
    # Decisions are committed before any send: a delivery failure cannot lose or alter them.
    pending = _pending_deliveries(store, now)
    summary["deliveries"] = _deliver(store, pending, sender_factory or _no_sender, now)
    return summary


# --------------------------------------------------------------------------- inspection


def _delivery_state(store, decision_id: str) -> dict:
    rows = _rows(store, "SELECT attempt, status, message_kind, error, recorded_at FROM "
                 "copilot_deliveries WHERE decision_id=? ORDER BY attempt, recorded_at",
                 [decision_id])  # fmt: skip
    statuses = {r["status"] for r in rows}
    by_attempt: dict[int, set] = {}
    for r in rows:
        by_attempt.setdefault(r["attempt"], set()).add(r["status"])
    state = ("sent" if "sent" in statuses else
             "unknown" if any(v == {"attempted"} for v in by_attempt.values()) else
             "failed" if "failed" in statuses else "not_attempted")  # fmt: skip
    return {"state": state, "attempts": rows}


def list_decisions(store, limit: int = 50) -> list[dict]:
    _require_tables(store)
    rows = _rows(
        store,
        "SELECT decision_id, strategy_id, symbol, bar_close, evaluated_at, decision, priority, "
        "policy_id, profile_id, payload FROM copilot_decisions ORDER BY evaluated_at DESC, "
        "decision_id LIMIT ?",
        [limit],
    )
    out = []
    for r in rows:
        p = json.loads(r.pop("payload"))
        dec = r["decision"]
        out.append({**r, "strategy": p["strategy"], "blocked_by": p["result"]["blocked_by"],
                    "delivery": _delivery_state(store, r["decision_id"])["state"]
                    if dec == "ALERT" else None})  # fmt: skip
    return out


def show_decision(store, decision_id: str) -> dict:
    _require_tables(store)
    rows = _rows(store, "SELECT * FROM copilot_decisions WHERE decision_id=?", [decision_id])
    if not rows:
        raise CopilotError(f"unknown decision {decision_id}")
    r = rows[0]
    p = json.loads(r.pop("payload"))
    return {**r, "record": p, "delivery": _delivery_state(store, decision_id)}
