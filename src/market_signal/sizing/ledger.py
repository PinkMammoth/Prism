"""Source adapters. Read-only: a DuckDB file is opened with ``read_only=True`` and never written.

Use an offline backup copy, never the live authoritative database. Runs are never pooled
silently: every record carries its run id and the run's execution policy identity.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from market_signal.research.lab.common import content_id

from .model import TradeRecord

FROZEN_SLIPPAGE_BPS = {"BTC": 2, "ETH": 2, "SOL": 4, "HYPE": 6, "LINK": 6, "AAVE": 8}


def _class(phase: int | None) -> str:
    return "EXPLICIT_INVALIDATION" if phase in (24, 30) else "DERIVABLE_INVALIDATION"


def from_nimble_position(p: dict, run: dict) -> TradeRecord:
    """Map one CLOSED ``paper_nimble_positions`` payload to a frozen trade record."""
    from market_signal.paper.v2 import spec as baseline

    th = p["thesis"]
    eq, xq = p["entry_quote"], p["exit_quote"]
    s = 1 if p["side"] == "long" else -1
    ref = th["entry_reference_price"]
    obj = th["objective"]
    vol = None
    if obj.get("family") == "volatility":  # target = ref * (1 + s * 0.5 * volatility_move)
        vol = 2 * s * (obj["price"] / ref - 1)
        vol = vol if vol > 0 else None
    asset = p["asset"]
    satellite = p.get("universe_cohort") == "SATELLITE" or asset not in baseline.COINS
    slip = eq.get("slippage_bps") if satellite else FROZEN_SLIPPAGE_BPS[asset]
    decimals = p.get("size_decimals") if satellite else baseline.RiskPolicy().size_decimals[asset]
    max_lev = (
        (p.get("universe_context") or {}).get("market", {}).get("max_leverage")
        if satellite
        else baseline.RiskPolicy().max_leverage[asset]
    )
    hid = p["hypothesis_id"]
    phase = next(
        (h.source_phase for h in baseline.bootstrap() if h.hypothesis_id == hid), None
    ) or (30 if obj.get("family") == "event_reaction" else None)
    funding = (p.get("funding") or 0.0) + (p.get("funding_reserve") or 0.0)
    return TradeRecord(
        source="paper_nimble:" + run["policy_id"],
        run_id=run["run_id"],
        trade_id=p["trade_id"],
        hypothesis_id=hid,
        family=p.get("family", ""),
        asset=asset,
        side=p["side"],
        cohort="SATELLITE" if satellite else "CORE",
        signal_at=p["trigger_at"],
        decision_at=p.get("intent_at") or p["entry_at"],
        priority=content_id("", [asset, hid, p["trigger_at"]]),
        entry_bid=float(eq["bid"]),
        entry_ask=float(eq["ask"]),
        invalidation_price=th["invalidation"]["price"],
        target_price=obj["price"],
        emergency_price=th["invalidation"].get("emergency_price"),
        causal_volatility=vol,
        slippage_bps=float(slip if slip is not None else 100.0),
        fee_bps=4.5,
        funding_rate_hourly=_entry_rate(p, th),
        max_hold_minutes=int(th["max_hold_minutes"]),
        size_decimals=int(decimals if decimals is not None else 0),
        venue_max_leverage=float(max_lev or 3.0),
        depth_usd=eq.get("depth_usd") if satellite else None,
        regime_ratio=None,  # not persisted at entry; flagged as unavailable
        invalidation_class=_class(phase),
        entry_at=p["entry_at"],
        exit_at=p["exit_at"],
        exit_bid=float(xq["bid"]),
        exit_ask=float(xq["ask"]),
        exit_reason=p["exit_reason"],
        exit_detail=p.get("exit_detail"),
        mfe=float(p.get("mfe") or 0.0),
        mae=float(p.get("mae") or 0.0),
        funding_paid_fraction=funding / p["notional"] if p["notional"] else 0.0,
        hold_minutes=float(p.get("hold_minutes") or 0.0),
        recorded_units=float(p["units"]),
        recorded_notional=float(p["notional"]),
        recorded_net_pnl=float(p["net_pnl"]),
        recorded_equity_at_decision=p.get("equity_at_decision"),
    )


def _entry_rate(p: dict, th: dict) -> float:
    """Adverse predicted hourly funding frozen at the fill (causal).

    The CLOSED payload's ``funding_state`` is overwritten at close, so it is never read here.
    ``entry_economics.adverse_funding_reserve`` = max(0, side*rate) * started hours.
    """
    e = p.get("entry_economics") or p.get("economics") or {}
    reserve = e.get("adverse_funding_reserve") or 0.0
    hours = math.ceil(th["max_hold_minutes"] / 60)
    return (1 if p["side"] == "long" else -1) * reserve / hours if hours else 0.0


def nimble_trades(
    db: Path | str, run_ids=None, *, start=None, end=None, hypothesis=None, cohort=None
) -> tuple[list[TradeRecord], dict]:
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        runs = {
            r[0]: {"run_id": r[0], "policy_id": r[1], "activated_at": str(r[2])}
            for r in con.execute(
                "SELECT run_id, policy_id, activated_at FROM paper_nimble_runs ORDER BY run_id"
            ).fetchall()
        }
        wanted = list(run_ids) if run_ids else sorted(runs)
        missing = [r for r in wanted if r not in runs]
        if missing:
            raise ValueError(f"unknown run ids: {missing}")
        rows = con.execute(
            "SELECT run_id, payload FROM paper_nimble_positions WHERE state='CLOSED' "
            "AND list_contains(?, run_id) ORDER BY run_id, trade_id",
            [wanted],
        ).fetchall()
    finally:
        con.close()
    out, excluded = [], []
    for rid, payload in rows:
        p = json.loads(payload)
        if not p.get("accounting_final", True):
            excluded.append({"trade_id": p["trade_id"], "reason": "ACCOUNTING_NOT_FINAL"})
            continue
        t = from_nimble_position(p, runs[rid])
        if hypothesis and t.hypothesis_id != hypothesis:
            continue
        if cohort and t.cohort != cohort:
            continue
        if (start and t.decision_at < start) or (end and t.decision_at > end):
            continue
        out.append(t)
    meta = {
        "source": "paper_nimble_positions (CLOSED)",
        "database": str(db),
        "opened_read_only": True,
        "runs": [runs[r] for r in wanted],
        "trades": len(out),
        "excluded": excluded,
    }
    return out, meta


def load_jsonl(path: Path | str) -> list[TradeRecord]:
    with open(path) as f:
        return [TradeRecord.from_dict(json.loads(line)) for line in f if line.strip()]


def write_jsonl(trades, path: Path | str) -> None:
    from market_signal.research.lab.common import canonical_json

    with open(path, "w") as f:
        for t in trades:
            f.write(canonical_json(t.to_dict()) + "\n")


def reconcile(trades) -> dict:
    """Does fixed_allocation_baseline_v1 reproduce the recorded source sizing and PnL?"""
    from .engine import size
    from .model import AccountSnapshot
    from .policy import baseline
    from .replay import evaluate

    pol = baseline()
    rows, worst = [], 0.0
    for t in trades:
        if t.recorded_equity_at_decision is None:
            continue
        dec = size(
            t.request(),
            AccountSnapshot(t.recorded_equity_at_decision, t.recorded_equity_at_decision),
            pol,
        )
        o = evaluate(t, dec, pol, t.recorded_equity_at_decision)
        err = abs(o.net_pnl - t.recorded_net_pnl)
        worst = max(worst, err)
        rows.append(
            {
                "trade_id": t.trade_id,
                "units_match": dec.units == t.recorded_units,
                "notional_error": abs(dec.notional - t.recorded_notional),
                "net_pnl_error": err,
            }
        )
    return {
        "trades": len(rows),
        "all_units_match": all(r["units_match"] for r in rows),
        "max_net_pnl_error": worst,
        "rows": rows,
    }
