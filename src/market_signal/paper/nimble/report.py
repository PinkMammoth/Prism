"""Execution evidence, cost attribution, latency, capital blocking and baseline comparison."""

from __future__ import annotations

import json
from collections import Counter

import pandas as pd

from market_signal.models.domain import utcnow
from market_signal.paper.v2 import report as baseline_report
from market_signal.paper.v2 import spec as baseline
from market_signal.paper.v2.data import clean, ts
from market_signal.research.lab.common import canonical_json, content_id

from . import engine, spec


def trades(store, rid):
    ps = engine.positions(store, rid, terminal=True)
    out = []
    for p in ps:
        if p["state"] != "CLOSED":
            out.append(p)
            continue
        curves = store.con.execute(
            "SELECT horizon_minutes,payload FROM paper_nimble_research WHERE opportunity_id=?",
            [p["opportunity_id"]],
        ).fetchall()
        rs = {h: json.loads(v) for h, v in curves}
        primary = next(
            h.horizon_minutes
            for h in engine.hypotheses(store, rid)
            if h.hypothesis_id == p["hypothesis_id"]
        )
        reference = rs.get(primary)
        out.append(
            {
                **p,
                "research_forward_curves": rs,
                **capture_metrics(p, reference),
            }
        )
    return out


def capture_metrics(p, reference):
    """Shared Phase 27 capture accounting for complete original primary research paths."""
    # Signal research and fill time can differ: normalize to the trade's entry reference.
    capture = avoid = None
    if reference and not reference["missing_path"]:
        s = engine.sign(p["side"])
        r = reference["reference_price"] / p["entry_ref"]
        whole_mfe = max(0.0, s * (r - 1) + reference["mfe"] * r)
        whole_mae = min(0.0, s * (r - 1) + reference["mae"] * r)
        realized = s * (p["exit_ref"] / p["entry_ref"] - 1)
        capture = realized / whole_mfe if realized > 0 and whole_mfe > 0 else None
        avoid = max(0.0, p["mae"] - whole_mae)
    return {
        "mfe_capture_ratio": capture,
        "mae_avoidance": avoid,
        "capture_metrics_basis": "complete_original_primary_research_path_or_unavailable",
    }


def signals(store, rid):
    return [
        json.loads(r[0])
        for r in store.con.execute(
            "SELECT payload FROM paper_nimble_opportunities "
            "WHERE run_id=? AND fired ORDER BY evaluated_at,opportunity_id",
            [rid],
        ).fetchall()
    ]


def metrics(ps, *, equity=100.0, days=1.0):
    closed = [p for p in ps if p["state"] == "CLOSED"]
    net = [p["net_pnl"] for p in closed]
    wins = sum(p > 0 for p in net)
    positive = sum(max(0, p) for p in net)
    negative = -sum(min(0, p) for p in net)
    return {
        "trades": len(closed),
        "price_pnl": sum(
            p.get("price_pnl", p.get("gross_pnl", 0) + p.get("slippage_cost", 0)) for p in closed
        ),
        "gross_pnl": sum(p["gross_pnl"] for p in closed),
        "net_pnl": sum(net),
        "liquidation_adjustment": sum(p.get("liquidation_adjustment", 0) for p in closed),
        "fees": sum(p["fees"] for p in closed),
        "slippage": sum(p["slippage_cost"] for p in closed),
        "funding": sum(p["funding"] for p in closed),
        "unsettled_adverse_reserve": sum(p.get("funding_reserve", 0) for p in closed),
        "provisional_trades": sum(not p.get("accounting_final", True) for p in closed),
        "win_rate": wins / len(closed) if closed else None,
        "profit_factor": positive / negative if negative else None,
        "expectancy_per_trade": sum(net) / len(closed) if closed else None,
        "expectancy_per_day": sum(net) / days,
        "average_hold_minutes": sum(
            p.get("hold_minutes", (ts(p["exit_at"]) - ts(p["entry_at"])).total_seconds() / 60)
            for p in closed
        )
        / len(closed)
        if closed
        else None,
        "turnover_notional": sum(p["notional"] + p["units"] * p["exit_fill"] for p in closed),
        "capital_utilization": sum(
            p["notional"] * (ts(p["exit_at"]) - ts(p["entry_at"])).total_seconds() for p in closed
        )
        / (days * 86400 * equity),
        "exit_reasons": dict(Counter(p["exit_reason"] for p in closed)),
        "mfe_capture_ratio_mean": _mean([p.get("mfe_capture_ratio") for p in closed]),
        "mae_avoidance_mean": _mean([p.get("mae_avoidance") for p in closed]),
        "entry_latency_seconds": _latencies([p.get("entry_latency_seconds") for p in closed]),
        "exit_latency_seconds": _latencies([p.get("exit_latency_seconds") for p in closed]),
    }


def _mean(values):
    xs = [x for x in values if x is not None]
    return sum(xs) / len(xs) if xs else None


def _latencies(values):
    xs = sorted(x for x in values if x is not None)
    return {
        "samples": len(xs),
        "median": xs[len(xs) // 2] if xs else None,
        "p95": xs[min(len(xs) - 1, int(len(xs) * 0.95))] if xs else None,
        "max": max(xs) if xs else None,
    }


def status(store, rid):
    r = engine.load(store, rid)
    ac = engine.account(store, rid)
    ps = engine.positions(store, rid)
    return clean(
        {
            "run_id": rid,
            "activation_at": r["activated_at"],
            "mode": "paper",
            "version": json.loads(r["definition"])["version"],
            "execution_policy_id": r["policy_id"],
            "risk_policy_id": r["risk_policy_id"],
            "thesis_policy_version": spec.THESIS_VERSION,
            "exit_policy_version": spec.EXIT_VERSION,
            "hypotheses": len(engine.hypotheses(store, rid)),
            "entry_universe_id": json.loads(r["definition"])["entry_universe_id"],
            **ac,
            "positions": ps,
            "baseline": engine.verify_baseline(store, rid),
            "pending_research": store.con.execute(
                "SELECT count(*) FROM paper_nimble_research_pending WHERE run_id=?", [rid]
            ).fetchone()[0],
            "latency_note": "observed quote-only decisions; runtime lock queue can delay fills",
        }
    )


def daily(store, rid, *, day=None):
    start = ts(day or (ts(utcnow()).floor("D") - pd.Timedelta(days=1)))
    end = start + pd.Timedelta(days=1)
    ps = [
        p for p in trades(store, rid) if p["state"] == "CLOSED" and start <= ts(p["exit_at"]) < end
    ]
    ss = [o for o in signals(store, rid) if start <= ts(o["evaluated_at"]) < end]
    ac = engine.account(store, rid)
    return {
        "mode": "paper",
        "run_id": rid,
        "day": start.date().isoformat(),
        **metrics(ps),
        "signals": len(ss),
        "rejections": dict(Counter(o["reason"] for o in ss if o["reason"])),
        "blocked_opportunities": sum(bool(o.get("capital_blockers")) for o in ss),
        "stale_capital_blockers": sum(
            any(b.get("stale_at_block", False) for b in o.get("capital_blockers", [])) for o in ss
        ),
        "drawdown": ac["drawdown"],
        "account_equity": ac["equity"],
        "open_positions": len(engine.positions(store, rid)),
        "evidence_lifecycle": "EXPLORATORY_EXECUTION_ONLY_NO_SCIENTIFIC_GRADUATION",
    }


def compare(store, rid):
    r = engine.load(store, rid)
    d = json.loads(r["definition"])
    now = ts(utcnow())
    start = ts(r["activated_at"])
    days = max((now - start).total_seconds() / 86400, 1 / 86400)
    ps = trades(store, rid)
    base = d["baseline"]["run"]
    fixed = baseline_report.trades(store, base["run_id"]) if base else []
    # Shared prospective interval, alongside all collected baseline history for transparency.
    matched = [
        {**p, "state": "CLOSED"}
        for p in fixed
        if p.get("exit_at") and ts(p["decision_at"]) >= start
    ]
    nimble = [p for p in ps if p["state"] == "CLOSED"]
    return {
        "window": {
            "start": start.isoformat(),
            "end": now.isoformat(),
            "review_at": d["comparison_review_at"],
        },
        "baseline_integrity": engine.verify_baseline(store, rid),
        "nimble": metrics(nimble, days=days),
        "fixed_horizon_v2": metrics(matched, days=days),
        "baseline_prior_closed_trades": sum(
            bool(p.get("exit_at")) and ts(p["decision_at"]) < start for p in fixed
        ),
        "nimble_drawdown": engine.account(store, rid).get(
            "max_drawdown", engine.account(store, rid)["drawdown"]
        ),
        "comparison_note": "original 28 entry definitions preserved; successor adds separate event cohort where present; descriptive prospective execution evidence",
        "baseline_run_id": base["run_id"] if base else None,
    }


def postmortem(store, rid, *, start, end, asset="BTC"):
    lo, hi = ts(start), ts(end)
    asset = asset.upper()
    ss = [
        o for o in signals(store, rid) if o["asset"] == asset and lo <= ts(o["evaluated_at"]) <= hi
    ]
    ps = [
        p
        for p in trades(store, rid)
        if p["asset"] == asset
        and p.get("entry_at")
        and ts(p["entry_at"]) <= hi
        and (not p.get("exit_at") or ts(p["exit_at"]) >= lo)
    ]
    q = store.con.execute(
        "SELECT min(low_px),max(high_px) FROM microstructure_minutes WHERE coin=? "
        "AND minute_open>=? AND minute_open<? AND status='COMPLETE' AND ingested_at<=?",
        [asset, lo.to_pydatetime(), hi.to_pydatetime(), hi.to_pydatetime()],
    ).fetchone()
    decisions = engine.rows(
        store,
        "SELECT recorded_at,payload FROM paper_nimble_events WHERE run_id=? "
        "AND event_type='exit_intent' AND recorded_at BETWEEN ? AND ?",
        [rid, lo.to_pydatetime(), hi.to_pydatetime()],
    )
    return clean(
        {
            "run_id": rid,
            "asset": asset,
            "start": lo,
            "end": hi,
            "observed_low_high": q,
            "signals": ss,
            "entries_exits": ps,
            "execution_metrics": metrics(ps),
            "dynamic_exit_decisions": [
                json.loads(d["payload"])
                for d in decisions
                if json.loads(d["payload"])["asset"] == asset
            ],
            "blocked_opportunities": [o for o in ss if o.get("capital_blockers")],
            "exited_before_window_end": [
                p["trade_id"] for p in ps if p.get("exit_at") and ts(p["exit_at"]) < hi
            ],
            "held_through_window_end": [
                p["trade_id"] for p in ps if not p.get("exit_at") or ts(p["exit_at"]) >= hi
            ],
            "note": "observed market range and realized capture; no hindsight execution or counterfactual alpha claim",
        }
    )


def record_daily(store, rid, *, sender=None, day=None, trade_details=False):
    p = daily(store, rid, day=day)
    report_id = content_id("nimblereport_", {"run": rid, "day": p["day"]})
    prior = store.con.execute(
        "SELECT payload FROM paper_nimble_reports WHERE report_id=?", [report_id]
    ).fetchone()
    if prior:
        p = json.loads(prior[0])
    else:
        store.con.execute(
            "INSERT INTO paper_nimble_reports VALUES (?,?,?,?,?)",
            [report_id, rid, p["day"], utcnow(), canonical_json(p)],
        )
    delivery = "summary_only"
    if (
        sender
        and not store.con.execute(
            "SELECT 1 FROM paper_nimble_deliveries WHERE report_id=? AND state='attempted'",
            [report_id],
        ).fetchone()
    ):
        store.con.execute(
            "INSERT INTO paper_nimble_deliveries VALUES (?,?,?)", [report_id, "attempted", utcnow()]
        )
        text = (
            f"PRISM NIMBLE PAPER {p['day']}\n{p['trades']} closed; net ${p['net_pnl']:.4f}\n"
            f"price ${p['price_pnl']:.4f}, fees ${p['fees']:.4f}, spread/slip ${p['slippage']:.4f}, funding ${p['funding']:.4f}"
        )
        if trade_details:
            details = [
                t
                for t in trades(store, rid)
                if t.get("exit_at") and ts(t["exit_at"]).date().isoformat() == p["day"]
            ]
            text += "\n" + "\n".join(
                f"{t['asset']} {t['side']} {t['exit_reason']} ${t['net_pnl']:.4f}"
                for t in details[:30]
            )
        try:
            ok = sender(text)
            delivery = "failed" if ok is False else "sent"
        except Exception:
            delivery = "failed"
        store.con.execute(
            "INSERT INTO paper_nimble_deliveries VALUES (?,?,?)", [report_id, delivery, utcnow()]
        )
    return {
        "report_id": report_id,
        "report": p,
        "delivery": delivery,
        "per_trade_notifications": False,
    }


_DEFERRED = (
    "EXECUTABLE_QUOTE_UNAVAILABLE",
    "FUNDING_UNAVAILABLE",
    "VENUE_UNAVAILABLE",
    "THESIS_ALREADY_STALE",
    "INVALID_TIMING",
)


def _frontiers(store, now):
    """Latest known decision inputs per core asset, so a quiet run is attributable."""
    out = {}
    for coin in baseline.COINS:
        bar = store.con.execute(
            "SELECT max(close_time),max(first_observed_at) FROM perp_intraday_bars "
            "WHERE source='hyperliquid' AND timeframe='1h' AND coin=? AND first_observed_at<=?",
            [coin, now.to_pydatetime()],
        ).fetchone()
        oi = store.con.execute(
            "SELECT max(captured_at) FROM context_hl_oi_hourly WHERE coin=? AND captured_at<=?",
            [coin, now.to_pydatetime()],
        ).fetchone()[0]
        minute = store.con.execute(
            "SELECT max(minute_open) FROM microstructure_minutes WHERE coin=? "
            "AND status='COMPLETE' AND ingested_at<=?",
            [coin, now.to_pydatetime()],
        ).fetchone()[0]
        funding = store.con.execute(
            "SELECT max(time) FROM perp_funding WHERE source='hyperliquid' AND coin=? "
            "AND available_at<=?",
            [coin, now.to_pydatetime()],
        ).fetchone()[0]
        out[coin] = {
            "bar_1h_close": bar[0],
            "bar_1h_observed": bar[1],
            "oi_capture": oi,
            "complete_minute": minute,
            "settled_funding": funding,
        }
    return clean(
        {
            coin: {
                k: {"at": ts(v).isoformat(), "age_minutes": (now - ts(v)).total_seconds() / 60}
                if v is not None
                else None
                for k, v in values.items()
            }
            for coin, values in out.items()
        }
    )


def _worker_heartbeat(store, now):
    try:
        hb = json.loads((store.path.parent / "paper_nimble_worker.json").read_text())
    except (OSError, ValueError):
        return None
    return {
        "heartbeat_at": hb.get("heartbeat_at"),
        "age_seconds": (now - ts(hb["heartbeat_at"])).total_seconds()
        if hb.get("heartbeat_at")
        else None,
        "state": hb.get("state"),
    }


def funnel(store, rid, *, start=None, end=None):
    """Read-only opportunity funnel: evaluation -> signal -> admission -> fill -> exit -> accounts.

    Unevaluated grid instants are candidates whose required inputs never became ready while
    their grid was current (the engine retries them silently); input frontiers explain them.
    """
    run = engine.load(store, rid)
    activation = ts(run["activated_at"])
    now = ts(utcnow())
    lo = max(activation, ts(start)) if start else activation
    hi = min(now, ts(end)) if end else now
    hs = engine.hypotheses(store, rid)
    window = [lo.to_pydatetime(), hi.to_pydatetime()]
    rows = store.con.execute(
        "SELECT hypothesis_id,fired,admission,coalesce(rejection_reason,''),count(*) "
        "FROM paper_nimble_opportunities WHERE run_id=? AND evaluated_at>? AND evaluated_at<=? "
        "GROUP BY ALL",
        [rid, *window],
    ).fetchall()
    phase = {h.hypothesis_id: h.source_phase for h in hs}
    evaluated, fired, outcome = Counter(), Counter(), Counter()
    for hid, f, admission, reason, n in rows:
        evaluated[phase.get(hid, "other")] += n
        if f:
            fired[phase.get(hid, "other")] += n
            outcome[reason or admission] += n
        else:
            outcome["not_fired:" + (reason or admission)] += n
    expected = missed = 0
    for h in hs:
        step = pd.Timedelta(minutes=h.cadence_minutes)
        grid = pd.date_range(activation.floor(step) + step, hi - step, freq=step)
        grid = [g for g in grid if g > lo - step and g > activation]
        if not grid:
            continue
        seen = store.con.execute(
            "SELECT count(DISTINCT (asset,signal_at)) FROM paper_nimble_opportunities "
            "WHERE run_id=? AND hypothesis_id=? AND signal_at>=? AND signal_at<=?",
            [rid, h.hypothesis_id, grid[0].to_pydatetime(), grid[-1].to_pydatetime()],
        ).fetchone()[0]
        expected += len(grid) * len(baseline.COINS)
        missed += len(grid) * len(baseline.COINS) - seen
    ps = [
        p
        for p in engine.positions(store, rid, terminal=True)
        if lo < ts(p["intent_at"]) <= hi or p["state"] in ("PENDING", "OPEN", "EXIT_PENDING")
    ]
    opened = [p for p in ps if p.get("entry_at")]
    closed = [p for p in opened if p["state"] == "CLOSED"]
    m = metrics(closed, equity=100.0, days=max((hi - lo).total_seconds() / 86400, 1 / 1440))
    ac = engine.account(store, rid)
    return clean(
        {
            "mode": "paper",
            "run_id": rid,
            "policy_id": run["policy_id"],
            "version": json.loads(run["definition"])["version"],
            "window": {"start": lo.isoformat(), "end": hi.isoformat()},
            "evaluations": {
                "recorded": sum(evaluated.values()),
                "by_phase": dict(evaluated),
                "expected_grid_candidates": expected,
                "deferred_never_ready": missed,
            },
            "signals_fired": {"total": sum(fired.values()), "by_phase": dict(fired)},
            "outcomes": dict(sorted(outcome.items())),
            "economic_rejects": outcome.get("UNECONOMIC_TARGET", 0),
            "deferred_or_unavailable": {k: outcome.get(k, 0) for k in _DEFERRED},
            "admitted_intents": outcome.get("ADMITTED", 0),
            "supported_existing": outcome.get("SUPPORTED", 0),
            "intents": {
                "total": len(ps),
                "by_state": dict(Counter(p["state"] for p in ps)),
                "cancellations": dict(
                    Counter(p.get("reason") for p in ps if p["state"] == "CANCELLED")
                ),
                "execution_expired": sum(p.get("reason") == "EXECUTION_EXPIRED" for p in ps),
            },
            "fills": len(opened),
            "open_positions": [
                {
                    k: p.get(k)
                    for k in (
                        "trade_id",
                        "asset",
                        "side",
                        "hypothesis_id",
                        "state",
                        "entry_at",
                        "entry_fill",
                        "notional",
                        "unrealised",
                    )
                }
                for p in opened
                if p["state"] in ("OPEN", "EXIT_PENDING")
            ],
            "closes": {
                "total": len(closed),
                "exit_reasons": m["exit_reasons"],
                "accounting_final": sum(bool(p.get("accounting_final")) for p in closed),
                "accounting_provisional": sum(not p.get("accounting_final") for p in closed),
            },
            "pnl": {
                k: m[k]
                for k in (
                    "price_pnl",
                    "gross_pnl",
                    "net_pnl",
                    "fees",
                    "slippage",
                    "funding",
                    "unsettled_adverse_reserve",
                    "liquidation_adjustment",
                )
            },
            "latency_seconds": {
                "intent_to_fill": _latencies([p.get("intent_to_fill_seconds") for p in opened]),
                "evidence_to_fill": _latencies([p.get("entry_latency_seconds") for p in opened]),
                "exit_condition_to_fill": _latencies(
                    [p.get("exit_latency_seconds") for p in closed]
                ),
                "exit_intent_to_fill": _latencies(
                    [p.get("decision_to_fill_seconds") for p in closed]
                ),
            },
            "capital_utilisation": m["capital_utilization"],
            "account": {
                k: ac.get(k)
                for k in (
                    "status",
                    "cash",
                    "equity",
                    "peak",
                    "drawdown",
                    "max_drawdown",
                    "gross_notional",
                )
            },
            "worker": _worker_heartbeat(store, now),
            "input_frontiers": _frontiers(store, now),
            "interpretation": "exploratory execution evidence; signal evidence is separate "
            "(paper_nimble_research forward curves)",
        }
    )
