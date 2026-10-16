"""Export only public DU ledger decisions and aggregate prospective outcomes, read-only."""

import gzip
import hashlib
import json
import sys

sys.path.insert(0, "/opt/prism/current/src")
from market_signal.data.store import Store

store = Store("/data/prism.duckdb", read_only=True, lock_timeout=30)
try:
    evaluations = []
    for eid, at, payload in store.con.execute(
        "SELECT evaluation_id,evaluated_at,payload FROM universe_evaluations ORDER BY evaluated_at"
    ).fetchall():
        ev = json.loads(gzip.decompress(payload))
        assert ev["stage"] == "RANKING ONLY"
        evaluations.append(
            {
                "id": eid,
                "stored_evaluated_at": at,
                "compressed_payload_sha256": hashlib.sha256(payload).hexdigest(),
                "decision": ev,
            }
        )
    outcomes = store.con.execute(
        "SELECT horizon_minutes,count(*),sum(octet_length(encode(CAST(payload AS VARCHAR)))),min(observed_at),max(observed_at) FROM universe_outcomes GROUP BY 1 ORDER BY 1"
    ).fetchall()
    out = {
        "public_market_data_only": True,
        "evaluations": evaluations,
        "outcomes_by_horizon": outcomes,
    }
finally:
    store.close()
print(json.dumps(out, default=str, separators=(",", ":")))
