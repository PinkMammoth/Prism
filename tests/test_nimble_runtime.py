"""Real spool/outbox integration, targeted authoritative dispatch and monitor scheduling."""

from __future__ import annotations

import json

import pytest

from market_signal.microstructure.ingest import ingest
from market_signal.microstructure.spool import Spool
from market_signal.paper.nimble import engine, triggers
from market_signal.paper.nimble.worker import Worker, marker, publish
from tests.test_nimble_paper import T0, Source, admit, h, observation, quote, time


def test_context_gateway_commit_enqueues_and_preserves_latency(store, tmp_path):
    from starlette.testclient import TestClient

    from market_signal.context.gateway.auth import Verifier
    from market_signal.context.gateway.demo import sample
    from market_signal.context.gateway.ingest import drain
    from market_signal.context.gateway.server import create_app
    from market_signal.context.gateway.spool import Spool as ContextSpool

    sp = ContextSpool(tmp_path / "gateway")
    with TestClient(create_app(sp, Verifier(tokens={"t" * 48: "test"}), dev=True)) as client:
        raw = sample()
        raw["test"] = False
        result = client.post(
            "/context/v1/events", json=raw, headers={"Authorization": "Bearer " + "t" * 48}
        )
        assert result.status_code == 202
    assert drain(store, sp)["ingested"] == 1
    row = store.con.execute(
        "SELECT source,available_at,payload FROM paper_nimble_triggers"
    ).fetchone()
    assert row[0] == "context"
    p = json.loads(row[2])
    assert p["event_ids"] and p["assets"]
    assert p["metadata"]["sender_at"] == raw["sent_at"]
    assert p["metadata"]["gateway_received_at"] <= p["metadata"]["context_available_at"]
    assert not store.con.execute("SELECT * FROM paper_nimble_runs").fetchall()
    assert (store.path.parent / "paper_nimble_notice.json").exists()
    assert drain(store, sp)["ingested"] == 0
    assert store.con.execute("SELECT count(*) FROM paper_nimble_triggers").fetchone()[0] == 1


def test_test_gateway_receipt_never_wakes_execution(store, tmp_path):
    from market_signal.context.entities import load_entities
    from market_signal.context.gateway.contract import validate
    from market_signal.context.gateway.ingest import drain
    from market_signal.context.gateway.spool import Spool as ContextSpool
    from market_signal.context.gateway.spool import now
    from tests.test_context_gateway import IDENTITY, sample

    sp = ContextSpool(tmp_path / "gateway")
    raw = sample()
    raw["test"] = True
    sub, obs = validate(raw, now(), load_entities())
    sp.accept(raw, sub, obs, IDENTITY, now())
    assert drain(store, sp)["ingested"] == 1
    assert not store.con.execute("SELECT * FROM paper_nimble_triggers").fetchall()


def test_minute_ingest_wakes_immediately_and_duplicate_does_not(store, tmp_path):
    from market_signal.microstructure import definitions as d
    from tests.test_microstructure import M, healthy, minute

    _eng, out = healthy()
    r = minute(out)
    sp = Spool(tmp_path / "micro", fsync=False)
    sp.append(
        {
            "kind": "run_start",
            "run_id": "msrun_test",
            "at": M,
            "role": "dev",
            "runtime_id": "test",
            "feature_version": d.FEATURE_VERSION,
        },
        M,
    )
    sp.append(r, M + 65000)
    sp.close()
    at = T0 + __import__("pandas").Timedelta(minutes=1)
    res = ingest(store, sp, now=at.to_pydatetime())
    assert res["minutes"] == 1
    row = store.con.execute(
        "SELECT source,available_at,payload FROM paper_nimble_triggers"
    ).fetchone()
    assert row[0] == "microstructure" and row[1] == at
    p = json.loads(row[2])
    assert p["assets"] == ["BTC"] and p["metadata"]["minute_close"]
    ingest(store, sp, now=at.to_pydatetime())
    assert store.con.execute("SELECT count(*) FROM paper_nimble_triggers").fetchone()[0] == 1


def test_worker_targeted_dispatch_idle_quote_monitor_and_restart(store, monkeypatch):
    from market_signal.paper.nimble import worker as module

    engine.register(store, now=T0)
    rid = engine.create(store, now=T0)["run_id"]
    publish(marker(store.path), {"run_id": rid})
    triggers.enqueue(
        store,
        "microstructure",
        "minuteBTC",
        ["BTC"],
        time(15),
        metadata={"minute_close": time(15).isoformat()},
    )
    path = store.path
    store.close()
    monkeypatch.setattr(module, "require_authoritative", lambda db: None)
    monkeypatch.setattr(module, "runtime_id", lambda: "test-runtime")
    called = []

    class Inputs(Source):
        def __init__(self, s, at):
            super().__init__()
            self.store = s
            self.now = at

        def observations(self, hs, activation, *, assets):
            called.append((len(hs), list(assets)))
            return [observation(h(), minute=15)]

        def updates(self, assets):
            return []

        def funding(self, since):
            return []

        def research_quotes(self, assets, since):
            return []

    monkeypatch.setattr(module, "Sources", Inputs)
    active_q = [quote(15, 0)]
    contexts = {"BTC": {"funding_rate": 0}}
    monkeypatch.setattr(module, "quotes_for_store", lambda s, at: (active_q, contexts))
    monkeypatch.setattr(module, "live_cache", lambda root, at, **kwargs: (active_q, contexts))
    w = Worker(path)
    result = w.tick(now=time(15))
    assert result["state"] == "OK" and called == [(8, ["BTC"])]
    assert result["admission"] == {"ADMITTED": 1}
    active_q[:] = [quote(15, 2)]
    w.tick(now=time(15, 2))
    assert w.cached_positions[0]["state"] == "OPEN"
    assert w.tick(now=time(15, 3))["db_opened"] is False
    active_q[:] = [quote(15, 4, 102)]
    w.tick(now=time(15, 4))
    assert w.cached_positions[0]["state"] == "EXIT_PENDING"
    active_q[:] = [quote(15, 6, 102)]
    w.tick(now=time(15, 6))
    assert not w.cached_positions and w.cached_account["gross_notional"] == 0
    # Restart notices the durable run and consumes remaining targeted context work.
    from market_signal.data.store import Store

    with __import__("contextlib").closing(Store(path)) as s:
        triggers.enqueue(
            s, "context", "eventETH", ["ETH"], time(15, 7), event_ids=["logical-event"]
        )
    w2 = Worker(path)
    active_q[:] = [quote(15, 8, asset="ETH")]
    r = w2.tick(now=time(15, 8))
    assert r["evaluated"] == [
        {"source": "context", "assets": ["ETH"], "hypotheses": 0, "observations": 0}
    ]
    assert called == [(8, ["BTC"])]


def test_runtime_lock_busy_does_not_consume_trigger(store, monkeypatch):
    from market_signal.ops.runtime import runtime_lock
    from market_signal.paper.nimble import worker as module

    engine.register(store, now=T0)
    rid = engine.create(store, now=T0)["run_id"]
    publish(marker(store.path), {"run_id": rid})
    triggers.enqueue(store, "context", "one", ["BTC"], time(1), event_ids=["e"])
    w = Worker(store.path)
    monkeypatch.setattr(module, "require_authoritative", lambda p: None)
    with runtime_lock(store.path, "test-other-writer", wait=0):
        result = w.tick(now=time(1))
    assert result["state"] == "BUSY"
    assert len(triggers.pending(store, rid, time(1), T0)) == 1


def test_min_notional_and_large_gap_loss_cap(store):
    engine.register(store, now=T0)
    rid = engine.create(store, now=T0)["run_id"]
    with store.transaction():
        engine.event(store, rid, "account_mark", "small", {"cash": 49, "equity": 49}, time(0.5))
    admit(store, rid)
    assert engine.positions(store, rid) == []
    assert (
        json.loads(
            store.con.execute("SELECT payload FROM paper_nimble_opportunities").fetchone()[0]
        )["reason"]
        == "MIN_NOTIONAL"
    )
    with store.transaction():
        engine.event(store, rid, "account_mark", "restore", {"cash": 100, "equity": 100}, time(2))
    admit(store, rid, h(side="short"), minute=3)
    from tests.test_nimble_paper import monitor

    monitor(store, rid, 3, 2)
    monitor(store, rid, 4, 0, 200)
    monitor(store, rid, 4, 2, 200)
    p = engine.state(store, rid)["closed"][0]
    assert p["margin_loss_capped"] and p["net_pnl"] == pytest.approx(-p["margin"] - p["entry_fee"])
    assert p["net_pnl"] == pytest.approx(p["gross_pnl"] - p["fees"] - p["funding"])


@pytest.mark.parametrize("phase", [22, 23])
def test_non_microstructure_geometry_causal_bar_fallback(store, monkeypatch, phase):
    import pandas as pd

    from market_signal.paper.nimble import data, spec

    hh = next(h for h in spec.baseline.bootstrap() if h.source_phase == phase)
    o = observation(hh, minute=60)
    monkeypatch.setattr(data, "load_microstructure", lambda *a, **kw: pd.DataFrame())

    def bars(*args, **kwargs):
        assert kwargs["known_at"] == time(60)
        return pd.DataFrame(
            [
                {
                    "close_time": time(60),
                    "first_observed_at": time(60),
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.0,
                }
            ]
        )

    monkeypatch.setattr(data, "load_bars", bars)
    geometry = data.Sources(store, time(60)).geometry("BTC", o)
    assert geometry["local_low"] == 99 and geometry["local_high"] == 101
    assert geometry["microstructure"] == {} and geometry["geometry_source"].endswith("bar_fallback")
    assert geometry["available_at"] <= time(60).isoformat()


def test_phase25a_postmortem_includes_separate_nimble_execution(store, project, monkeypatch):
    from typer.testing import CliRunner

    from market_signal.cli.main import app
    from market_signal.paper.v2 import engine as baseline

    baseline.register(store, now=T0)
    baseline.create(store, now=T0)
    engine.register(store, now=T0)
    rid = engine.create(store, now=T0)["run_id"]
    path = store.path
    store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(path))
    result = CliRunner().invoke(
        app,
        ["paper", "postmortem", "--start", T0.isoformat(), "--end", time(60).isoformat(), "--json"],
    )
    assert result.exit_code == 0, (result.stdout, result.exception)
    payload = json.loads(result.stdout)
    assert payload["paper_only"] and payload["nimble_execution"]["run_id"] == rid


def test_gapped_minutes_cannot_supply_geometry_or_two_minute_flow(store, monkeypatch):
    import pandas as pd

    from market_signal.paper.nimble import data

    df = pd.DataFrame(
        {"complete": [True] * 15, "minute_close": [time(i * 2).isoformat() for i in range(15)]}
    )
    monkeypatch.setattr(data, "load_microstructure", lambda *args, **kwargs: df)
    source = data.Sources(store, time(30))
    with pytest.raises(ValueError, match="GEOMETRY_UNAVAILABLE"):
        source.geometry("BTC", observation(h()))
    monkeypatch.setattr(data, "load_microstructure", lambda *args, **kwargs: df.tail(2))
    monkeypatch.setattr(source, "context", lambda asset: {})
    assert source.updates(["BTC"]) == []


def test_summary_delivery_failure_fails_scheduler_command(store, monkeypatch):
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from market_signal.cli.main import app
    from market_signal.paper.nimble import report
    from market_signal.portfolio.telegram import TelegramClient

    engine.register(store, now=T0)
    engine.create(store, now=T0)
    path = store.path
    store.close()
    monkeypatch.setenv("PRISM_DB_PATH", str(path))
    monkeypatch.setattr(
        TelegramClient, "from_settings", lambda settings: SimpleNamespace(send=lambda text: None)
    )
    monkeypatch.setattr(report, "record_daily", lambda *args, **kwargs: {"delivery": "failed"})
    result = CliRunner().invoke(app, ["paper", "nimble", "brief", "--send"])
    assert result.exit_code == 1
