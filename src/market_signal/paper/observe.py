"""Read-only observability of a paper run (Strategy Lab Phase 13).

Everything here is derived from the run's append-only ``paper_events`` (via
``account.replay``) and its recorded ``paper_cycles``; there is no second source of truth.
Nothing here can create, change or remove a paper event, policy or Lab record. The only
writes are observability artifacts: immutable ``paper_snapshots``, ``paper_briefs`` and the
brief's delivery attempts in ``paper_notifications``.

Phase 13 is read-only with respect to paper trading policy and execution. It observes the
frozen Phase 12 experiment and does not adapt it.

Operational misses are distinguished from deliberate policy or risk rejections:

- ``EXPECTED_SKIP``: the system deliberately chose not to enter (promotion, risk limits,
  conflicts, a paused/killed account, the daily-loss halt).
- ``MISSED_EXECUTION``: the system would have traded but operational timing or data
  prevented it. An intent rejected only for operational reasons (entry window passed,
  stale data) is re-decided with the run's frozen risk policy on the SAME recorded
  pre-trade snapshot, as if timing and data had been fine. Only if that counterfactual
  accepts it is it a missed execution; otherwise it is an expected skip. No hypothetical
  fill is ever constructed.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

import pandas as pd

from market_signal.models.domain import utcnow
from market_signal.paper import engine, render
from market_signal.paper.account import AccountState, apply
from market_signal.paper.risk import allocate
from market_signal.research.lab.common import canonical_json, content_id

OBSERVE_VERSION = "paper_observe_v1"
SNAPSHOT_VERSION = "paper_snapshot_v1"
BRIEF_VERSION = "paper_brief_v1"
BAR = timedelta(days=1)
# Display-only thresholds (never used by the engine).
LIQUIDATION_WARN_DISTANCE = 0.20  # a position within 20% of its modelled liquidation
COMPONENT_STALE = timedelta(hours=36)  # a daily component not run for this long is stale
RECENT_BARS = 7  # window for "recent" degradations

DELIBERATE = {
    "max_open_positions": "risk_rejection",
    "max_strategy_allocation": "risk_rejection",
    "max_asset_exposure": "risk_rejection",
    "insufficient_headroom": "risk_rejection",
    "daily_loss_limit": "risk_rejection",
    "duplicate_position_no_add": "conflicting_position",
    "conflicting_position": "conflicting_position",
    "conflicting_signals_same_bar": "conflicting_position",
    "duplicate_signal_same_bar": "conflicting_position",
    "account_paused": "account_paused_or_killed",
    "account_killed": "account_paused_or_killed",
    "account_stopped": "account_paused_or_killed",
    "promotion_ineligible": "promotion_rejection",
}
OPERATIONAL = {"missed_execution_window", "stale_or_gapped_data"}
CATEGORY_ORDER = ("account_paused_or_killed", "promotion_rejection", "conflicting_position",
                  "risk_rejection")  # fmt: skip


def _ts(value) -> pd.Timestamp:
    return engine._ts(value)


def _iso(value) -> str | None:
    return None if value is None else _ts(value).isoformat()


def _hours(a, b) -> float:
    return (_ts(b) - _ts(a)).total_seconds() / 3600


# --------------------------------------------------------------------------- the view


class RunView:
    """One run's ledger, replayed account, recorded cycles and notifications, read once."""

    def __init__(self, store, run_id: str, now=None, events: list[dict] | None = None):
        self.store, self.run_id = store, run_id
        self.now = _ts(now or utcnow())
        self.ctx = engine.load_run(store, run_id)
        self.events = events if events is not None else engine.events(store, run_id)
        self.state = AccountState()
        self._bar_snapshots: dict[str, dict] = {}
        for e in self.events:  # single pass: capture each bar's pre-trade snapshot
            if e["event_type"] == "risk_decision" and e["market_time"] not in self._bar_snapshots:
                self._bar_snapshots[e["market_time"]] = self._pretrade(e["market_time"])
            apply(self.state, e)
        self.cycles = engine._rows(
            store, "SELECT cycle_id, started_at, finished_at, status, events_written, summary "
            "FROM paper_cycles WHERE run_id=? ORDER BY started_at, finished_at", [run_id])  # fmt: skip
        for c in self.cycles:
            c["started_at"], c["finished_at"] = _ts(c["started_at"]), _ts(c["finished_at"])
            c["summary"] = json.loads(c["summary"])
        self.notes = engine._rows(
            store, "SELECT subject_id, attempt, status, recorded_at FROM paper_notifications "
            "WHERE run_id=? ORDER BY recorded_at", [run_id])  # fmt: skip

    def _pretrade(self, bar: str) -> dict:
        """The engine's allocation snapshot, rebuilt from the replay at that point."""
        st = self.state
        return {
            "status": st.status, "halted": bar in st.halted_bars,
            "equity": st.marks[-1]["equity"] if st.marks else st.starting_equity, "cash": st.cash,
            "positions": {s: {"side": p["side"], "strategy_id": p["strategy_id"],
                              "notional": p["units"] * p["last_price"]}
                          for s, p in st.positions.items()},
            "orders": {o["order"]["symbol"]: {"side": o["order"]["position_side"],
                                              "strategy_id": o["strategy_id"],
                                              "notional": o["order"]["target_notional"],
                                              "reserved_cash": o["reserved_cash"]}
                       for o in st.entry_orders()},
        }  # fmt: skip

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == kind]

    @property
    def window(self) -> timedelta:
        return self.ctx.window

    @property
    def last_mark(self) -> dict | None:
        return self.state.marks[-1] if self.state.marks else None


# --------------------------------------------------------------------------- account


def account_summary(v: RunView) -> dict:
    st, risk = v.state, v.ctx.risk
    m = v.last_mark
    equity = m["equity"] if m else st.starting_equity
    realised = sum(t["net_pnl"] for t in st.closed)
    unrealised = sum(st.position_value(p, p["last_price"]) - p["margin"] - p["entry_fee"]
                     for p in st.positions.values())  # fmt: skip
    reserved = sum(o["reserved_cash"] for o in st.entry_orders())
    created = _ts(v.ctx.definition.created_at)
    marks = st.marks
    return {
        "run_id": v.run_id, "mode": "paper", "status": st.status,
        "created_at": created.isoformat(), "age_days": round(_hours(created, v.now) / 24, 2),
        "as_of_bar": st.last_bar, "currency": risk.currency,
        "starting_equity": st.starting_equity, "equity": equity, "cash": st.cash,
        "realised_pnl": realised, "unrealised_pnl": unrealised, "net_pnl": equity - st.starting_equity,
        "return_on_starting_equity": equity / st.starting_equity - 1,
        "peak_equity": st.peak_equity, "drawdown": m["drawdown"] if m else 0.0,
        "max_drawdown": max((x["drawdown"] for x in marks), default=0.0),
        "gross_notional": m["gross_notional"] if m else 0.0,
        "gross_exposure": m["exposure"] if m and m["exposure"] is not None else 0.0,
        "margin_in_use": sum(p["margin_bal"] for p in st.positions.values()),
        "free_cash": st.cash - reserved,
        "open_positions": len(st.positions), "pending_entry_orders": len(st.entry_orders()),
        "closed_trades": len(st.closed),
        "maturity": v.ctx.maturity.level(len(st.closed), len(marks)),
        "observed_days": len(marks),
        "reconciles_with_ledger": abs(st.starting_equity + realised + unrealised - equity) < 1e-6,
    }  # fmt: skip


def liquidation_distance(side: int, mark: float, liq: float) -> float:
    """Fraction the mark must move adversely to reach the modelled liquidation level."""
    return (mark - liq) / mark if side > 0 else (liq - mark) / mark


def positions_view(v: RunView) -> list[dict]:
    st = v.state
    out = []
    for s in sorted(st.positions):
        p = st.positions[s]
        liq = st.liquidation_price(p)
        dist = liquidation_distance(p["side"], p["last_price"], liq)
        remaining = (round((_ts(p["exit_bar"]) - _ts(st.last_bar)) / BAR) if st.last_bar
                     else None)  # fmt: skip
        out.append({
            "position_id": p["position_id"], "symbol": s, "side": "LONG" if p["side"] > 0 else "SHORT",
            "strategy": p["strategy"], "signal_bar": p["signal_bar"], "entry_time": p["entry_time"],
            "entry_ref": p["entry_ref"], "entry_fill": p["entry_fill"], "mark": p["last_price"],
            "mark_bar": st.last_bar, "units": p["units"],
            "notional_at_entry": p["notional"], "notional_at_mark": p["units"] * p["last_price"],
            "leverage": p["leverage"], "margin_committed": p["margin"], "margin_balance": p["margin_bal"],
            "unrealised_pnl": st.position_value(p, p["last_price"]) - p["margin"] - p["entry_fee"],
            "funding_paid_to_date": p["funding_paid"], "entry_fee": p["entry_fee"],
            "liquidation": {"price": liq, "distance": dist, "approximate": True,
                            "model": v.ctx.risk.liquidation_model,
                            "near": dist < LIQUIDATION_WARN_DISTANCE,
                            "note": "approximate isolated-margin model; not Hyperliquid parity"},
            "scheduled_exit_bar": p["exit_bar"], "bars_remaining": remaining,
        })  # fmt: skip
    return out


def risk_view(v: RunView, health_state: dict | None = None) -> dict:
    st, r = v.state, v.ctx.risk
    acct = account_summary(v)
    eq = acct["equity"]
    m = v.last_mark
    per_asset = (m or {}).get("per_asset_notional") or {}
    per_strategy = (m or {}).get("per_strategy_notional") or {}
    held = len(st.positions) + len(st.entry_orders())
    reserve = r.min_free_cash_fraction * eq
    gross = acct["gross_exposure"]
    streak = engine._error_streak(v.store, v.run_id)
    halted_last = st.last_bar in st.halted_bars if st.last_bar else False
    day_ret = (m or {}).get("day_return")
    request = r.position_notional_fraction * eq
    can_open = max(min(r.max_open_positions - held,
                       int((r.max_gross_notional_fraction * eq - acct["gross_notional"]) // request)
                       if request else 0), 0)  # fmt: skip
    permitted = st.status == "ACTIVE"
    return {
        "status": st.status,
        "new_entries_permitted": permitted,
        "why_not": None if permitted else f"run {st.status}",
        "positions": {"current": held, "limit": r.max_open_positions},
        "gross_exposure": {"current": gross, "limit": r.max_gross_notional_fraction},
        "per_asset_exposure": {s: {"current": n / eq, "limit": r.max_asset_notional_fraction}
                               for s, n in sorted(per_asset.items())},
        "per_strategy_exposure": {k: {"current": n / eq, "limit": r.max_strategy_notional_fraction}
                                  for k, n in sorted(per_strategy.items())},
        "free_cash": {"current": acct["free_cash"], "required_reserve": reserve,
                      "headroom": acct["free_cash"] - reserve},
        "daily_loss": {"last_bar_return": day_ret, "halt_at": -r.daily_loss_halt_fraction,
                       "halted_on_last_bar": halted_last,
                       "note": "a halt applies to its bar only"},
        "drawdown": {"current": acct["drawdown"], "kill_at": r.max_drawdown_kill_fraction},
        "failed_cycle_streak": {"current": streak, "auto_pause_at": r.max_consecutive_error_cycles},
        "new_positions_possible_now": can_open if permitted else 0,
        "position_size_now": request,
        "leverage": r.leverage,
        "near_liquidation": [p["symbol"] for p in positions_view(v) if p["liquidation"]["near"]],
        "policy_id": r.policy_id,
    }  # fmt: skip


# --------------------------------------------------------------------------- intents


def _cycles_in(v: RunView, start, end) -> list[dict]:
    return [c for c in v.cycles if _ts(start) <= c["started_at"] < _ts(end)]


def _window_cause(v: RunView, bar: str) -> str:
    """Why bar T was not processed inside its entry window, from recorded cycles."""
    cyc = _cycles_in(v, bar, _ts(bar) + v.window)
    if not cyc:
        return "offline_gap"
    if all(c["status"] == "error" for c in cyc):
        return "engine_error"
    return "data_late"


def intents_view(v: RunView) -> list[dict]:
    """Every intent and its final disposition (ENTERED, PENDING_FILL, EXPECTED_SKIP,
    MISSED_EXECUTION) with promotion result, risk result and reasons."""
    risk = {e["payload"]["intent_id"]: e["payload"] for e in v.of("risk_decision")}
    submitted = {e["payload"]["intent_id"]: e["payload"]["order"]["order_id"]
                 for e in v.of("order_submitted") if e["payload"]["order"]["purpose"] == "entry"}  # fmt: skip
    ended = {}
    for kind in ("order_filled", "order_expired", "order_rejected", "order_cancelled"):
        for e in v.of(kind):
            ended[e["payload"]["order_id"]] = (kind, e["payload"])
    intents = v.of("intent_created")
    by_bar: dict[str, list[dict]] = {}
    for e in intents:
        by_bar.setdefault(e["market_time"], []).append(e["payload"])
    counterfactual: dict[str, dict] = {}
    for bar, group in by_bar.items():
        snap = v._bar_snapshots.get(bar)
        if snap is None:
            continue
        cands = [{"intent_id": i["intent_id"], "strategy_id": i["strategy_id"], "symbol": i["symbol"],
                  "side": i["side"], "live": True, "data_ok": True, "promotion": i["promotion"]}
                 for i in group]  # fmt: skip
        counterfactual.update(allocate(v.ctx.risk, v.ctx.execution, snap, cands,
                                       run_id=v.run_id, bar_close=bar))  # fmt: skip
    out = []
    for e in intents:
        i = e["payload"]
        d = risk.get(i["intent_id"]) or {}
        reasons = d.get("reasons") or []
        base = [r.split(":")[0] for r in reasons]
        rec = {
            "bar": i["signal_bar"], "strategy": i["strategy"], "symbol": i["symbol"],
            "side": "LONG" if i["side"] > 0 else "SHORT", "intent_id": i["intent_id"],
            "promotion": i["promotion"]["decision"], "promotion_blocked_by": i["promotion"]["blocked_by"],
            "risk": d.get("decision"), "risk_reasons": reasons, "order": None,
        }  # fmt: skip
        oid = submitted.get(i["intent_id"])
        if oid:
            rec["order"] = oid
            kind, payload = ended.get(oid, (None, None))
            if kind == "order_filled":
                rec.update(
                    disposition="ENTERED", category="entered", reason="filled at the next open"
                )
            elif kind is None:
                rec.update(
                    disposition="PENDING_FILL", category="pending", reason="awaiting T+1 bar"
                )
            elif kind == "order_expired":
                rec.update(disposition="MISSED_EXECUTION", category="stale_or_missing_data",
                           reason=payload["reason"])  # fmt: skip
            else:
                rec.update(disposition="MISSED_EXECUTION", category="execution_error",
                           reason=payload.get("reason", kind))  # fmt: skip
        elif any(b not in OPERATIONAL for b in base):
            cat = min((DELIBERATE[b] for b in base if b in DELIBERATE),
                      key=CATEGORY_ORDER.index)  # fmt: skip
            rec.update(disposition="EXPECTED_SKIP", category=cat, reason=", ".join(reasons))
            if any(b in OPERATIONAL for b in base):
                rec["also_operational"] = [r for r in reasons if r.split(":")[0] in OPERATIONAL]
        else:
            cause = (_window_cause(v, i["signal_bar"]) if "missed_execution_window" in base
                     else "stale_or_missing_data")  # fmt: skip
            cf = counterfactual.get(i["intent_id"], {})
            rec["counterfactual"] = {"decision": cf.get("decision"), "reasons": cf.get("reasons"),
                                     "note": "frozen risk policy on the same recorded snapshot, "
                                             "timing/data assumed fine; no fill reconstructed"}  # fmt: skip
            if cf.get("decision") == "ACCEPTED":
                rec.update(
                    disposition="MISSED_EXECUTION", category=cause, reason=", ".join(reasons)
                )
            else:
                cfr = [r.split(":")[0] for r in cf.get("reasons") or []]
                cat = min((DELIBERATE[b] for b in cfr if b in DELIBERATE),
                          key=CATEGORY_ORDER.index, default="risk_rejection")  # fmt: skip
                rec.update(disposition="EXPECTED_SKIP", category=cat,
                           reason=f"would have been rejected anyway: {', '.join(cf.get('reasons') or [])}",
                           also_operational=reasons)  # fmt: skip
        out.append(rec)
    return out


def skipped_view(v: RunView) -> list[dict]:
    return [i for i in intents_view(v) if i["disposition"] in ("EXPECTED_SKIP", "MISSED_EXECUTION")]


# --------------------------------------------------------------------------- coverage & gaps


def _expected_bars(v: RunView) -> list[pd.Timestamp]:
    st = v.state
    bars, b = [], v.ctx.definition.first_bar
    while b <= v.now:
        if st.terminal and st.flat and (st.last_bar is None or b > _ts(st.last_bar)):
            break  # a finished run expects no more bars
        bars.append(b)
        b += BAR
    return bars


def coverage(v: RunView) -> dict:
    """Per expected bar: ON_TIME, LATE, PENDING (window open) or UNPROCESSED (window passed),
    from the recorded marks and cycles; plus cycle counts. Nothing is assumed."""
    marks = {m["bar_close"]: m for m in v.state.marks}
    rows = []
    for b in _expected_bars(v):
        k = b.isoformat()
        m = marks.get(k)
        attended = _cycles_in(v, b, b + v.window)
        if m:
            late = m["latency_hours"] > v.window.total_seconds() / 3600
            status = "LATE" if late else "ON_TIME"
            cause = _window_cause(v, k) if late else None
        elif v.now < b + v.window:
            status, cause = "PENDING", None
        else:
            status, cause = "UNPROCESSED", _window_cause(v, k)
        rows.append({"bar": k, "status": status, "cause": cause,
                     "processed_at": m["processed_at"] if m else None,
                     "latency_hours": round(m["latency_hours"], 2) if m else None,
                     "cycles_in_window": len(attended),
                     "error_cycles_in_window": sum(c["status"] == "error" for c in attended)})  # fmt: skip
    ok = [c for c in v.cycles if c["status"] == "ok"]
    streak = 0
    for c in reversed(v.cycles):
        if c["status"] != "ok":
            break
        streak += 1
    created = _ts(v.ctx.definition.created_at)
    days = pd.date_range(created.floor("D"), v.now.floor("D"), freq="D")
    attended_days = {c["started_at"].floor("D") for c in v.cycles}
    counts = Counter(r["status"] for r in rows)
    nxt = (_ts(v.state.last_bar) + BAR) if v.state.last_bar else v.ctx.definition.first_bar
    return {
        "expected_bars": len(rows), "on_time": counts["ON_TIME"], "late": counts["LATE"],
        "unprocessed": counts["UNPROCESSED"], "pending": counts["PENDING"],
        "missed_entry_windows": counts["LATE"] + counts["UNPROCESSED"],
        "cycles": {"total": len(v.cycles), "ok": len(ok),
                   "error": sum(c["status"] == "error" for c in v.cycles),
                   "consecutive_ok": streak,
                   "last_ok": ok[-1]["started_at"].isoformat() if ok else None,
                   "last": v.cycles[-1]["started_at"].isoformat() if v.cycles else None},
        "calendar_days": len(days), "days_with_a_cycle": len(attended_days),
        "days_without_a_cycle": [d.date().isoformat() for d in days if d not in attended_days],
        "next_bar": nxt.isoformat(),
        "next_entry_window": [nxt.isoformat(), (nxt + v.window).isoformat()],
        "bars": rows,
    }  # fmt: skip


def gaps_view(v: RunView) -> list[dict]:
    """Operational gap audit: each bar not processed inside its entry window."""
    cov = coverage(v)
    intents = intents_view(v)
    funding = {(e["payload"]["position_id"], e["payload"]["bar_close"]): e["payload"]
               for e in v.of("funding_accrued")}  # fmt: skip
    states = {e["market_time"]: e["payload"]["states"] for e in v.of("signals_evaluated")}
    marks = {m["bar_close"]: m for m in v.state.marks}
    issues = [e["payload"] for e in v.of("data_issue")]
    out = []
    for r in cov["bars"]:
        if r["status"] not in ("LATE", "UNPROCESSED"):
            continue
        b = r["bar"]
        mark = marks.get(b)
        held = sorted((mark or {}).get("per_asset_notional", {}))
        bar_intents = [i for i in intents if i["bar"] == b]
        fund = [f for (pid, fb), f in funding.items() if fb == b]
        out.append({
            "bar": b, "status": r["status"], "cause": r["cause"],
            "cycles_in_window": r["cycles_in_window"], "processed_at": r["processed_at"],
            "signals": sum(s["state"] == "SIGNAL" for s in states.get(b, [])) if b in states else None,
            "intents": [{k: i[k] for k in ("strategy", "symbol", "side", "disposition", "category")}
                        for i in bar_intents],
            "trades_made_impossible": sum(i["disposition"] == "MISSED_EXECUTION" for i in bar_intents),
            "positions_held": held,
            "marks_recovered": mark is not None,
            "funding_recovered": {"events": len(fund), "missing_settlements": sum(f["missing"] for f in fund)},
            "data_issues": [i for i in issues if i.get("bar_close") == b],
            "unknowable": (["signals on this bar are unknown until it is processed"]
                           if mark is None else [])
            + ["entry fills that were never placed are not reconstructed (by design)"],
        })  # fmt: skip
    return out


# --------------------------------------------------------------------------- trades & notifications


def _sent_at(v: RunView, subject_id: str) -> str | None:
    sent = [
        n["recorded_at"] for n in v.notes if n["subject_id"] == subject_id and n["status"] == "sent"
    ]
    return _iso(min(sent)) if sent else None


def notification_lag(v: RunView) -> list[dict]:
    """Execution time the simulated fill represents vs when it was recorded and notified."""
    out = []
    for e in v.of("position_opened") + v.of("position_closed"):
        p = e["payload"]
        executed = p["entry_time"] if e["event_type"] == "position_opened" else p["exit_time"]
        notified = _sent_at(v, e["event_id"])
        out.append({
            "event": e["event_type"], "symbol": p["symbol"], "position_id": p["position_id"],
            "execution_time": executed, "recorded_at": e["recorded_at"], "notified_at": notified,
            "record_lag_hours": round(_hours(executed, e["recorded_at"]), 2),
            "notification_lag_hours": None if notified is None else round(_hours(executed, notified), 2),
        })  # fmt: skip
    return out


def trades_view(v: RunView) -> list[dict]:
    lags = {(n["position_id"], n["event"]): n for n in notification_lag(v)}
    out = []
    for t in v.state.closed:
        issues = []
        if t.get("late_processing"):
            issues.append("exit processed after the entry window (filled at the scheduled close)")
        if t["funding_missing_settlements"]:
            issues.append(f"{t['funding_missing_settlements']} funding settlement(s) missing")
        if t["reason"] == "liquidation":
            issues.append("simulated liquidation (approximate model)")
        out.append({
            "strategy": t["strategy"], "symbol": t["symbol"], "side": "LONG" if t["side"] > 0 else "SHORT",
            "signal_bar": t["signal_bar"], "entry_time": t["entry_time"], "exit_time": t["exit_time"],
            "entry_fill": t["entry_fill"], "exit_fill": t["exit_fill"], "bars_held": t["bars_held"],
            "notional": t["notional"], "gross_pnl": t["gross_pnl"], "fees": t["fees"],
            "funding": t["funding"], "slippage_cost": t["slippage_cost"], "net_pnl": t["net_pnl"],
            "return_on_margin": t["return_on_margin"], "return_on_notional": t["return_on_notional"],
            "exit_reason": t["reason"], "operational_issues": issues,
            "notification_lag_hours": {
                "open": (lags.get((t["position_id"], "position_opened")) or {}).get("notification_lag_hours"),
                "close": (lags.get((t["position_id"], "position_closed")) or {}).get("notification_lag_hours"),
            },
        })  # fmt: skip
    return out


def equity_curve(v: RunView) -> list[dict]:
    return [{"bar": m["bar_close"], "equity": m["equity"], "peak": m["peak_equity"],
             "drawdown": m["drawdown"], "exposure": m["exposure"],
             "open_positions": m["open_positions"]} for m in v.state.marks]  # fmt: skip


# --------------------------------------------------------------------------- contributions


def contributions(v: RunView, intents: list[dict] | None = None) -> dict:
    """Descriptive totals per strategy and per asset. Never a ranking."""
    intents = intents_view(v) if intents is None else intents
    closed = v.state.closed
    strategies = {}
    for m in v.ctx.definition.cohort:
        name = m.strategy_name
        mine = [t for t in closed if t["strategy"] == name]
        ints = [i for i in intents if i["strategy"] == name]
        strategies[name] = {
            "signals": sum(e["payload"]["strategy"] == name for e in v.of("signal_consumed")),
            "intents": len(ints), "entered": sum(i["disposition"] == "ENTERED" for i in ints),
            "closed_trades": len(mine), "wins": sum(t["net_pnl"] > 0 for t in mine),
            "losses": sum(t["net_pnl"] <= 0 for t in mine),
            "net_pnl": sum(t["net_pnl"] for t in mine), "fees": sum(t["fees"] for t in mine),
            "funding": sum(t["funding"] for t in mine),
            "expected_skips": sum(i["disposition"] == "EXPECTED_SKIP" for i in ints),
            "missed_executions": sum(i["disposition"] == "MISSED_EXECUTION" for i in ints),
            "open_positions": sum(p["strategy"] == name for p in v.state.positions.values()),
        }  # fmt: skip
    marks = v.state.marks
    assets = {}
    for s in sorted({a for m in v.ctx.definition.cohort for a in m.assets}):
        mine = [t for t in closed if t["symbol"] == s]
        expo = [
            (m.get("per_asset_notional") or {}).get(s, 0.0) / m["equity"]
            for m in marks
            if m["equity"] > 0
        ]
        assets[s] = {
            "closed_trades": len(mine), "net_pnl": sum(t["net_pnl"] for t in mine),
            "fees": sum(t["fees"] for t in mine), "funding": sum(t["funding"] for t in mine),
            "avg_exposure": sum(expo) / len(expo) if expo else 0.0,
            "missed_executions": sum(i["disposition"] == "MISSED_EXECUTION" and i["symbol"] == s
                                     for i in intents),
            "open": s in v.state.positions,
        }  # fmt: skip
    return {"strategies": strategies, "assets": assets,
            "note": "descriptive totals; tiny samples rank nothing"}  # fmt: skip


# --------------------------------------------------------------------------- health


def components(store, now) -> dict:
    """Read-only freshness of the components the paper trader depends on (SQL only)."""
    now = _ts(now)

    def one(sql: str) -> pd.Timestamp | None:
        try:
            row = store.con.execute(sql).fetchone()
        except Exception:
            return None
        return None if not row or row[0] is None else _ts(row[0])

    def state(t: pd.Timestamp | None) -> str:
        return "never" if t is None else "current" if now - t <= COMPONENT_STALE else "stale"

    bar = one("SELECT max(close_time) FROM perp_bars WHERE source='hyperliquid' AND timeframe='1d'")
    fwd_run = one("SELECT max(finished_at) FROM lab_forward_runs WHERE kind='check'")
    cop_run = one("SELECT max(finished_at) FROM copilot_runs")
    return {
        "data": {"state": state(bar), "newest_bar": _iso(bar)},
        "forward_tracking": {"state": state(fwd_run), "last_check": _iso(fwd_run)},
        "copilot": {"state": state(cop_run), "last_run": _iso(cop_run)},
    }


def health(v: RunView, cov: dict | None = None) -> dict:
    """HEALTHY / DEGRADED / STALE / PAUSED / STOPPED / KILLED from explicit conditions.

    Telegram delivery failures are reported but never make the account unhealthy.
    """
    st = v.state
    cov = coverage(v) if cov is None else cov
    reasons: list[str] = []
    stale = [r for r in cov["bars"] if r["status"] == "UNPROCESSED"]
    recent = [r for r in cov["bars"][-RECENT_BARS:] if r["status"] == "LATE"]
    streak = engine._error_streak(v.store, v.run_id)
    near = [p["symbol"] for p in positions_view(v) if p["liquidation"]["near"]]
    recent_bars = {r["bar"] for r in cov["bars"][-RECENT_BARS:]}
    missing_funding = sum(e["payload"]["missing"] for e in v.of("funding_accrued")
                          if e["market_time"] in recent_bars)  # fmt: skip
    issues = [e for e in v.of("data_issue") if e["market_time"] in recent_bars]
    if stale:
        reasons.append(f"{len(stale)} bar(s) past their entry window not yet processed "
                       f"(oldest {stale[0]['bar']}, {stale[0]['cause']})")  # fmt: skip
    if streak:
        reasons.append(f"{streak} consecutive failed cycle(s)")
    if recent:
        reasons.append(f"{len(recent)} recent bar(s) processed after the entry window")
    if issues:
        reasons.append(f"{len(issues)} recent data issue(s)")
    if missing_funding:
        reasons.append(f"{missing_funding} funding settlement(s) missing recently")
    if near:
        reasons.append(f"near modelled liquidation (<{LIQUIDATION_WARN_DISTANCE:.0%}): {near}")
    if st.status in ("KILLED", "STOPPED", "PAUSED"):
        level = st.status
    elif stale:
        level = "STALE"
    elif reasons:
        level = "DEGRADED"
    else:
        level = "HEALTHY"
    attempts: dict[tuple, set] = {}
    for n in v.notes:
        attempts.setdefault((n["subject_id"], n["attempt"]), set()).add(n["status"])
    sent = {k[0] for k, val in attempts.items() if "sent" in val}
    failed = {k[0] for k, val in attempts.items() if "failed" in val} - sent
    unknown = {k[0] for k, val in attempts.items() if val == {"attempted"}} - sent
    return {
        "state": level, "reasons": reasons,
        "last_cycle_ok": bool(v.cycles) and v.cycles[-1]["status"] == "ok",
        "notifications": {"failed_unsent": len(failed), "unknown_outcome": len(unknown),
                          "note": "delivery problems never affect the paper account or its health"},
    }  # fmt: skip


# --------------------------------------------------------------------------- status


def run_status(store, run_id: str, now=None) -> dict:
    v = RunView(store, run_id, now)
    cov = coverage(v)
    intents = intents_view(v)
    return {
        "observe_version": OBSERVE_VERSION, "now": v.now.isoformat(),
        "account": account_summary(v), "health": health(v, cov), "risk": risk_view(v),
        "positions": positions_view(v),
        "coverage": {k: val for k, val in cov.items() if k != "bars"},
        "intents": dict(Counter(i["disposition"] for i in intents)),
        "components": components(store, v.now),
        "policies": {"promotion": v.ctx.promotion.policy_id, "risk": v.ctx.risk.policy_id,
                     "execution": v.ctx.execution.policy_id, "exit": v.ctx.exit.policy_id},
    }  # fmt: skip


def current_run(store) -> str:
    """The open run, else the newest run (observation never creates one)."""
    rs = engine.runs(store)
    if not rs:
        raise engine.PaperError("no paper run exists")
    open_ = [r["run_id"] for r in rs if not engine.account(store, r["run_id"]).terminal]
    return open_[-1] if open_ else rs[-1]["run_id"]


# --------------------------------------------------------------------------- snapshots


def snapshot_payload(v: RunView) -> dict:
    """``paper_execution`` snapshot: ledger-derived only, reproducible from events[:as_of_seq]."""
    st = v.state
    acct = account_summary(v)
    intents = intents_view(v)
    closed = st.closed
    marks = st.marks
    expo = [m["exposure"] for m in marks if m["exposure"] is not None]
    disp = Counter(i["disposition"] for i in intents)
    payload = {
        "stage": engine.EVIDENCE_STAGE, "snapshot_version": SNAPSHOT_VERSION, "mode": "paper",
        "run_id": v.run_id, "as_of_seq": st.seq, "as_of_bar": st.last_bar, "status": st.status,
        "observed_days": len(marks), "closed_trades": len(closed), "open_trades": len(st.positions),
        "wins": sum(t["net_pnl"] > 0 for t in closed), "losses": sum(t["net_pnl"] <= 0 for t in closed),
        "gross_pnl": sum(t["gross_pnl"] for t in closed), "net_pnl": acct["net_pnl"],
        "realised_pnl": acct["realised_pnl"], "unrealised_pnl": acct["unrealised_pnl"],
        "fees": sum(t["fees"] for t in closed), "funding": sum(t["funding"] for t in closed),
        "slippage": sum(t["slippage_cost"] for t in closed),
        "equity": acct["equity"], "starting_equity": acct["starting_equity"],
        "return_on_starting_equity": acct["return_on_starting_equity"],
        "max_drawdown": acct["max_drawdown"],
        "avg_exposure": sum(expo) / len(expo) if expo else 0.0,
        "signals": st.counts["signal_consumed"], "intents": len(intents),
        "entered": disp["ENTERED"], "pending_fill": disp["PENDING_FILL"],
        "expected_skips": disp["EXPECTED_SKIP"], "missed_executions": disp["MISSED_EXECUTION"],
        "skips_by_category": dict(Counter(i["category"] for i in intents
                                          if i["disposition"] == "EXPECTED_SKIP")),
        "missed_by_category": dict(Counter(i["category"] for i in intents
                                           if i["disposition"] == "MISSED_EXECUTION")),
        "contributions": contributions(v, intents),
        "maturity": {"policy": v.ctx.maturity.policy_id, "level": acct["maturity"]},
        "reconciles_with_ledger": acct["reconciles_with_ledger"],
        "events_digest": hashlib.sha256("".join(e["event_id"] for e in v.events).encode()).hexdigest(),
        "policies": {"promotion": v.ctx.promotion.policy_id, "risk": v.ctx.risk.policy_id,
                     "execution": v.ctx.execution.policy_id, "exit": v.ctx.exit.policy_id},
        "descriptive_only": True,
    }  # fmt: skip
    return json.loads(canonical_json(payload))


def record_snapshot(store, run_id: str, now=None) -> dict:
    """Append the snapshot as of the ledger's current last event (idempotent per sequence).

    An existing snapshot for the same (run, sequence, version) is returned unchanged; it is
    never recomputed in place.
    """
    v = RunView(store, run_id, now)
    payload = snapshot_payload(v)
    row = store.con.execute(
        "SELECT snapshot_id, payload FROM paper_snapshots WHERE run_id=? AND as_of_seq=? AND "
        "snapshot_version=?", [run_id, payload["as_of_seq"], SNAPSHOT_VERSION]).fetchone()  # fmt: skip
    if row:
        return {"snapshot_id": row[0], "recorded": False, **json.loads(row[1])}
    sid = content_id("papersnap_", payload)
    store.con.execute(
        "INSERT INTO paper_snapshots VALUES (?,?,?,?,?,?,?)",
        [sid, run_id, SNAPSHOT_VERSION, payload["as_of_seq"],
         None if payload["as_of_bar"] is None else _ts(payload["as_of_bar"]).to_pydatetime(),
         v.now.to_pydatetime(), canonical_json(payload)],
    )  # fmt: skip
    return {"snapshot_id": sid, "recorded": True, **payload}


def snapshots(store, run_id: str) -> list[dict]:
    rows = engine._rows(store, "SELECT snapshot_id, as_of_seq, as_of_bar, recorded_at, payload FROM "
                        "paper_snapshots WHERE run_id=? ORDER BY as_of_seq", [run_id])  # fmt: skip
    return [{**r, "as_of_bar": _iso(r["as_of_bar"]), "recorded_at": _iso(r["recorded_at"]),
             "payload": json.loads(r["payload"])} for r in rows]  # fmt: skip


# --------------------------------------------------------------------------- daily brief


def _pct(x: float, signed: bool = True) -> str:
    return f"{x:+.2%}" if signed else f"{x:.2%}"


def _when(iso: str) -> str:
    return _ts(iso).strftime("%d %b %Y")


def brief(v: RunView, bar: str | None = None) -> dict:
    """The compact PAPER daily brief for a completed paper day (default: the last one)."""
    st = v.state
    bar = bar or st.last_bar
    acct = account_summary(v)
    rv = risk_view(v)
    h = health(v)
    comp = components(v.store, v.now)
    intents = [i for i in intents_view(v) if bar and i["bar"] == bar]
    today = {
        "signals": sum(e["market_time"] == bar for e in v.of("signal_consumed")),
        "opened": sum(e["market_time"] == bar for e in v.of("position_opened")),
        "closed": sum(e["market_time"] == bar for e in v.of("position_closed")),
        "rejected": sum(i["disposition"] == "EXPECTED_SKIP" for i in intents),
        "missed": sum(i["disposition"] == "MISSED_EXECUTION" for i in intents),
        "orders_submitted": sum(i["disposition"] in ("PENDING_FILL", "ENTERED") for i in intents),
    }
    title = f"daily brief · {_when(bar)}" if bar else "status · no completed paper day yet"
    lines = [f"{render.HEADER}", f"{title}", "", "<b>PAPER ACCOUNT</b>",
             f"Equity: {acct['equity']:,.2f} {acct['currency']}",
             f"PnL: {_pct(acct['return_on_starting_equity'])} ({acct['net_pnl']:+,.2f} {acct['currency']})",
             f"Drawdown: {_pct(acct['drawdown'], False)} / {v.ctx.risk.max_drawdown_kill_fraction:.0%} kill",
             f"Positions: {rv['positions']['current']} / {rv['positions']['limit']}",
             f"Gross exposure: {acct['gross_exposure']:.0%} / {v.ctx.risk.max_gross_notional_fraction:.0%}",
             "", "<b>TODAY</b>",
             f"Signals: {today['signals']} · Opened: {today['opened']} · Closed: {today['closed']}",
             f"Orders submitted: {today['orders_submitted']} · Rejected: {today['rejected']} · "
             f"Missed: {today['missed']}"]  # fmt: skip
    pos = positions_view(v)
    if pos:
        lines += ["", "<b>POSITIONS</b>"]
        for p in pos:
            lines.append(f"{p['symbol']} {p['side']} · {p['strategy']} · uPnL "
                         f"{p['unrealised_pnl']:+,.2f} · exit {_when(p['scheduled_exit_bar'])} "
                         f"({p['bars_remaining']} bars) · liq≈ {p['liquidation']['distance']:.0%} away")  # fmt: skip
    lines += ["", "<b>HEALTH</b>",
              f"Paper engine: {h['state']}" + (f" ({'; '.join(h['reasons'])})" if h["reasons"] else ""),
              f"Last cycle: {'OK' if h['last_cycle_ok'] else 'FAILED' if v.cycles else 'none yet'}",
              f"Data: {comp['data']['state']}", f"Forward tracking: {comp['forward_tracking']['state']}",
              f"Co-pilot: {comp['copilot']['state']}", f"Paper maturity: {acct['maturity']}",
              "", render.FOOTER]  # fmt: skip
    return {"bar": bar, "today": today, "text": "\n".join(lines), "health": h["state"]}


def record_brief(
    store,
    run_id: str,
    *,
    now=None,
    send: bool = False,
    sender_factory: Callable[[], Callable[[str], None]] | None = None,
) -> dict:
    """Snapshot -> brief for the last completed paper day (once per day) -> optional send.

    Call only after the paper cycle has committed. The brief for a day is stored once and
    never rebuilt; delivery is attempted only for unsent briefs recorded in the last 24 h,
    and an attempt of unknown outcome is never retried.
    """
    snap = record_snapshot(store, run_id, now)
    v = RunView(store, run_id, now)
    out: dict = {"snapshot_id": snap["snapshot_id"], "snapshot_recorded": snap["recorded"]}
    if v.state.last_bar is None:
        return {**out, "brief": None, "note": "no completed paper day yet"}
    row = store.con.execute(
        "SELECT brief_id, text FROM paper_briefs WHERE run_id=? AND bar_close=? AND brief_version=?",
        [run_id, _ts(v.state.last_bar).to_pydatetime(), BRIEF_VERSION]).fetchone()  # fmt: skip
    if row:
        brief_id, text, recorded = row[0], row[1], False
    else:
        b = brief(v)
        text = b["text"]
        payload = {"run_id": run_id, "bar": b["bar"], "today": b["today"], "health": b["health"],
                   "snapshot_id": snap["snapshot_id"], "brief_version": BRIEF_VERSION}  # fmt: skip
        brief_id = content_id("paperbrief_", {**payload, "text": text})
        store.con.execute("INSERT INTO paper_briefs VALUES (?,?,?,?,?,?,?,?)",
                          [brief_id, run_id, _ts(b["bar"]).to_pydatetime(), BRIEF_VERSION,
                           v.now.to_pydatetime(), render.sha256(text), text, canonical_json(payload)])  # fmt: skip
        recorded = True
    out.update(brief_id=brief_id, brief_recorded=recorded, bar=v.state.last_bar, text=text)
    if send:
        out["delivery"] = _deliver_brief(store, run_id, brief_id, text, v.now,
                                         sender_factory or engine._no_sender)  # fmt: skip
    return out


def _deliver_brief(store, run_id, brief_id, text, now, sender_factory) -> dict:
    rows = store.con.execute("SELECT attempt, status FROM paper_notifications WHERE subject_id=?",
                             [brief_id]).fetchall()  # fmt: skip
    states: dict[int, set] = {}
    for a, s in rows:
        states.setdefault(a, set()).add(s)
    if any("sent" in s for s in states.values()):
        return {"status": "already_sent"}
    if any(s == {"attempted"} for s in states.values()):
        return {"status": "unknown_outcome_not_resent"}
    rec = store.con.execute(
        "SELECT recorded_at FROM paper_briefs WHERE brief_id=?", [brief_id]
    ).fetchone()
    if _ts(now) - _ts(rec[0]) > engine.NOTIFY_WINDOW:
        return {"status": "expired_not_sent"}
    attempt = max(states, default=0) + 1

    def mark(status: str, error: str | None = None) -> None:
        store.con.execute(
            "INSERT INTO paper_notifications VALUES (?,?,?,?,?,?,?,?,?)",
            ["papernote_" + uuid4().hex, run_id, brief_id, attempt, status,
             "telegram", render.sha256(text), error, max(_ts(now), _ts(utcnow())).to_pydatetime()],
        )  # fmt: skip

    mark("attempted")
    try:
        sender_factory()(text)
    except Exception as exc:
        mark("failed", f"{type(exc).__name__}: {exc}")
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "attempt": attempt}
    mark("sent")
    return {"status": "sent", "attempt": attempt}
