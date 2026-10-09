"""Reproducible loopback HTTP + separate five-second worker demo; isolated files only."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path

import httpx

from market_signal.context.gateway.demo import sample
from market_signal.context.gateway.health import inspect
from market_signal.context.gateway.spool import Spool, now
from market_signal.context.snapshot import context_snapshot
from market_signal.data.store import Store
from market_signal.ops.runtime import claim_authority


def rss_mb(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return None


def main():
    with tempfile.TemporaryDirectory(prefix="prism-gateway-wire-") as directory:
        root = Path(directory)
        db = root / "prism.duckdb"
        with closing(Store(db)):
            pass
        claim_authority(db, "gateway_benchmark", "isolated development transport benchmark")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        token = uuid.uuid4().hex + uuid.uuid4().hex
        env = {
            **os.environ,
            "PRISM_DB_PATH": str(db),
            "PRISM_RAW_DIR": str(root / "raw"),
            "PRISM_CONTEXT_GATEWAY_DIR": str(root / "spool"),
            "PORT": str(port),
            "PRISM_RUNTIME_ROLE": "authoritative",
            "PRISM_RUNTIME_ID": "gateway_benchmark",
            "PRISM_CONTEXT_GATEWAY_TOKENS": json.dumps({token: "benchmark_v1"}),
            "PRISM_CONTEXT_OAUTH_ISSUER": "",
            "PRISM_CONTEXT_OAUTH_JWKS": "",
            "PRISM_CONTEXT_OAUTH_PRINCIPALS": "{}",
            "PRISM_CONTEXT_RATE_PER_MINUTE": "60",
        }
        env.pop("RAILWAY_ENVIRONMENT_ID", None)
        processes = []
        try:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from market_signal.context.gateway.launch import run; run(dev=True)",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(server)
            worker = subprocess.Popen(
                [sys.executable, "-m", "market_signal.context.gateway.worker"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            processes.append(worker)
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(timeout=5) as client:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if client.get(url + "/healthz").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("server did not start")
                    time.sleep(0.1)
                before = now()
                acks = []
                for _ in range(20):
                    start = time.perf_counter()
                    response = client.post(
                        url + "/context/v1/events",
                        json=sample(test=False),
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    response.raise_for_status()
                    acks.append((response.json(), time.perf_counter() - start))
                spool = Spool(root / "spool")
                deadline = time.monotonic() + 20
                while spool.pending():
                    if time.monotonic() > deadline:
                        raise RuntimeError("ingest did not catch up")
                    time.sleep(0.05)
                details = [inspect(spool, ack["receipt_id"]) for ack, _ in acks]
                latency = sorted(d["latency"]["gateway_to_available_s"] for d in details)
                ack_latency = sorted(seconds for _, seconds in acks)
                with closing(Store(db, read_only=True, lock_timeout=5)) as store:
                    early = context_snapshot(store, "BTC", before, include_positioning=False)
                    available = max(
                        datetime.fromisoformat(d["completion"]["context_available_at"])
                        for d in details
                    )
                    late = context_snapshot(
                        store, "BTC", max(now(), available), include_positioning=False
                    )
                disk = sum(p.stat().st_size for p in spool.root.rglob("*") if p.is_file())
                print(
                    json.dumps(
                        {
                            "mode": "isolated_loopback_http_separate_authoritative_worker",
                            "samples": 20,
                            "production_modified": False,
                            "ack_p50_s": ack_latency[10],
                            "ack_p95_s": ack_latency[18],
                            "gateway_to_available_p50_s": latency[10],
                            "gateway_to_available_p95_s": latency[18],
                            "gateway_to_available_max_s": max(latency),
                            "first_receipt": acks[0][0],
                            "first_event_latency": details[0]["latency"],
                            "before_excludes": not early["active_events"],
                            "after_includes": bool(late["active_events"]),
                            "first_seen_matches_receipt": late["active_events"][0]["first_seen_at"]
                            == acks[0][0]["gateway_received_at"],
                            "server_rss_mb": rss_mb(server.pid),
                            "worker_rss_mb": rss_mb(worker.pid),
                            "spool_bytes_per_receipt": disk / 20,
                        },
                        indent=2,
                    )
                )
        finally:
            for p in processes:
                p.terminate()
            for p in processes:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


if __name__ == "__main__":
    main()
