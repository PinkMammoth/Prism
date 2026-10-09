"""Synthetic development demo always uses a temporary spool and temporary database."""

from __future__ import annotations

import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from starlette.testclient import TestClient

from market_signal.context.gateway.auth import Verifier
from market_signal.context.gateway.health import inspect
from market_signal.context.gateway.ingest import drain
from market_signal.context.gateway.server import create_app
from market_signal.context.gateway.spool import Spool, now
from market_signal.context.snapshot import context_snapshot
from market_signal.data.store import Store


def sample(*, test=True):
    t = now()
    return {
        "schema": "context_import_v1",
        "submission_id": uuid.uuid4().hex,
        "external_event_id": "synthetic-development-demo",
        "sent_at": t.isoformat(),
        "sender_version": "demo_v1",
        "test": test,
        "item": {
            "first_seen_at": (t - timedelta(seconds=1)).isoformat(),
            "event_time": (t - timedelta(seconds=2)).isoformat(),
            "published_at": (t - timedelta(seconds=2)).isoformat(),
            "source": "Prism development fixture",
            "source_type": "ai_monitor",
            "url": "https://example.com/prism-development-fixture",
            "assets": ["BTC"],
            "entities": ["bitcoin"],
            "category": "protocol",
            "subcategory": "protocol_upgrade",
            "title": "SYNTHETIC development fixture: Bitcoin documentation update",
            "summary": "Transport test only; no live market development.",
            "confidence": "REPORTED",
            "factual_claims": ["This is an isolated synthetic development fixture."],
            "provenance": ["Prism isolated test generator"],
        },
    }


def demo() -> dict:
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="prism-context-demo-") as directory:
        root = Path(directory)
        spool = Spool(root / "spool")
        before = now()
        app = create_app(spool, Verifier(tokens={token: "development_demo"}), dev=True)
        with TestClient(app) as client, closing(Store(root / "demo.duckdb")) as store:
            t0 = time.perf_counter()
            response = client.post(
                "/context/v1/events",
                json=sample(test=False),
                headers={"Authorization": f"Bearer {token}"},
            )
            ack_s = time.perf_counter() - t0
            if response.status_code != 202:
                raise RuntimeError(response.json())
            receipt = response.json()
            # Simulate a five-second worker cadence, included in measured latency.
            time.sleep(5)
            ingest = drain(store, spool)
            detail = inspect(spool, receipt["receipt_id"])
            after = max(now(), datetime.fromisoformat(detail["completion"]["context_available_at"]))
            early = context_snapshot(store, "BTC", before, include_positioning=False)
            late = context_snapshot(store, "BTC", after, include_positioning=False)
            eid = detail["completion"]["logical_event_id"]
            return {
                "mode": "isolated_development_http_demo",
                "production_modified": False,
                "ack_seconds": ack_s,
                "receipt": receipt,
                "ingest": ingest,
                "latency": detail["latency"],
                "context_event_id": eid,
                "first_seen_at": late["active_events"][0]["first_seen_at"],
                "before_receipt_excludes_event": not any(
                    e["event_id"] == eid for e in early["active_events"]
                ),
                "after_ingest_includes_event": any(
                    e["event_id"] == eid for e in late["active_events"]
                ),
            }
