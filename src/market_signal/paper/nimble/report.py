"""Execution evidence, cost attribution, latency, capital blocking and baseline comparison."""

from __future__ import annotations

import json
from collections import Counter

import pandas as pd

from market_signal.models.domain import utcnow
from market_signal.paper.v2 import report as baseline_report
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
            for h in spec.baseline.bootstrap()
            if h.hypothesis_id == p["hypothesis_id"]
        )
        reference = rs.get(primary)
        # Ratios compare prices on the SAME entry reference. Signal research and fill time
        # can differ, so normalize extrema back to this trade's actual reference.
        capture = avoid = None
        if reference and not reference["missing_path"]:
            s = engine.sign(p["side"])
            r = reference["reference_price"] / p["entry_ref"]
            whole_mfe = max(0.0, s * (r - 1) + reference["mfe"] * r)
            whole_mae = min(0.0, s * (r - 1) + reference["mae"] * r)
            realized = s * (p["exit_ref"] / p["entry_ref"] - 1)
            capture = realized / whole_mfe if realized > 0 and whole_mfe > 0 else None
            avoid = max(0.0, p["mae"] - whole_mae)
        out.append(
            {
                **p,
                "research_forward_curves": rs,
                "mfe_capture_ratio": capture,
                "mae_avoidance": avoid,
                "capture_metrics_basis": "complete_original_primary_research_path_or_unavailable",
            }
        )
    return out


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
            "version": spec.VERSION,
            "execution_policy_id": r["policy_id"],
            "risk_policy_id": r["risk_policy_id"],
            "thesis_policy_version": spec.THESIS_VERSION,
            "exit_policy_version": spec.EXIT_VERSION,
            "hypotheses": 28,
            "entry_universe_id": spec.baseline.universe_id(),
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
        "comparison_note": "same 28 entry definitions; latency/fill/cost/exit treatment differ; descriptive prospective execution evidence",
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
