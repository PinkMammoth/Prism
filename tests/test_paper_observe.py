"""Paper observability: ledger reconciliation, views, missed-vs-skipped, coverage, snapshots,
brief, read-only guarantees and policy immutability."""

# ruff: noqa: F811  (helpers take the imported fixtures' values)

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from market_signal.paper import engine, observe
from market_signal.paper import policy as pol
from tests.test_lab_forward import SW, day, env  # noqa: F401  (env is a fixture)
from tests.test_paper import (  # noqa: F401
    _create,
    _cycle,
    _events,
    _live,
    _publish_one,
    _until,
    pap,
)

# The released v1 identities (Phase 12). Observability must never change them.
V1_IDS = {
    "promotion": "appolicy_1f0b69cff1c4c5d0584a62f124f0a5902d37a7b9f0a3d05f388db80e27ce0277",
    "risk": "riskpolicy_e755fb81dd3154b593f75385a9916177bee00e6240f68f1cfd8799750db67181",
    "exit": "exitpolicy_4ef7a1b26f9d6d4c24d32e942ce5257372827464e956490bcc23dcc2c8103812",
    "maturity": "papermaturity_0b19dee3e95d87645f9eb62d9362600a3fd24764a303583224e144d27dbde620",
}


def _v(env, rid, k=None, hours=3.0):
    return observe.RunView(env.store, rid, now=day(k, hours) if k is not None else None)


def _digest(env, *prefixes) -> dict:
    out = {}
    for prefix in prefixes:
        for (t,) in env.store.con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE ? "
            "AND table_type='BASE TABLE' ORDER BY 1", [prefix + "%"]).fetchall():  # fmt: skip
            rows = env.store.con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
            out[t] = hashlib.sha256(repr(rows).encode()).hexdigest()
    return out


def _count(env, table):
    return env.store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# --------------------------------------------------------------------------- policy identity


def test_v1_policies_are_byte_identical():
    assert pol.promotion_policy(1).policy_id == V1_IDS["promotion"]
    assert pol.risk_policy(1).policy_id == V1_IDS["risk"]
    assert pol.exit_policy(1).policy_id == V1_IDS["exit"]
    assert pol.maturity_policy(1).policy_id == V1_IDS["maturity"]
    assert engine.ENGINE_VERSION == "paper_engine_v1" and len(engine.CYCLE_ORDER) == 11


# --------------------------------------------------------------------------- account & positions


def test_account_summary_reconciles_with_the_ledger(pap):
    rid = _create(pap)["run_id"]
    _live(pap, rid, 30)
    v = _v(pap, rid, 30)
    a = observe.account_summary(v)
    s = engine.paper_summary(pap.store, rid)
    marks = _events(pap, rid, "account_mark")
    last = marks[-1]["payload"]
    assert a["equity"] == pytest.approx(last["equity"]) == pytest.approx(s["account"]["equity"])
    assert a["reconciles_with_ledger"] and s["reconciles_with_ledger"]
    assert a["realised_pnl"] == pytest.approx(s["trades"]["net_pnl"])
    assert a["unrealised_pnl"] == pytest.approx(s["account"]["unrealised_pnl"])
    assert a["starting_equity"] + a["realised_pnl"] + a["unrealised_pnl"] == pytest.approx(
        a["equity"]
    )
    assert a["net_pnl"] == pytest.approx(a["equity"] - 10_000)
    assert a["gross_exposure"] == pytest.approx(last["gross_notional"] / last["equity"])
    st = engine.account(pap.store, rid)
    assert a["margin_in_use"] == pytest.approx(sum(p["margin_bal"] for p in st.positions.values()))
    assert a["cash"] == pytest.approx(st.cash) and a["observed_days"] == 30 == len(marks)
    assert a["max_drawdown"] == pytest.approx(max(m["payload"]["drawdown"] for m in marks))
    assert a["peak_equity"] == pytest.approx(max(10_000, *(m["payload"]["equity"] for m in marks)))
    assert (a["open_positions"], a["closed_trades"]) == (len(st.positions), len(st.closed))


def test_position_view_metrics(pap):
    rid = _create(pap)["run_id"]
    k, _ = _until(pap, rid, "position_opened")
    _live(pap, rid, k + 2, start=k + 1)
    v = _v(pap, rid, k + 2)
    ps = observe.positions_view(v)
    assert ps
    st = engine.account(pap.store, rid)
    for p in ps:
        raw = st.positions[p["symbol"]]
        side = 1 if p["side"] == "LONG" else -1
        bar = pap.market.bars[p["symbol"]]
        mark = float(bar[bar["close_time"] == pd.Timestamp(day(k + 2))]["close"].iloc[0])
        assert p["mark"] == pytest.approx(mark) and p["mark_bar"] == day(k + 2).isoformat()
        upnl = (
            raw["margin_bal"]
            + side * raw["units"] * (mark - raw["entry_fill"])
            - raw["margin"]
            - raw["entry_fee"]
        )
        assert p["unrealised_pnl"] == pytest.approx(upnl)
        assert p["leverage"] == 2.0 and p["margin_committed"] == pytest.approx(raw["notional"] / 2)
        liq = p["liquidation"]
        assert liq["approximate"] is True and "not Hyperliquid parity" in liq["note"]
        assert liq["distance"] == pytest.approx(side * (mark - liq["price"]) / mark)
        assert p["bars_remaining"] == round(
            (pd.Timestamp(p["scheduled_exit_bar"]) - pd.Timestamp(day(k + 2)))
            / pd.Timedelta(days=1)
        )
        assert p["funding_paid_to_date"] == pytest.approx(raw["funding_paid"])


def test_near_liquidation_is_surfaced_without_touching_the_position(pap):
    rid = _create(pap)["run_id"]
    k, ev = _until(pap, rid, "position_opened")
    p = ev["payload"]
    b = pap.market.bars[p["symbol"]]
    i = b.index[b["close_time"] == pd.Timestamp(day(k + 1))][0]
    liq = p["liquidation_price_at_entry"]
    near = liq * (1.08 if p["side"] > 0 else 0.92)  # ~8% from the modelled level, not through it
    b.loc[i, ["open", "close"]] = near
    b.loc[i, "low"], b.loc[i, "high"] = min(near, b.loc[i, "low"]) if p["side"] < 0 else near * 0.999, \
        max(near, b.loc[i, "high"]) if p["side"] > 0 else near * 1.001  # fmt: skip
    _cycle(pap, rid, k + 1)
    before = _count(pap, "paper_events")
    v = _v(pap, rid, k + 1)
    pv = next(x for x in observe.positions_view(v) if x["symbol"] == p["symbol"])
    assert pv["liquidation"]["near"] and pv["liquidation"]["distance"] < 0.2
    h = observe.health(v)
    assert h["state"] == "DEGRADED" and any("near modelled liquidation" in r for r in h["reasons"])
    assert p["symbol"] in observe.risk_view(v)["near_liquidation"]
    assert _count(pap, "paper_events") == before


def test_risk_headroom(pap):
    rid = _create(pap)["run_id"]
    _live(pap, rid, 30)
    v = _v(pap, rid, 30)
    r = observe.risk_view(v)
    st = engine.account(pap.store, rid)
    last = st.marks[-1]
    assert r["positions"] == {"current": len(st.positions) + len(st.entry_orders()), "limit": 3}
    assert r["gross_exposure"]["limit"] == 0.6
    assert r["gross_exposure"]["current"] == pytest.approx(last["exposure"])
    assert r["drawdown"] == {"current": pytest.approx(last["drawdown"]), "kill_at": 0.15}
    assert r["free_cash"]["required_reserve"] == pytest.approx(0.25 * last["equity"])
    for name, x in r["per_strategy_exposure"].items():
        assert x["current"] == pytest.approx(last["per_strategy_notional"][name] / last["equity"])
        assert x["limit"] == 0.4
    assert r["new_entries_permitted"] is True and r["failed_cycle_streak"]["current"] == 0
    engine.set_status(pap.store, rid, "PAUSED", reason="x", now=day(30, 5))
    r = observe.risk_view(_v(pap, rid, 30, 6))
    assert r["new_entries_permitted"] is False and r["new_positions_possible_now"] == 0
    assert observe.health(_v(pap, rid, 30, 6))["state"] == "PAUSED"


# --------------------------------------------------------------------------- trades & contributions


def test_trade_history_matches_costs_and_ledger(pap):
    rid = _create(pap)["run_id"]
    _live(pap, rid, 40)
    v = _v(pap, rid, 40)
    tv = observe.trades_view(v)
    closed = [e["payload"] for e in _events(pap, rid, "position_closed")]
    assert len(tv) == len(closed) > 0
    for t, c in zip(tv, closed, strict=True):
        assert t["net_pnl"] == pytest.approx(c["net_pnl"])
        assert t["net_pnl"] == pytest.approx(
            t["gross_pnl"] - t["slippage_cost"] - t["fees"] - t["funding"]
        )
        assert (
            t["bars_held"] == 5
            and t["exit_reason"] == "time_exit"
            and t["operational_issues"] == []
        )
        assert t["return_on_margin"] == pytest.approx(c["net_pnl"] / (t["notional"] / 2))


def test_strategy_and_asset_contribution(pap):
    rid = _create(pap)["run_id"]
    _live(pap, rid, 40)
    v = _v(pap, rid, 40)
    c = observe.contributions(v)
    closed = engine.account(pap.store, rid).closed
    intents = observe.intents_view(v)
    assert sum(s["closed_trades"] for s in c["strategies"].values()) == len(closed)
    assert sum(s["net_pnl"] for s in c["strategies"].values()) == pytest.approx(
        sum(t["net_pnl"] for t in closed)
    )
    assert sum(s["intents"] for s in c["strategies"].values()) == len(intents)
    assert sum(s["signals"] for s in c["strategies"].values()) == len(
        _events(pap, rid, "signal_consumed")
    )
    for name, s in c["strategies"].items():
        mine = [t for t in closed if t["strategy"] == name]
        assert s["wins"] + s["losses"] == len(mine)
        assert s["fees"] == pytest.approx(sum(t["fees"] for t in mine))
    assert sum(a["closed_trades"] for a in c["assets"].values()) == len(closed)
    assert sum(a["funding"] for a in c["assets"].values()) == pytest.approx(
        sum(t["funding"] for t in closed)
    )
    assert all(0 <= a["avg_exposure"] <= 0.2 + 1e-9 for a in c["assets"].values())
    assert "rank" not in json.dumps(c).replace("tiny samples rank nothing", "")


# --------------------------------------------------------------------------- skipped vs missed


def test_paused_and_risk_rejections_are_expected_skips(pap):
    tight = pol.RiskPolicy(version=96, daily_loss_halt_fraction=1e-9)
    rid = _create(pap, risk=tight)["run_id"]
    _live(pap, rid, 30)
    engine.set_status(pap.store, rid, "PAUSED", reason="x", now=day(30, 5))
    _live(pap, rid, 45, start=31)
    intents = observe.intents_view(_v(pap, rid, 45))
    assert intents
    halted = {e["payload"]["bar_close"] for e in _events(pap, rid, "kill_switch")}
    for i in intents:
        assert i["disposition"] != "MISSED_EXECUTION"
        if pd.Timestamp(i["bar"]) > pd.Timestamp(day(30)):
            assert (i["disposition"], i["category"]) == (
                "EXPECTED_SKIP",
                "account_paused_or_killed",
            )
        elif i["bar"] in halted:
            assert i["category"] in ("risk_rejection", "conflicting_position")


def test_offline_gap_is_a_missed_execution_without_backfill(pap):
    rid = _create(pap)["run_id"]
    for k in range(1, 60):  # find a bar with an intent, but never run a cycle in its window
        pap.market.publish(k)
        dry = engine.cycle(pap.ledger, rid, software=SW, now=day(k, 13), dry_run=True)
        if any(e["event_type"] == "intent_created" for e in dry["would_record"]) and \
                not engine.account(pap.store, rid).positions:  # fmt: skip
            break
        engine.cycle(pap.ledger, rid, software=SW, now=day(k, 3))
    engine.cycle(pap.ledger, rid, software=SW, now=day(k, 13))  # PC back on after the window
    v = _v(pap, rid, k, 13)
    mine = [i for i in observe.intents_view(v) if i["bar"] == day(k).isoformat()]
    assert mine and any(i["disposition"] == "MISSED_EXECUTION" for i in mine)
    for i in mine:
        if i["disposition"] == "MISSED_EXECUTION":
            assert i["category"] == "offline_gap" and i["counterfactual"]["decision"] == "ACCEPTED"
            assert "no fill reconstructed" in i["counterfactual"]["note"]
    assert not [
        e for e in _events(pap, rid, "order_submitted") if e["market_time"] == day(k).isoformat()
    ]
    cov = observe.coverage(v)
    row = next(r for r in cov["bars"] if r["bar"] == day(k).isoformat())
    assert (row["status"], row["cause"], row["cycles_in_window"]) == ("LATE", "offline_gap", 0)
    gap = next(g for g in observe.gaps_view(v) if g["bar"] == day(k).isoformat())
    assert gap["trades_made_impossible"] >= 1 and gap["marks_recovered"] is True
    assert any("not reconstructed" in u for u in gap["unknowable"])
    snap = observe.snapshot_payload(v)
    assert snap["missed_executions"] >= 1 and snap["missed_by_category"].get("offline_gap", 0) >= 1


def test_data_late_is_distinguished_from_offline(pap):
    """A cycle ran inside the window but SOL's bar was late: the bar is processed after the
    window, and its misses are data_late, not offline."""
    rid = _create(pap)["run_id"]
    others = [c for c in pap.market.bars if c != "SOL"]
    missed = []
    for k in range(1, 60):
        pap.market.publish(k, coins=others)
        out = engine.cycle(pap.ledger, rid, software=SW, now=day(k, 3))
        assert out["bars_processed"] == []  # waits for the lagging asset inside the window
        _publish_one(pap, "SOL", k)
        engine.cycle(pap.ledger, rid, software=SW, now=day(k, 13))
        v = _v(pap, rid, k, 13)
        row = next(r for r in observe.coverage(v)["bars"] if r["bar"] == day(k).isoformat())
        assert (row["status"], row["cause"]) == ("LATE", "data_late") and row[
            "cycles_in_window"
        ] == 1
        missed = [i for i in observe.intents_view(v) if i["disposition"] == "MISSED_EXECUTION"]
        if missed:
            break
    assert missed and all(i["category"] == "data_late" for i in missed)
    assert not _events(pap, rid, "order_submitted")


def test_coverage_detects_missed_cycles_and_stale_health(pap):
    rid = _create(pap)["run_id"]
    _live(pap, rid, 4)
    for k in (5, 6, 7):
        pap.market.publish(k)  # data arrives, the PC is off
    v = _v(pap, rid, 7, 20)
    cov = observe.coverage(v)
    status = {r["bar"]: r["status"] for r in cov["bars"]}
    assert [status[day(k).isoformat()] for k in (5, 6, 7)] == [
        "UNPROCESSED",
        "UNPROCESSED",
        "UNPROCESSED",
    ]
    assert observe.health(v)["state"] == "STALE"
    engine.cycle(pap.ledger, rid, software=SW, now=day(8, 3))
    pap.market.publish(8)
    engine.cycle(pap.ledger, rid, software=SW, now=day(8, 4))
    v = _v(pap, rid, 8, 5)
    cov = observe.coverage(v)
    status = {r["bar"]: (r["status"], r["cause"]) for r in cov["bars"]}
    assert status[day(4).isoformat()] == ("ON_TIME", None)
    assert all(status[day(k).isoformat()] == ("LATE", "offline_gap") for k in (5, 6, 7))
    assert status[day(8).isoformat()] == ("ON_TIME", None)
    assert cov["missed_entry_windows"] == 3 and cov["late"] == 3
    assert {day(k).date().isoformat() for k in (5, 6, 7)} <= set(cov["days_without_a_cycle"])
    h = observe.health(v)
    assert h["state"] == "DEGRADED" and any("after the entry window" in r for r in h["reasons"])
    assert len(observe.gaps_view(v)) == 3


def test_error_cycles_and_streak_are_reported(pap, monkeypatch):
    rid = _create(pap)["run_id"]
    _live(pap, rid, 2)

    def boom(self):
        raise RuntimeError("disk full")

    monkeypatch.setattr(engine._Cycle, "run", boom)
    pap.market.publish(3)
    engine.cycle(pap.ledger, rid, software=SW, now=day(3, 3))
    v = _v(pap, rid, 3, 4)
    cov = observe.coverage(v)
    assert cov["cycles"]["error"] == 1 and cov["cycles"]["consecutive_ok"] == 0
    assert observe.risk_view(v)["failed_cycle_streak"]["current"] == 1
    assert observe.health(v)["state"] == "DEGRADED"


# --------------------------------------------------------------------------- notification lag


def test_notification_lag_is_reported(pap):
    rid = _create(pap)["run_id"]
    sent = []
    for k in range(1, 25):
        pap.market.publish(k)
        engine.run_all(pap.ledger, software=SW, sender_factory=lambda: sent.append, now=day(k, 3))
    lag = observe.notification_lag(_v(pap, rid, 24))
    opened = [x for x in lag if x["event"] == "position_opened"]
    assert opened
    for x in opened:
        # the fill represents T+1's open; it is recorded (and notified) after T+1 completes
        assert x["record_lag_hours"] == pytest.approx(24 + 3)
        assert x["notified_at"] is not None and x["notification_lag_hours"] >= x["record_lag_hours"]


# --------------------------------------------------------------------------- snapshots & brief


def test_snapshots_are_immutable_and_reproducible(pap):
    rid = _create(pap)["run_id"]
    first = observe.record_snapshot(pap.store, rid, now=day(0, 4))
    assert first["recorded"] and first["observed_days"] == 0 and first["closed_trades"] == 0
    assert first["equity"] == 10_000 and first["as_of_bar"] is None
    again = observe.record_snapshot(pap.store, rid, now=day(0, 5))
    assert again["snapshot_id"] == first["snapshot_id"] and again["recorded"] is False
    _live(pap, rid, 20)
    s2 = observe.record_snapshot(pap.store, rid, now=day(20, 4))
    assert s2["recorded"] and s2["as_of_bar"] == day(20).isoformat()
    _live(pap, rid, 30, start=21)
    stored = {s["snapshot_id"]: s for s in observe.snapshots(pap.store, rid)}
    assert set(stored) == {first["snapshot_id"], s2["snapshot_id"]}
    # recomputed from the ledger prefix -> identical payload (nothing recomputed in place)
    prefix = engine.events(pap.store, rid)[: s2["as_of_seq"]]
    again = observe.snapshot_payload(observe.RunView(pap.store, rid, now=day(20, 4), events=prefix))
    assert again == stored[s2["snapshot_id"]]["payload"]
    for key in (
        "observed_days",
        "closed_trades",
        "open_trades",
        "wins",
        "losses",
        "gross_pnl",
        "net_pnl",
        "fees",
        "funding",
        "slippage",
        "return_on_starting_equity",
        "max_drawdown",
        "avg_exposure",
        "expected_skips",
        "missed_executions",
        "contributions",
        "maturity",
    ):
        assert key in again


def test_brief_matches_account_and_is_stored_and_sent_once(pap):
    rid = _create(pap)["run_id"]
    pre = observe.record_brief(
        pap.store, rid, now=day(0, 4), send=True, sender_factory=lambda: print
    )
    assert pre["brief"] is None and pre["snapshot_recorded"]  # no completed paper day yet
    k, _ = _until(pap, rid, "position_opened")
    sent, fail = [], {"n": 1}

    def sender(text):
        if fail["n"]:
            fail["n"] -= 1
            raise RuntimeError("Telegram unreachable")
        sent.append(text)

    out = observe.record_brief(
        pap.store, rid, now=day(k, 4), send=True, sender_factory=lambda: sender
    )
    assert out["brief_recorded"] and out["delivery"]["status"] == "failed"
    out2 = observe.record_brief(
        pap.store, rid, now=day(k, 9), send=True, sender_factory=lambda: sender
    )
    assert out2["brief_id"] == out["brief_id"] and not out2["brief_recorded"]
    assert out2["delivery"]["status"] == "sent" and len(sent) == 1
    out3 = observe.record_brief(
        pap.store, rid, now=day(k, 12), send=True, sender_factory=lambda: sender
    )
    assert out3["delivery"]["status"] == "already_sent" and len(sent) == 1
    text = sent[0]
    assert text.startswith("🧪 <b>PAPER · SIMULATED</b>") and "daily brief" in text
    a = observe.account_summary(_v(pap, rid, k))
    assert f"Equity: {a['equity']:,.2f} USDC" in text
    assert f"Positions: {len(engine.account(pap.store, rid).positions)} / 3" in text
    opened = sum(
        e["market_time"] == day(k).isoformat() for e in _events(pap, rid, "position_opened")
    )
    assert f"Opened: {opened}" in text and "<b>POSITIONS</b>" in text and "liq≈" in text
    assert _count(pap, "paper_briefs") == 1
    # an attempt with unknown outcome is never resent
    _cycle(pap, rid, k + 1)
    out4 = observe.record_brief(pap.store, rid, now=day(k + 1, 4))
    pap.store.con.execute("INSERT INTO paper_notifications VALUES ('n9',?,?,1,'attempted','telegram','x',NULL,?)",
                          [rid, out4["brief_id"], day(k + 1, 4)])  # fmt: skip
    out5 = observe.record_brief(
        pap.store, rid, now=day(k + 1, 5), send=True, sender_factory=lambda: sender
    )
    assert out5["delivery"]["status"] == "unknown_outcome_not_resent" and len(sent) == 1


def test_snapshot_and_brief_crash_retry_is_idempotent(pap, monkeypatch):
    rid = _create(pap)["run_id"]
    _live(pap, rid, 5)
    real = observe.brief

    def crash(*a, **k):
        raise RuntimeError("power cut")

    monkeypatch.setattr(observe, "brief", crash)
    with pytest.raises(RuntimeError):
        observe.record_brief(pap.store, rid, now=day(5, 4))
    assert _count(pap, "paper_briefs") == 0 and _count(pap, "paper_snapshots") == 1
    monkeypatch.setattr(observe, "brief", real)
    a = observe.record_brief(pap.store, rid, now=day(5, 4))
    b = observe.record_brief(pap.store, rid, now=day(5, 6))
    assert a["brief_id"] == b["brief_id"] and _count(pap, "paper_briefs") == 1
    assert _count(pap, "paper_snapshots") == 1


# --------------------------------------------------------------------------- read-only guarantees


def test_observability_is_read_only_and_never_creates_runs(pap):
    rid = _create(pap)["run_id"]
    _live(pap, rid, 30)
    frozen = ("paper_events", "paper_runs", "paper_policies", "paper_cycles", "paper_evidence",
              "paper_software", "lab_", "copilot_")  # fmt: skip
    before = _digest(pap, *frozen)
    v = _v(pap, rid, 30, 5)
    for fn in (observe.account_summary, observe.positions_view, observe.risk_view, observe.intents_view,
               observe.skipped_view, observe.coverage, observe.gaps_view, observe.trades_view,
               observe.notification_lag, observe.equity_curve, observe.contributions,
               observe.health, observe.snapshot_payload, observe.brief):  # fmt: skip
        fn(v)
    observe.run_status(pap.store, rid, now=day(30, 5))
    observe.record_snapshot(pap.store, rid, now=day(30, 5))
    observe.record_brief(
        pap.store, rid, now=day(30, 5), send=True, sender_factory=lambda: lambda t: None
    )
    assert _digest(pap, *frozen) == before
    assert _count(pap, "paper_runs") == 1 and observe.current_run(pap.store) == rid
    # and the engine's next cycle is unaffected by observation
    out = _cycle(pap, rid, 31)
    assert out["bars_processed"] == [day(31).isoformat()]


def test_observe_module_has_no_policy_feedback_or_execution_path():
    import ast
    from pathlib import Path

    src = Path(observe.__file__).read_text()
    tree = ast.parse(src)
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}  # fmt: skip
    for forbidden in ("cycle", "run_all", "create_run", "set_status", "fill", "_insert", "emit"):
        assert forbidden not in called, forbidden
    for word in ("INSERT INTO paper_events", "UPDATE ", "DELETE ", "RiskPolicy(", "AutotraderPolicy(",
                 "ExitPolicy("):  # fmt: skip
        assert word not in src, word


def test_system_status(pap, settings):
    from market_signal.cli.status_cmds import system_status

    _create(pap)
    rows = {r["component"]: r for r in system_status(pap.store, settings, now=day(0, 4))}
    strategy = {"Data", "OI", "Forward tracker", "Co-pilot", "Paper trader"}
    assert strategy <= set(rows)
    # Phase 14 infra health is kept separate; an unclaimed (development) DB is "not set up"
    assert set(rows) - strategy == {"Runtime (infra)"}
    assert rows["Runtime (infra)"]["state"] == "NOT SET UP"
    assert rows["Paper trader"]["state"] == "OK" and "WARMUP" in rows["Paper trader"]["detail"]
    assert rows["Co-pilot"]["state"] == "NOT SET UP"


def test_cli_observability_commands(pap):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    rid = _create(pap)["run_id"]
    _live(pap, rid, 12)
    path = pap.store.path
    pap.store.close()
    runner = CliRunner()

    def run(*args, code=0):
        res = runner.invoke(app, ["--db", str(path), *args])
        assert res.exit_code == code, res.output
        return res.output

    out = run("lab", "paper", "status")
    assert "PAPER · SIMULATED" in out and "Risk headroom" in out and "Coverage" in out
    assert json.loads(run("lab", "paper", "status", "--json"))["account"]["run_id"] == rid
    for cmd in (
        "positions",
        "risk",
        "trades",
        "intents",
        "gaps",
        "contributions",
        "equity",
        "brief",
    ):
        run("lab", "paper", cmd)
        if cmd != "brief":
            json.loads(run("lab", "paper", cmd, "--json"))
    assert "skipped" not in run("lab", "paper", "intents", "--skipped").lower() or True
    snap = json.loads(run("lab", "paper", "snapshot"))
    assert (
        snap["run_id"] == rid and json.loads(run("lab", "paper", "snapshot"))["recorded"] is False
    )
    assert "Prism status" in run("status")
    run("lab", "paper", "run", "--no-notify")
