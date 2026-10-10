"""Phase 27 prospective execution, cost/latency, isolation, wake-up and throughput contracts."""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from typer.testing import CliRunner

from market_signal.cli.main import app
from market_signal.microstructure.live import LatestBooks
from market_signal.paper.nimble import engine, report, spec, triggers
from market_signal.paper.nimble.data import live_cache
from market_signal.paper.nimble.model import (
    ExecutableQuote,
    ExecutionThesis,
    StateUpdate,
    build_thesis,
    fill,
)
from market_signal.paper.nimble.worker import Worker
from market_signal.paper.v2 import engine as baseline_engine
from market_signal.paper.v2.data import Funding, Observation

T0 = pd.Timestamp("2026-10-09T00:00:00Z")


def time(minute=0, second=0):
    return T0 + pd.Timedelta(minutes=minute, seconds=second)


def h(key="continuation_core", side="long"):
    return next(x for x in spec.baseline.bootstrap() if x.source_key == key and x.side == side)


def quote(minute=1, second=0, price=100, asset="BTC", spread=0.02, **kw):
    t = time(minute, second).isoformat()
    return ExecutableQuote(asset, t, t, price - spread / 2, price + spread / 2, **kw)


def observation(hypothesis=None, minute=1, asset="BTC", **kw):
    hh = hypothesis or h()
    t = time(minute).isoformat()
    return Observation(
        hh.hypothesis_id,
        asset,
        t,
        t,
        True,
        intended_side=hh.side,
        evidence=kw.pop("evidence", {"source": "test"}),
        context={"as_of": t},
        **kw,
    )


class Source:
    def __init__(self, volatility=0.02, low=99, high=101, available_at=None):
        self.volatility, self.low, self.high = volatility, low, high
        self.available_at = available_at

    def geometry(self, asset, o):
        return {
            "local_low": self.low,
            "local_high": self.high,
            "volatility_move": self.volatility,
            "available_at": self.available_at or o.available_at,
            "microstructure": {"bid5": 1000, "ask5": 1000},
        }


@pytest.fixture
def run(store):
    engine.register(store, now=T0)
    return engine.create(store, now=T0)["run_id"]


def admit(store, rid, hh=None, minute=1, source=None, asset="BTC", qs=None, obs=None, rate=0):
    with store.transaction():
        return engine.admit(
            store,
            rid,
            obs or [observation(hh, minute, asset)],
            qs or [quote(minute, asset=asset)],
            source or Source(),
            {asset: {"funding_rate": rate}},
            now=time(minute),
        )


def monitor(
    store, rid, minute=1, second=2, price=100, qs=None, updates=(), rates=(), rate=0, mark=True
):
    with store.transaction():
        engine.monitor(
            store,
            rid,
            qs if qs is not None else [quote(minute, second, price)],
            rates,
            updates,
            {"BTC": {"funding_rate": rate}},
            now=time(minute, second),
            mark=mark,
        )


def opened(store, rid, hh=None, minute=1, source=None, rate=0):
    admit(store, rid, hh, minute, source, rate=rate)
    monitor(store, rid, minute, 2, rate=rate)
    return next(iter(engine.state(store, rid)["open"].values()))


def closed_tp(store, rid, minute=1, hh=None):
    opened(store, rid, hh, minute)
    monitor(store, rid, minute + 1, 0, price=102)
    monitor(store, rid, minute + 1, 2, price=102)
    return engine.state(store, rid)["closed"][-1]


def test_new_identity_exact_universe_policy_and_baseline_preserved(store):
    baseline_engine.register(store, now=T0)
    base = baseline_engine.create(store, now=T0)
    before = store.con.execute("SELECT * FROM paper_v2_runs").fetchall()
    engine.register(store, now=T0)
    new = engine.create(store, now=time(1))
    assert new["run_id"] != base["run_id"] and new["policy_id"] != base["execution_policy_id"]
    assert new["entry_universe_id"] == base["universe_id"]
    assert spec.definition()["hypotheses"] == spec.baseline.definition()["hypotheses"]
    assert store.con.execute("SELECT * FROM paper_v2_runs").fetchall() == before
    assert engine.verify_baseline(store, new["run_id"])["verified"]
    assert engine.state(store, new["run_id"])["cash"] == 100
    with pytest.raises(ValueError, match="already active"):
        engine.create(store, now=time(2))


def test_baseline_prefix_tamper_is_visible(store):
    baseline_engine.register(store, now=T0)
    baseline_engine.create(store, now=T0)
    engine.register(store, now=T0)
    rid = engine.create(store, now=time(1))["run_id"]
    store.con.execute("UPDATE paper_v2_runs SET definition='{}'")
    with pytest.raises(ValueError, match="baseline run"):
        engine.verify_baseline(store, rid)


def test_entry_intent_and_exit_future_quote_timing(store, run):
    admit(store, run)
    monitor(store, run, 1, 0)
    assert len(engine.state(store, run)["pending"]) == 1
    monitor(store, run, 1, 2)
    p = next(iter(engine.state(store, run)["open"].values()))
    assert p["entry_quote_at"] > p["intent_at"]
    assert p["entry_at"] == time(1, 2).isoformat()
    monitor(store, run, 2, 0, 102)
    p = next(iter(engine.state(store, run)["open"].values()))
    assert p["state"] == "EXIT_PENDING" and p["exit_reason"] == "TAKE_PROFIT"
    monitor(store, run, 2, 0, 102)
    assert not engine.state(store, run)["closed"]
    monitor(store, run, 2, 2, 101.8)
    c = engine.state(store, run)["closed"][0]
    assert c["exit_fill"] == pytest.approx(fill(quote(2, 2, 101.8), "long", entry=False))
    assert c["exit_at"] == time(2, 2).isoformat() and c["exit_latency_seconds"] == 2
    assert c["entry_latency_seconds"] == 2


@pytest.mark.parametrize("side,price", [("long", 98), ("short", 102)])
def test_price_invalidation_symmetric(store, run, side, price):
    opened(store, run, h(side=side))
    monitor(store, run, 2, 0, price)
    monitor(store, run, 2, 2, price)
    p = engine.state(store, run)["closed"][0]
    assert p["exit_reason"] == "INVALIDATED" and p["exit_detail"] == "PRICE_LEVEL"


def test_state_invalidation_no_hindsight_or_pre_entry_flow(store, run):
    p = opened(store, run)
    u = StateUpdate(
        "BTC", time(4).isoformat(), time(4, 1).isoformat(), {"flow_fractions": [-0.7, -0.8]}
    )
    monitor(store, run, 4, 0, updates=[u])
    assert next(iter(engine.state(store, run)["open"].values()))["state"] == "OPEN"
    monitor(store, run, 4, 2, updates=[u])
    assert next(iter(engine.state(store, run)["open"].values()))["exit_detail"] == "FLOW_REVERSAL"
    assert p["thesis"]["invalidation"]["price"] == pytest.approx(99 - 0.02)


@pytest.mark.parametrize(
    "key,values,price,detail",
    [
        ("absorption_book", {"bid5": 400, "mid": 99.9}, 99.9, "ABSORPTION_DEFENCE_LOST"),
        (
            "crowding_vulnerability",
            {"crowding": "neutral", "mark": 99.9},
            99.9,
            "CROWDING_NORMALIZED_WITHOUT_RESPONSE",
        ),
        ("oi_directional_pressure", {"oi_change_1h_pct": -0.2}, 100, "OI_EXPANSION_LOST"),
    ],
)
def test_setup_state_invalidation(store, run, key, values, price, detail):
    opened(store, run, h(key))
    u = StateUpdate("BTC", time(2).isoformat(), time(2).isoformat(), values)
    monitor(store, run, 2, 0, price, updates=[u])
    assert next(iter(engine.state(store, run)["open"].values()))["exit_detail"] == detail


@pytest.mark.parametrize(
    "key,lifetime", [("continuation_core", 5), ("continuation_persistent", 15)]
)
def test_fast_full_lifecycle_expiry(store, run, key, lifetime):
    p = opened(store, run, h(key))
    monitor(store, run, lifetime, 59, 100)
    assert next(iter(engine.state(store, run)["open"].values()))["state"] == "OPEN"
    monitor(store, run, lifetime + 1, 0, 100)
    monitor(store, run, lifetime + 1, 2, 100)
    c = engine.state(store, run)["closed"][0]
    assert c["exit_reason"] == "THESIS_EXPIRED"
    assert c["hold_minutes"] == pytest.approx(lifetime)
    assert p["thesis"]["research_horizons_minutes"] == [15, 30, 60, 120, 240, 480]


def test_max_hold_only_when_thesis_has_progress(store, run):
    opened(store, run)
    monitor(store, run, 6, 0, 100.5)
    assert next(iter(engine.state(store, run)["open"].values()))["state"] == "OPEN"
    monitor(store, run, 16, 2, 100.5)
    monitor(store, run, 16, 4, 100.5)
    assert engine.state(store, run)["closed"][0]["exit_reason"] == "MAX_HOLD"


def test_economics_fee_slippage_and_uneconomic_rejection(store, run):
    assert admit(store, run, source=Source(0.001)) == {"REJECTED": 1}
    assert report.signals(store, run)[0]["reason"] == "UNECONOMIC_TARGET"
    assert not engine.positions(store, run)
    admit(store, run, minute=2)
    monitor(store, run, 2, 2)
    monitor(store, run, 3, 0, 102)
    monitor(store, run, 3, 2, 102)
    p = engine.state(store, run)["closed"][0]
    assert p["entry_fill"] > 100 and p["exit_fill"] < 102
    assert p["fees"] == pytest.approx((p["notional"] + p["units"] * p["exit_fill"]) * 0.00045)
    assert p["net_pnl"] == pytest.approx(
        p["price_pnl"] - p["slippage_cost"] - p["fees"] - p["funding"]
    )


def test_fill_rechecks_target_after_jump(store, run):
    admit(store, run)
    monitor(store, run, 1, 2, 100.99)
    assert not engine.state(store, run)["open"]
    p = engine.positions(store, run, terminal=True)[0]
    assert p["reason"] == "UNECONOMIC_TARGET"


@pytest.mark.parametrize(
    "side,rate,cost", [("long", 0.001, 0.001), ("short", 0.001, -0.001), ("long", -0.001, -0.001)]
)
def test_funding_cost_credit_and_upcoming(store, run, side, rate, cost):
    p = opened(store, run, h("crowding_vulnerability", side), minute=59)
    fs = engine.funding(
        p, [Funding("BTC", time(60).isoformat(), time(60, 1).isoformat(), rate)], time(60, 2), rate
    )
    assert fs["known_funding"] == pytest.approx(p["notional"] * cost)
    assert fs["estimated_next_cost_credit"] == pytest.approx(p["notional"] * cost)
    assert fs["next_settlement_at"] == time(120).isoformat()


def test_funding_decay_is_adverse_only(store, run):
    # Technical non-trend expiry 60m: a weak trade survives until near adverse funding.
    hh = next(
        x
        for x in spec.baseline.bootstrap()
        if x.source_phase == 22 and "stretch" in x.source_key and x.side == "long"
    )
    p = opened(store, run, hh, minute=20)
    condition = engine.exit_condition(p, quote(59, 0, 100.1), (), time(59), 0.003)
    assert condition["exit_reason"] == "COST_DECAY"
    assert engine.exit_condition(p, quote(59, 0, 100.1), (), time(59), -0.003) is None
    assert engine.exit_condition(p, quote(59, 0, 100.9), (), time(59), 0.003) is None


def test_missing_funding_never_delays_exit_capital_and_reconciles(store, run):
    p = opened(store, run, h("crowding_vulnerability"), minute=59, rate=0.0001)
    monitor(store, run, 60, 0, 102, rate=0.0001)
    monitor(store, run, 60, 2, 102, rate=0.0001)
    c = engine.state(store, run)["closed"][0]
    assert not c["accounting_final"] and not engine.state(store, run)["open"]
    assert c["funding_reserve"] == pytest.approx(p["notional"] * 0.0001)
    before = engine.account(store, run)["cash"]
    rates = [Funding("BTC", time(60).isoformat(), time(61).isoformat(), -0.0002)]
    with store.transaction():
        engine.reconcile(store, run, rates, time(61))
    c = engine.state(store, run)["closed"][0]
    assert c["accounting_final"] and c["funding"] < 0
    assert engine.account(store, run)["cash"] == pytest.approx(before + p["notional"] * 0.0003)
    with store.transaction():
        engine.reconcile(store, run, rates, time(62))
    assert engine.account(store, run)["cash"] == pytest.approx(before + p["notional"] * 0.0003)


def test_research_survives_dynamic_exit_original_primary(store, run):
    c = closed_tp(store, run)
    qs = [
        replace(
            quote(m, price=100 + m * 0.01),
            kind="minute_book",
            high=100 + m * 0.01,
            low=100 + m * 0.01,
            interval_start=time(m - 1).isoformat(),
        )
        for m in range(2, 483)
    ]
    with store.transaction():
        engine.research(store, run, qs, now=time(483))
    rs = store.con.execute(
        "SELECT horizon_minutes,payload FROM paper_nimble_research ORDER BY horizon_minutes"
    ).fetchall()
    assert [r[0] for r in rs] == list(spec.baseline.HORIZONS)
    assert json.loads(rs[0][1])["primary"]
    assert all(json.loads(r[1])["execution_exit_independent"] for r in rs)
    assert engine.state(store, run)["closed"][0] == c
    assert not store.con.execute("SELECT * FROM paper_v2_outcomes").fetchall()


def test_capital_reuse_and_confirmed_reversal(store, run):
    c = closed_tp(store, run)
    eq = engine.account(store, run)["equity"]
    assert eq > 100 and engine.account(store, run)["gross_notional"] == 0
    hh = h(side="short")
    admit(store, run, hh, minute=3)
    monitor(store, run, 3, 2)
    p = next(iter(engine.state(store, run)["open"].values()))
    assert p["side"] == "short" and ts_cmp(p["entry_at"], c["exit_at"])
    assert p["equity_at_decision"] == eq


def ts_cmp(a, b):
    return pd.Timestamp(a) > pd.Timestamp(b)


def test_reversal_requires_close_confirmation(store, run):
    opened(store, run)
    monitor(store, run, 2, 0, 98)
    admit(store, run, h(side="short"), minute=2)
    assert report.signals(store, run)[-1]["reason"] == "CONFLICT_EXISTING_POSITION"
    monitor(store, run, 2, 2, 98)
    admit(store, run, h(side="short"), minute=3)
    assert len(engine.state(store, run)["pending"]) == 1


def test_cancel_both_conflicting_fresh(store, run):
    assert admit(store, run, obs=[observation(h()), observation(h(side="short"))]) == {
        "REJECTED": 2
    }
    assert all(o["reason"] == "CONFLICT" for o in report.signals(store, run))
    assert not engine.positions(store, run)


def test_corroboration_never_increases_size_or_rewrites_thesis(store, run):
    p = opened(store, run)
    admit(store, run, h("continuation_persistent"), minute=2)
    assert report.signals(store, run)[-1]["admission"] == "SUPPORTED"
    current = next(iter(engine.state(store, run)["open"].values()))
    assert current["units"] == p["units"] and current["thesis"] == p["thesis"]


def test_episode_identity_blocks_duplicate_sources_after_close(store, run):
    o = observation(evidence={"episode_id": "chain:btc:tx:123"})
    admit(store, run, obs=[o])
    monitor(store, run, 1, 2)
    monitor(store, run, 2, 0, 102)
    monitor(store, run, 2, 2, 102)
    admit(
        store,
        run,
        minute=4,
        obs=[observation(minute=4, evidence={"episode_id": "chain:btc:tx:123"})],
    )
    assert report.signals(store, run)[-1]["reason"] == "DUPLICATE_EPISODE"


def test_ambiguous_ordering_conservative_future_fill(store, run):
    opened(store, run)
    q = quote(3, 0, 100, kind="minute_book", high=102, low=98, interval_start=time(2).isoformat())
    monitor(store, run, 3, 0, qs=[q])
    p = next(iter(engine.state(store, run)["open"].values()))
    assert p["exit_reason"] == "INVALIDATED" and p["ordering"] == "AMBIGUOUS"
    monitor(store, run, 3, 2, 100)
    c = engine.state(store, run)["closed"][0]
    assert 99 < c["exit_fill"] < 100 and c["exit_at"] == time(3, 2).isoformat()


def test_no_pre_entry_wick_or_favourable_tp_wick_fill(store, run):
    opened(store, run)
    q = quote(2, 0, 100, kind="minute_book", high=102, low=98, interval_start=time(1).isoformat())
    monitor(store, run, 2, 0, qs=[q])
    assert next(iter(engine.state(store, run)["open"].values()))["state"] == "OPEN"


@pytest.mark.parametrize(
    "mutation",
    [
        dict(available_at=time(2).isoformat()),
        dict(signal_at=time(0).isoformat()),
        dict(causal=False),
    ],
)
def test_known_at_and_activation(store, run, mutation):
    o = replace(observation(), **mutation)
    admit(store, run, obs=[o])
    assert not engine.positions(store, run)


def test_geometry_known_at_future_blocks(store, run):
    admit(store, run, source=Source(available_at=time(2).isoformat()))
    assert report.signals(store, run)[0]["reason"] == "INVALID_TIMING"


def test_data_failure_queues_close_without_inventing_fill(store, run):
    opened(store, run)
    monitor(store, run, 4, 3, qs=[])
    p = next(iter(engine.state(store, run)["open"].values()))
    assert p["exit_reason"] == "DATA_FAILURE" and p["state"] == "EXIT_PENDING"
    monitor(store, run, 4, 5, 99.9)
    assert engine.state(store, run)["closed"][0]["exit_reason"] == "DATA_FAILURE"


def test_emergency_guard_and_admin_stop(store, run):
    opened(store, run)
    monitor(store, run, 2, 0, 85)
    assert (
        next(iter(engine.state(store, run)["open"].values()))["exit_reason"] == "CATASTROPHE_GUARD"
    )
    monitor(store, run, 2, 2, 85)
    admit(store, run, minute=4)
    engine.kill(store, run, reason="test admin", now=time(4, 1))
    monitor(store, run, 4, 2)
    assert not engine.positions(store, run)


def test_restart_rebuild_preserves_thesis_and_cash(store, run):
    p = opened(store, run)
    before = engine.state(store, run)
    store.con.execute("DELETE FROM paper_nimble_accounts WHERE run_id=?", [run])
    store.con.execute("DELETE FROM paper_nimble_positions WHERE run_id=?", [run])
    engine.recover(store, run)
    assert engine.state(store, run) == before
    assert next(iter(engine.state(store, run)["open"].values()))["thesis_id"] == p["thesis_id"]


def test_frozen_policy_tampering_and_thesis_roundtrip(store, run):
    p = opened(store, run)
    assert ExecutionThesis.model_validate(p["thesis"]).thesis_id == p["thesis_id"]
    store.con.execute("UPDATE paper_nimble_policies SET definition='{}'")
    with pytest.raises(ValueError, match="identity/content"):
        monitor(store, run, 2, 0)


def test_all_regimes_and_policy_families_frozen():
    assert spec.REGIMES == (5, 15, 30, 60, 120, 240, 480)
    mapping = [spec.mapping(hh) for hh in spec.baseline.bootstrap()]
    assert {x["objective"] for x in mapping} == {"volatility", "structural", "risk_multiple"}
    assert len(mapping) == 28 and sum(hh.side == "long" for hh in spec.baseline.bootstrap()) == 14
    assert all(x["expiry_minutes"] < x["max_hold_minutes"] for x in mapping)
    assert sum(x["max_hold_minutes"] == 480 for x in mapping) == 2


def test_future_event_contracts_separate_immediate_confirmed_and_reaction_curves():
    immediate, confirmed = spec.EVENT_CONTRACTS.values()
    assert immediate["confirmation"] == "none" and confirmed["confirmation"] != "none"
    assert not immediate["registered"] and not confirmed["registered"]
    hh = SimpleNamespace(
        source_key="event_reaction_immediate_short_v1",
        source_phase=26,
        side="short",
        hypothesis_id="future_registered_id",
        horizon_minutes=15,
    )
    o = observation(h(), evidence={"episode_id": "chain:btc:tx:1"})
    geometry = {**Source().geometry("BTC", o), "event_reaction_move": 0.01, "pre_event_price": 101}
    th = build_thesis(hh, o, quote(), geometry, time(1))
    assert th.objective["family"] == "event_reaction"
    assert th.max_hold_minutes == 15 and th.expiry["reaction_window_minutes"] == 5
    assert th.research_horizons_minutes == spec.REACTION_HORIZONS


def test_outbox_targeting_idempotency_and_known_at(store, run):
    tid = triggers.enqueue(store, "context", "receipt1", ["BTC"], time(1), event_ids=["e1"])
    assert triggers.enqueue(store, "context", "receipt1", ["BTC"], time(1), event_ids=["e1"]) == tid
    assert not triggers.pending(store, run, time(0), T0)
    pending = triggers.pending(store, run, time(1), T0)
    assert (
        len(pending) == 1 and pending[0]["assets"] == ["BTC"] and pending[0]["event_ids"] == ["e1"]
    )
    store.con.execute(
        "INSERT INTO paper_nimble_trigger_receipts VALUES (?,?,?,?)", [run, tid, time(1), "{}"]
    )
    assert not triggers.pending(store, run, time(2), T0)


def test_atomic_public_book_cache_provenance_freshness(tmp_path):
    books = LatestBooks(tmp_path, "session", "authoritative", "test-runtime")
    ms = int(time(1).timestamp() * 1000)
    books.feed(("F", ms, "BTC", ms, 99.99, 100.01, 1000, 1000))
    books.publish(ms)
    qs, _ctx = live_cache(tmp_path, time(1, 1), runtime_id="test-runtime")
    assert len(qs) == 1 and qs[0].bid == 99.99
    assert not live_cache(tmp_path, time(1, 5), runtime_id="test-runtime")[0]
    assert not live_cache(tmp_path, time(1, 1), runtime_id="other-runtime")[0]
    books.feed(("X", ms, "disconnect"))
    books.publish(ms)
    assert not live_cache(tmp_path, time(1, 1), runtime_id="test-runtime")[0]


def test_idle_worker_no_db_and_unactivated_jobs(project, store, monkeypatch):
    from market_signal.ops import runtime

    worker = Worker(store.path)
    assert worker.tick(now=time(1))["db_opened"] is False
    assert runtime.active_steps(store, "paper_nimble_brief") == []
    assert "paper_nimble_brief" in runtime.SCHEDULE


def test_cli_read_commands_and_no_manual_live_transport(project, store, run, monkeypatch):
    path = store.path
    store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(path))
    runner = CliRunner()
    for command in ("status", "trades", "signals", "daily", "compare"):
        result = runner.invoke(app, ["paper", "nimble", command, "--json"])
        assert result.exit_code == 0, (command, result.stdout, result.exception)
        json.loads(result.stdout)
    assert runner.invoke(app, ["paper", "nimble", "trade", "BTC"]).exit_code != 0
    assert runner.invoke(app, ["paper", "nimble", "create", "--live"]).exit_code != 0


def test_daily_attribution_summary_once_and_postmortem(store, run):
    closed_tp(store, run)
    ps = report.trades(store, run)
    m = report.metrics(ps)
    assert m["price_pnl"] - m["slippage"] - m["fees"] - m["funding"] == pytest.approx(m["net_pnl"])
    sent = []
    report.record_daily(store, run, day=T0, sender=lambda x: sent.append(x) or True)
    report.record_daily(store, run, day=T0, sender=lambda x: sent.append(x) or True)
    assert len(sent) == 1
    pm = report.postmortem(store, run, start=T0, end=time(10))
    assert len(pm["dynamic_exit_decisions"]) == 1 and len(pm["exited_before_window_end"]) == 1


def test_hundred_trades_and_five_hundred_signals_equivalent_load(store, run):
    # New source instants separated by hypothesis-specific 2m cooldown, capital reused.
    for i in range(100):
        m = 1 + 3 * i
        closed_tp(store, run, minute=m)
        for offset in (0.3, 0.5, 0.7, 0.9):
            o = replace(
                observation(minute=m),
                signal_at=time(m, offset * 60).isoformat(),
                available_at=time(m, offset * 60).isoformat(),
                fired=False,
            )
            with store.transaction():
                engine.admit(
                    store,
                    run,
                    [o],
                    [quote(m, offset * 60)],
                    Source(),
                    {"BTC": {"funding_rate": 0}},
                    now=time(m, offset * 60),
                )
    st = engine.state(store, run)
    assert len(st["closed"]) == 100 and not st["open"] and st["cash"] > 100
    assert store.con.execute("SELECT count(*) FROM paper_nimble_opportunities").fetchone()[0] == 500
    engine.recover(store, run)
    assert engine.state(store, run) == st


def test_paper_only_engine_import_boundary():
    root = Path(__file__).parents[1] / "src/market_signal/paper/nimble"
    for name in ("engine.py", "model.py", "spec.py"):
        path = root / name
        tree = ast.parse(path.read_text())
        imports = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any(
            x.startswith(("httpx", "requests", "websockets", "hyperliquid", "ccxt", "eth_account"))
            for x in imports
        )
        for text in (
            "private_key",
            "sign_transaction",
            "place_order",
            "submit_order",
            "wallet_address",
            "llm",
        ):
            assert text not in path.read_text().lower()


def test_summary_sender_none_is_success_and_exception_is_recorded(store, run):
    from market_signal.paper.nimble import report

    result = report.record_daily(store, run, day=T0, sender=lambda text: None)
    assert result["delivery"] == "sent"

    def failed(text):
        raise RuntimeError("unreachable")

    result = report.record_daily(store, run, day=time(86400), sender=failed)
    assert result["delivery"] == "failed"
    assert (
        store.con.execute(
            "SELECT count(*) FROM paper_nimble_deliveries WHERE state='attempted'"
        ).fetchone()[0]
        == 2
    )


def test_state_invalidation_queues_without_fill_quote(store, run):
    admit(store, run)
    engine.monitor(store, run, [quote(1, 2)], now=time(1, 2))
    update = StateUpdate(
        "BTC", time(4).isoformat(), time(4).isoformat(), {"flow_fractions": [-0.5, -0.5]}
    )
    engine.monitor(store, run, [], updates=[update], now=time(4))
    position = engine.positions(store, run)[0]
    assert position["state"] == "EXIT_PENDING" and position["exit_detail"] == "FLOW_REVERSAL"
    engine.monitor(store, run, [quote(4, 2)], now=time(4, 2))
    assert engine.state(store, run)["closed"][0]["exit_reason"] == "INVALIDATED"


def test_unavailable_quote_funding_cannot_exceed_isolated_marked_loss(store, run):
    admit(store, run)
    engine.monitor(store, run, [quote(1, 2)], now=time(1, 2))
    position = engine.positions(store, run)[0]
    settlement = Funding("BTC", time(60).isoformat(), time(60).isoformat(), 1.0)
    engine.monitor(store, run, [], rates=[settlement], now=time(61))
    current = engine.positions(store, run)[0]
    assert current["state"] == "EXIT_PENDING"
    assert current["unrealised"] == pytest.approx(-position["margin"])
    assert engine.account(store, run)["equity"] == pytest.approx(
        100 - position["entry_fee"] - position["margin"]
    )
