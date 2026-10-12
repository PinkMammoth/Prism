"""Read-only public snapshot validation; writes only local evidence, never production."""

import json
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from market_signal.data.http import HttpClient
from market_signal.universe.engine import gates, rank
from market_signal.universe.policy import POLICY
from market_signal.universe.source import fetch


def main():
    root = Path("docs/evidence/dynamic-universe-v1")
    now = int(time.time())
    http = HttpClient(
        "hyperliquid",
        "https://api.hyperliquid.xyz",
        max_retries=0,
        timeout_seconds=10,
        requests_per_second=2,
    )
    wall, cpu = time.perf_counter(), time.process_time()
    try:
        result = fetch(http, {"previous": None, "history": {}, "pins": {}, "states": {}}, now)
        payloads = http.drain()
    finally:
        http.close()
    actual = int(time.time())
    markets = result["markets"]
    summary = {
        "policy_id": POLICY.identity,
        "received_at": actual,
        "prepared_at": now,
        "wall_seconds": time.perf_counter() - wall,
        "cpu_seconds": time.process_time() - cpu,
        "markets": len(markets),
        "active": sum(m.active for m in markets),
        "supported_native": sum(m.active and m.supported for m in markets),
        "hip3_quarantined": sum(not m.supported for m in markets),
        "http_requests": len(payloads),
        "http_response_bytes": sum(len(p.body) for p in payloads),
        "errors": result["errors"],
        "rejection_counts": dict(Counter(reason for m in markets for reason in gates(m, actual))),
        "markets_data": [asdict(m) for m in markets],
        "ranking": rank(markets, actual),
        "note": "First-seen listing age and unobserved 1h history are unavailable; no asset is executable. No historical listing age is inferred.",
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "snapshot-sanity.json").write_text(json.dumps(summary, indent=2) + "\n")
    # Raw public API payloads, bounded snapshot, useful for reproducible schema sanity.
    import gzip

    body = json.dumps(
        [
            {
                "params": p.params,
                "body": json.loads(p.body),
                "received_at": p.fetched_at.isoformat(),
            }
            for p in payloads
        ]
    ).encode()
    (root / "snapshot-raw.json.gz").write_bytes(gzip.compress(body, mtime=0))
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in ("markets_data", "ranking")}, indent=2
        )
    )


if __name__ == "__main__":
    main()
