"""Phase A causality on a real DuckDB containing intelligence source tables ONLY."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import duckdb
import pandas as pd
import pytest

from market_signal.context import ledger
from market_signal.data.store import MIGRATIONS
from market_signal.intelligence.clocks import AccessClock, ClockBasis
from market_signal.intelligence.definitions import DEFINITION
from market_signal.intelligence.publication import (
    access_catalog_from_publication,
    publish_context_commit,
)
from market_signal.intelligence.repository import TABLES, ReadRepository
from market_signal.intelligence.snapshot import context_package, resolve_evidence, validate_package
from market_signal.intraday.bars import upsert_bars
from market_signal.models.domain import Timeframe
from market_signal.research.lab.common import canonical_json, content_id

T = datetime(2026, 11, 1, 12, tzinfo=UTC)
H = timedelta(hours=1)


class MinimalStore:
    def __init__(self, path):
        self.path = path
        self.con = duckdb.connect(str(path))

    @contextmanager
    def transaction(self):
        self.con.execute("BEGIN")
        try:
            yield
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")


@pytest.fixture
def minimal(tmp_path, monkeypatch):
    monkeypatch.delenv("PRISM_RUNTIME_ROLE", raising=False)
    monkeypatch.delenv("PRISM_RUNTIME_ID", raising=False)
    store = MinimalStore(tmp_path / "intelligence.duckdb")
    # Reuse exact released SOURCE schemas, never instantiate Store or its migrations.
    tables = TABLES | {
        "context_provider_runs",
        "context_gateway_ingests",
        "context_free_ingests",
        "context_free_observations",
    }
    schema = re.sub(r"--[^\n]*", "", "\n".join(MIGRATIONS))
    for table in (
        "context_sources",
        "context_events",
        *sorted(tables - {"context_sources", "context_events"}),
    ):
        ddl = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \(.*?;", schema, re.S)
        assert ddl, table
        store.con.execute(ddl[0])
    yield store
    store.con.close()


class SelectOnly:
    def __init__(self, connection):
        self.connection, self.queries = connection, []

    def execute(self, sql, *args):
        assert sql.lstrip().upper().startswith("SELECT"), sql
        assert not re.search(r"\b(paper_\w+|runtime_\w+|nimble\w*)\b", sql, re.I), sql
        self.queries.append(sql)
        return self.connection.execute(sql, *args)


def reader(minimal, **kwargs):
    return ReadRepository(SelectOnly(minimal.con), **kwargs)


def seed_event(minimal, at=T - H):
    return ledger.ingest(
        minimal,
        [
            {
                "source_id": "aave_official",
                "source_type": "protocol_announcement",
                "subcategory": "exploit_suspected",
                "title": "Aave reports incident",
                "confidence": "OFFICIAL",
                "assets": ["AAVE"],
                "dedup_key": "incident:aave:1",
                "published_at": (at - H).isoformat(),
                "source_ref": "https://example.org/incident",
                "raw_sha256": "a" * 64,
            }
        ],
        now=at,
    ).new_events[0]


def seed_bars(minimal, coin="AAVE", hours=80):
    opens = pd.date_range(T - hours * H, periods=hours, freq="1h")
    frame = pd.DataFrame(
        {
            "open_time": opens,
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": [100.0 + i / 10 for i in range(hours)],
            "volume": 100.0,
            "trades": 10,
        }
    )
    for row in frame.to_dict("records"):
        upsert_bars(
            minimal,
            "hyperliquid",
            coin,
            Timeframe.H1,
            pd.DataFrame([row]),
            observed_at=(row["open_time"] + H).to_pydatetime(),
            run_id="bars",
        )


def package(minimal, cutoff=T, **kwargs):
    return context_package(reader(minimal), "AAVE", cutoff, basis="origin-observed", **kwargs)


def delayed_catalog(package, *, consumer="local"):
    return {
        eid: AccessClock(
            "local",
            e["clocks"]["origin_observed_at"],
            (pd.Timestamp(e["clocks"]["origin_observed_at"]) + timedelta(minutes=5)).isoformat(),
            consumer,
            (pd.Timestamp(e["clocks"]["origin_observed_at"]) + timedelta(minutes=10)).isoformat(),
            (pd.Timestamp(e["clocks"]["origin_observed_at"]) + timedelta(minutes=15)).isoformat(),
        )
        for eid, e in package["evidence"].items()
    }


def test_intelligence_only_fixture_and_select_only_read(minimal, monkeypatch):
    seed_event(minimal)
    seed_bars(minimal)
    for coin in ("BTC", "ETH"):
        seed_bars(minimal, coin, hours=30)
    tables = {r[0] for r in minimal.con.execute("SHOW TABLES").fetchall()}
    assert not any(name.startswith(("paper_", "runtime_", "lab_", "copilot_")) for name in tables)
    before = {
        name: minimal.con.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in tables
    }
    first = package(minimal, cohort=["AAVE", "BTC", "ETH"])
    monkeypatch.setenv("PRISM_PROFILE", "invalid-profile-intentionally")
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setenv("PRISM_RUNTIME_ID", "not-an-authority")
    second = package(minimal, cohort=["ETH", "AAVE", "BTC", "ETH"])
    assert first == second
    assert first["events"] and first["market"]["breadth_known_assets"] == 3
    assert first["activity"]["state"] != "unknown"
    assert before == {
        name: minimal.con.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in tables
    }
    assert not list(minimal.path.parent.glob("*notice*"))


def test_revision_safe_breadth_and_original_evidence_resolution(minimal):
    seed_bars(minimal)
    early = package(minimal)
    row = minimal.con.execute(
        "SELECT * FROM perp_intraday_bars ORDER BY open_time DESC LIMIT 1"
    ).df()
    changed = row[["open_time", "open", "high", "low", "close", "volume", "trades"]].copy()
    changed["close"], changed["high"] = 1000.0, 1001.0
    upsert_bars(
        minimal, "hyperliquid", "AAVE", Timeframe.H1, changed, observed_at=T + H, run_id="revision"
    )
    assert package(minimal) == early
    assert (
        package(minimal, cutoff=T + 2 * H)["market"]["returns_24h_pct"]["AAVE"]
        != early["market"]["returns_24h_pct"]["AAVE"]
    )
    evidence = early["market"]["return_evidence_ids"]["AAVE"][-1]
    assert resolve_evidence(canonical_json(early), evidence)["record"]["revision"] == 0


def test_event_updates_links_and_provenance_are_causal(minimal):
    event = seed_event(minimal)
    early = package(minimal)
    ledger.ingest(
        minimal,
        [
            {
                "source_id": "other",
                "source_type": "protocol_announcement",
                "subcategory": "exploit_confirmed",
                "title": "Confirmed",
                "confidence": "OFFICIAL",
                "dedup_key": "incident:aave:1",
                "attributes": {"kind": "security", "status": "confirmed"},
            }
        ],
        now=T + H,
    )
    assert package(minimal) == early
    later = package(minimal, cutoff=T + 2 * H)
    assert early["events"][0]["subcategory"] == "exploit_suspected"
    assert later["events"][0]["subcategory"] == "exploit_confirmed"
    report = resolve_evidence(early, early["events"][0]["evidence_ids"][0])["record"]
    assert report["event_id"] == event and report["raw_sha256"] == "a" * 64
    assert report["source_ref"] == "https://example.org/incident"
    assert json.loads(report["observation"])["source_type"] == "protocol_announcement"
    assert any(
        e["table"] == "context_asset_links" and e["record"]["mapping_version"]
        for e in early["evidence"].values()
    )


def test_three_bases_and_delayed_local_receipt(minimal):
    seed_event(minimal, at=T)
    origin = package(minimal)
    clocks = delayed_catalog(origin)

    def read(basis, cutoff, consumer="local"):
        return context_package(
            reader(minimal, clocks=clocks, consumer_id=consumer), "AAVE", cutoff, basis=basis
        )

    assert read("origin-observed", T)["events"]
    assert not read("origin-committed", T + timedelta(minutes=4))["events"]
    assert read("origin-committed", T + timedelta(minutes=5))["events"]
    assert not read("consumer-available", T + timedelta(minutes=14))["events"]
    assert read("consumer-available", T + timedelta(minutes=15))["events"]
    assert not read("consumer-available", T + H, "other-consumer")["events"]
    without = context_package(reader(minimal), "AAVE", T + H, basis="origin-committed")
    assert not without["events"] and any("unproven" in w for w in without["freshness"]["warnings"])


def test_committed_revision_falls_back_and_requires_contiguous_dependencies(minimal):
    seed_bars(minimal)
    initial = package(minimal)
    clocks = delayed_catalog(initial)
    row = minimal.con.execute(
        "SELECT * FROM perp_intraday_bars ORDER BY open_time DESC LIMIT 1"
    ).df()
    changed = row[["open_time", "open", "high", "low", "close", "volume", "trades"]].copy()
    changed["close"], changed["high"] = 1000.0, 1001.0
    upsert_bars(
        minimal, "hyperliquid", "AAVE", Timeframe.H1, changed, observed_at=T + H, run_id="revision"
    )
    revised = package(minimal, cutoff=T + 2 * H)
    for eid, e in revised["evidence"].items():
        if e["table"] == "perp_intraday_bars" and e["record"]["revision"] == 1:
            clocks[eid] = AccessClock("local", (T + H).isoformat(), (T + 3 * H).isoformat())
    hidden = context_package(
        reader(minimal, clocks=clocks), "AAVE", T + 2 * H, basis="origin-committed"
    )
    assert (
        hidden["market"]["returns_24h_pct"]["AAVE"] == initial["market"]["returns_24h_pct"]["AAVE"]
    )
    visible = context_package(
        reader(minimal, clocks=clocks), "AAVE", T + 3 * H, basis="origin-committed"
    )
    assert (
        visible["market"]["returns_24h_pct"]["AAVE"] != hidden["market"]["returns_24h_pct"]["AAVE"]
    )


def seed_positioning(minimal, *, hours=169, missing=None):
    for i, at in enumerate(pd.date_range(T - (hours - 1) * H, periods=hours, freq="1h")):
        if at == missing:
            continue
        minimal.con.execute(
            "INSERT INTO context_hl_oi_hourly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                "AAVE",
                at,
                at,
                100.0 + i,
                10000.0 + i,
                100.0,
                100.0,
                100.0,
                0.001,
                0.0,
                99.0,
                101.0,
                1e6,
                "hl_oi_fixed_hourly_v1",
                "oi",
            ],
        )
        minimal.con.execute(
            "INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?)",
            ["AAVE", "hyperliquid", at, 0.001, 0.0, at, "market_close", "funding"],
        )
    minimal.con.execute(
        "INSERT INTO ingestion_runs(run_id,provider,dataset,started_at,finished_at,status) VALUES (?,?,?,?,?,?)",
        ["funding", "hyperliquid", "funding", T, T, "ok"],
    )


def test_venue_separation_and_complete_daily_weekly_persistence(minimal):
    seed_positioning(minimal)
    result = package(minimal)["positioning"]["venues"]
    for name in ("daily", "weekly"):
        p = result["hyperliquid"]["persistence"][name]
        assert p["oi_complete"] and p["funding_complete"]
        assert p["oi_change_pct"] > 0 and p["funding_positive_share"] == 1.0
    assert result["binance"]["open_interest"] is None
    assert not result["binance"]["persistence"]["daily"]["funding_complete"]
    assert result["binance"]["persistence"]["daily"]["funding_expected_settlements"] is None


def test_missing_persistence_is_unknown_not_zero(minimal):
    seed_positioning(minimal, missing=pd.Timestamp(T - 6 * H))
    p = package(minimal)["positioning"]["venues"]["hyperliquid"]["persistence"]
    for name in ("daily", "weekly"):
        assert not p[name]["oi_complete"] and p[name]["oi_change_pct"] is None
        assert not p[name]["funding_complete"] and p[name]["funding_sum"] is None


def test_funding_backfill_and_macro_rules_do_not_backdate_prism_knowledge(minimal):
    seed_positioning(minimal)
    before = package(minimal, cutoff=T - H)
    assert before["positioning"]["venues"]["hyperliquid"]["funding_24h_mean"] is None
    minimal.con.execute(
        "INSERT INTO macro_observations VALUES (?,?,?,?,?,?,?,?,?)",
        [
            "DGS2",
            "fred",
            (T - 48 * H).date(),
            (T - 48 * H).date(),
            None,
            4.0,
            T - 48 * H,
            "vintage",
            "funding",
        ],
    )
    assert package(minimal, cutoff=T - H)["market"]["macro_known"]["DGS2"] is None
    assert package(minimal)["market"]["macro_known"]["DGS2"]["value"] == 4.0


def test_source_rule_without_receipt_is_withheld(minimal):
    before = package(minimal)
    minimal.con.execute(
        "INSERT INTO perp_funding VALUES (?,?,?,?,?,?,?,?)",
        ["AAVE", "hyperliquid", T - H, 0.001, 0.0, T - H, "market_close", "unknown"],
    )
    result = package(minimal)
    assert result == before
    assert result["positioning"]["venues"]["hyperliquid"]["funding_24h_mean"] is None
    assert any("clock unproven" in w for w in result["freshness"]["warnings"])


def test_retained_manifest_is_resolvable_and_tamper_evident(minimal):
    seed_event(minimal)
    frozen = package(minimal)
    eid = next(iter(frozen["evidence"]))
    encoded = canonical_json(frozen)
    assert validate_package(encoded) == frozen
    resolved = resolve_evidence(frozen, eid)
    resolved["record"].clear()
    assert resolve_evidence(frozen, eid)["record"]
    with pytest.raises(KeyError):
        resolve_evidence(frozen, "unknown")
    bad = json.loads(encoded)
    bad["evidence"][eid]["record"]["tampered"] = True
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_package(bad)
    bad["package_id"] = content_id(
        "intpackage_", {k: v for k, v in bad.items() if k != "package_id"}
    )
    with pytest.raises(ValueError, match="evidence content"):
        validate_package(bad)


def test_read_open_missing_file_and_predicate_bounds(tmp_path, minimal, monkeypatch):
    with pytest.raises(FileNotFoundError):
        ReadRepository.open(tmp_path / "missing.duckdb")
    assert not (tmp_path / "missing.duckdb").exists()
    with pytest.raises(ValueError):
        reader(minimal).rows("paper_nimble_accounts")
    with pytest.raises(ValueError):
        reader(minimal).rows("context_events", where="1; DELETE FROM context_events")
    with pytest.raises(ValueError):
        context_package(reader(minimal), "AAVE", T, basis="latest")
    with pytest.raises(ValueError):
        context_package(reader(minimal), "AAVE", datetime(2026, 1, 1), basis="origin-observed")
    with pytest.raises(ValueError):
        package(minimal, horizon="months")
    monkeypatch.setitem(DEFINITION, "max_assets", 1)
    with pytest.raises(ValueError, match="budget"):
        package(minimal)


def test_read_only_file_checksum_and_frozen_v1_source(minimal):
    seed_event(minimal)
    minimal.con.close()
    before = hashlib.sha256(minimal.path.read_bytes()).hexdigest()
    with ReadRepository.open(minimal.path) as repo:
        assert context_package(repo, "AAVE", T, basis=ClockBasis.ORIGIN_OBSERVED)["events"]
    assert hashlib.sha256(minimal.path.read_bytes()).hexdigest() == before
    minimal.con = duckdb.connect(str(minimal.path))


def test_publication_neutrality_copy_isolation_and_delivery_failure():
    receipt = {
        "receipt_id": "receipt1",
        "logical_event_id": "ctxev_example",
        "provider": "example",
        "ingest_status": "INGESTED",
        "gateway_received_at": T.isoformat(),
        "context_available_at": (T + H).isoformat(),
    }
    seen = []
    pub = publish_context_commit(receipt, {}, consumers=(lambda event: seen.append(event.payload),))
    assert seen[0]["event_ids"] == ["ctxev_example"]
    assert seen[0]["clocks"]["available_at_here"] is None
    seen[0].clear()
    assert pub.payload["event_ids"]
    assert publish_context_commit(receipt, {}).publication_id == pub.publication_id
    assert publish_context_commit({**receipt, "ingest_status": "TEST_EXCLUDED"}, {}) is None

    def fail(_publication):
        raise RuntimeError("retry me")

    with pytest.raises(RuntimeError, match="retry me"):
        publish_context_commit(receipt, {}, consumers=(fail,))


def test_publication_catalog_binds_context_only_and_later_reports_stay_unknown(minimal):
    event = seed_event(minimal, at=T)
    seed_bars(minimal)
    pub = publish_context_commit(
        {
            "receipt_id": "commit1",
            "logical_event_id": event,
            "ingest_status": "INGESTED",
            "gateway_received_at": T.isoformat(),
            "context_available_at": (T + timedelta(minutes=5)).isoformat(),
        },
        {},
    )
    initial = package(minimal)
    clocks = access_catalog_from_publication(
        initial,
        pub,
        consumer_id="manual",
        received_at_here=(T + timedelta(minutes=10)).isoformat(),
        available_at_here=(T + timedelta(minutes=15)).isoformat(),
    )
    assert clocks and all(
        initial["evidence"][eid]["table"].startswith("context_") for eid in clocks
    )
    accessible = context_package(
        reader(minimal, clocks=clocks, consumer_id="manual"),
        "AAVE",
        T + H,
        basis="consumer-available",
    )
    assert accessible["events"] and accessible["market"]["returns_24h_pct"]["AAVE"] is None
    ledger.ingest(
        minimal,
        [
            {
                "source_id": "later",
                "source_type": "protocol_announcement",
                "subcategory": "exploit_confirmed",
                "title": "Later revision",
                "confidence": "OFFICIAL",
                "dedup_key": "incident:aave:1",
            }
        ],
        now=T + H,
    )
    later = package(minimal, cutoff=T + 2 * H)
    assert access_catalog_from_publication(later, pub) == access_catalog_from_publication(
        initial, pub
    )


def test_package_byte_and_operation_row_bounds(minimal, monkeypatch):
    seed_event(minimal)
    monkeypatch.setitem(DEFINITION, "max_package_bytes", 100)
    with pytest.raises(ValueError, match="byte budget"):
        package(minimal)
    monkeypatch.setitem(DEFINITION, "max_package_bytes", 16 * 1024 * 1024)
    monkeypatch.setitem(DEFINITION, "max_rows", 1)
    with pytest.raises(ValueError, match="row budget"):
        package(minimal)


def test_binance_positioning_never_borrows_hyperliquid_values(minimal):
    seed_positioning(minimal)
    minimal.con.execute(
        "INSERT INTO perp_oi_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            "binance",
            "AAVE",
            "AAVEUSDT",
            "usdm_perpetual",
            "1h",
            T - H,
            777.0,
            77700.0,
            "provider",
            T,
            T,
            "bn",
        ],
    )
    result = package(minimal)["positioning"]["venues"]
    assert result["binance"]["open_interest"] == 777.0
    assert result["hyperliquid"]["open_interest"] != 777.0
    assert result["binance"]["funding_24h_mean"] is None
    assert result["binance"]["crowding"]["skew"] == "unknown"


def test_replaced_oi_is_not_visible_under_original_ingestion_time(minimal):
    minimal.con.execute(
        "INSERT INTO perp_oi_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            "binance",
            "AAVE",
            "AAVEUSDT",
            "usdm_perpetual",
            "1h",
            T - H,
            777.0,
            77700.0,
            "provider",
            T,
            T + H,
            "bn",
        ],
    )
    assert package(minimal)["positioning"]["venues"]["binance"]["open_interest"] is None
    assert (
        package(minimal, cutoff=T + H)["positioning"]["venues"]["binance"]["open_interest"] == 777.0
    )


def test_free_ingest_neutral_subscriber_commit_retry_without_execution_schema(minimal, tmp_path):
    import httpx

    from market_signal.context.free_sources.ingest import drain
    from tests.test_free_event_sources import body, collector

    c, spool = collector(
        tmp_path, lambda _: httpx.Response(200, content=body(at=T)), clock=lambda: T
    )
    c.poll(c.cfg["sources"][0])
    c.close()
    publications = []

    def failed_after_commit(publication):
        with duckdb.connect(str(minimal.path)) as separate:
            assert separate.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
        publications.append(publication)
        raise RuntimeError("delivery failed after durable commit")

    first = drain(minimal, spool, consumers=(failed_after_commit,))
    assert first["errors"] == first["backlog"] == 1
    second = drain(minimal, spool, consumers=(publications.append,))
    assert second["ingested"] == 1 and second["backlog"] == 0
    assert minimal.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
    assert publications[0].payload["idempotency_key"] == publications[1].payload["idempotency_key"]
    assert not any(name.startswith("paper_") for name in reader(minimal).tables)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"origin_committed_at": (T - H).isoformat()},
        {"received_at_here": T.isoformat()},
        {"available_at_here": T.isoformat()},
    ],
)
def test_invalid_access_clock_order(kwargs):
    with pytest.raises(ValueError):
        AccessClock("local", T.isoformat(), **kwargs)
