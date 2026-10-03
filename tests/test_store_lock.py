"""A second process holding the DuckDB lock: Store waits (bounded) or raises DatabaseBusy."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from market_signal.data.store import DatabaseBusy, Store


def _hold(path, seconds: float) -> subprocess.Popen:
    code = (f"import duckdb, time, sys; c = duckdb.connect({str(path)!r}); "
            f"print('locked', flush=True); time.sleep({seconds}); c.close()")  # fmt: skip
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "locked"
    return p


def test_busy_raises_quickly_and_names_the_holder(tmp_path):
    db = tmp_path / "x.duckdb"
    Store(db).close()
    p = _hold(db, 30)
    try:
        t = time.monotonic()
        with pytest.raises(DatabaseBusy) as exc:
            Store(db, read_only=True, lock_timeout=0.5)
        assert time.monotonic() - t < 5
        assert exc.value.pid == p.pid and "PID" in exc.value.holder
    finally:
        p.kill()
        p.wait()


def test_waits_for_the_lock_then_opens(tmp_path):
    db = tmp_path / "y.duckdb"
    Store(db).close()
    p = _hold(db, 1.5)
    told = []
    try:
        s = Store(db, lock_timeout=30, on_wait=told.append)
        s.close()
        assert len(told) == 1 and "Waiting up to 30s" in told[0]
    finally:
        p.wait()


def test_non_lock_errors_are_not_swallowed(tmp_path):
    bad = tmp_path / "not_a_db.duckdb"
    bad.write_text("this is not a database")
    with pytest.raises(Exception) as exc:
        Store(bad, lock_timeout=5)
    assert not isinstance(exc.value, DatabaseBusy)
