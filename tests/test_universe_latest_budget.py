"""Latest-evaluation reads retain causal filtering under a bounded DB memory budget."""

import base64
import gzip
import json
import random
from types import SimpleNamespace

import duckdb

from market_signal.universe import ledger
from market_signal.universe.policy import POLICY


def test_refresh_database_budget_applies_at_connection(tmp_path):
    from market_signal.cli.universe_cmds import REFRESH_DB_CONFIG
    from market_signal.data.store import Store

    store = Store(
        tmp_path / "bounded.duckdb", authority_check=False, connect_config=REFRESH_DB_CONFIG
    )
    try:
        assert store.con.execute("SELECT current_setting('threads')").fetchone()[0] == 1
        assert (
            store.con.execute("SELECT current_setting('memory_limit')").fetchone()[0] == "61.0 MiB"
        )
        assert store.con.execute("SELECT count(*) FROM universe_evaluations").fetchone()[0] == 0
    finally:
        store.close()


def test_latest_selects_policy_and_causal_clock(tmp_path):
    con = duckdb.connect(str(tmp_path / "history.duckdb"))
    store = SimpleNamespace(con=con)
    try:
        assert ledger.latest(store) is None
        con.execute(
            "CREATE TABLE universe_evaluations "
            "(evaluation_id VARCHAR, policy_id VARCHAR, evaluated_at BIGINT, payload BLOB)"
        )
        assert ledger.latest(store) is None
        for eid, policy, at in (
            ("first", POLICY.identity, 10),
            ("next", POLICY.identity, 20),
            ("other", "other", 30),
        ):
            con.execute(
                "INSERT INTO universe_evaluations VALUES (?,?,?,?)",
                [eid, policy, at, gzip.compress(json.dumps({"at": at}).encode())],
            )
        assert ledger.latest(store) == {"at": 20, "evaluation_id": "next"}
        assert ledger.latest(store, at=15) == {"at": 10, "evaluation_id": "first"}
        assert ledger.latest(store, at=5) is None
    finally:
        con.close()


def test_latest_does_not_materialize_historical_payloads(tmp_path):
    path = str(tmp_path / "large-history.duckdb")
    con = duckdb.connect(path)
    try:
        con.execute(
            "CREATE TABLE universe_evaluations "
            "(evaluation_id VARCHAR, policy_id VARCHAR, evaluated_at BIGINT, payload BLOB)"
        )
        rng = random.Random(17)
        # Distinct compressed payloads prevent constant-value/dedup shortcuts.
        for i in range(128):
            payload = gzip.compress(
                json.dumps(
                    {"at": i, "padding": base64.b64encode(rng.randbytes(128 * 1024)).decode()}
                ).encode()
            )
            con.execute(
                "INSERT INTO universe_evaluations VALUES (?,?,?,?)",
                [str(i), POLICY.identity, i, payload],
            )
    finally:
        con.close()
    con = duckdb.connect(path, read_only=True, config={"threads": "1", "memory_limit": "4MB"})
    try:
        result = ledger.latest(SimpleNamespace(con=con), at=64)
        assert result["evaluation_id"] == "64" and result["at"] == 64
    finally:
        con.close()
