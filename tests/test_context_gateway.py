"""Phase 26A transport, auth, timing, failure recovery and execution boundary."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient

from market_signal.context import ledger
from market_signal.context.entities import load_entities
from market_signal.context.gateway.auth import Verifier
from market_signal.context.gateway.contract import Submission, action_schema, validate
from market_signal.context.gateway.demo import sample
from market_signal.context.gateway.health import inspect, status
from market_signal.context.gateway.ingest import drain
from market_signal.context.gateway.server import MAX_BODY, create_app, submit
from market_signal.context.gateway.spool import Spool, now
from market_signal.context.snapshot import context_snapshot
from market_signal.data.store import Store

TOKEN = "t" * 48
IDENTITY = "work_test_v1"


@pytest.fixture
def sp(tmp_path):
    return Spool(tmp_path / "spool")


@pytest.fixture
def client(sp):
    with TestClient(create_app(sp, Verifier(tokens={TOKEN: IDENTITY}), dev=True)) as c:
        yield c


def post(client, payload=None, token=TOKEN):
    return client.post(
        "/context/v1/events", json=payload or sample(), headers={"Authorization": f"Bearer {token}"}
    )


def test_auth_before_processing_and_rotation(client, sp):
    assert post(client, token="invalid").status_code == 401
    assert sp.records() == []
    assert post(client).status_code == 202
    rotated = "r" * 48
    v = Verifier(tokens={TOKEN: IDENTITY, rotated: IDENTITY})
    assert asyncio.run(v.verify_token(TOKEN)).client_id == IDENTITY
    assert asyncio.run(v.verify_token(rotated)).client_id == IDENTITY
    assert asyncio.run(Verifier(tokens={rotated: IDENTITY}).verify_token(TOKEN)) is None
    assert TOKEN not in (sp.root / "audit.jsonl").read_text()


def test_authoritative_receipt_retry_conflict(client, sp):
    raw = sample()
    before = now()
    first = post(client, raw).json()
    assert first["status"] == "ACCEPTED"
    assert before.isoformat() <= first["gateway_received_at"] <= now().isoformat()
    second = post(client, raw).json()
    assert second["status"] == "DUPLICATE"
    assert second["receipt_id"] == first["receipt_id"]
    assert second["gateway_received_at"] == first["gateway_received_at"]
    assert len(sp.records()) == 1
    raw["item"]["title"] = "Different event"
    assert post(client, raw).status_code == 409


def test_stale_unknown_request_rejected_but_accepted_retry_allowed(client, sp):
    raw = sample()
    raw["sent_at"] = (now() - timedelta(minutes=10)).isoformat()
    raw["item"]["first_seen_at"] = (now() - timedelta(minutes=11)).isoformat()
    assert post(client, raw).json()["error"] == "stale_submission"
    fresh = sample()
    assert post(client, fresh).status_code == 202
    # Timestamp window applies only to new receipts: a durable retry remains idempotent.
    sub, obs = validate(fresh, now(), load_entities())
    assert sp.accept(fresh, sub, obs, IDENTITY, now() + timedelta(days=1))["status"] == "DUPLICATE"


@pytest.mark.parametrize(
    "mutation",
    [
        {"direction": "long"},
        {"size": 100},
        {"stop": 1},
        {"target": 2},
        {"materiality": 100},
        {"interpretation": "possible sale"},
        {"first_seen_at": "2050-01-01T00:00:00Z"},
        {"published_at": "2050-01-01T00:00:00Z"},
        {"event_time": "2050-01-01T00:00:00Z"},
        {"entities": ["imaginary_foundation"]},
        {"assets": ["MADEUP"]},
        {"subcategory": "imaginary"},
        {"category": "invented"},
        {"confidence": "OFFICIAL"},
        {"confidence": "CONFIRMED"},
        {"confidence": "DENIED"},
        {"source_type": "regulator"},
        {"update_kind": "denial"},
        {"dedup_key": "forced:event"},
        {"relevance_end": "2050-01-01T00:00:00Z"},
        {"url": "https://127.0.0.1/private"},
        {"url": "https://user:secret@example.com/private"},
        {"attributes": {"kind": "generic", "facts": {"order": "buy"}}},
    ],
)
def test_schema_and_ai_boundary(client, sp, mutation):
    raw = sample()
    raw["item"].update(mutation)
    assert post(client, raw).status_code == 422
    assert sp.records() == []


@pytest.mark.parametrize(
    "body", ["{", '{"schema":1,"schema":2}', '{"n":NaN}', "[1]", '{"n":1e999}']
)
def test_malformed_json(client, body):
    assert (
        client.post(
            "/context/v1/events", content=body, headers={"Authorization": f"Bearer {TOKEN}"}
        ).status_code
        == 400
    )


def test_schema_envelope_and_limits(client):
    raw = sample()
    raw["schema"] = "other_v1"
    assert post(client, raw).status_code == 422
    raw = sample()
    raw["gateway_received_at"] = now().isoformat()
    assert post(client, raw).status_code == 422
    assert (
        client.post(
            "/context/v1/events",
            content=b"x" * (MAX_BODY + 1),
            headers={"Authorization": f"Bearer {TOKEN}"},
        ).status_code
        == 413
    )
    raw = sample()
    raw["item"]["assets"] = ["BTC"] * 17
    assert post(client, raw).status_code == 422
    for _ in range(10):
        assert post(client).status_code == 202
    assert post(client).status_code == 429


def test_aliases_and_marketwide(client, sp):
    raw = sample()
    raw["item"].update(assets=["Bitcoin"], entities=["Hyperliquid"])
    assert post(client, raw).status_code == 202
    o = sp.records()[0]["observation"]
    assert o["assets"] == ["BTC"] and o["entities"] == ["hyperliquid"]
    raw = sample()
    raw["item"].update(assets=[], entities=[], market_wide=True)
    assert post(client, raw).status_code == 202


def test_fsync_before_ack_and_restart(client, sp, monkeypatch):
    real = os.fsync
    calls = []

    def fsync(fd):
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    ack = post(client).json()
    assert len(calls) >= 5  # receipt file+directory, seal file+directory, audit file
    recovered = Spool(sp.root)
    assert recovered.read("receipts", ack["receipt_id"])
    assert len(recovered.pending()) == 1


def test_retry_fsyncs_existing_receipt_and_timing_seal(client, sp, monkeypatch):
    raw = sample()
    original = post(client, raw).json()
    real = os.fsync
    paths = []

    def fsync(fd):
        paths.append(os.readlink(f"/proc/self/fd/{fd}"))
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    retry = post(client, raw).json()
    assert retry["status"] == "DUPLICATE"
    assert retry["spool_fsynced_at"] == original["spool_fsynced_at"]
    rid = original["receipt_id"]
    for kind in ("receipts", "seals"):
        assert str(sp.root / kind / f"{rid}.json") in paths
        assert str(sp.root / kind) in paths


def test_tls_rejections_are_globally_rate_limited(sp):
    app = create_app(sp, Verifier(tokens={TOKEN: IDENTITY}))
    with TestClient(app, base_url="http://testserver") as client:
        for _ in range(120):
            assert client.get("/healthz").status_code == 400
        assert client.get("/healthz").status_code == 429


def test_spool_failure_no_success(client, sp, monkeypatch):
    def fail(fd):
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail)
    response = post(client)
    assert response.status_code == 503
    assert response.json()["error"] == "spool_failure"
    assert sp.records() == []


def test_full_spool(client, sp):
    sp.max_bytes = 0
    assert post(client).json()["error"] == "spool_full"


def test_reject_diagnostics_cannot_fill_a_full_spool(client, sp):
    sp.max_bytes = 0
    assert post(client, token="invalid").status_code == 401
    assert not (sp.root / "audit.jsonl").exists()
    assert sp.last_failure_at
    assert sp.records() == []


def test_spool_write_failures_count_as_rejects(sp):
    with sp.lock():
        sp.audit("spool_failure")
    result = status(sp)
    assert result["rejected_24h"] == 1
    assert result["last_rejected_submission"]
    assert result["provider_quality"]["accepted_rate_24h"] == 0


def test_durable_retry_survives_full_diagnostic_capacity(client, sp):
    raw = sample()
    original = post(client, raw).json()
    audit = (sp.root / "audit.jsonl").read_bytes()
    sp.max_bytes = 0
    retry = post(client, raw)
    assert retry.status_code == 200
    assert retry.json()["receipt_id"] == original["receipt_id"]
    assert retry.json()["gateway_received_at"] == original["gateway_received_at"]
    assert (sp.root / "audit.jsonl").read_bytes() == audit
    assert sp.last_failure_at
    assert len(sp.records()) == 1


def test_snapshot_timing_dedupe_and_audit(client, sp, tmp_path):
    raw = sample(test=False)
    before = now()
    ack = post(client, raw).json()
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        result = drain(store, sp)
        assert result["ingested"] == 1
        detail = inspect(sp, ack["receipt_id"])
        eid = detail["completion"]["logical_event_id"]
        state = ledger.state_asof(store, eid)
        assert state["first_seen_at"] == ack["gateway_received_at"]
        assert state["confidence"] == "REPORTED"
        assert not context_snapshot(store, "BTC", before, include_positioning=False)[
            "active_events"
        ]
        assert (
            context_snapshot(
                store,
                "BTC",
                max(
                    now(),
                    datetime.fromisoformat(
                        sp.read("done", sp.records()[0]["receipt_id"])["context_available_at"]
                    ),
                ),
                include_positioning=False,
            )["active_events"][0]["event_id"]
            == eid
        )
        raw["submission_id"] = "another-observation"
        assert post(client, raw).status_code == 202
        assert drain(store, sp)["ingested"] == 1
        assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
        assert store.con.execute("SELECT count(*) FROM context_gateway_ingests").fetchone()[0] == 2
        assert detail["latency"]["gateway_to_available_s"] >= 0


def test_dedupe_existing_official_and_later_corroboration(client, sp, tmp_path):
    raw = sample(test=False)
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        old = now() - timedelta(seconds=10)
        source = {
            "source_id": "official_provider",
            "source_type": "protocol_announcement",
            "subcategory": "protocol_upgrade",
            "title": "Bitcoin update",
            "confidence": "OFFICIAL",
            "source_ref": raw["item"]["url"],
            "assets": ["BTC"],
            "entities": ["bitcoin"],
        }
        eid = ledger.ingest(store, [source], now=old).new_events[0]
        ack = post(client, raw).json()
        assert drain(store, sp)["ingested"] == 1
        st = ledger.state_asof(store, eid)
        assert st["first_seen_at"] == old.isoformat()
        assert st["first_source"] == "official_provider"
        assert st["confidence"] == "OFFICIAL"
        assert st["title"] == "Bitcoin update"
        assert st["summary"] == ""
        assert st["sources"] == ["official_provider", "chatgpt_work_v1"]
        assert sp.read("done", ack["receipt_id"])["logical_event_id"] == eid
        assert len(st["history"]) == 2


def test_test_category_excluded(client, sp, tmp_path):
    post(client, sample(test=True))
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        assert drain(store, sp)["ingested"] == 1
        assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 0
        assert sp.read("done", sp.records()[0]["receipt_id"])["ingest_status"] == "TEST_EXCLUDED"


def test_optional_test_flag_defaults_to_a_normal_context_event(client, sp, tmp_path):
    raw = sample(test=False)
    del raw["test"]
    ack = post(client, raw).json()
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        assert drain(store, sp)["ingested"] == 1
        detail = inspect(sp, ack["receipt_id"])
        assert detail["completion"]["ingest_status"] == "INGESTED"
        assert detail["completion"]["logical_event_id"]
        assert status(sp)["p50_ingest_latency_s"] is not None


def test_ingest_failure_catchup_and_commit_recovery(client, sp, tmp_path, monkeypatch):
    ack = post(client, sample(test=False)).json()
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        real = ledger.ingest

        def fail(*a, **kw):
            raise RuntimeError("temporarily locked")

        monkeypatch.setattr(ledger, "ingest", fail)
        assert drain(store, sp)["errors"] == 1
        assert len(sp.pending()) == 1
        monkeypatch.setattr(ledger, "ingest", real)
        done = sp.complete

        def fail_complete(*a):
            raise OSError("killed after DB commit")

        monkeypatch.setattr(sp, "complete", fail_complete)
        assert drain(store, sp)["errors"] == 1
        assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
        monkeypatch.setattr(sp, "complete", done)
        assert drain(store, sp)["ingested"] == 1
        assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
        assert sp.read("done", ack["receipt_id"])["recovered_after_commit"] is True


def test_tls_and_public_routes(sp):
    with TestClient(create_app(sp, Verifier(tokens={TOKEN: IDENTITY}))) as client:
        assert post(client).status_code == 400
    with TestClient(
        create_app(sp, Verifier(tokens={TOKEN: IDENTITY})), base_url="https://testserver"
    ) as client:
        assert post(client).status_code == 202
        assert client.get("/healthz").json() == {"running": True}
        for path in (
            "/",
            "/docs",
            "/debug",
            "/db",
            "/orders",
            "/risk",
            "/paper",
            "/context/v1/receipts",
        ):
            assert client.get(path).status_code == 404
        assert client.post("/mcp", json={}).status_code == 401
        assert post(client, token=TOKEN).status_code == 202
        assert (
            client.post("/mcp", json={}, headers={"Authorization": f"Bearer {TOKEN}"}).status_code
            == 401
        )


def test_gateway_health(client, sp):
    raw = sample()
    post(client, raw)
    post(client, raw)
    for _ in range(10):
        post(client, token="invalid")
    h = status(sp)
    assert datetime.fromisoformat(h["started_at"]).tzinfo is not None
    assert h["running"] and h["accepted_24h"] == 1 and h["duplicates_24h"] == 1
    assert h["rejected_24h"] == 10 and h["spool_backlog"] == 1
    assert "auth_rejected_spike" in h["alerts"]


def test_oauth_signature_scope_subject_audience_and_expiry():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = Verifier(
        issuer="https://issuer.example.com/",
        audience="https://gateway.example.com/mcp",
        jwks_url="https://issuer.example.com/jwks",
        principals={"user|client": IDENTITY},
    )

    class Keys:
        def get_signing_key_from_jwt(self, token):
            class K:
                pass

            k = K()
            k.key = key.public_key()
            return k

    v.jwks = Keys()
    claims = {
        "iss": v.issuer,
        "aud": v.audience,
        "sub": "user",
        "azp": "client",
        "iat": int(now().timestamp()),
        "exp": int(now().timestamp()) + 300,
        "scope": "context:submit",
    }
    token = jwt.encode(claims, key, algorithm="RS256")
    assert asyncio.run(v.verify_token(token)).claims["integration_id"] == IDENTITY
    for mutation in (
        {"aud": "wrong"},
        {"iss": "wrong"},
        {"scope": "orders:write"},
        {"sub": "untrusted"},
        {"azp": "other"},
        {"exp": 0},
    ):
        assert (
            asyncio.run(v.verify_token(jwt.encode(claims | mutation, key, algorithm="RS256")))
            is None
        )
    assert (
        asyncio.run(v.verify_token(jwt.encode(claims, "wrongkey" * 8, algorithm="HS256"))) is None
    )


def test_work_tool_schema_and_boundary(sp):
    app = create_app(sp, Verifier(tokens={TOKEN: IDENTITY}), dev=True)
    tools = asyncio.run(app.mcp.list_tools())
    assert [t.name for t in tools] == ["submit_market_event"]
    schema = tools[0].inputSchema["properties"]["payload"]
    assert tools[0].inputSchema == action_schema()
    assert schema["required"] == Submission.model_json_schema(by_alias=True)["required"]
    assert tools[0].annotations.idempotentHint is True
    assert tools[0].meta["securitySchemes"][0]["scopes"] == ["context:submit"]
    assert asyncio.run(app.mcp.list_resources()) == []
    assert asyncio.run(app.mcp.list_prompts()) == []


def test_no_execution_imports_or_process_access():
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "src/market_signal/context/gateway" / name
        for name in ("auth.py", "contract.py", "spool.py", "server.py", "ingest.py", "health.py")
    ]
    forbidden = (
        "paper",
        "execution",
        "signing",
        "private_key",
        "risk",
        "strategy",
        "thesis",
        "subprocess",
    )
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import | ast.ImportFrom):
                modules = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                assert not any(part in m.split(".") for part in forbidden for m in modules), (
                    path,
                    modules,
                )
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import sys; import market_signal.context.gateway.server; print('\\n'.join(sys.modules))",
        ],
        text=True,
    )
    assert not any(
        f"market_signal.{part}." in output for part in ("paper", "execution", "strategies")
    )


def test_oauth_mcp_call_and_synchronous_receipt(sp, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = Verifier(
        issuer="https://issuer.example.com/",
        audience="https://testserver/mcp",
        jwks_url="https://issuer.example.com/jwks",
        principals={"user|client": IDENTITY},
    )

    class Keys:
        def get_signing_key_from_jwt(self, token):
            class K:
                pass

            k = K()
            k.key = key.public_key()
            return k

    verifier.jwks = Keys()
    token = jwt.encode(
        {
            "iss": verifier.issuer,
            "aud": verifier.audience,
            "sub": "user",
            "azp": "client",
            "iat": int(now().timestamp()),
            "exp": int(now().timestamp()) + 300,
            "scope": "context:submit",
        },
        key,
        algorithm="RS256",
    )
    with TestClient(create_app(sp, verifier), base_url="https://testserver") as client:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200 and metadata.json()["resource"] == verifier.audience
        init = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert init.status_code == 200
        tools = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert [t["name"] for t in tools.json()["result"]["tools"]] == ["submit_market_event"]
        raw = sample(test=False)
        before = now()
        call = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "submit_market_event", "arguments": {"payload": raw}},
        }
        response = client.post("/mcp", headers=headers, json=call)
        assert response.status_code == 200
        result = response.json()["result"]
        assert "structuredContent" in result, result
        ack = result["structuredContent"]
        assert ack["status"] == "ACCEPTED" and sp.read("receipts", ack["receipt_id"])
        assert (
            client.post("/mcp", headers=headers, json=call).json()["result"]["structuredContent"][
                "status"
            ]
            == "DUPLICATE"
        )
        with closing(Store(tmp_path / "ctx.duckdb")) as store:
            assert drain(store, sp)["ingested"] == 1
            assert not context_snapshot(store, "BTC", before, include_positioning=False)[
                "active_events"
            ]
            assert context_snapshot(
                store,
                "BTC",
                max(
                    now(),
                    datetime.fromisoformat(
                        sp.read("done", sp.records()[0]["receipt_id"])["context_available_at"]
                    ),
                ),
                include_positioning=False,
            )["active_events"]


def test_crash_after_fsync_before_response(sp, tmp_path):
    raw = sample(test=False)
    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(raw))
    code = """
import json, os, sys
from pathlib import Path
from market_signal.context.gateway.spool import Spool, now
from market_signal.context.gateway.server import submit
spool = Spool(Path(sys.argv[1]))
spool.audit = lambda *a, **kw: os._exit(97)
submit(spool, json.loads(Path(sys.argv[2]).read_text()), "work_test_v1", now())
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(sp.root), str(payload_file)], capture_output=True
    )
    assert process.returncode == 97
    recovered = Spool(sp.root)
    assert len(recovered.pending()) == 1
    ack = submit(recovered, raw, IDENTITY, now())
    assert ack["status"] == "DUPLICATE"
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        assert drain(store, recovered)["ingested"] == 1
        assert len(recovered.pending()) == 0


def test_ingest_crash_rolls_back_entire_receipt(client, sp, tmp_path, monkeypatch):
    post(client, sample(test=False))
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        real = ledger.record_run

        def fail(*a, **kw):
            raise RuntimeError("crash after ledger wrote, before receipt commit")

        monkeypatch.setattr(ledger, "record_run", fail)
        assert drain(store, sp)["errors"] == 1
        for table in ("context_events", "context_event_updates", "context_gateway_ingests"):
            assert store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        monkeypatch.setattr(ledger, "record_run", real)
        assert drain(store, sp)["ingested"] == 1


def test_partial_audit_restart(client, sp):
    post(client)
    with (sp.root / "audit.jsonl").open("ab") as f:
        f.write(b'{"partial":')
    post(client)
    assert len(sp.audits()) == 2


def test_worker_respects_runtime_lock(client, sp, tmp_path, monkeypatch):
    from market_signal.context.gateway.worker import tick
    from market_signal.ops.runtime import runtime_lock

    post(client)
    db = tmp_path / "existing.duckdb"
    with runtime_lock(db, "other", wait=0):
        result = tick(db, sp)
    assert result["busy"] is True and result["backlog"] == 1
    assert not db.exists()


def test_actual_duckdb_lock_catchup(client, sp, tmp_path, monkeypatch):
    from market_signal.context.gateway.worker import tick
    from market_signal.ops.runtime import claim_authority

    db = tmp_path / "locked.duckdb"
    with closing(Store(db)):
        pass
    claim_authority(db, "gw_test", "isolated gateway lock test")
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setenv("PRISM_RUNTIME_ID", "gw_test")
    post(client, sample(test=False))
    code = """
import sys
from pathlib import Path
from market_signal.data.store import Store
store = Store(Path(sys.argv[1]), authority_check=False)
print("ready", flush=True)
sys.stdin.readline()
store.close()
"""
    p = subprocess.Popen(
        [sys.executable, "-c", code, str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert p.stdout.readline().strip() == "ready"
        from market_signal.data.store import DatabaseBusy

        with pytest.raises(DatabaseBusy):
            tick(db, sp)
        assert len(sp.pending()) == 1
    finally:
        p.communicate("release\n", timeout=10)
    assert tick(db, sp)["ingested"] == 1
    assert not sp.pending()


def test_provider_quality_later_corroboration_and_denial(client, sp, tmp_path):
    from market_signal.context.gateway.quality import describe

    raw = sample(test=False)
    ack = post(client, raw).json()
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        drain(store, sp)
        eid = sp.read("done", ack["receipt_id"])["logical_event_id"]
        source = {
            "source_id": "official_a",
            "source_type": "protocol_announcement",
            "subcategory": "protocol_upgrade",
            "title": raw["item"]["title"],
            "confidence": "OFFICIAL",
            "source_ref": raw["item"]["url"],
            "assets": ["BTC"],
            "entities": ["bitcoin"],
        }
        ledger.ingest(store, [source], now=now())
        q = describe(store)
        assert q["descriptive_only"] and q["corroborated_later"] == 1
        assert q["median_time_to_corroboration_s"] >= 0
        ledger.ingest(
            store,
            [source | {"source_id": "official_b", "confidence": "DENIED", "update_kind": "denial"}],
            now=now(),
        )
        assert describe(store)["denied_later"] == 1
        assert ledger.state_asof(store, eid)["confidence"] == "DENIED"


def test_schema_forbids_embedded_execution(client):
    for key in ("position_size", "order_parameters", "executable_code", "shell", "materiality"):
        raw = sample()
        raw["item"]["attributes"] = {"kind": "generic", "facts": {key: "100"}}
        assert post(client, raw).status_code == 422


def test_dedicated_entrypoint_import_boundary():
    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import sys; import market_signal.context.gateway.launch; print('\\n'.join(sys.modules))",
        ],
        text=True,
    )
    assert "market_signal.cli.main" not in output
    assert "market_signal.data.store" not in output
    assert not any(
        f"market_signal.{part}." in output for part in ("paper", "execution", "strategies")
    )


def test_action_json_schema_references_resolve():
    from jsonschema import Draft202012Validator

    schema = action_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({"payload": sample()})
    artifact = (
        Path(__file__).resolve().parents[1]
        / "config/context/gateway/submit_market_event.schema.json"
    )
    assert json.loads(artifact.read_text())["inputSchema"] == schema


def test_timestamps_and_flags_are_strict_json(client):
    for key, value in (
        ("first_seen_at", 0),
        ("published_at", 0),
        ("market_wide", "true"),
        ("scheduled", 1),
    ):
        raw = sample()
        raw["item"][key] = value
        assert post(client, raw).status_code == 422


def test_work_report_never_overwrites_official_facts(client, sp, tmp_path):
    raw = sample(test=False)
    raw["item"]["summary"] = "An unsupported assertion that must remain just a report."
    raw["item"]["attributes"] = {"kind": "generic", "facts": {"amount_btc": 12345}}
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        old = now() - timedelta(seconds=10)
        source = {
            "source_id": "official_fact_provider",
            "source_type": "protocol_announcement",
            "subcategory": "protocol_upgrade",
            "title": "Official title",
            "summary": "Official facts",
            "confidence": "OFFICIAL",
            "source_ref": raw["item"]["url"],
            "assets": ["BTC"],
            "entities": ["bitcoin"],
            "attributes": {"kind": "generic", "facts": {"amount_btc": 1}},
        }
        eid = ledger.ingest(store, [source], now=old).new_events[0]
        ack = post(client, raw).json()
        assert drain(store, sp)["ingested"] == 1
        state = ledger.state_asof(store, eid)
        assert state["title"] == "Official title" and state["summary"] == "Official facts"
        assert state["attributes"]["facts"]["amount_btc"] == 1
        assert state["confidence"] == "OFFICIAL"
        update = store.con.execute(
            "SELECT observation FROM context_event_updates WHERE event_id=?", [eid]
        ).fetchone()[0]
        assert json.loads(update)["summary"] == raw["item"]["summary"]
        assert json.loads(update)["confidence"] == "REPORTED"
        # Another publication found by the same Work provider enriches URL provenance only.
        raw["submission_id"] = "another-source"
        raw["item"]["url"] = "https://example.com/second-source"
        post(client, raw)
        assert drain(store, sp)["ingested"] == 1
        assert (
            store.con.execute(
                "SELECT count(*) FROM context_event_updates WHERE event_id=?", [eid]
            ).fetchone()[0]
            == 2
        )
        assert sp.read("done", ack["receipt_id"])["logical_event_id"] == eid


def test_interpretation_cannot_hide_in_facts(client):
    raw = sample()
    raw["item"]["attributes"] = {"kind": "generic", "facts": {"interpretation": "sale risk"}}
    assert post(client, raw).status_code == 422


def test_actual_kill_after_commit_recovery(client, sp, tmp_path):
    import signal

    ack = post(client, sample(test=False)).json()
    db = tmp_path / "killed_ingest.duckdb"
    with closing(Store(db)):
        pass
    code = """
import os, signal, sys
from pathlib import Path
from market_signal.context.gateway.spool import Spool
from market_signal.context.gateway.ingest import drain
from market_signal.data.store import Store
spool = Spool(Path(sys.argv[2]))
spool.complete = lambda *a, **kw: os.kill(os.getpid(), signal.SIGKILL)
store = Store(Path(sys.argv[1]))
drain(store, spool)
"""
    child = subprocess.run([sys.executable, "-c", code, str(db), str(sp.root)], capture_output=True)
    assert child.returncode == -signal.SIGKILL
    assert sp.read("done", ack["receipt_id"]) is None
    with closing(Store(db)) as store:
        assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
        assert drain(store, sp)["ingested"] == 1
        assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
        assert sp.read("done", ack["receipt_id"])["recovered_after_commit"]


def test_duplicate_spool_entry_is_safe(client, sp, tmp_path, monkeypatch):
    post(client, sample(test=False))
    rec = sp.pending()[0]
    pending = sp.pending
    monkeypatch.setattr(sp, "pending", lambda: [rec, rec])
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        assert drain(store, sp)["ingested"] == 2
        assert store.con.execute("SELECT count(*) FROM context_events").fetchone()[0] == 1
        assert store.con.execute("SELECT count(*) FROM context_gateway_ingests").fetchone()[0] == 1
    monkeypatch.setattr(sp, "pending", pending)
    assert not sp.pending()


def test_ingestion_mutates_context_and_information_outbox_only(client, sp, tmp_path):
    post(client, sample(test=False))
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        names = [
            r[0]
            for r in store.con.execute("SHOW TABLES").fetchall()
            if not r[0].startswith("context_") and r[0] != "paper_nimble_triggers"
        ]
        before = {
            name: store.con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names
        }
        assert drain(store, sp)["ingested"] == 1
        after = {
            name: store.con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names
        }
        assert before == after


def test_no_trading_route_accepts_write(client):
    for path in ("/orders", "/paper/trades", "/risk", "/strategies", "/shell", "/db"):
        assert (
            client.post(
                path,
                json={"order": "buy", "size": 100},
                headers={"Authorization": f"Bearer {TOKEN}"},
            ).status_code
            == 404
        )


def test_work_cannot_reactivate_denied_event(client, sp, tmp_path):
    raw = sample(test=False)
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        source = {
            "source_id": "official_denial",
            "source_type": "protocol_announcement",
            "subcategory": "protocol_upgrade",
            "title": raw["item"]["title"],
            "confidence": "DENIED",
            "source_ref": raw["item"]["url"],
            "assets": ["BTC"],
        }
        eid = ledger.ingest(store, [source], now=now()).new_events[0]
        post(client, raw)
        assert drain(store, sp)["ingested"] == 1
        assert ledger.state_asof(store, eid)["confidence"] == "DENIED"
        assert not ledger.is_active(ledger.state_asof(store, eid), now())


def test_internal_timestamps_survive_clock_backstep(sp, tmp_path, monkeypatch):
    import market_signal.context.gateway.ingest as ingest_module
    import market_signal.context.gateway.spool as spool_module

    received = now()
    raw = sample(test=False)
    received = max(received, now())
    monkeypatch.setattr(spool_module, "now", lambda: received - timedelta(seconds=10))
    ack = submit(sp, raw, IDENTITY, received)
    assert ack["spool_fsynced_at"] >= ack["gateway_received_at"]
    monkeypatch.setattr(ingest_module, "now", lambda: received - timedelta(seconds=10))
    with closing(Store(tmp_path / "ctx.duckdb")) as store:
        assert drain(store, sp)["ingested"] == 1
        detail = inspect(sp, ack["receipt_id"])
        assert detail["latency"]["gateway_to_available_s"] >= 0
        assert context_snapshot(store, "BTC", received, include_positioning=False)["active_events"]
