"""Phase 29 causal acquisition, costs, failure isolation and writer recovery."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from market_signal.context import ledger
from market_signal.context.free_sources.collector import Collector
from market_signal.context.free_sources.health import metrics, status
from market_signal.context.free_sources.ingest import drain
from market_signal.context.free_sources.parsers import parse
from market_signal.context.free_sources.registry import active, load
from market_signal.context.free_sources.spool import Spool

REPO = Path(__file__).resolve().parents[1]
T = datetime(2026, 10, 10, 14, tzinfo=UTC)


def cfg():
    return load(REPO / "config/context/free_sources.v1.yaml")


def source(pid="coinbase_status_v1"):
    return next(s for s in cfg()["sources"] if s["id"] == pid)


def incident(at=T, status="investigating", iid="isolated1"):
    return dict(
        id=iid,
        name="Trading API outage",
        status=status,
        created_at=(at - timedelta(minutes=10)).isoformat(),
        updated_at=at.isoformat(),
        started_at=(at - timedelta(minutes=12)).isoformat(),
        incident_updates=[dict(body="Order matching is unavailable.", updated_at=at.isoformat())],
        components=[],
    )


def body(at=T, **kwargs):
    return json.dumps({"incidents": [incident(at, **kwargs)]}).encode()


def collector(tmp_path, handler, clock=lambda: T, sources=None):
    sp = Spool(tmp_path / "sources")
    c = cfg()
    c["sources"] = sources or [source()]
    return Collector(
        sp, c, stage=4, clock=clock, jitter=lambda: 0, transport=httpx.MockTransport(handler)
    ), sp


def test_registry_free_only_and_bounded(tmp_path):
    c = cfg()
    assert len(c["sources"]) <= 20 and len(active(c, 1)) == 3
    assert len(active(c, 4)) == 18
    for s in c["sources"]:
        assert s["api_gbp_month"] == s["subscription_gbp_month"] == 0
    c["sources"][0]["api_gbp_month"] = 0.01
    p = tmp_path / "paid.yaml"
    p.write_text(yaml.safe_dump(c))
    with pytest.raises(ValueError, match="DATA_COST"):
        load(p)


def test_success_conditional_304_restart_and_no_db(tmp_path):
    requests = []

    def handler(req):
        requests.append(req)
        return (
            httpx.Response(304)
            if req.headers.get("if-none-match")
            else httpx.Response(
                200,
                content=body(),
                headers={"etag": "v1", "last-modified": "Sat, 10 Oct 2026 13:59:00 GMT"},
            )
        )

    c, sp = collector(tmp_path, handler)
    first = c.poll(source())
    c.close()
    assert first["consecutive_failures"] == 0 and len(sp.pending()) == 1
    c, sp2 = collector(tmp_path, handler)
    second = c.poll(source(), force=True)
    c.close()
    assert second["http_status"] == 304 and len(sp2.pending()) == 1
    assert requests[-1].headers["if-modified-since"] and second["totals"]["requests"] == 2
    assert not list(tmp_path.rglob("*.duckdb"))


@pytest.mark.parametrize("failure", ["timeout", "dns", 429, 500, "xml", "json", "schema"])
def test_failures_backoff_retry_and_isolation(tmp_path, failure):
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) > 1:
            return httpx.Response(200, content=body())
        if failure == "timeout":
            raise httpx.ReadTimeout("isolated", request=req)
        if failure == "dns":
            raise httpx.ConnectError("isolated", request=req)
        if failure in (429, 500):
            return httpx.Response(failure, headers={"retry-after": "600"})
        return httpx.Response(
            200, content=b"<broken" if failure == "xml" else b"bad" if failure == "json" else b"{}"
        )

    c, sp = collector(tmp_path, handler)
    bad = c.poll(source())
    assert bad["consecutive_failures"] == 1 and not sp.pending()
    assert datetime.fromisoformat(bad["next_poll"]) >= T + timedelta(seconds=120)
    if failure == 429:
        assert datetime.fromisoformat(bad["next_poll"]) >= T + timedelta(seconds=600)
    c.poll(source())
    assert len(calls) == 1
    good = c.poll(source(), force=True)
    c.close()
    assert good["consecutive_failures"] == 0 and len(sp.pending()) == 1


def test_stale_etag_daily_refresh(tmp_path):
    seen = []

    def handler(req):
        seen.append(req.headers.get("if-none-match"))
        return httpx.Response(200, content=body(), headers={"etag": "stale"})

    c, sp = collector(tmp_path, handler)
    c.poll(source())
    c.clock = lambda: T + timedelta(days=1, seconds=1)
    c.poll(source())
    c.close()
    assert seen == [None, None] and len(sp.pending()) == 1


def test_duplicate_body_hash_item_hash_and_updates(tmp_path, store):
    payload = [body()]
    c, sp = collector(tmp_path, lambda _: httpx.Response(200, content=payload[0]))
    c.poll(source())
    c.poll(source(), force=True)
    assert len(sp.pending()) == 1
    assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 0
    assert drain(store, sp)["ingested"] == 1
    eid = store.con.execute("SELECT event_id FROM context_events").fetchone()[0]
    # A different irrelevant incident changes feed hash but must not revise the old event.
    payload[0] = json.dumps(
        {"incidents": [incident(), dict(incident(iid="routine"), name="Customer support delay")]}
    ).encode()
    c.poll(source(), force=True)
    assert not sp.pending()
    payload[0] = body(T + timedelta(minutes=1), status="resolved")
    c.clock = lambda: T + timedelta(minutes=1)
    c.poll(source(), force=True)
    c.close()
    assert drain(store, sp)["ingested"] == 1
    st = ledger.state_asof(store, eid, None)
    assert len(st["history"]) == 2 and st["history"][-1]["kind"] == "resolution"
    assert st["first_seen_at"] == T.isoformat()


def test_causal_publication_receipt_and_trader_boundary(tmp_path, store):
    c, sp = collector(tmp_path, lambda _: httpx.Response(200, content=body()))
    c.poll(source())
    c.close()
    rec = sp.pending()[0]
    assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 0
    assert drain(store, sp)["errors"] == 0
    row = store.con.execute("SELECT first_seen_at,published_at FROM context_events").fetchone()
    assert row[0] == T and row[1] == T - timedelta(minutes=10)
    assert store.con.execute("SELECT count(*) FROM paper_nimble_positions").fetchone()[0] == 0
    assert store.con.execute("SELECT count(*) FROM paper_nimble_triggers").fetchone()[0] == 1
    assert store.con.execute("SELECT count(*) FROM context_work_research_pending").fetchone()[0] > 0
    done = sp.read("done", rec["receipt_id"])
    assert datetime.fromisoformat(done["context_available_at"]) >= T
    assert drain(store, sp)["ingested"] == 0


def rss(
    title="Ethereum emergency security release",
    date=T,
    url="https://example.org/notice",
    desc="Consensus-critical fix.",
):  # fixture only
    return f"<rss><channel><item><title>{title}</title><link>{url}</link><guid>notice1</guid><description>{desc}</description><pubDate>{date.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item></channel></rss>".encode()


def test_rss_atom_updates_malformed_and_noise():
    s = source("ethereum_official_rss_v1")
    observations, _stats = parse(s, rss(), T)
    assert len(observations) == 1 and datetime.fromisoformat(observations[0]["published_at"]) == T
    atom = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>notice</id><title>Ethereum emergency security release</title><link href="https://example.org/notice"/><updated>{T.isoformat()}</updated><summary>Consensus-critical fix.</summary></entry></feed>'.encode()
    assert len(parse(s, atom, T)[0]) == 1
    assert not parse(
        s, rss(title="Developer documentation update", desc="Documentation corrections only."), T
    )[0]
    for malformed in (b"<bad", b"<html/>"):
        with pytest.raises(ValueError):
            parse(s, malformed, T)


@pytest.mark.parametrize("minutes,reason", [(181, "stale"), (-10, "clock_skew")])
def test_source_freshness_and_clock_skew(minutes, reason):
    s = source()
    s["freshness_seconds"] = 7200
    _, stats = parse(s, body(T - timedelta(minutes=minutes)), T)
    assert stats["rejected"][reason] == 1


def release(title="Emergency security release", body="Critical consensus fix", **extra):
    return dict(
        id=10,
        name=title,
        tag_name="v1",
        body=body,
        html_url="https://github.com/ethereum/go-ethereum/releases/tag/v1",
        published_at=T.isoformat(),
        updated_at=T.isoformat(),
        draft=False,
        prerelease=False,
        **extra,
    )


def test_github_relevance_and_advisories():
    s = source("github_geth_releases_v1")
    assert len(parse(s, json.dumps([release()]).encode(), T)[0]) == 1
    assert not parse(
        s, json.dumps([release("Minor release", "Documentation and developer tooling")]).encode(), T
    )[0]
    s = source("github_geth_security_advisories_v1")
    advisory = dict(
        ghsa_id="GHSA-example",
        summary="Critical vulnerability",
        description="Urgent client upgrade",
        severity="critical",
        html_url="https://github.com/ethereum/go-ethereum/security/advisories/GHSA-example",
        published_at=T.isoformat(),
    )
    assert len(parse(s, json.dumps([advisory]).encode(), T)[0]) == 1
    advisory["severity"] = "low"
    assert not parse(s, json.dumps([advisory]).encode(), T)[0]


def test_regulator_and_macro_filter():
    s = source("sec_rss_v1")
    assert parse(s, rss("SEC charges crypto exchange Coinbase"), T)[0]
    assert not parse(s, rss("SEC hosts crypto roundtable"), T)[0]
    s = source("fed_rss_v1")
    assert not parse(s, rss("Federal Reserve routine speech", desc="Normal operations."), T)[0]
    obs = parse(
        s, rss("Federal Reserve emergency liquidity facility", desc="Emergency statement."), T
    )[0]
    assert obs[0]["subcategory"] == "central_bank_statement"


def test_two_providers_exact_incident_and_corroboration(tmp_path, store):
    s = source()
    obs = parse(s, body(), T)[0][0]
    # Earlier secondary report at the official incident URL; later official confirmation.
    prior = copy.deepcopy(obs)
    prior.update(
        source_id="other_free_news_v1", source_type="rss", confidence="REPORTED", dedup_key=None
    )
    from market_signal.context.taxonomy import SourceType

    ledger.register_source(store, "other_free_news_v1", SourceType.RSS)
    out = ledger.ingest(store, [prior], now=T - timedelta(seconds=60))
    sp = Spool(tmp_path / "sp")
    sp.accept_batch(s, [obs], T, {"detected": 1, "rejected": {}})
    assert drain(store, sp)["errors"] == 0
    assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
    st = ledger.state_asof(store, out.new_events[0], None)
    assert (
        set(st["sources"]) == {"coinbase_status_v1", "other_free_news_v1"}
        and st["confidence"] == "OFFICIAL"
    )
    m = metrics(store, sp)
    assert m["discovery_comparisons"][0]["order"][1]["delay_from_first_seconds"] == 60


def test_db_failure_then_catchup_and_after_commit_recovery(tmp_path, store, monkeypatch):
    sp = Spool(tmp_path / "sp")
    obs = parse(source(), body(), T)[0]
    sp.accept_batch(source(), obs, T, {"detected": 1, "rejected": {}})
    original = ledger.ingest

    def broken(*args, **kwargs):
        raise RuntimeError("isolated DB failure")

    monkeypatch.setattr(ledger, "ingest", broken)
    assert drain(store, sp)["errors"] == 1 and len(sp.pending()) == 1
    assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 0
    monkeypatch.setattr(ledger, "ingest", original)
    complete = sp.complete
    monkeypatch.setattr(sp, "complete", broken)
    assert drain(store, sp)["errors"] == 1
    assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
    monkeypatch.setattr(sp, "complete", complete)
    assert drain(store, sp)["ingested"] == 1
    assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
    assert store.con.execute("SELECT count(*) FROM paper_nimble_triggers").fetchone()[0] == 1


def test_spool_fsync_and_restart(tmp_path, monkeypatch):
    import market_signal.context.gateway.spool as primitive

    calls = []
    original = primitive.os.fsync
    monkeypatch.setattr(primitive.os, "fsync", lambda fd: (calls.append(fd), original(fd))[1])
    sp = Spool(tmp_path / "sp")
    obs = parse(source(), body(), T)[0]
    rec = sp.accept_batch(source(), obs, T, {"detected": 1, "rejected": {}})
    assert len(calls) >= 4
    assert Spool(tmp_path / "sp").pending()[0] == rec


def test_spool_failure_does_not_advance_conditional_cache(tmp_path, monkeypatch):
    c, sp = collector(
        tmp_path, lambda _: httpx.Response(200, content=body(), headers={"etag": "v1"})
    )

    def full(*args):
        raise OSError("isolated full spool")

    monkeypatch.setattr(sp, "accept_batch", full)
    st = c.poll(source())
    c.close()
    assert st["error"] == "OSError" and not st.get("etag") and not st.get("body_hash")


def test_runtime_failure_isolation_and_due_scheduler(tmp_path):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if "coinbase" in str(req.url):
            raise httpx.ConnectError("isolated", request=req)
        return httpx.Response(200, content=body())

    c, sp = collector(tmp_path, handler, sources=[source(), source("kraken_status_v1")])
    result = c.tick()
    c.tick()
    c.close()
    assert (
        result["coinbase_status_v1"]["error"] and result["kraken_status_v1"]["last_successful_poll"]
    )
    assert len(calls) == 2 and len(sp.pending()) == 1
    assert status(sp, cfg(), stage=1)["backlog"] == 1


def test_information_only_imports_and_no_execution_changes():
    text = "\n".join(
        p.read_text() for p in (REPO / "src/market_signal/context/free_sources").glob("*.py")
    )
    assert "market_signal.paper" not in text and "openai" not in text and "anthropic" not in text
    from market_signal.data.store import MIGRATIONS

    assert "paper_" not in MIGRATIONS[28] and "ALTER" not in MIGRATIONS[28]


def test_github_rate_reset_and_http_date_retry(tmp_path):
    from market_signal.context.free_sources.collector import retry_delay

    assert retry_delay({"retry-after": "Sat, 10 Oct 2026 14:20:00 GMT"}, T) == 1200
    assert (
        retry_delay(
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(T.timestamp() + 1800))}, T
        )
        == 1800
    )
    c, sp = collector(
        tmp_path,
        lambda _: httpx.Response(
            403,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(int(T.timestamp() + 1800)),
            },
        ),
        sources=[source("github_geth_releases_v1")],
    )
    out = c.tick()
    c.close()
    st = out["github_geth_releases_v1"]
    assert datetime.fromisoformat(st["next_poll"]) >= T + timedelta(seconds=1800)
    assert not sp.pending()


def test_status_chain_rpc_is_not_chain_halt_and_stablecoin_is_not_depeg():
    it = incident()
    it["name"] = "Mainnet RPC service outage"
    observations, _ = parse(source("solana_status_v1"), json.dumps({"incidents": [it]}).encode(), T)
    assert observations[0]["subcategory"] == "validator_issue"
    it["name"] = "USDC delayed mints and burns"
    observations, _ = parse(source("circle_status_v1"), json.dumps({"incidents": [it]}).encode(), T)
    assert observations[0]["subcategory"] == "custody_disruption"


def test_unexpected_304_without_receipt_and_bounded_payload(tmp_path):
    c, sp = collector(tmp_path, lambda _: httpx.Response(304))
    st = c.poll(source())
    c.close()
    assert st["error"] == "ValueError" and not st.get("body_hash")
    c, sp = collector(tmp_path, lambda _: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)))
    st = c.poll(source(), force=True)
    c.close()
    assert st["error"] == "ValueError" and not sp.pending()


def test_durable_receipt_before_state_failure(tmp_path, monkeypatch):
    c, sp = collector(tmp_path, lambda _: httpx.Response(200, content=body()))
    original = sp.save_state

    def broken(*args):
        raise OSError("isolated state failure")

    monkeypatch.setattr(sp, "save_state", broken)
    with pytest.raises(OSError):
        c.poll(source())
    assert len(Spool(sp.root).pending()) == 1
    monkeypatch.setattr(sp, "save_state", original)
    c.poll(source())
    c.close()
    assert len(sp.pending()) == 1


def test_legacy_rss_not_polled_twice(settings, store, monkeypatch):
    from market_signal.context.providers.rss import RssProvider
    from market_signal.context.service import build

    monkeypatch.setenv("PRISM_FREE_SOURCES", "on")
    monkeypatch.setenv("PRISM_FREE_SOURCES_STAGE", "4")
    assert not any(isinstance(p, RssProvider) for p in build(settings, store, "news"))


def test_cost_report_hard_activation_gate(tmp_path):
    from market_signal.context.free_sources.registry import verify_cost_report

    c = cfg()
    assert (
        verify_cost_report(c, REPO / "docs/evidence/phase29/free-source-cost-report.json")[
            "total_incremental_subscription_gbp_month"
        ]
        == 0
    )
    report = json.loads((REPO / "docs/evidence/phase29/free-source-cost-report.json").read_text())
    report["sources"][0]["DATA_COST"] = "£5"
    p = tmp_path / "report.json"
    p.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="zero-data-cost"):
        verify_cost_report(c, p)
    c["sources"][0]["cadence_seconds"] = 120
    with pytest.raises(ValueError, match="stale"):
        verify_cost_report(c)


def test_shared_gateway_worker_drains_free_receipts_without_work_auth(tmp_path, store, monkeypatch):
    import contextlib

    import market_signal.data.store as store_module
    import market_signal.ops.runtime as runtime
    from market_signal.context.gateway.spool import Spool as GatewaySpool
    from market_signal.context.gateway.worker import tick

    monkeypatch.setenv("PRISM_FREE_SOURCES", "on")
    monkeypatch.setenv("PRISM_FREE_SOURCES_DIR", str(tmp_path / "free"))
    sp = Spool(tmp_path / "free")
    sp.accept_batch(source(), parse(source(), body(), T)[0], T, {"detected": 1, "rejected": {}})
    monkeypatch.setattr(runtime, "runtime_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(runtime, "require_authoritative", lambda *a: None)

    class BorrowedStore:
        def __new__(cls, *a, **k):
            return store

    # Avoid closing the fixture connection until its own teardown.
    monkeypatch.setattr(store, "close", lambda: None)
    monkeypatch.setattr(store_module, "Store", BorrowedStore)
    assert tick(store.path, GatewaySpool(tmp_path / "gateway"))["free_sources"]["ingested"] == 1
    assert (
        not sp.pending()
        and store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
    )


def test_received_after_full_payload_not_response_headers(tmp_path):
    times = iter([T, T, T + timedelta(seconds=10), T + timedelta(seconds=10)])
    c, sp = collector(
        tmp_path, lambda _: httpx.Response(200, content=body()), clock=lambda: next(times)
    )
    c.poll(source())
    c.close()
    assert datetime.fromisoformat(sp.pending()[0]["gateway_received_at"]) == T + timedelta(
        seconds=10
    )


def test_durable_rollout_is_bounded_and_does_not_touch_db(tmp_path, monkeypatch):
    from market_signal.context.free_sources.spool import replace_durable

    monkeypatch.setenv("PRISM_FREE_SOURCES_DIR", str(tmp_path / "sp"))
    sp = Spool(tmp_path / "sp")
    replace_durable(sp.root / "rollout.json", {"stage": 2})
    assert len(active(cfg())) == 6
    assert not list(tmp_path.rglob("*.duckdb"))


def test_broad_discovery_availability_is_not_publication_time():
    s = source("gdelt_crypto_discovery_v1")
    data = {
        "articles": [
            dict(
                title="SEC suspends crypto exchange trading",
                url="https://www.reuters.com/crypto/notice",
                seendate="20261010T140000Z",
            )
        ]
    }
    observations, _ = parse(s, json.dumps(data).encode(), T)
    assert observations[0]["published_at"] is None
    assert (
        datetime.fromisoformat(observations[0]["attributes"]["facts"]["source_availability_at"])
        == T
    )
    assert not s["enabled"]
