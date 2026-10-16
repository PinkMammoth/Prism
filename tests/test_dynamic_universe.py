"""Synthetic calibration is mechanical evidence, never a profitability result."""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from market_signal.cli.main import app
from market_signal.microstructure.engine import DAY_MS, Engine, LargePrints, day_str, replay
from market_signal.microstructure.spool import Spool
from market_signal.microstructure.ws import Collector, subscriptions
from market_signal.paper.nimble import dynamic_spec, spec
from market_signal.paper.nimble import engine as nimble
from market_signal.universe import ledger
from market_signal.universe.engine import Market, State, cost, evaluate, gates, rank
from market_signal.universe.execution import SatelliteQuote, size
from market_signal.universe.policy import CORE, POLICY
from market_signal.universe.portability import availability, mapping
from market_signal.universe.source import book_metrics, decode, enrich_history
from market_signal.universe.subscriptions import publish, read

NOW = 1_800_000_000


def market(asset="SAT", **values):
    return replace(
        Market(
            asset,
            NOW,
            NOW - 10 * 86400,
            100,
            50e6,
            20e6,
            spread_bps=2,
            depth_usd=100e3,
            trades_per_minute=50,
            rv_bps=200,
            range_bps=400,
            return_bps=150,
            volume_acceleration=1.5,
            oi_change=0.03,
            warmup={"lightweight": True},
            quote_at=NOW,
            size_decimals=2,
            max_leverage=10,
        ),
        **values,
    )


def next_markets(markets, now):
    return [replace(m, observed_at=now, quote_at=now) for m in markets]


def cycle(markets, states=None, count=3, start=NOW, pins=None, warming=True):
    states = states or {}
    for i in range(count):
        at = start + i * POLICY.cadence_seconds
        out = evaluate(next_markets(markets, at), states, at, pins, warming=warming)
        states = {a: State(**s) for a, s in out["states"].items()}
    return out, states


def test_liquid_satellite_promotes_and_illiquid_volatility_fails():
    ms = [
        market(),
        market("DEAD", volume_usd=1e3, oi_usd=1e3, rv_bps=10000, spread_bps=100, depth_usd=10),
        market("SLEEP", rv_bps=1, range_bps=1, return_bps=1),
    ]
    out, _ = cycle(ms)
    rows = {r["asset"]: r for r in out["rows"]}
    assert rows["SAT"]["tier"] == 2
    assert rows["DEAD"]["rank"] is None
    assert rows["SAT"]["score"] > rows["SLEEP"]["score"]
    assert set(rows["DEAD"]["reasons"]) >= {"LOW_VOLUME", "LOW_OI", "THIN_DEPTH", "WIDE_SPREAD"}


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"volume_usd": 4_999_999}, "LOW_VOLUME"),
        ({"oi_usd": 1_999_999}, "LOW_OI"),
        ({"spread_bps": 16}, "WIDE_SPREAD"),
        ({"spread_bps": -1}, "WIDE_SPREAD"),
        ({"depth_usd": 24999}, "THIN_DEPTH"),
        ({"trades_per_minute": 1}, "LOW_TRADE_FREQUENCY"),
        ({"healthy": False}, "DATA_DEGRADED"),
        ({"rv_bps": float("nan")}, "DATA_DEGRADED"),
        ({"observed_at": NOW - 181}, "DATA_STALE"),
        ({"observed_at": NOW + 1}, "DATA_STALE"),
        ({"first_seen": NOW - 86400}, "NEW_LISTING_WARMUP"),
        ({"active": False}, "MARKET_INACTIVE"),
        ({"supported": False}, "UNSUPPORTED_MARKET"),
        ({"venue_available": False}, "VENUE_UNAVAILABLE"),
        ({"spread_bps": None}, "DATA_DEGRADED"),
        ({"rv_bps": 1}, "UNECONOMIC_EXECUTION"),
        ({"warmup": {}}, "LIGHTWEIGHT_WARMUP"),
        ({"quote_at": NOW - 181}, "DATA_STALE"),
    ],
)
def test_hard_gates(change, reason):
    assert reason in gates(market(**change), NOW, execution=True)


def test_ranking_permutation_ties_and_duplicate_identity():
    ms = [market(a) for a in ("C", "B", "A")]
    expected = rank(ms, NOW)
    for i in range(20):
        random.Random(i).shuffle(ms)
        assert rank(ms, NOW) == expected
    assert [r["asset"] for r in expected] == ["A", "B", "C"]
    with pytest.raises(ValueError, match="duplicate"):
        rank([market(), market()], NOW)


def test_absolute_size_does_not_automatically_win():
    ms = [market("BTC", volume_usd=5e9, oi_usd=5e9, rv_bps=1, range_bps=2, return_bps=1), market()]
    assert rank(ms, NOW)[0]["asset"] == "SAT"


def test_cost_conservative_and_depth_volatility_impact_monotonic():
    c = cost(market())
    assert c["round_trip_bps"] == 2 + 2 * (4.5 + 10)
    assert cost(market(impact_bps=25))["slippage_bps"] == 25
    assert cost(market(depth_usd=100))["slippage_bps"] > c["slippage_bps"]
    assert cost(market(spread_bps=None))["round_trip_bps"] is None


def test_spike_hysteresis_and_missing_evaluation_resets():
    out, st = cycle([market()], count=1)
    assert out["states"]["SAT"]["tier"] == 1
    out, st = cycle([market(rv_bps=1)], st, count=1, start=NOW + 600)
    assert st["SAT"].good == 0
    out, st = cycle([market()], st, count=1, start=NOW + 3600)
    assert st["SAT"].good == 1
    assert st["SAT"].tier == 1


def test_dwell_sustained_rank_and_safety_override():
    _, st = cycle([market()])
    _out, st = cycle([market(rv_bps=1)], st, count=6, start=NOW + 1800)
    # Uneconomic is a safety failure and bypasses dwell.
    assert st["SAT"].tier != 2
    _, st = cycle([market()])
    _, st = cycle([market(spread_bps=100)], st, count=1, start=NOW + 1800)
    assert st["SAT"].tier == 0


def test_rank_demotion_requires_six_evaluations_and_six_hour_dwell():
    _, st = cycle([market()])
    ms = [market()] + [
        market(
            f"X{i:02}",
            rv_bps=500,
            range_bps=800,
            return_bps=600,
            volume_usd=100e6,
            oi_usd=50e6,
            volume_acceleration=2,
            oi_change=0.1,
        )
        for i in range(20)
    ]
    _out, st = cycle(ms, st, count=6, start=NOW + 1800)
    assert st["SAT"].tier == 2
    # Restart after dwell still requires consecutive poor rankings; no hidden offline evaluation.
    _, st = cycle(ms, st, count=6, start=NOW + 24000)
    assert st["SAT"].tier == 1


def test_risk_pin_overrides_delisting_and_removed_markets():
    _, st = cycle([market()])
    out, st = cycle(
        [market(active=False)],
        st,
        count=1,
        start=NOW + 1800,
        pins={"SAT": ["OPEN", "UNSETTLED_ACCOUNTING"]},
    )
    assert st["SAT"].tier == 3 and st["SAT"].demotion_pending
    assert "SAT" in out["desired_subscriptions"]
    out = evaluate([], st, NOW + 2400, {"SAT": ["OPEN"]})
    assert out["states"]["SAT"]["tier"] == 3
    assert "SAT" in out["desired_subscriptions"]


def test_caps_core_and_warmed_pool_union():
    ms = [market(f"X{i:02}") for i in range(40)]
    out, st = cycle(ms)
    assert len(out["candidates"]) == 30
    assert sum(s.tier == 2 for a, s in st.items() if a not in CORE) == POLICY.satellite_cap
    assert len(out["desired_subscriptions"]) == 6 + POLICY.rich_satellite_cap
    assert set(CORE).issubset(out["desired_subscriptions"])
    # Risk may exceed selection cap, never silently dropped.
    out = evaluate(
        next_markets(ms, NOW + 1800), st, NOW + 1800, {"RISK": ["EXIT_PENDING"]}, warming=True
    )
    assert "RISK" in out["desired_subscriptions"]
    assert len(out["desired_subscriptions"]) == 7 + POLICY.rich_satellite_cap


def test_reentry_rich_since_and_gap_reset():
    _, st = cycle([market()])
    original = st["SAT"].rich_since
    _out, st = cycle([market(active=False)], st, count=1, start=NOW + 1800)
    assert st["SAT"].rich_since is None
    _, st = cycle([market()], st, count=1, start=NOW + 2400)
    assert st["SAT"].rich_since > original
    hist = [asdict(replace(market(), observed_at=NOW + i * 600)) for i in range(6)]
    assert enrich_history([replace(market(), observed_at=NOW + 3600)], {"SAT": hist}, NOW + 3600)[
        0
    ].warmup["lightweight"]
    hist[2]["observed_at"] -= 5000
    assert not enrich_history(
        [replace(market(), observed_at=NOW + 3600)], {"SAT": hist}, NOW + 3600
    )[0].warmup["lightweight"]


def test_ledger_restart_reproducibility_conflict_and_policy_integrity(store):
    out, _ = cycle([market()])
    eid = ledger.append(store, out, {"source": "synthetic"})
    assert ledger.append(store, out, {"source": "synthetic"}) == eid
    assert ledger.latest(store)["evaluation_id"] == eid
    assert evaluate(
        next_markets([market()], NOW + 1800), ledger.state(store), NOW + 1800
    ) == evaluate(
        next_markets([market()], NOW + 1800),
        {a: State(**s) for a, s in out["states"].items()},
        NOW + 1800,
    )
    with pytest.raises(ValueError, match="conflict"):
        ledger.append(store, {**out, "stage": "tampered"}, {"source": "synthetic"})
    store.con.execute("UPDATE universe_policies SET definition='{}'")
    with pytest.raises(ValueError, match="content mismatch"):
        ledger.append(store, out, {"source": "synthetic"})


def test_prospective_outcomes_real_clock_no_interpolation(store):
    for i in range(4):
        out, _ = cycle([market(price=100 + i)], count=1, start=NOW + i * 600)
        ledger.append(store, out, {"i": i})
    assert ledger.collect_outcomes(store, NOW + 1800) > 0
    row = store.con.execute(
        "SELECT payload FROM universe_outcomes WHERE horizon_minutes=15 ORDER BY observed_at LIMIT 1"
    ).fetchone()
    result = json.loads(row[0])
    assert result["lag_seconds"] == 300
    assert result["return_bps"] == pytest.approx(200)
    assert ledger.collect_outcomes(store, NOW + 1800) == 0


def test_new_listing_and_causal_schema_decode():
    pair = [
        {"universe": [{"name": "NEW", "szDecimals": 2, "maxLeverage": 10}]},
        [{"markPx": "100", "openInterest": "100000", "dayNtlVlm": "50000000", "funding": "0"}],
    ]
    m = decode(pair, NOW, {})[0]
    assert m.first_seen == NOW and "NEW_LISTING_WARMUP" in gates(m, NOW)
    pair[1] = []
    with pytest.raises(ValueError, match="alignment"):
        decode(pair, NOW, {})


def test_nearby_depth_excludes_far_liquidity_crossed_and_stale():
    book = {
        "time": NOW * 1000,
        "levels": [
            [{"px": "99.99", "sz": "1000"}, {"px": "90", "sz": "100000"}],
            [{"px": "100.01", "sz": "2000"}],
        ],
    }
    metrics = book_metrics(book, NOW)
    assert metrics["depth_usd"] == 99990
    book["time"] -= 181000
    with pytest.raises(ValueError, match="stale"):
        book_metrics(book, NOW)
    book["time"] = NOW * 1000
    book["levels"][0][0]["px"] = "101"
    with pytest.raises(ValueError, match="crossed"):
        book_metrics(book, NOW)


def test_portability_and_no_fake_history(store):
    mapping_rows = mapping()
    assert len(mapping_rows) == 34
    assert all(h["classification"] == "CORE_ONLY" for h in mapping_rows if h["source_phase"] == 30)
    assert all(
        h["classification"] == "CORE_ONLY"
        for h in mapping_rows
        if h["key"] == "crowding_vulnerability"
    )
    ready = availability(store, "SAT", NOW, NOW - 4 * 86400)
    assert not any(h["available"] for h in ready["hypotheses"])
    assert ready["complete_micro_windows"] == 0


def test_dynamic_identity_risk_and_unwarmed_activation(store):
    assert dynamic_spec.policy_id() != spec.policy_id()
    assert dynamic_spec.definition()["risk"] == spec.definition()["risk"]
    nimble.register(store, now=datetime.fromtimestamp(NOW, UTC), dynamic=True)
    with pytest.raises(ValueError, match="budget required"):
        nimble.create(store, now=datetime.fromtimestamp(NOW, UTC), dynamic=True)
    assert store.con.execute("SELECT count(*) FROM paper_nimble_runs").fetchone()[0] == 0


def test_satellite_paper_fills_adverse_and_sizing():
    from market_signal.paper.nimble.model import fill

    q = SatelliteQuote(
        "SAT",
        str(datetime.fromtimestamp(NOW, UTC)),
        str(datetime.fromtimestamp(NOW, UTC)),
        99,
        101,
        depth_usd=100000,
        slippage_bps=20,
    )
    assert fill(q, "long", entry=True) > q.ask
    assert fill(q, "long", entry=False) < q.bid
    units, notional = size(100, 31, "SAT", decimals=2)
    assert units == 0.64 and notional <= 20


def test_subscription_plan_pins_staleness_and_identity(tmp_path):
    out, _ = cycle([market()])
    out["evaluation_id"] = "synthetic"
    publish(tmp_path, out, role="scratch", runtime_id="test")
    desired, error = read(tmp_path, CORE, out["at"], role="scratch", runtime_id="test")
    assert not error and len(desired) == 7
    assert (
        read(tmp_path, desired, out["at"] + 2000, role="scratch", runtime_id="test")[0] == desired
    )
    assert read(tmp_path, CORE, out["at"], role="scratch", runtime_id="other")[0] == CORE
    assert len(subscriptions(desired)) == 4 * len(desired)
    assert len({json.dumps(s, sort_keys=True) for s in subscriptions(desired)}) == 4 * len(desired)


def test_collector_dynamic_add_remove_keeps_core_and_reconnect_set(tmp_path):
    out, _ = cycle([market()])
    out["evaluation_id"] = "synthetic"
    publish(tmp_path, out, role="scratch", runtime_id="test")
    c = Collector(Spool(tmp_path), CORE, role="scratch", runtime_id="test", dynamic=True)
    c.engine = Engine(CORE, lambda r: None, run_id="test")
    c.engine.start(NOW * 1000)
    old = c.engine.coin["BTC"]
    asyncio.run(c._sync_universe(out["at"] * 1000))
    assert "SAT" in c.coins and c.engine.coin["BTC"] is old
    out["desired_subscriptions"] = sorted(CORE)
    out["at"] += 600
    publish(tmp_path, out, role="scratch", runtime_id="test")
    asyncio.run(c._sync_universe(out["at"] * 1000))
    assert "SAT" not in c.coins and "SAT" in c.engine.retiring


def test_dynamic_engine_journal_gap_reentry_replay():
    records = []
    e = Engine(("BTC",), records.append, run_id="test")
    e.start(NOW * 1000)
    e.set_coins(("BTC", "SAT"), NOW * 1000 + 1000)
    e.tick(NOW * 1000 + 120000)
    e.set_coins(("BTC",), NOW * 1000 + 120001)
    e.tick(NOW * 1000 + 600000)
    e.set_coins(("BTC", "SAT"), NOW * 1000 + 600001)
    assert any(r.get("coin") == "SAT" and r.get("flags") == "subscription_paused" for r in records)
    assert replay(e.journal, ("BTC",), "test") == records


def test_large_print_retention_exact_threshold_equivalence():
    days = {}
    for i in range(20):
        days[day_str(NOW * 1000 - i * DAY_MS)] = {
            "SAT": {"bins": {"100": 1200 + i, "120": 20}, "minutes": 1000}
        }
    old = LargePrints(json.loads(json.dumps(days)))
    bounded = LargePrints(json.loads(json.dumps(days)))
    for offset in range(15):
        now = NOW * 1000 + offset * DAY_MS
        for day in (day_str(now), day_str(now - DAY_MS)):
            assert bounded.threshold("SAT", day) == old.threshold("SAT", day)
        bounded.prune(now)
        assert len(bounded.days) <= 9 and len(bounded._cache) <= 18
        hist = {"100": 2000, "140": 20}
        bounded.add("SAT", day_str(now), hist, True)
        old.add("SAT", day_str(now), hist, True)


def test_readonly_cli_compatibility(store, monkeypatch):
    monkeypatch.setenv("PRISM_DB_PATH", str(store.path))
    out, _ = cycle([market()])
    ledger.append(store, out, {})
    store.close()
    runner = CliRunner()
    for args in (
        ["universe", "status", "--json"],
        ["universe", "rank", "--json"],
        ["universe", "asset", "SAT", "--json"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["policy_id"] == POLICY.identity


def dynamic_run(store, monkeypatch):
    """Injected synthetic ledger readiness only; this is not a production warmup claim."""
    from market_signal.paper.v2 import spec as baseline
    from market_signal.universe import budget

    out, _ = cycle([market()])
    hypothesis = next(h for h in baseline.bootstrap() if h.name == "continuation_core_long_v1")
    row = next(r for r in out["rows"] if r["asset"] == "SAT")
    row["hypotheses"] = {
        "warmup": {"microstructure": True},
        "hypotheses": [
            {"hypothesis_id": hypothesis.hypothesis_id, "available": True, "reasons": []}
        ],
    }
    ledger.append(store, out, {"synthetic_readiness": True})
    monkeypatch.setattr(budget, "validate", lambda now: {"synthetic": True})
    at = datetime.fromtimestamp(NOW + 1300, UTC)
    nimble.register(store, now=at, dynamic=True)
    rid = nimble.create(store, now=at, dynamic=True)["run_id"]
    return rid, hypothesis


def test_dynamic_paper_lifecycle_context_future_quote_and_pin(store, monkeypatch):
    from market_signal.paper.v2.data import Observation
    from tests.test_nimble_paper import Source

    rid, h = dynamic_run(store, monkeypatch)
    at = datetime.fromtimestamp(NOW + 1800, UTC)
    q = SatelliteQuote(
        "SAT", at.isoformat(), at.isoformat(), 99.99, 100.01, depth_usd=100000, slippage_bps=3
    )
    o = Observation(
        h.hypothesis_id,
        "SAT",
        at.isoformat(),
        at.isoformat(),
        True,
        intended_side=h.side,
        context={},
    )
    result = nimble.admit(store, rid, [o], [q], Source(), {"SAT": {"funding_rate": 0}}, now=at)
    assert result["ADMITTED"] == 1
    p = nimble.positions(store, rid)[0]
    assert p["universe_cohort"] == "SATELLITE"
    assert p["universe_context"]["rank"] == 1
    assert p["universe_context"]["evaluation_id"] == ledger.latest(store)["evaluation_id"]
    assert p["satellite_slippage_bps"] == cost(market())["slippage_bps"]
    assert ledger.risk_pins(store, NOW + 1800)["SAT"] == ["PENDING", "RESEARCH_OUTCOME"]
    # Same quote cannot execute an intent, even if it was executable at admission.
    nimble.monitor(store, rid, [q], now=at)
    assert nimble.positions(store, rid)[0]["state"] == "PENDING"
    future = datetime.fromtimestamp(NOW + 1802, UTC)
    future_q = replace(q, at=future.isoformat(), available_at=future.isoformat())
    nimble.monitor(store, rid, [future_q], now=future)
    p = nimble.positions(store, rid)[0]
    assert p["state"] == "OPEN" and p["notional"] <= 20
    assert p["entry_fill"] > q.ask
    assert ledger.risk_pins(store, NOW + 1802)["SAT"] == ["OPEN", "RESEARCH_OUTCOME"]
    from market_signal.paper.dashboard.report import Report, runs

    run = next(r for r in runs(store.con) if r["run_id"] == rid)
    assert run["cohort"] == "dynamic"
    report = Report(store.con, run, start=at, end=future)
    assert report.trades[0]["universe_cohort"] == "SATELLITE"
    assert ledger.status(store, NOW + 1802)["execution_active"]


def test_pending_delisting_cancels_entry_but_keeps_risk_evidence(store, monkeypatch):
    from market_signal.paper.v2.data import Observation
    from tests.test_nimble_paper import Source

    rid, h = dynamic_run(store, monkeypatch)
    at = datetime.fromtimestamp(NOW + 1800, UTC)
    q = SatelliteQuote(
        "SAT", at.isoformat(), at.isoformat(), 99.99, 100.01, depth_usd=100000, slippage_bps=10
    )
    o = Observation(
        h.hypothesis_id, "SAT", at.isoformat(), at.isoformat(), True, intended_side=h.side
    )
    nimble.admit(store, rid, [o], [q], Source(), {"SAT": {"funding_rate": 0}}, now=at)
    out = evaluate(
        [market(active=False, observed_at=NOW + 1801, quote_at=NOW + 1801)],
        ledger.state(store),
        NOW + 1801,
        ledger.risk_pins(store, NOW + 1801),
        warming=True,
    )
    ledger.append(store, out, {"delisting": "synthetic"})
    future = datetime.fromtimestamp(NOW + 1802, UTC)
    nimble.monitor(
        store, rid, [replace(q, at=future.isoformat(), available_at=future.isoformat())], now=future
    )
    assert not nimble.positions(store, rid)
    assert "SAT" in ledger.risk_pins(store, NOW + 1802)  # research remains owed


def test_new_listing_is_prospectively_warmed_but_never_executable():
    out, st = cycle([market(first_seen=NOW)])
    assert st["SAT"].tier == 1
    assert "SAT" in out["desired_subscriptions"]
    assert not out["rows"][0]["entry_eligible"]
    assert "NEW_LISTING_WARMUP" in out["rows"][0]["execution_reasons"]


def test_budget_refuses_stale_and_short_storage_runway(tmp_path):
    from market_signal.universe.budget import validate

    now = datetime.fromtimestamp(NOW, UTC)
    b = {
        "policy_id": POLICY.identity,
        "validated": True,
        "measured_at": NOW,
        "rich_assets": 6 + POLICY.rich_satellite_cap,
        "minimum_runway_days": 30,
        "daily_growth_bytes": 100,
        "available_bytes": 4000,
        "worker_cpu_seconds_per_minute": 5,
    }
    path = tmp_path / "budget.json"
    path.write_text(json.dumps(b))
    assert validate(now, path=path) == b
    for changed in ({"available_bytes": 2000}, {"measured_at": NOW - 90000}, {"validated": False}):
        path.write_text(json.dumps({**b, **changed}))
        with pytest.raises(ValueError, match="insufficient"):
            validate(now, path=path)


def test_rich_funding_observed_availability_and_no_overwrite(store):
    from market_signal.universe.rich import commit

    payload = {
        "SAT": {
            "bars": [],
            "funding": [
                {
                    "coin": "SAT",
                    "time": (NOW - 3600) * 1000,
                    "fundingRate": "0.0001",
                    "premium": "0",
                }
            ],
            "errors": [],
        }
    }
    commit(store, payload, NOW)
    commit(store, payload, NOW + 600)
    row = store.con.execute(
        "SELECT time,available_at,pit_method FROM perp_funding WHERE coin='SAT'"
    ).fetchone()
    assert row[0].timestamp() == NOW - 3600
    assert row[1].timestamp() == NOW and row[2] == "observed"


def test_subscription_changes_are_authorized_and_idempotent(store):
    from market_signal.microstructure.ingest import _Ingest

    at = datetime.fromtimestamp(NOW, UTC)
    ingest = _Ingest(store, at)
    start = {
        "kind": "run_start",
        "run_id": "collector-test",
        "role": "scratch",
        "runtime_id": None,
        "at": NOW * 1000,
    }
    change = {
        "kind": "universe_change",
        "run_id": "collector-test",
        "at": NOW * 1000 + 1000,
        "policy_id": POLICY.identity,
        "added": ["SAT"],
        "removed": [],
    }
    ingest.apply([start, change, change, {**change, "run_id": "unknown"}])
    row = store.con.execute(
        "SELECT run_id,effective_at,ingested_at,payload FROM microstructure_subscription_events"
    ).fetchall()
    assert len(row) == 1 and row[0][0] == "collector-test"
    assert row[0][1].timestamp() == NOW + 1 and row[0][2] == at
    assert ingest.stats["universe_changes"] == 1
    assert ingest.stats["rejected_unknown_run"] == 1


def test_restart_retains_stale_valid_risk_plan(tmp_path):
    decision = {
        "at": NOW,
        "desired_subscriptions": [*CORE, "SAT"],
        "pins": {"SAT": ["OPEN_POSITION"]},
        "evaluation_id": "e",
    }
    publish(tmp_path, decision, role="scratch", runtime_id=None)
    coins, reason = read(tmp_path, CORE, NOW + 86400, role="scratch", runtime_id=None, restart=True)
    assert "SAT" in coins and reason is None
    assert read(tmp_path, CORE, NOW + 86400, role="scratch", runtime_id=None)[0] == CORE


def test_retired_asset_is_absent_from_live_health():
    e = Engine(("BTC", "SAT"), lambda r: None, run_id="test")
    e.start(NOW * 1000)
    e.set_coins(("BTC",), NOW * 1000 + 1000)
    assert set(e.health(NOW * 1000 + 2000)["coins"]) == {"BTC"}


@pytest.mark.parametrize("populated", [False, True])
def test_frontend_radar_readonly_and_empty_compatibility(store, monkeypatch, populated):
    import sys
    from contextlib import contextmanager
    from pathlib import Path

    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from market_signal.data.store import Store

    if populated:
        out, _ = cycle([market()])
        ledger.append(store, out, {"synthetic": True})
    path, raw = store.path, store.raw_dir
    store.close()
    repo = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(repo / "dashboard"))
    # Streamlit replaces __main__; restore it before later multiprocessing tests spawn.
    monkeypatch.setitem(sys.modules, "__main__", sys.modules["__main__"])
    import common

    @contextmanager
    def readonly(**kwargs):
        assert kwargs["read_only"]
        s = Store(path, raw, read_only=True)
        try:
            yield s
        finally:
            s.close()

    monkeypatch.setattr(common, "store", readonly)
    monkeypatch.setattr(common, "db_version", lambda: path.stat().st_mtime)
    st.cache_data.clear()
    before = path.stat().st_mtime_ns
    page = AppTest.from_file(
        str(repo / "dashboard" / "views" / "universe.py"), default_timeout=30
    ).run()
    assert not page.exception and path.stat().st_mtime_ns == before
    assert (
        len(page.dataframe) >= 1
        if populated
        else any("not been activated" in m.value for m in page.info)
    )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "0"])
def test_nonfinite_metadata_is_quarantined_and_serializable(value):
    from market_signal.research.lab.common import canonical_json

    markets = decode(
        [
            {"universe": [{"name": "SAT", "szDecimals": 2, "maxLeverage": 10}]},
            [{"markPx": value, "dayNtlVlm": "10000000", "openInterest": "100000", "funding": "0"}],
        ],
        NOW,
        {},
    )
    assert not markets[0].healthy and "DATA_DEGRADED" in gates(markets[0], NOW)
    assert canonical_json(evaluate(markets, {}, NOW))


def test_suspicious_book_levels_fail_closed():
    book = {
        "time": NOW * 1000,
        "levels": [[{"px": "100", "sz": "Infinity"}], [{"px": "101", "sz": "100"}]],
    }
    with pytest.raises(ValueError, match="nonfinite"):
        book_metrics(book, NOW)
    book["levels"][0] = [{"px": "99", "sz": "100"}, {"px": "100", "sz": "100"}]
    with pytest.raises(ValueError, match="unordered"):
        book_metrics(book, NOW)


def test_outcome_write_batch_rolls_back_partial_evidence(store, monkeypatch):
    from market_signal.universe import source

    def broken(s, now):
        s.con.execute("INSERT INTO universe_outcomes VALUES ('bad','SAT',15,?, '{}')", [now])
        raise ValueError("outcome source failure")

    monkeypatch.setattr(ledger, "collect_outcomes", broken)
    with pytest.raises(ValueError, match="outcome source failure"):
        source.commit(
            store,
            {"markets": [market()], "listing_first_seen": {"SAT": NOW}, "errors": {}, "light": []},
            NOW,
        )
    assert store.con.execute("SELECT count(*) FROM universe_outcomes").fetchone()[0] == 0
    # Ranking is independent of future-outcome research; its durable decision stays auditable.
    assert ledger.latest(store)["at"] == NOW


def test_failed_satellite_does_not_reset_healthy_core_feeds(tmp_path):
    from market_signal.microstructure import definitions as d

    class Socket:
        closed = 0

        async def close(self):
            self.closed += 1

    c = Collector(
        Spool(tmp_path), coins=(*CORE, "SAT"), role="scratch", runtime_id=None, dynamic=True
    )
    c.engine = Engine(c.coins, lambda r: None, run_id="test")
    t = NOW * 1000
    c.engine.start(t - 120000)
    c.conn_started = t - 120000
    c.ws = Socket()
    for asset in CORE:
        c.engine.coin[asset].acked = set(d.FEEDS)
        c.engine.coin[asset].last["book5"] = t
    asyncio.run(c._watchdog(t))
    assert c.ws.closed == 0
    assert c.engine.health(t)["coins"]["SAT"]["subscribed"] == []
    c.engine.coin["BTC"].last["book5"] = t - 120000
    asyncio.run(c._watchdog(t))
    assert c.ws.closed == 1 and "BTC" in c.close_reason


def test_cli_inspects_removed_risk_pin(store, monkeypatch):
    out = evaluate([], {"REMOVED": State(tier=2)}, NOW, {"REMOVED": ["OPEN"]})
    ledger.append(store, out, {"removed": True})
    store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(store.path))
    result = CliRunner().invoke(app, ["universe", "asset", "REMOVED", "--json"])
    assert result.exit_code == 0, result.stdout
    row = json.loads(result.stdout)
    assert row["tier"] == 3 and row["market"] is None and row["state"]["pins"] == ["OPEN"]


@pytest.mark.parametrize(
    "last_close_hour,observed_in_future,expected_fresh",
    [(17, False, True), (16, False, False), (17, True, False)],
)
def test_technical_freshness_utc_datetime_vs_nanoseconds(
    store, last_close_hour, observed_in_future, expected_fresh
):
    """Use real BarSeries int64 timestamps; fresh/stale/availability semantics stay exact."""
    import numpy as np
    import pandas as pd

    from market_signal.intraday.bars import upsert_bars
    from market_signal.models.domain import Timeframe

    now = datetime(2026, 10, 16, 17, 40, tzinfo=UTC)
    last_close = pd.Timestamp(datetime(2026, 10, 16, last_close_hour, tzinfo=UTC))
    times = pd.date_range(end=last_close - pd.Timedelta(hours=1), periods=1080, freq="h")
    i = np.arange(len(times))
    close = 100 + i * 0.01 + np.sin(i / 7)
    bars = pd.DataFrame(
        {
            "open_time": times,
            "open": close - 0.05,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 100 + i % 15,
            "trades": 100,
        }
    )
    observed = pd.Timestamp(now) + pd.Timedelta(seconds=1 if observed_in_future else -1)
    upsert_bars(
        store,
        "hyperliquid",
        "SAT",
        Timeframe.H1,
        bars,
        observed_at=observed,
        run_id="timestamp-representation-regression",
    )
    store.con.execute(
        "INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?)",
        [
            "SAT",
            "hyperliquid",
            now.replace(minute=0),
            0.0001,
            None,
            now,
            "observed",
            "timestamp-representation-regression",
        ],
    )
    result = availability(store, "SAT", int(now.timestamp()), int(now.timestamp()))
    technical = [h for h in result["hypotheses"] if h["source_phase"] == 22]
    assert bool(technical)
    assert all("DATA_STALE" not in h["reasons"] for h in technical) == expected_fresh
    assert result["warmup"]["technical"] == expected_fresh
