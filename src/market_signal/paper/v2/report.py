"""Read-only explanatory paper reports; research and execution evidence stay separate."""

from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta

import pandas as pd

from market_signal.models.domain import Timeframe, utcnow
from market_signal.research.lab.common import canonical_json, content_id

from . import engine, spec
from .data import ts


def opportunities(store, rid, *, rejected=False, start=None, end=None, asset=None) -> list[dict]:
    args = [rid]
    where = "o.run_id=?"
    for clause, val in (
        ("o.evaluated_at >= ?", start),
        ("o.evaluated_at < ?", end),
        ("o.asset=?", asset),
    ):
        if val is not None:
            where += " AND " + clause
            args.append(ts(val).to_pydatetime() if clause != "o.asset=?" else val)
    if rejected:
        where += " AND o.admission='REJECTED'"
    out = engine.rows(
        store,
        "SELECT o.opportunity_id,o.payload,f.payload AS outcome FROM "
        "paper_v2_opportunities o LEFT JOIN paper_v2_outcomes f USING(opportunity_id) WHERE "
        + where
        + " ORDER BY o.evaluated_at,o.opportunity_id",
        args,
    )
    return [
        {
            "opportunity_id": r["opportunity_id"],
            **json.loads(r["payload"]),
            "outcome": json.loads(r["outcome"]) if r["outcome"] else None,
        }
        for r in out
    ]


def hypotheses(store, rid) -> dict:
    states = engine.state(store, rid)["hypotheses"]
    return {
        "universe_id": engine.load(store, rid)["universe_id"],
        "hypotheses": [
            {
                "hypothesis_id": h.hypothesis_id,
                **h.model_dump(mode="json"),
                "cooldown_minutes": h.cooldown_minutes,
                "lifecycle": states.get(h.hypothesis_id, {"state": "ENABLED_EXPLORATORY"}),
                "scientific_evidence_is_separate": True,
            }
            for h in spec.bootstrap()
        ],
        "long": sum(h.side == "long" for h in spec.bootstrap()),
        "short": sum(h.side == "short" for h in spec.bootstrap()),
    }


def trades(store, rid) -> list[dict]:
    st = engine.state(store, rid)
    return (
        [{**p, "state": "CLOSED"} for p in st["closed"]]
        + [{**p, "state": "OPEN"} for p in st["open"].values()]
        + [{**p, "state": "PENDING"} for p in st["pending"].values()]
    )


def explain(store, rid, tid) -> dict:
    trade = next((t for t in trades(store, rid) if t["trade_id"] == tid), None)
    if trade is None:
        raise ValueError("trade not found")
    support = opportunities(store, rid)
    support = [s for s in support if s["trade_id"] == tid]
    snapshots = {}
    for o in support:
        sid = o.get("context_snapshot_id")
        row = store.con.execute(
            "SELECT payload FROM paper_v2_context_snapshots WHERE snapshot_id=?", [sid]
        ).fetchone()
        if row:
            snapshots[sid] = json.loads(row[0])
    return {
        "context_snapshots": snapshots,
        "paper_only": True,
        "trade": trade,
        "primary_trigger": trade["primary_hypothesis_id"],
        "supporting_observations": support,
        "answer": f"PAPER V2 {trade['side'].upper()} {trade['asset']}: registered deterministic "
        f"hypothesis; {trade['evidence']['maturity']} scientific maturity; "
        f"{trade['horizon_minutes']}m fixed horizon",
        "scientific_confirmation": "Paper admission/PnL grants no scientific or live approval.",
    }


def _stats(st, signals, since, end):
    opened = [
        p
        for p in list(st["open"].values()) + st["closed"]
        if ts(since) <= ts(p["entry_at"]) < ts(end)
    ]
    closed = [p for p in st["closed"] if ts(since) <= ts(p["exit_at"]) < ts(end)]
    fired = [o for o in signals if o["fired"]]
    rejected = [o for o in signals if o["admission"] == "REJECTED"]
    return {
        "signals": len(fired),
        "trades": len(opened),
        "long": sum(p["side"] == "long" for p in opened),
        "short": sum(p["side"] == "short" for p in opened),
        "admissions": sum(o["admission"] == "ADMITTED" for o in signals),
        "support_events": sum(o["admission"] == "SUPPORTED" for o in signals),
        "rejected": len([o for o in rejected if o["fired"]]),
        "rejections": dict(Counter(o["rejection_reason"] for o in rejected)),
        "no_signal": sum(o["admission"] == "NO_SIGNAL" for o in signals),
        "realised_pnl": sum(p["net_pnl"] for p in closed),
        "fees": sum(p["exit_fee"] for p in closed) + sum(p["entry_fee"] for p in opened),
        "funding": sum(p["funding"] for p in closed),
        "win": sum(p["net_pnl"] > 0 for p in closed),
        "loss": sum(p["net_pnl"] <= 0 for p in closed),
        "mfe_mean": sum(p["mfe"] or 0 for p in closed) / len(closed) if closed else None,
        "mae_mean": sum(p["mae"] or 0 for p in closed) / len(closed) if closed else None,
        "families": dict(
            Counter((o["hypothesis"] or {}).get("family", "unregistered") for o in fired)
        ),
        "regimes": dict(
            Counter(canonical_json((o.get("context") or {}).get("market", {})) for o in fired)
        ),
        "side_audit": {
            side: {
                "signals": sum(o["side"] == side for o in fired),
                "admissions": sum(
                    o["side"] == side and o["admission"] == "ADMITTED" for o in signals
                ),
                "rejections": dict(
                    Counter(o["rejection_reason"] for o in rejected if o["side"] == side)
                ),
            }
            for side in ("long", "short")
        },
    }


def status(store, rid, *, now=None) -> dict:
    at = ts(now or utcnow())
    run = engine.load(store, rid)
    st = engine.state(store, rid)
    signal_rows = opportunities(store, rid, start=at - timedelta(days=1), end=at)
    summary = _stats(st, signal_rows, at - timedelta(days=1), at)
    evals = engine.rows(
        store,
        "SELECT * FROM paper_v2_evaluations WHERE run_id=? ORDER BY evaluated_at DESC LIMIT 100",
        [rid],
    )
    failure_count = 0
    for e in evals:
        if e["status"] == "ok":
            break
        failure_count += 1
    last = json.loads(evals[0]["payload"]) if evals else {}
    dependencies = last.get("dependencies", {}).get("sources", {})
    stale = [k for k, v in dependencies.items() if not v["healthy"]]
    mark = st["marks"][-1] if st["marks"] else {}
    healthy = (
        bool(evals)
        and failure_count == 0
        and at - ts(evals[0]["evaluated_at"]) <= timedelta(minutes=30)
    )
    return {
        "run_id": rid,
        "mode": "paper",
        "active": st["status"] == "ACTIVE",
        "state": st["status"],
        "healthy": healthy,
        "dependency_health": "DEGRADED" if stale else "HEALTHY",
        "failure_count": failure_count,
        "last_evaluation": last.get("evaluated_at"),
        "last_trade": max(
            (p["entry_at"] for p in list(st["open"].values()) + st["closed"]), default=None
        ),
        "activation_at": ts(run["activated_at"]).isoformat(),
        "equity": st["equity"],
        "starting_equity": 100,
        "realised_pnl": sum(p["net_pnl"] for p in st["closed"]),
        "unrealised_pnl": sum(v["unrealised"] for v in mark.get("positions", {}).values()),
        "max_drawdown": max((m["drawdown"] for m in st["marks"]), default=0),
        "exposure": {
            k: mark.get(k, 0)
            for k in (
                "gross_notional",
                "net_notional",
                "long_notional",
                "short_notional",
                "directional_concentration",
            )
        },
        "btc_beta_concentration": None,
        "concentration_note": "no causal beta estimate in this paper model; same-direction gross is visible",
        "open_positions": list(st["open"].values()),
        "pending": list(st["pending"].values()),
        "hypotheses_active": len(spec.bootstrap()) - len(st["hypotheses"]),
        "data_dependencies": dependencies,
        "stale_dependencies": stale,
        "missing_funding": mark.get("missing_funding", []),
        "provisional_equity": mark.get("provisional", False),
        "last_24h": summary,
        "runtime": {k: last.get(k) for k in ("wall_seconds", "cpu_seconds", "peak_rss_mb")},
        "db_rows": {
            t: store.con.execute(f"SELECT count(*) FROM {t} WHERE run_id=?", [rid]).fetchone()[0]
            for t in ("paper_v2_opportunities", "paper_v2_events", "paper_v2_evaluations")
        },
        "db_storage": {
            "shared_database_bytes": store.path.stat().st_size,
            "shared_wal_bytes": (
                store.path.with_suffix(store.path.suffix + ".wal").stat().st_size
                if store.path.with_suffix(store.path.suffix + ".wal").exists()
                else 0
            ),
            "run_payload_bytes": sum(
                store.con.execute(
                    f"SELECT coalesce(sum(octet_length(encode(payload))),0) FROM {t} WHERE run_id=?",
                    [rid],
                ).fetchone()[0]
                for t in ("paper_v2_opportunities", "paper_v2_events", "paper_v2_evaluations")
            ),
            "note": "shared file sizes include all research; payload bytes exclude indexes and shared contexts",
        },
    }


def frequency(store, rid, *, now=None) -> dict:
    at = ts(now or utcnow())
    start = ts(engine.load(store, rid)["activated_at"])
    st = engine.state(store, rid)
    obs = opportunities(store, rid)
    days = pd.date_range(start.floor("D"), at.floor("D"), freq="D")
    trade_counts = Counter(
        ts(p["entry_at"]).floor("D") for p in list(st["open"].values()) + st["closed"]
    )
    n = max(len(days), 1)
    opened = list(st["open"].values()) + st["closed"]
    return {
        "calendar_days_including_partial": n,
        "signals_per_day": sum(o["fired"] for o in obs) / n,
        "trades_per_day": len(opened) / n,
        "long_per_day": sum(p["side"] == "long" for p in opened) / n,
        "short_per_day": sum(p["side"] == "short" for p in opened) / n,
        "zero_trade_day_share": sum(trade_counts[d] == 0 for d in days) / n,
        "days_ge_share": {
            str(k): sum(trade_counts[d] >= k for d in days) / n for k in (1, 3, 5, 10)
        },
        "diagnostic_target": 5,
        "target_is_quota": False,
        "bottleneck": "inspect no_signal, dependency health and rejection counts"
        if len(opened) / n < 1
        else None,
    }


def postmortem(store, rid, *, start, end, asset="BTC") -> dict:
    from market_signal.intraday.bars import load_bars

    start, end = ts(start), ts(end)
    if end <= start:
        raise ValueError("end must follow start")
    if asset not in spec.COINS:
        raise ValueError("asset outside frozen universe")
    b = load_bars(
        store, "hyperliquid", asset, Timeframe.H1, start=start - timedelta(days=1), end=end
    )
    move, episodes = None, []
    if len(b):
        chosen = b[(b["close_time"] >= start) & (b["close_time"] <= end)]
        if len(chosen):
            move = float(chosen.iloc[-1]["close"] / chosen.iloc[0]["open"] - 1)
        prices = {ts(r["close_time"]): r["close"] for r in b.to_dict("records")}
        for t, price in sorted(prices.items()):
            prev = prices.get(t - timedelta(days=1))
            if t < start or not prev:
                continue
            ret = price / prev - 1
            threshold = 0.05 if asset == "BTC" else 0.10
            if abs(ret) >= threshold:
                episodes.append(
                    {
                        "at": t.isoformat(),
                        "return_24h": ret,
                        "direction": "UP" if ret > 0 else "DOWN",
                        "threshold": "BTC_8%"
                        if asset == "BTC" and abs(ret) >= 0.08
                        else "BTC_5%"
                        if asset == "BTC"
                        else "ASSET_10%",
                    }
                )
    obs = opportunities(store, rid, start=start, end=end, asset=asset)
    counts = Counter(o["rejection_reason"] or o["admission"] for o in obs)
    available = [h for h in hypotheses(store, rid)["hypotheses"]]
    activation = ts(engine.load(store, rid)["activated_at"])
    return {
        "paper_only": True,
        "analysis_only_thresholds": True,
        "asset": asset,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "move": move,
        "major_moves": episodes,
        "hypotheses": available,
        "observations": obs,
        "reasons": dict(counts),
        "signal_count": sum(o["fired"] for o in obs),
        "admitted": sum(o["admission"] == "ADMITTED" for o in obs),
        "participation": sum(o["admission"] in ("ADMITTED", "SUPPORTED") for o in obs if o["fired"])
        / max(1, sum(o["fired"] for o in obs)),
        "coverage": {
            "activation": activation.isoformat(),
            "period_precedes_activation": start < activation,
            "observed_decisions": len(obs),
            "evaluated_instants": len({o["evaluated_at"] for o in obs}),
            "note": "Missing evaluations are unobserved, never inferred NO_SIGNAL; pre-activation signals/trades are never fabricated.",
        },
        "explanation": "All recorded decisions and frozen-horizon rejected outcomes are shown. No retrospective trades.",
    }


def daily(store, rid, *, day=None, now=None) -> dict:
    at = ts(now or utcnow())
    start = ts(day) if day else at.floor("D") - timedelta(days=1)
    start = start.floor("D")
    end = start + timedelta(days=1)
    st = engine.state(store, rid)
    obs = opportunities(store, rid, start=start, end=end)
    marks = [m for m in st["marks"] if ts(m["at"]) < end]
    earlier = [m for m in marks if ts(m["at"]) < start]
    equity = marks[-1]["equity"] if marks else 100
    pnl = equity - (earlier[-1]["equity"] if earlier else 100)
    stats = _stats(st, obs, start, end)
    moves = [postmortem(store, rid, start=start, end=end, asset=a) for a in spec.COINS]
    largest = max(
        moves, key=lambda x: max((abs(m["return_24h"]) for m in x["major_moves"]), default=0)
    )
    summary = {
        "run_id": rid,
        "day": start.date().isoformat(),
        "equity": equity,
        "daily_pnl": pnl,
        **stats,
        "frequency": frequency(store, rid, now=end),
        "hypotheses_active": len(spec.bootstrap()) - len(st["hypotheses"]),
        "largest_missed_move": largest if largest["major_moves"] else None,
        "open_positions": [
            p
            for p in list(st["open"].values()) + st["closed"]
            if ts(p["entry_at"]) < end and (p.get("exit_at") is None or ts(p["exit_at"]) >= end)
        ],
    }
    recent_start = at - timedelta(days=1)
    recent = _stats(st, opportunities(store, rid, start=recent_start, end=at), recent_start, at)
    summary["telegram_snapshot"] = {
        "as_of": at.isoformat(),
        "equity": st["equity"],
        "open_positions": len(st["open"]),
        "last_24h": recent,
    }
    reasons = (
        ", ".join(f"{k} {v}" for k, v in Counter(recent["rejections"]).most_common(3)) or "none"
    )
    summary["telegram"] = (
        f"PAPER V2 · {summary['day']}\nEquity {st['equity']:.2f} simulated USDC · day {pnl:+.2f}\n"
        f"Open {len(st['open'])} · trades {recent['trades']} (L {recent['long']} / S {recent['short']}) · last 24h\n"
        f"Hypotheses {summary['hypotheses_active']} · signals {recent['signals']} · rejected {recent['rejected']}\n"
        f"Blocks: {reasons}\nPaper execution evidence; scientific maturity remains separate."
    )
    return summary


def compare_v1(store, rid, *, now=None) -> dict:
    from market_signal.paper.retirement import digests, frozen

    old = store.con.execute(
        "SELECT run_id FROM paper_retirements ORDER BY retired_at DESC LIMIT 1"
    ).fetchone()
    if not old:
        raise ValueError("retire v1 first to freeze comparison")
    baseline = frozen(store, old[0])
    if baseline["historical_digests"] != digests(store, old[0]):
        raise ValueError("v1 history changed after retirement")
    at = ts(now or utcnow())
    s = status(store, rid, now=at)
    f = frequency(store, rid, now=at)
    sts = engine.state(store, rid)
    v1days = (
        ts(baseline["retired_at"]) - ts(baseline["definition"]["created_at"])
    ).total_seconds() / 86400
    v1 = {
        "run_id": old[0],
        "state": "RETIRED",
        "days_running": v1days,
        "signals": baseline["signals"],
        "trades": baseline["entered"],
        "trades_per_day": baseline["entered"] / max(v1days, 1e-9),
        "long": sum(
            x.get("closed_trades", 0) for x in baseline["contributions"]["strategies"].values()
        ),
        "short": 0,
        "opportunity_participation": None,
        **{
            k: baseline[k]
            for k in (
                "equity",
                "realised_pnl",
                "unrealised_pnl",
                "fees",
                "max_drawdown",
                "avg_exposure",
            )
        },
    }
    # Count side from the frozen definition and per-strategy contributions, including short runs.
    for side in ("long", "short"):
        v1[side] = sum(
            baseline["contributions"]["strategies"][m["strategy_name"]]["entered"]
            for m in baseline["definition"]["cohort"]
            if m["side"] == side
        )
    all_obs = opportunities(store, rid)
    v2 = {
        "run_id": rid,
        "days_running": (at - ts(s["activation_at"])).total_seconds() / 86400,
        "signals": sum(o["fired"] for o in all_obs),
        "trades": len(sts["open"]) + len(sts["closed"]),
        "long": sum(p["side"] == "long" for p in list(sts["open"].values()) + sts["closed"]),
        "short": sum(p["side"] == "short" for p in list(sts["open"].values()) + sts["closed"]),
        "opportunity_participation": sum(
            o["admission"] in ("ADMITTED", "SUPPORTED") for o in all_obs if o["fired"]
        )
        / max(1, sum(o["fired"] for o in all_obs)),
        "fees": sum(p["fees"] for p in sts["closed"])
        + sum(p["entry_fee"] for p in sts["open"].values()),
        **{
            k: s[k]
            for k in ("equity", "realised_pnl", "unrealised_pnl", "max_drawdown", "exposure")
        },
        **f,
    }
    return {
        "v1": v1,
        "v2": v2,
        "scientific_evidence": "separate from execution; neither return proves alpha",
    }


def record_daily(store, rid, *, now=None, sender=None) -> dict:
    at = ts(now or utcnow())
    day = at.floor("D") - timedelta(days=1)
    if day < ts(engine.load(store, rid)["activated_at"]).floor("D"):
        return {"recorded": False, "reason": "no completed prospective day"}
    report_id = content_id("paperreport_v2_", {"run_id": rid, "day": day.date().isoformat()})
    row = store.con.execute(
        "SELECT payload FROM paper_v2_reports WHERE report_id=?", [report_id]
    ).fetchone()
    payload = json.loads(row[0]) if row else daily(store, rid, day=day, now=at)
    if row is None:
        store.con.execute(
            "INSERT INTO paper_v2_reports VALUES (?,?,?,?,?)",
            [report_id, rid, day.date(), at.to_pydatetime(), canonical_json(payload)],
        )
    delivery = "not_requested"
    if sender is not None:
        exists = store.con.execute(
            "SELECT 1 FROM paper_v2_deliveries WHERE report_id=?", [report_id]
        ).fetchone()
        if exists:
            delivery = "already_attempted"
        else:
            store.con.execute(
                "INSERT INTO paper_v2_deliveries VALUES (?,?,?,?)",
                [report_id, "attempted", at.to_pydatetime(), None],
            )
            try:
                sender(payload["telegram"])
                delivery, detail = "sent", None
            except Exception as exc:
                delivery, detail = "failed", f"{type(exc).__name__}: {exc}"
            store.con.execute(
                "INSERT INTO paper_v2_deliveries VALUES (?,?,?,?)",
                [report_id, delivery, ts(utcnow()).to_pydatetime(), detail],
            )
    return {
        "report_id": report_id,
        "recorded": row is None,
        "delivery": delivery,
        "payload": payload,
    }
