"""Read-only reporting contracts and Streamlit paper page interaction tests."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import duckdb
import pytest

from market_signal.paper.dashboard.report import Report, comparison, freshness, runs
from market_signal.paper.nimble import engine as nimble
from market_signal.paper.nimble import report as nimble_report
from market_signal.paper.v2 import engine as baseline
from tests.test_exploratory_paper_v2 import evaluate, hypothesis, obs, quote
from tests.test_nimble_paper import T0, closed_tp, opened, time

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def accounts(store):
    baseline.register(store, now=T0)
    base = baseline.create(store, now=T0)["run_id"]
    nimble.register(store, now=T0)
    nim = nimble.create(store, now=time(1))["run_id"]
    return base, nim


def report(store, rid, end=None, start=None):
    return Report(
        store.con,
        next(r for r in runs(store.con) if r["run_id"] == rid),
        end=end or time(300),
        start=start,
    )


def test_empty_database_and_empty_active_runs(store, accounts):
    a, b = accounts
    metadata = runs(store.con)
    assert {r["run_id"] for r in metadata} == {a, b}
    assert all(r["starting_equity"] == 100 for r in metadata)
    for rid in accounts:
        r = report(store, rid)
        assert r.stats["closed_trades"] == r.stats["total_trades"] == r.stats["open_positions"] == 0
        for key in (
            "net_pnl",
            "fees",
            "win_rate",
            "average_winner",
            "average_loser",
            "profit_factor",
            "expectancy_per_trade",
            "average_hold_minutes",
            "max_drawdown",
        ):
            assert r.stats[key] is None
        assert r.stats["current_equity"] == r.stats["realized_equity"] == 100
    assert (
        comparison(report(store, a), report(store, b), store.con)["conclusion"]
        == "Insufficient sample for conclusion."
    )


def test_open_nimble_dynamic_thesis_and_marks(store, accounts):
    _, rid = accounts
    p = opened(store, rid, minute=2)
    r = report(store, rid)
    positions = r.open_positions()
    assert r.stats["open_positions"] == 1
    assert positions[0]["target"] == p["thesis"]["objective"]["price"]
    assert positions[0]["invalidation"] == p["thesis"]["invalidation"]["price"]
    assert positions[0]["maximum_hold_minutes"] == p["thesis"]["max_hold_minutes"]
    assert positions[0]["mark_ref"] is not None
    assert positions[0]["funding_accrued"] == 0
    assert positions[0]["exit_state"] == "OPEN"
    assert positions[0]["age_minutes"] > 0
    assert r.stats["realized_equity"] == pytest.approx(100 - p["entry_fee"])
    assert r.stats["capital_utilization"] > 0


def test_v2_open_and_fixed_horizon_closed_trade(store, accounts):
    rid, _ = accounts
    h = hypothesis()
    evaluate(store, rid, signals=[obs(h)], prices=[quote(15)])
    evaluate(store, rid, at=17, prices=[quote(17)])
    r = report(store, rid)
    assert r.stats["open_positions"] == 1
    p = r.open_positions()[0]
    assert p["exit_state"] == f"Fixed horizon {h.horizon_minutes} min"
    assert p["hypothesis_name"] == h.name
    assert p["funding_accrued"] is None
    assert p["maximum_hold_minutes"] == h.horizon_minutes
    due = 17 + h.horizon_minutes
    evaluate(store, rid, at=due + 1, prices=[quote(due, 102)])
    r = report(store, rid)
    closed, total = r.closed_page()
    assert total == 1 and closed[0]["exit_reason"] == "FIXED_HORIZON"
    assert closed[0]["hold_minutes"] == h.horizon_minutes
    assert closed[0]["mfe_capture_ratio"] is None
    assert r.stats["net_pnl"] == pytest.approx(baseline.state(store, rid)["closed"][0]["net_pnl"])
    assert r.stats["open_positions"] == 0
    assert r.stats["funding"] == 0
    assert r.stats["price_pnl"] - r.stats["slippage"] - r.stats["fees"] - r.stats[
        "funding"
    ] == pytest.approx(r.stats["net_pnl"])


def test_nimble_tp_exit_and_shared_metrics(store, accounts):
    _, rid = accounts
    p = closed_tp(store, rid, minute=2)
    r = report(store, rid)
    closed, total = r.closed_page()
    assert total == 1
    assert closed[0]["exit_reason"] == "TAKE_PROFIT"
    assert r.stats["net_pnl"] == pytest.approx(p["net_pnl"])
    expected = nimble_report.metrics(nimble_report.trades(store, rid))
    for key in (
        "win_rate",
        "profit_factor",
        "expectancy_per_trade",
        "average_hold_minutes",
        "mfe_capture_ratio_mean",
        "mae_avoidance_mean",
    ):
        assert r.stats[key] == expected[key]
    assert r.stats["profit_factor"] is None  # no losing-trade denominator
    exits = {x["exit_reason"]: x for x in r.exits()}
    assert exits["TAKE_PROFIT"]["count"] == 1
    assert exits["MAX_HOLD"]["count"] == 0 and exits["MAX_HOLD"]["average_return"] is None
    assert r.stats["price_pnl"] - r.stats["slippage"] + r.stats["liquidation_adjustment"] - r.stats[
        "fees"
    ] - r.stats["funding"] - r.stats["unsettled_adverse_reserve"] == pytest.approx(p["net_pnl"])


def test_funding_adjustment_reconciles_ledger_and_current_projection(store, accounts):
    _, rid = accounts
    p = closed_tp(store, rid, minute=2)
    revised = {
        **p,
        "funding": 0.2,
        "net_pnl": p["net_pnl"] - 0.2,
        "trade_return": (p["net_pnl"] - 0.2) / p["notional"],
        "funding_cash_adjustment": -0.2,
    }
    nimble.event(store, rid, "funding_adjustment", "test-delayed-funding", revised, time(4))
    r = report(store, rid)
    assert r.stats["funding"] == pytest.approx(0.2)
    assert r.stats["net_pnl"] == pytest.approx(revised["net_pnl"])
    cash = [v for v in r.curve(store.con) if v["series"].startswith("Realized")]
    assert cash[-1]["value"] == pytest.approx(nimble.account(store, rid)["cash"])
    early = report(store, rid, end=time(3))
    cash = [v for v in early.curve(store.con) if v["series"].startswith("Realized")]
    assert cash[-1]["value"] != pytest.approx(r.stats["realized_equity"])


def test_long_short_groups_hypothesis_family_asset(store, accounts):
    from tests.test_nimble_paper import h

    _, rid = accounts
    closed_tp(store, rid, minute=2)
    # A second projection fixture checks symmetric aggregation without changing any policy.
    original = nimble.positions(store, rid, terminal=True)[0]
    other = {
        **original,
        "trade_id": "short-fixture",
        "side": "short",
        "hypothesis_id": h(side="short").hypothesis_id,
        "opportunity_id": None,
        "net_pnl": -0.5,
        "trade_return": -0.025,
    }
    store.con.execute(
        "INSERT INTO paper_nimble_positions VALUES (?,?,?,?,?)",
        [rid, other["trade_id"], "CLOSED", time(4), json.dumps(other)],
    )
    r = report(store, rid)
    groups = {x["side"]: x for x in r.groups(("side",))}
    assert groups["long"]["closed_trades"] == groups["short"]["closed_trades"] == 1
    assert groups["long"]["win_rate"] == 1 and groups["short"]["win_rate"] == 0
    assert groups["short"]["net_pnl"] == groups["short"]["expectancy_per_trade"] == -0.5
    assert r.stats["profit_factor"] == pytest.approx(original["net_pnl"] / 0.5)
    assert sum(x["net_pnl"] or 0 for x in r.groups()) == pytest.approx(r.stats["net_pnl"])
    assert all(x["family"] for x in r.groups())
    assert all(x["hypothesis_name"] for x in r.groups())
    assert sum(x["signals"] for x in r.groups()) == 1
    assert sum(x["admissions"] for x in r.groups()) == 1


def test_drawdown_from_authoritative_marks_and_observation_timestamps(store, accounts):
    _, rid = accounts
    nimble.event(
        store, rid, "account_mark", "peak", {"equity": 110, "drawdown": 0, "peak": 110}, time(2)
    )
    nimble.event(
        store, rid, "account_mark", "trough", {"equity": 88, "drawdown": 0.2, "peak": 110}, time(3)
    )
    r = report(store, rid)
    assert r.stats["max_drawdown"] == 0.2
    curve = r.curve(store.con)
    assert any(v["timestamp"] == time(1) and v["value"] == 100 for v in curve)
    mtm = [v for v in curve if v["series"].startswith("Observed")]
    assert [(v["timestamp"], v["value"]) for v in mtm] == [(time(2), 110), (time(3), 88)]
    assert report(store, rid, start=time(3)).stats["max_drawdown"] == 0.2


def test_capture_uses_complete_primary_research_path_only(store, accounts):
    _, rid = accounts
    p = closed_tp(store, rid, minute=2)
    opportunity = json.loads(
        store.con.execute(
            "SELECT payload FROM paper_nimble_opportunities WHERE opportunity_id=?",
            [p["opportunity_id"]],
        ).fetchone()[0]
    )
    research = {"missing_path": False, "reference_price": p["entry_ref"], "mfe": 0.04, "mae": -0.02}
    store.con.execute(
        "INSERT INTO paper_nimble_research VALUES (?,?,?,?,?)",
        [
            p["opportunity_id"],
            opportunity["research_primary_minutes"],
            time(150),
            time(150),
            json.dumps(research),
        ],
    )
    r = report(store, rid)
    expected = nimble_report.trades(store, rid)[0]
    assert r.trades[0]["mfe_capture_ratio"] == pytest.approx(expected["mfe_capture_ratio"])
    assert r.trades[0]["mae_avoidance"] == pytest.approx(expected["mae_avoidance"])
    research["missing_path"] = True
    store.con.execute("UPDATE paper_nimble_research SET payload=?", [json.dumps(research)])
    assert report(store, rid).trades[0]["mfe_capture_ratio"] is None


def test_freshness_unknown_manifest_stale_and_production_mode(tmp_path):
    p = tmp_path / "copy.duckdb"
    p.write_bytes(b"snapshot")
    metadata = [{"last_update": time(2)}]
    local = freshness(p, metadata, now=time(3))
    assert local["source"] == "Local snapshot" and not local["stale"]
    assert local["last_production_sync"] is None
    assert "unknown" in local["snapshot_basis"]
    assert freshness(p, metadata, now=time(40))["stale"]
    manifest = {
        "snapshot_at": time(2).isoformat(),
        "downloaded_at": time(100).isoformat(),
        "bytes": p.stat().st_size,
        "local_mtime_ns": p.stat().st_mtime_ns,
    }
    p.with_suffix(".snapshot.json").write_text(json.dumps(manifest))
    fresh = freshness(p, metadata, now=time(100))
    assert fresh["stale"] and fresh["last_production_sync"] == time(100)
    assert freshness(p, metadata, now=time(3), live=True)["source"] == "Live / production-backed"
    p.write_bytes(b"replacement")
    assert freshness(p, metadata, now=time(3))["last_production_sync"] is None
    assert freshness(p, [], now=time(3))["stale"]


def test_large_trade_table_pagination_sort_and_filters(store, accounts):
    _, rid = accounts
    original = closed_tp(store, rid, minute=2)
    records = []
    for i in range(3000):
        p = {
            **original,
            "trade_id": f"fixture-{i:04}",
            "opportunity_id": None,
            "side": "short" if i % 2 else "long",
            "net_pnl": i / 100,
            "trade_return": i / 2000,
        }
        records.append([rid, p["trade_id"], "CLOSED", time(4), json.dumps(p)])
    with store.transaction():
        store.con.executemany("INSERT INTO paper_nimble_positions VALUES (?,?,?,?,?)", records)
    r = report(store, rid)
    page, total = r.closed_page(side="short", sort="net_pnl", page=1)
    assert total == 1500 and len(page) == 50
    assert page[0]["net_pnl"] > page[-1]["net_pnl"]
    assert len({p["trade_id"] for p in page}) == 50
    assert sum(x["closed_trades"] for x in r.groups()) == 3001
    with pytest.raises(ValueError):
        r.closed_page(sort="DROP TABLE paper_nimble_positions")
    with pytest.raises(ValueError):
        r.closed_page(page_size=10000)
    assert len(r.activity(store.con)) <= 50


def test_chart_sampling_bounded_and_retains_extrema(store, accounts):
    _, rid = accounts
    values = [
        [
            rid,
            i + 2,
            f"mark-{i}",
            "account_mark",
            time(i + 2),
            json.dumps(
                {
                    "equity": 50 if i == 700 else 150 if i == 900 else 100,
                    "drawdown": 0.5 if i == 700 else 0,
                }
            ),
        ]
        for i in range(1500)
    ]
    with store.transaction():
        store.con.executemany("INSERT INTO paper_nimble_events VALUES (?,?,?,?,?,?)", values)
    r = report(store, rid, end=time(2000))
    curve = r.curve(store.con, max_points=100)
    assert len(curve) <= 201
    assert {v["value"] for v in curve} >= {50, 100, 150}
    assert all(v["timestamp"] in {time(1), *(time(i + 2) for i in range(1500))} for v in curve)


def test_read_only_connection_no_mutation_and_missing_file(store, accounts):
    sys.path.insert(0, str(REPO / "dashboard"))
    import paper_data

    p = store.path
    store.close()
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    with paper_data.connection(p) as con:
        metadata = runs(con)
        for run in metadata:
            r = Report(con, run, end=time(300))
            r.closed_page()
            r.open_positions()
            r.groups()
            r.exits()
            r.curve(con)
            r.activity(con)
        with pytest.raises(duckdb.InvalidInputException):
            con.execute("DELETE FROM paper_nimble_runs")
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before
    absent = p.parent / "absent.duckdb"
    with pytest.raises(FileNotFoundError), paper_data.connection(absent):
        pass
    assert not absent.exists()


def render_page(monkeypatch, path):
    from streamlit.testing.v1 import AppTest

    sys.path.insert(0, str(REPO / "dashboard"))
    monkeypatch.setenv("PRISM_PAPER_DB_PATH", str(path))
    monkeypatch.delenv("PRISM_SOURCE_OVERRIDE", raising=False)
    monkeypatch.delenv("PRISM_PAPER_DATA_MODE", raising=False)
    return AppTest.from_file(
        str(REPO / "dashboard" / "views" / "paper.py"), default_timeout=30
    ).run()


def test_frontend_empty_and_missing_source(monkeypatch, store):
    store.close()
    at = render_page(monkeypatch, store.path)
    assert not at.exception
    assert any("No Paper v2" in msg.value for msg in at.info)
    absent = store.path.parent / "missing.duckdb"
    at = render_page(monkeypatch, absent)
    assert not at.exception and not absent.exists()
    assert any("No paper snapshot found" in msg.value for msg in at.info)


def test_frontend_comparison_refresh_and_time_controls(monkeypatch, store, accounts):
    _, rid = accounts
    closed_tp(store, rid, minute=2)
    store.close()
    at = render_page(monkeypatch, store.path)
    assert not at.exception, [e.value for e in at.exception]
    assert at.radio[0].value == "All time"
    assert any("Insufficient sample" in msg.value for msg in at.info)
    assert any("stale" in msg.value for msg in at.warning)
    assert at.button[0].label == "Reload local snapshot"
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    at.button[0].click().run()
    assert not at.exception
    at.radio[0].set_value("24h").run()
    assert not at.exception
    at.toggle[0].set_value(True).run()
    assert not at.exception
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    assert not any("pause" in b.label.lower() or "close" in b.label.lower() for b in at.button)


def test_busy_source_never_uses_legacy_writable_fallback(monkeypatch, store):
    sys.path.insert(0, str(REPO / "dashboard"))
    from streamlit.testing.v1 import AppTest

    import common
    import paper_data
    from market_signal.data.store import DatabaseBusy

    store.close()
    monkeypatch.setenv("PRISM_PAPER_DB_PATH", str(store.path))
    absent = store.path.parent / "legacy-missing.duckdb"
    monkeypatch.setenv("PRISM_DB_PATH", str(absent))

    def busy(*args, **kwargs):
        raise DatabaseBusy(store.path, "busy", 0)

    def forbidden(*args, **kwargs):
        pytest.fail("paper page entered a legacy database helper")

    monkeypatch.setattr(paper_data, "metadata", busy)
    monkeypatch.setattr(common, "store", forbidden)
    at = AppTest.from_file(str(REPO / "dashboard" / "views" / "paper.py")).run()
    assert not at.exception
    assert any("Paper source is busy" in x.value for x in at.warning)
    assert not absent.exists()


@pytest.mark.parametrize(
    "reason",
    [
        "TAKE_PROFIT",
        "PRICE_INVALIDATION",
        "STATE_INVALIDATION",
        "THESIS_EXPIRED",
        "COST_DECAY",
        "MAX_HOLD",
        "CATASTROPHE_GUARD",
        "INVALIDATED",
        "LIQUIDATION",
    ],
)
def test_exact_exit_reasons_retained(store, accounts, reason):
    _, rid = accounts
    p = closed_tp(store, rid, minute=2)
    p["exit_reason"] = reason
    store.con.execute(
        "UPDATE paper_nimble_positions SET payload=? WHERE run_id=? AND trade_id=?",
        [json.dumps(p), rid, p["trade_id"]],
    )
    r = report(store, rid)
    assert r.closed_page(reason=reason)[1] == 1
    category = next(x for x in r.exits() if x["exit_reason"] == reason)
    assert category["count"] == 1 and category["net_pnl"] == pytest.approx(p["net_pnl"])


def test_standalone_app_opens_performance_read_only(monkeypatch, store, accounts):
    from streamlit.testing.v1 import AppTest

    store.close()
    monkeypatch.setenv("PRISM_PAPER_DB_PATH", str(store.path))
    sys.path.insert(0, str(REPO / "dashboard"))
    before = hashlib.sha256(store.path.read_bytes()).hexdigest()
    at = AppTest.from_file(str(REPO / "dashboard" / "paper_app.py")).run()
    assert not at.exception and at.title[0].value == "Paper trader performance"
    assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
