"""Authoritative deterministic PAPER state machine; append-only evidence and atomic projections.

Quote-only monitoring reads at most three positions. Entry evaluation is targeted by the
information outbox. All executable fills require a quote observed after an intent. A
missing funding settlement delays accounting finality, never a risk exit or capital release.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from uuid import uuid4

import pandas as pd

from market_signal.models.domain import utcnow
from market_signal.paper.v2 import engine as baseline_engine
from market_signal.paper.v2 import spec as baseline
from market_signal.paper.v2.data import clean, ts
from market_signal.research.lab.common import canonical_json, content_id

from . import spec
from .model import ExecutionThesis, build_thesis, economics, fill, sign


def rows(store, sql, args=()):
    cur = store.con.execute(sql, args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def runs(store):
    return rows(store, "SELECT * FROM paper_nimble_runs ORDER BY activated_at")


def current(store):
    rs = runs(store)
    if not rs:
        raise ValueError("nimble run is not activated")
    return rs[-1]["run_id"]


def register(store, *, now=None):
    at = ts(now or utcnow())
    store.con.execute(
        "INSERT INTO paper_nimble_policies VALUES (?,?,?) ON CONFLICT DO NOTHING",
        [spec.policy_id(), at.to_pydatetime(), canonical_json(spec.definition())],
    )
    return {
        "policy_id": spec.policy_id(),
        "risk_policy_id": spec.risk_id(),
        "hypotheses": 28,
        "version": spec.VERSION,
        "mode": "paper",
    }


def load(store, rid):
    run = rows(store, "SELECT * FROM paper_nimble_runs WHERE run_id=?", [rid])
    if not run:
        raise ValueError("unknown nimble run")
    run = run[0]
    p = store.con.execute(
        "SELECT definition FROM paper_nimble_policies WHERE policy_id=?", [run["policy_id"]]
    ).fetchone()
    if (
        run["policy_id"] != spec.policy_id()
        or not p
        or canonical_json(json.loads(p[0])) != canonical_json(spec.definition())
        or run["risk_policy_id"] != spec.risk_id()
    ):
        raise ValueError("frozen nimble policy identity/content mismatch")
    return run


def account(store, rid):
    r = store.con.execute(
        "SELECT payload FROM paper_nimble_accounts WHERE run_id=?", [rid]
    ).fetchone()
    if not r:
        raise ValueError("account projection missing; recover from ledger")
    return json.loads(r[0])


def positions(store, rid, *, terminal=False):
    return [
        json.loads(r[0])
        for r in store.con.execute(
            "SELECT payload FROM paper_nimble_positions WHERE run_id=? "
            + ("" if terminal else "AND state IN ('PENDING','OPEN','EXIT_PENDING') ")
            + "ORDER BY trade_id",
            [rid],
        ).fetchall()
    ]


def state(store, rid):
    ac = account(store, rid)
    ps = positions(store, rid, terminal=True)
    return {
        **ac,
        "open": {p["trade_id"]: p for p in ps if p["state"] in ("OPEN", "EXIT_PENDING")},
        "pending": {p["trade_id"]: p for p in ps if p["state"] == "PENDING"},
        "closed": [p for p in ps if p["state"] == "CLOSED"],
    }


def _account_write(store, rid, ac, now):
    store.con.execute(
        "INSERT INTO paper_nimble_accounts VALUES (?,?,?) ON CONFLICT(run_id) "
        "DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
        [rid, ts(now).to_pydatetime(), canonical_json(clean(ac))],
    )


def _project(store, rid, kind, p, now):
    ac = account(store, rid)
    if kind == "position_mark":
        old = store.con.execute(
            "SELECT payload FROM paper_nimble_positions WHERE run_id=? AND trade_id=?",
            [rid, p["trade_id"]],
        ).fetchone()
        if not old:
            raise ValueError("position mark without an opening ledger event")
        p = {**json.loads(old[0]), **p}
    elif p.get("thesis_id") and "thesis" not in p:
        frozen = store.con.execute(
            "SELECT payload FROM paper_nimble_theses WHERE thesis_id=?", [p["thesis_id"]]
        ).fetchone()
        if not frozen:
            raise ValueError("frozen entry thesis is missing")
        th = ExecutionThesis.model_validate(json.loads(frozen[0]))
        if th.thesis_id != p["thesis_id"]:
            raise ValueError("frozen entry thesis content mismatch")
        p = {**p, "thesis": th.model_dump(mode="json")}

    if kind in (
        "pending",
        "opened",
        "exit_intent",
        "closed",
        "cancelled",
        "position_mark",
        "funding_adjustment",
    ):
        store.con.execute(
            "INSERT INTO paper_nimble_positions VALUES (?,?,?,?,?) "
            "ON CONFLICT(run_id,trade_id) DO UPDATE SET state=excluded.state,"
            "updated_at=excluded.updated_at,payload=excluded.payload",
            [rid, p["trade_id"], p["state"], ts(now).to_pydatetime(), canonical_json(clean(p))],
        )
    if kind == "opened":
        ac["cash"] -= p["entry_fee"]
    elif kind == "closed":
        ac["cash"] += p["net_pnl"] + p["entry_fee"]
    elif kind == "funding_adjustment":
        ac["cash"] += p["funding_cash_adjustment"]
        ac["equity"] += p["funding_cash_adjustment"]
    elif kind == "account_mark":
        ac.update(p)
    elif kind == "run_state":
        ac["status"] = p["status"]
        ac["stop_reason"] = p["reason"]
        ac["stop_at"] = ts(now).isoformat()
    ac["seq"] += 1
    _account_write(store, rid, ac, now)


def event(store, rid, kind, key, p, now):
    if store.con.execute(
        "SELECT 1 FROM paper_nimble_events WHERE run_id=? AND event_key=?", [rid, key]
    ).fetchone():
        return False
    seq = account(store, rid)["seq"] + 1
    store.con.execute(
        "INSERT INTO paper_nimble_events VALUES (?,?,?,?,?,?)",
        [
            rid,
            seq,
            key,
            kind,
            ts(now).to_pydatetime(),
            canonical_json(clean({k: v for k, v in p.items() if k != "thesis"})),
        ],
    )
    _project(store, rid, kind, p, now)
    return True


def baseline_snapshot(store):
    rs = baseline_engine.runs(store)
    if not rs:
        return {"classification": "EXPLORATORY_FIXED_HORIZON_BASELINE", "run": None}
    r = rs[-1]
    snapshot = {
        "classification": "EXPLORATORY_FIXED_HORIZON_BASELINE",
        "run": clean(r),
        "definition": baseline.definition(),
        "prefixes": {},
    }
    for table, order in (
        ("paper_v2_events", "seq"),
        ("paper_v2_opportunities", "evaluated_at,opportunity_id"),
        ("paper_v2_evaluations", "evaluated_at,evaluation_id"),
    ):
        rs = rows(store, f"SELECT * FROM {table} WHERE run_id=? ORDER BY {order}", [r["run_id"]])
        snapshot["prefixes"][table] = {
            "rows": len(rs),
            "order": order,
            "digest": content_id("", clean(rs)),
        }
    return snapshot


def verify_baseline(store, rid):
    snap = json.loads(load(store, rid)["definition"])["baseline"]
    if not snap["run"]:
        return {"verified": True, "baseline": None}
    old = rows(store, "SELECT * FROM paper_v2_runs WHERE run_id=?", [snap["run"]["run_id"]])[0]
    if canonical_json(clean(old)) != canonical_json(snap["run"]):
        raise ValueError("baseline run definition changed")
    for table, p in snap["prefixes"].items():
        rs = rows(
            store,
            f"SELECT * FROM {table} WHERE run_id=? ORDER BY {p['order']} LIMIT ?",
            [old["run_id"], p["rows"]],
        )
        if content_id("", clean(rs)) != p["digest"]:
            raise ValueError("baseline evidence changed: " + table)
    return {
        "verified": True,
        "baseline_run_id": old["run_id"],
        "classification": snap["classification"],
    }


def create(store, *, now=None, comparison_days=7):
    at = ts(now or utcnow())
    p = store.con.execute(
        "SELECT registered_at,definition FROM paper_nimble_policies WHERE policy_id=?",
        [spec.policy_id()],
    ).fetchone()
    if (
        not p
        or at < ts(p[0])
        or canonical_json(json.loads(p[1])) != canonical_json(spec.definition())
    ):
        raise ValueError("register the released frozen nimble policy first")
    for r in runs(store):
        if account(store, r["run_id"])["status"] == "ACTIVE" or positions(store, r["run_id"]):
            raise ValueError("a nimble run is already active or draining")
    d = {
        "version": spec.VERSION,
        "mode": "paper",
        "activated_at": at.isoformat(),
        "nonce": uuid4().hex,
        "policy_id": spec.policy_id(),
        "risk_policy_id": spec.risk_id(),
        "thesis_policy_version": spec.THESIS_VERSION,
        "exit_policy_version": spec.EXIT_VERSION,
        "entry_universe_id": baseline.universe_id(),
        "baseline": baseline_snapshot(store),
        "comparison_review_at": (at + pd.Timedelta(days=comparison_days)).isoformat(),
    }
    rid = content_id("paperrun_nimble_", d)
    with store.transaction():
        store.con.execute(
            "INSERT INTO paper_nimble_runs VALUES (?,?,?,?,?,?)",
            [rid, at.to_pydatetime(), "paper", spec.policy_id(), spec.risk_id(), canonical_json(d)],
        )
        _account_write(
            store,
            rid,
            {
                "status": "ACTIVE",
                "cash": 100.0,
                "equity": 100.0,
                "peak": 100.0,
                "drawdown": 0.0,
                "seq": 0,
                "gross_notional": 0.0,
                "degraded": [],
            },
            at,
        )
        event(store, rid, "activation", "activation", d, at)
    return {"run_id": rid, **d, "starting_equity": 100.0}


def recover(store, rid):
    load(store, rid)
    with store.transaction():
        store.con.execute("DELETE FROM paper_nimble_positions WHERE run_id=?", [rid])
        _account_write(
            store,
            rid,
            {
                "status": "ACTIVE",
                "cash": 100.0,
                "equity": 100.0,
                "peak": 100.0,
                "drawdown": 0.0,
                "seq": 0,
                "gross_notional": 0.0,
                "degraded": [],
            },
            utcnow(),
        )
        for r in rows(
            store, "SELECT * FROM paper_nimble_events WHERE run_id=? ORDER BY seq", [rid]
        ):
            _project(store, rid, r["event_type"], json.loads(r["payload"]), r["recorded_at"])
    return {"recovered": True, "seq": account(store, rid)["seq"]}


def kill(store, rid, *, reason, now=None):
    if not reason.strip():
        raise ValueError("stop reason required")
    load(store, rid)
    with store.transaction():
        event(
            store,
            rid,
            "run_state",
            "admin_stop",
            {"status": "STOPPED", "reason": "ADMIN_STOP", "detail": reason},
            now or utcnow(),
        )
    return {"state": "STOPPED", "closing": "next future executable quote"}


def _rates(rates, now):
    return [r for r in rates if math.isfinite(r.rate) and ts(r.at) <= ts(r.available_at) <= ts(now)]


def funding(position, rates, now, predicted=0.0):
    hours = pd.date_range(
        ts(position["entry_at"]).floor("h") + pd.Timedelta(hours=1), ts(now).floor("h"), freq="h"
    )
    found = {ts(r.at): r.rate for r in _rates(rates, now) if r.asset == position["asset"]}
    missing = [h.isoformat() for h in hours if h not in found]
    known = sign(position["side"]) * position["notional"] * sum(found.get(h, 0) for h in hours)
    reserve = len(missing) * position["notional"] * max(0.0, sign(position["side"]) * predicted)
    next_at = ts(now).floor("h") + pd.Timedelta(hours=1)
    marginal = sign(position["side"]) * position["notional"] * predicted
    return {
        "current_rate": predicted,
        "next_settlement_at": next_at.isoformat(),
        "known_funding": known,
        "missing_settlements": missing,
        "adverse_reserve": reserve,
        "cumulative_paid_received": known,
        "estimated_next_cost_credit": marginal,
        "marginal_holding_cost": marginal,
    }


def _state_invalid(p, updates, now):
    th = p["thesis"]
    rule = th["invalidation"]["state_rule"]
    s = sign(p["side"])
    for u in sorted(updates, key=lambda u: ts(u.available_at)):
        if u.asset != p["asset"] or not ts(p["entry_at"]) < ts(u.observed_at) <= ts(
            u.available_at
        ) <= ts(now):
            continue
        v = u.values
        if rule == "event_reaction" and (
            v.get("event_active") is False or v.get("event_confidence") == "DENIED"
        ):
            return u, "EVENT_STATE_INVALIDATED"
        if rule in ("flow_reversal_two_minutes", "event_reaction"):
            f = v.get("flow_fractions", [])
            if (
                len(f) == 2
                and all(x is not None and s * x <= -0.35 for x in f)
                and ts(u.observed_at) - pd.Timedelta(minutes=2) >= ts(p["entry_at"])
            ):
                return u, "FLOW_REVERSAL"
        elif rule == "absorption_defence_lost":
            defended = "bid5" if s > 0 else "ask5"
            original = th["microstructure_state"].get(defended)
            depth = v.get(defended)
            mid = v.get("mid")
            if (
                original
                and depth is not None
                and depth < 0.5 * original
                and mid is not None
                and s * (mid - p["entry_ref"]) < 0
            ):
                return u, "ABSORPTION_DEFENCE_LOST"
        elif rule == "crowding_normalized":
            if (
                v.get("crowding") == "neutral"
                and v.get("mark")
                and s * (v["mark"] - p["entry_ref"]) <= 0
            ):
                return u, "CROWDING_NORMALIZED_WITHOUT_RESPONSE"
        elif rule == "oi_expansion_lost":
            if v.get("oi_change_1h_pct") is not None and v["oi_change_1h_pct"] <= 0:
                return u, "OI_EXPANSION_LOST"
    return None, None


def exit_condition(p, q, updates, now, predicted=0.0, status="ACTIVE", stop_reason="ADMIN_STOP"):
    """Pure, cheap deterministic decision. Wicks are observations, never executable fills."""
    if p["state"] != "OPEN":
        return None
    th = p["thesis"]
    s = sign(p["side"])
    entry = ts(p["entry_at"])
    at = ts(now)
    reason = detail = observed = available = None
    ordering = "OBSERVED"
    if status != "ACTIVE":
        reason, detail = (
            "ADMIN_STOP" if stop_reason == "ADMIN_STOP" else "CATASTROPHE_GUARD",
            stop_reason,
        )
    if q and q.valid(now):
        observed, available = q.at, q.available_at
        px = q.bid if s > 0 else q.ask
        progress = s * (px - p["entry_ref"])
        target = s * (th["objective"]["price"] - p["entry_ref"])
        tp = s * (px - th["objective"]["price"]) >= 0
        invalid = s * (px - th["invalidation"]["price"]) <= 0
        emergency = s * (px - th["invalidation"]["emergency_price"]) <= 0
        # Include only a full observed interval after entry; a pre-entry wick is unknowable exposure.
        if (
            q.high is not None
            and q.low is not None
            and q.interval_start
            and ts(q.interval_start) >= entry
        ):
            tp_touch = s * ((q.high if s > 0 else q.low) - th["objective"]["price"]) >= 0
            bad_touch = s * ((q.low if s > 0 else q.high) - th["invalidation"]["price"]) <= 0
            if tp_touch and bad_touch:
                invalid = True
                ordering = "AMBIGUOUS"
        if reason is None:
            if emergency:
                reason, detail = "CATASTROPHE_GUARD", "EMERGENCY_PRICE"
            elif invalid:
                reason, detail = "INVALIDATED", "PRICE_LEVEL"
            elif tp:
                reason, detail = "TAKE_PROFIT", "OBJECTIVE_REACHED"
            else:
                u, why = _state_invalid(p, updates, now)
                if u:
                    reason, detail = "INVALIDATED", why
                    observed, available = u.observed_at, u.available_at
                elif (
                    at - ts(th["trigger_at"])
                    >= pd.Timedelta(minutes=th["expiry"]["reaction_window_minutes"])
                    and progress < target * th["expiry"]["minimum_progress_target_fraction"]
                ):
                    reason, detail = "THESIS_EXPIRED", "REACTION_FAILED"
                elif at - entry >= pd.Timedelta(minutes=th["max_hold_minutes"]):
                    reason, detail = "MAX_HOLD", "BACKSTOP"
                else:
                    remaining = max(0.0, s * (th["objective"]["price"] - px) / p["entry_ref"])
                    next_hour = at.floor("h") + pd.Timedelta(hours=1)
                    stale = (at - ts(th["trigger_at"])).total_seconds() >= th["expiry"][
                        "reaction_window_minutes"
                    ] * 60 * 0.5
                    if (
                        stale
                        and progress < target * 0.8
                        and (next_hour - at).total_seconds() <= 120
                        and s * predicted > 0
                        and s * predicted > remaining * 0.25
                    ):
                        reason, detail = "COST_DECAY", "ADVERSE_UPCOMING_FUNDING"
    elif at - ts(p.get("last_quote_received_at", p["entry_at"])) >= pd.Timedelta(seconds=180):
        reason, detail = "DATA_FAILURE", "EXECUTABLE_QUOTE_STALE"
    # State/expiry/backstop decisions do not need a quote to queue a close on data failure.
    if reason is None and at - entry >= pd.Timedelta(minutes=th["max_hold_minutes"]):
        reason, detail = "MAX_HOLD", "BACKSTOP_WITHOUT_QUOTE"
    if reason:
        return {
            "exit_reason": reason,
            "exit_detail": detail,
            "ordering": ordering,
            "condition_observed_at": observed or at.isoformat(),
            "condition_available_at": available or at.isoformat(),
            "evaluator_at": at.isoformat(),
            "exit_intent_at": at.isoformat(),
            "funding_decision_rate": predicted,
        }
    return None


def _quote(qs, asset, now):
    valid = [q for q in qs if q.asset == asset and q.valid(now)]
    return max(valid, key=lambda q: (ts(q.available_at), ts(q.at))) if valid else None


def _predicted(asset, contexts, updates, p=None):
    c = contexts.get(asset, {})
    r = c.get("funding_rate")
    if r is None:
        r = next(
            (
                u.values.get("funding_rate")
                for u in reversed(updates)
                if u.asset == asset and u.values.get("funding_rate") is not None
            ),
            None,
        )
    if r is None and p:
        r = p.get("funding_state", {}).get("current_rate")
    return r if r is not None and math.isfinite(r) else None


def _mark(store, rid, qs, rates, updates, contexts, now):
    ac = account(store, rid)
    floating = gross = net = 0.0
    for p in positions(store, rid):
        if p["state"] == "PENDING":
            continue
        q = _quote(qs, p["asset"], now)
        px = q.mid if q else p.get("mark_ref", p["entry_ref"])
        rate = _predicted(p["asset"], contexts, updates, p) or 0.0
        fs = funding(p, rates, now, rate)
        unrealised = (
            max(-p["margin"], sign(p["side"]) * p["units"] * (px - p["entry_fill"]))
            - fs["known_funding"]
            - fs["adverse_reserve"]
        )
        floating += unrealised
        gross += p["units"] * px
        net += sign(p["side"]) * p["units"] * px
        favourable = sign(p["side"]) * (px / p["entry_ref"] - 1)
        changed = {
            "trade_id": p["trade_id"],
            "state": p["state"],
            "mark_ref": px,
            "unrealised": unrealised,
            "funding_state": fs,
            "mfe": max(p.get("mfe", 0), favourable),
            "mae": min(p.get("mae", 0), favourable),
            "last_quote_received_at": (q.received_at or q.available_at)
            if q
            else p.get("last_quote_received_at", p["entry_at"]),
        }
        event(
            store,
            rid,
            "position_mark",
            "mark:" + p["trade_id"] + ":" + ts(now).isoformat(),
            changed,
            now,
        )
    equity = ac["cash"] + floating
    peak = max(ac["peak"], equity)
    event(
        store,
        rid,
        "account_mark",
        "account:" + ts(now).isoformat(),
        {
            "equity": equity,
            "peak": peak,
            "drawdown": 1 - equity / peak,
            "max_drawdown": max(ac.get("max_drawdown", 0.0), 1 - equity / peak),
            "gross_notional": gross,
            "net_notional": net,
            "long_notional": (gross + net) / 2,
            "short_notional": (gross - net) / 2,
        },
        now,
    )
    if equity <= peak * (1 - spec.RISK["catastrophe_drawdown"]) and ac["status"] == "ACTIVE":
        event(
            store,
            rid,
            "run_state",
            "catastrophe",
            {"status": "STOPPED", "reason": "CATASTROPHE_GUARD"},
            now,
        )


def _close(store, rid, p, q, rates, now, rate):
    xf = fill(q, p["side"], entry=False)
    fee = p["units"] * xf * spec.EXECUTION["fee_bps"] / 10000
    fs = funding(p, rates, now, rate)
    price_pnl = sign(p["side"]) * p["units"] * (q.mid - p["entry_ref"])
    slip = p["units"] * (abs(p["entry_fill"] - p["entry_ref"]) + abs(xf - q.mid))
    gross = sign(p["side"]) * p["units"] * (xf - p["entry_fill"])
    uncapped_net = gross - p["entry_fee"] - fee - fs["known_funding"] - fs["adverse_reserve"]
    liquidation_adjustment = max(0.0, -p["margin"] - p["entry_fee"] - uncapped_net)
    gross += liquidation_adjustment
    net = uncapped_net + liquidation_adjustment
    move = sign(p["side"]) * (q.mid / p["entry_ref"] - 1)
    mfe = max(p.get("mfe", 0), move)
    mae = min(p.get("mae", 0), move)
    closed = {
        **p,
        "state": "CLOSED",
        "exit_at": ts(now).isoformat(),
        "exit_quote_at": q.at,
        "exit_quote_available_at": q.available_at,
        "exit_quote": asdict(q),
        "exit_fill": xf,
        "exit_ref": q.mid,
        "fill_at": ts(now).isoformat(),
        "exit_fee": fee,
        "fees": p["entry_fee"] + fee,
        "price_pnl": price_pnl,
        "liquidation_adjustment": liquidation_adjustment,
        "margin_loss_capped": bool(liquidation_adjustment),
        "gross_pnl": gross,
        "slippage_cost": slip,
        "funding": fs["known_funding"],
        "funding_reserve": fs["adverse_reserve"],
        "funding_state": fs,
        "net_pnl": net,
        "accounting_final": not fs["missing_settlements"],
        "trade_return": net / p["notional"],
        "hold_minutes": (ts(now) - ts(p["entry_at"])).total_seconds() / 60,
        "mfe": mfe,
        "mae": mae,
        "mfe_capture_ratio": move / mfe if move > 0 and mfe > 0 else None,
        "mae_avoidance": None,
        "metric_note": "research-path metrics finalized separately after frozen horizons",
        "exit_latency_seconds": (ts(now) - ts(p["condition_available_at"])).total_seconds(),
        "decision_to_fill_seconds": (ts(now) - ts(p["exit_intent_at"])).total_seconds(),
    }
    event(store, rid, "closed", "closed:" + p["trade_id"], closed, now)


def reconcile(store, rid, rates, now):
    # Only pending-accounting closes; no scan/replay of settled trade history on the hot path.
    ps = store.con.execute(
        "SELECT payload FROM paper_nimble_positions WHERE run_id=? AND state='CLOSED' "
        "AND json_extract(payload,'$.accounting_final')=false",
        [rid],
    ).fetchall()
    for (raw,) in ps:
        p = json.loads(raw)
        fs = funding(p, rates, p["exit_at"], p["funding_state"]["current_rate"])
        # Settlements may become available after exit; use NOW for availability filtering only.
        hours = pd.date_range(
            ts(p["entry_at"]).floor("h") + pd.Timedelta(hours=1),
            ts(p["exit_at"]).floor("h"),
            freq="h",
        )
        found = {ts(r.at): r.rate for r in _rates(rates, now) if r.asset == p["asset"]}
        missing = [h.isoformat() for h in hours if h not in found]
        known = sign(p["side"]) * p["notional"] * sum(found.get(h, 0) for h in hours)
        reserve = len(missing) * p["notional"] * max(0, sign(p["side"]) * fs["current_rate"])
        raw_gross = p["gross_pnl"] - p.get("liquidation_adjustment", 0.0)
        uncapped_net = raw_gross - p["fees"] - known - reserve
        adjustment = max(0.0, -p["margin"] - p["entry_fee"] - uncapped_net)
        adjusted_net = uncapped_net + adjustment
        if missing == p["funding_state"]["missing_settlements"] and known == p["funding"]:
            continue
        fixed = {
            **p,
            "funding": known,
            "gross_pnl": raw_gross + adjustment,
            "liquidation_adjustment": adjustment,
            "margin_loss_capped": bool(adjustment),
            "funding_reserve": reserve,
            "net_pnl": adjusted_net,
            "funding_cash_adjustment": adjusted_net - p["net_pnl"],
            "accounting_final": not missing,
            "trade_return": adjusted_net / p["notional"],
            "funding_state": {
                **fs,
                "known_funding": known,
                "missing_settlements": missing,
                "adverse_reserve": reserve,
            },
        }
        key = (
            "funding:" + p["trade_id"] + ":" + content_id("", {"missing": missing, "known": known})
        )
        event(store, rid, "funding_adjustment", key, fixed, now)


def monitor(store, rid, qs, rates=(), updates=(), contexts=None, *, now=None, mark=True):
    at = ts(now or utcnow())
    contexts = contexts or {}
    load(store, rid)
    ac = account(store, rid)
    for p in positions(store, rid):
        q = _quote(qs, p["asset"], at)
        rate = _predicted(p["asset"], contexts, updates, p) or 0.0
        if p["state"] == "PENDING":
            reason = None
            if ac["status"] != "ACTIVE":
                reason = "ADMIN_STOP"
            elif at - ts(p["intent_at"]) > pd.Timedelta(seconds=60):
                reason = "EXECUTION_EXPIRED"
            elif q and ts(q.at) > ts(p["intent_at"]) and ts(q.available_at) > ts(p["intent_at"]):
                th = ExecutionThesis.model_validate(p["thesis"])
                e = economics(th, q, rate)
                if not e["economic"]:
                    reason = "UNECONOMIC_TARGET"
                elif sign(p["side"]) * (q.mid - th.invalidation["price"]) <= 0:
                    reason = "INVALIDATED_BEFORE_FILL"
                else:
                    xf = fill(q, p["side"], entry=True)
                    units, ntl = baseline_engine.size(account(store, rid)["equity"], xf, p["asset"])
                    active = [x for x in positions(store, rid) if x["state"] != "PENDING"]
                    gross = sum(x["notional"] for x in active)
                    if ntl < spec.RISK["min_notional"]:
                        reason = "MIN_NOTIONAL"
                    elif (
                        gross + ntl
                        > account(store, rid)["equity"] * spec.RISK["gross_fraction"] + 1e-9
                    ):
                        reason = "EXPOSURE_CAP"
                    else:
                        opened = {
                            **p,
                            "state": "OPEN",
                            "entry_at": at.isoformat(),
                            "entry_quote_at": q.at,
                            "entry_quote_available_at": q.available_at,
                            "entry_quote": asdict(q),
                            "entry_fill": xf,
                            "entry_ref": q.mid,
                            "units": units,
                            "notional": ntl,
                            "margin": ntl / spec.RISK["leverage"],
                            "entry_fee": ntl * spec.EXECUTION["fee_bps"] / 10000,
                            "entry_economics": e,
                            "mfe": 0.0,
                            "mae": 0.0,
                            "funding_state": {"current_rate": rate},
                            "last_quote_received_at": q.received_at or q.available_at,
                            "entry_latency_seconds": (
                                at - ts(p["evidence_available_at"])
                            ).total_seconds(),
                            "intent_to_fill_seconds": (at - ts(p["intent_at"])).total_seconds(),
                        }
                        event(store, rid, "opened", "opened:" + p["trade_id"], opened, at)
            if reason:
                event(
                    store,
                    rid,
                    "cancelled",
                    "cancel:" + p["trade_id"],
                    {**p, "state": "CANCELLED", "reason": reason},
                    at,
                )
        elif p["state"] == "EXIT_PENDING":
            if (
                q
                and ts(q.at) > ts(p["exit_intent_at"])
                and ts(q.available_at) > ts(p["exit_intent_at"])
            ):
                _close(store, rid, p, q, rates, at, rate)
        else:
            condition = exit_condition(
                p, q, updates, at, rate, ac["status"], ac.get("stop_reason", "ADMIN_STOP")
            )
            if condition:
                event(
                    store,
                    rid,
                    "exit_intent",
                    "exit:" + p["trade_id"],
                    {**p, "state": "EXIT_PENDING", **condition},
                    at,
                )
    if mark:
        _mark(store, rid, qs, rates, updates, contexts, at)


def admit(store, rid, observations, qs, source, contexts=None, *, now=None):
    at = ts(now or utcnow())
    run = load(store, rid)
    activation = ts(run["activated_at"])
    hs = {h.hypothesis_id: h for h in baseline.bootstrap()}
    contexts = contexts or {}
    candidates = []
    for o in observations:
        if ts(o.signal_at) <= activation:
            continue
        if store.con.execute(
            "SELECT 1 FROM paper_nimble_opportunities WHERE run_id=? AND hypothesis_id=? "
            "AND asset=? AND signal_at=?",
            [rid, o.hypothesis_id, o.asset, ts(o.signal_at).to_pydatetime()],
        ).fetchone():
            continue
        h = hs.get(o.hypothesis_id)
        reason = None
        if not h or o.asset not in baseline.COINS:
            reason = "HYPOTHESIS_NOT_REGISTERED"
        elif not o.causal or not ts(o.signal_at) <= ts(o.available_at) <= at:
            reason = "INVALID_TIMING"
        elif o.intended_side and o.intended_side != h.side:
            reason = "SIDE_MISMATCH"
        elif not o.healthy:
            # Unready dependency is retriable at the SAME source instant after ingestion.
            continue
        elif not o.warm:
            reason = "FEATURE_WARMUP"
        elif not o.context_available:
            reason = "CONTEXT_UNAVAILABLE"
        elif not o.fired:
            reason = "NO_SIGNAL"
        elif at - ts(o.signal_at) >= pd.Timedelta(
            minutes=min(20, spec.mapping(h)["expiry_minutes"])
        ):
            reason = "THESIS_ALREADY_STALE"
        candidates.append({"h": h, "o": o, "reason": reason})
    opposing = {
        a
        for a in {c["o"].asset for c in candidates}
        if len(
            {
                c["h"].side
                for c in candidates
                if c["o"].asset == a and c["h"] and c["reason"] is None
            }
        )
        > 1
    }
    results = {}
    for c in sorted(
        candidates,
        key=lambda c: content_id("", [c["o"].asset, c["o"].hypothesis_id, c["o"].signal_at]),
    ):
        h, o, reason = c["h"], c["o"], c["reason"]
        tid = thesis = None
        admission = "REJECTED"
        ac = account(store, rid)
        oid = content_id(
            "nimbleopportunity_",
            {"run": rid, "hypothesis": o.hypothesis_id, "asset": o.asset, "signal": o.signal_at},
        )
        qs_asset = _quote(qs, o.asset, at)
        if reason is None:
            reason = (
                "CATASTROPHE_GUARD"
                if ac["status"] != "ACTIVE"
                else "HYPOTHESIS_DEGRADED"
                if o.hypothesis_id in ac.get("degraded", [])
                else "CONFLICT"
                if o.asset in opposing
                else None
            )
        if reason is None:
            last = store.con.execute(
                "SELECT max(evaluated_at) FROM paper_nimble_opportunities WHERE run_id=? "
                "AND hypothesis_id=? AND asset=? AND admission='ADMITTED'",
                [rid, o.hypothesis_id, o.asset],
            ).fetchone()[0]
            if last and at - ts(last) < pd.Timedelta(minutes=spec.mapping(h)["cooldown_minutes"]):
                reason = "COOLDOWN"
        active = positions(store, rid)
        existing = next((p for p in active if p["asset"] == o.asset), None)
        if reason is None and existing:
            if existing["side"] != h.side:
                reason = "CONFLICT_EXISTING_POSITION"
            else:
                admission = "SUPPORTED"
                tid = existing["trade_id"]
                event(
                    store,
                    rid,
                    "support",
                    "support:" + oid,
                    {
                        "trade_id": tid,
                        "hypothesis_id": h.hypothesis_id,
                        "evidence_available_at": o.available_at,
                        "context": o.context,
                        "episode": "existing_exposure_unchanged",
                    },
                    at,
                )
        elif reason is None:
            if len(active) >= spec.RISK["max_positions"]:
                reason = "MAX_POSITIONS"
            elif (
                sum(p.get("notional", p.get("target_notional", 0)) for p in active)
                + ac["equity"] * 0.2
                > ac["equity"] * 0.6 + 1e-8
            ):
                reason = "EXPOSURE_CAP"
            elif qs_asset is None:
                reason = "EXECUTABLE_QUOTE_UNAVAILABLE"
            else:
                rate = _predicted(o.asset, contexts, ())
                if rate is None:
                    p = (o.evidence or {}).get("positioning", {})
                    rate = p.get("predicted_funding_hourly")
                if rate is None or not math.isfinite(rate):
                    reason = "FUNDING_UNAVAILABLE"
                else:
                    try:
                        geometry = source.geometry(o.asset, o)
                        if geometry.get("available_at") and ts(geometry["available_at"]) > at:
                            raise ValueError("INVALID_TIMING")
                        thesis = build_thesis(h, o, qs_asset, geometry, at)
                        e = economics(thesis, qs_asset, rate)
                        if not e["economic"]:
                            reason = "UNECONOMIC_TARGET"
                        elif (
                            sign(h.side)
                            * (
                                thesis.invalidation["price"]
                                - thesis.invalidation["emergency_price"]
                            )
                            <= 0
                        ):
                            reason = "UNBOUNDED_INVALIDATION"
                        else:
                            _, ntl = baseline_engine.size(
                                ac["equity"], fill(qs_asset, h.side, entry=True), o.asset
                            )
                            if ntl < spec.RISK["min_notional"]:
                                reason = "MIN_NOTIONAL"
                            elif store.con.execute(
                                "SELECT 1 FROM paper_nimble_theses WHERE run_id=? "
                                "AND json_extract_string(payload,'$.episode_id')=?",
                                [rid, thesis.episode_id],
                            ).fetchone():
                                reason = "DUPLICATE_EPISODE"
                            else:
                                tid = content_id(
                                    "papertrade_nimble_", {"run": rid, "opportunity": oid}
                                )
                                td = thesis.model_dump(mode="json")
                                store.con.execute(
                                    "INSERT INTO paper_nimble_theses VALUES (?,?,?,?,?)",
                                    [
                                        thesis.thesis_id,
                                        rid,
                                        tid,
                                        at.to_pydatetime(),
                                        canonical_json(td),
                                    ],
                                )
                                event(
                                    store,
                                    rid,
                                    "pending",
                                    "pending:" + tid,
                                    {
                                        "state": "PENDING",
                                        "trade_id": tid,
                                        "opportunity_id": oid,
                                        "hypothesis_id": h.hypothesis_id,
                                        "asset": o.asset,
                                        "side": h.side,
                                        "family": h.family,
                                        "thesis_id": thesis.thesis_id,
                                        "thesis": td,
                                        "trigger_at": o.signal_at,
                                        "evidence_available_at": o.available_at,
                                        "evaluator_noticed_at": at.isoformat(),
                                        "intent_at": at.isoformat(),
                                        "target_notional": ac["equity"] * 0.2,
                                        "equity_at_decision": ac["equity"],
                                        "economics": e,
                                        "risk_policy_id": spec.risk_id(),
                                        "funding_state": {"current_rate": rate},
                                    },
                                    at,
                                )
                                admission = "ADMITTED"
                    except ValueError as exc:
                        if str(exc) not in (
                            "GEOMETRY_UNAVAILABLE",
                            "INVALID_GEOMETRY",
                            "INVALID_TIMING",
                        ):
                            raise
                        reason = str(exc)
        if reason == "NO_SIGNAL":
            admission = "NO_SIGNAL"
        payload = clean(
            {
                **asdict(o),
                "context": o.context
                if o.fired
                else {"snapshot_id": (o.context or {}).get("snapshot_id")},
                "side": h.side if h else o.intended_side,
                "admission": admission,
                "reason": reason,
                "trade_id": tid,
                "hypothesis": h.model_dump(mode="json") if h else None,
                "research_primary_minutes": h.horizon_minutes if h else None,
                "research_horizons_minutes": baseline.HORIZONS,
                "evaluated_at": at.isoformat(),
                "execution_thesis_id": thesis.thesis_id if thesis else None,
                "capital_blockers": [
                    {
                        "trade_id": p["trade_id"],
                        "since": p.get("entry_at", p["intent_at"]),
                        "notional": p.get("notional", p.get("target_notional")),
                        "state": p["state"],
                        "stale_at_block": (
                            at - ts(p["thesis"]["trigger_at"])
                            >= pd.Timedelta(
                                minutes=p["thesis"]["expiry"]["reaction_window_minutes"]
                            )
                        )
                        and sign(p["side"])
                        * (
                            p.get("mark_ref", p["thesis"]["entry_reference_price"])
                            - p["thesis"]["entry_reference_price"]
                        )
                        < 0.25
                        * sign(p["side"])
                        * (
                            p["thesis"]["objective"]["price"] - p["thesis"]["entry_reference_price"]
                        ),
                        "expiry_at": (
                            ts(p["thesis"]["trigger_at"])
                            + pd.Timedelta(minutes=p["thesis"]["expiry"]["reaction_window_minutes"])
                        ).isoformat(),
                    }
                    for p in active
                ]
                if reason in ("MAX_POSITIONS", "EXPOSURE_CAP", "CONFLICT_EXISTING_POSITION")
                else [],
            }
        )
        store.con.execute(
            "INSERT INTO paper_nimble_opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                oid,
                rid,
                o.hypothesis_id,
                o.asset,
                ts(o.signal_at).to_pydatetime(),
                ts(o.available_at).to_pydatetime(),
                at.to_pydatetime(),
                o.fired,
                admission,
                reason,
                tid,
                canonical_json(payload),
            ],
        )
        if o.fired and h and o.causal and ts(o.available_at) <= at and qs_asset:
            # Descriptive signal curve; reference is the known quote at notice, never a historical fill.
            curve = {
                "asset": o.asset,
                "side": h.side,
                "reference": qs_asset.mid,
                "reference_at": at.isoformat(),
                "reference_quote_at": qs_asset.at,
                "hypothesis_id": h.hypothesis_id,
                "primary_minutes": h.horizon_minutes,
                "horizons": sorted(set((*baseline.HORIZONS, h.horizon_minutes))),
                "completed": [],
            }
            store.con.execute(
                "INSERT INTO paper_nimble_research_pending VALUES (?,?,?,?)",
                [oid, rid, at.to_pydatetime(), canonical_json(curve)],
            )
        results[admission] = results.get(admission, 0) + 1
    return results


def research(store, rid, quotes, *, now=None):
    """Forward descriptive returns outlive execution positions, including rejected opportunities."""
    at = ts(now or utcnow())
    pending = rows(store, "SELECT * FROM paper_nimble_research_pending WHERE run_id=?", [rid])
    for row in pending:
        p = json.loads(row["payload"])
        done = set(p["completed"])
        path = sorted(
            [
                q
                for q in quotes
                if q.asset == p["asset"]
                and q.valid(at, fresh=False)
                and ts(q.at) > ts(p["reference_at"])
            ],
            key=lambda q: ts(q.at),
        )
        for horizon in p["horizons"]:
            if horizon in done:
                continue
            due = ts(p["reference_at"]) + pd.Timedelta(minutes=horizon)
            ends = [q for q in path if ts(q.at) >= due]
            if not ends:
                continue
            q = ends[0]
            s = sign(p["side"])
            ref = p["reference"]
            used = [
                x
                for x in path
                if ts(x.at) <= ts(q.at)
                and (x.interval_start is None or ts(x.interval_start) >= ts(p["reference_at"]))
            ]
            favourable = [
                s * ((x.high if s > 0 else x.low) / ref - 1)
                for x in used
                if x.high is not None and x.low is not None
            ]
            adverse = [
                s * ((x.low if s > 0 else x.high) / ref - 1)
                for x in used
                if x.high is not None and x.low is not None
            ]
            result = {
                "kind": "DESCRIPTIVE_FORWARD_RETURN",
                "hypothesis_id": p["hypothesis_id"],
                "research_horizon_minutes": horizon,
                "primary": horizon == p["primary_minutes"],
                "reference_at": p["reference_at"],
                "reference_quote_at": p["reference_quote_at"],
                "reference_price": ref,
                "observed_at": q.at,
                "available_at": q.available_at,
                "forward_return": s * (q.mid / ref - 1),
                "mfe": max([0, *favourable]),
                "mae": min([0, *adverse]),
                "observation_delay_seconds": (ts(q.at) - due).total_seconds(),
                "execution_exit_independent": True,
                "missing_path": len(used) < horizon - 1,
            }
            store.con.execute(
                "INSERT INTO paper_nimble_research VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    row["opportunity_id"],
                    horizon,
                    ts(q.at).to_pydatetime(),
                    ts(q.available_at).to_pydatetime(),
                    canonical_json(result),
                ],
            )
            done.add(horizon)
        if done == set(p["horizons"]):
            store.con.execute(
                "DELETE FROM paper_nimble_research_pending WHERE opportunity_id=?",
                [row["opportunity_id"]],
            )
        elif done != set(p["completed"]):
            p["completed"] = sorted(done)
            store.con.execute(
                "UPDATE paper_nimble_research_pending SET payload=? WHERE opportunity_id=?",
                [canonical_json(p), row["opportunity_id"]],
            )


def degrade(store, rid, now):
    rule = spec.EXECUTION["degradation"]
    ac = account(store, rid)
    degraded = list(ac.get("degraded", []))
    ps = [
        json.loads(r[0])
        for r in store.con.execute(
            "SELECT payload FROM paper_nimble_positions "
            "WHERE run_id=? AND state='CLOSED' AND json_extract(payload,'$.accounting_final')=true "
            "ORDER BY json_extract_string(payload,'$.exit_at')",
            [rid],
        ).fetchall()
    ]
    for hid in {p["hypothesis_id"] for p in ps} - set(degraded):
        trades = [p for p in ps if p["hypothesis_id"] == hid]
        recent = trades[-rule["recent"] :]
        if (
            len(trades) >= rule["minimum_trades"]
            and sum(p["trade_return"] for p in recent) / len(recent) <= rule["mean_net"]
            and sum(p["net_pnl"] < 0 for p in recent) >= rule["losses"]
        ):
            degraded.append(hid)
    if degraded != ac.get("degraded", []):
        event(
            store,
            rid,
            "account_mark",
            "degradation:" + ts(now).isoformat(),
            {"degraded": sorted(degraded)},
            now,
        )
