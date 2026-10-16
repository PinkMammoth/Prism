"""Real worker/CLI/ledger integration with virtual time, no network or backdated live data."""

import contextlib
import io
import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from market_signal.cli import universe_cmds
from market_signal.data.http import HttpClient
from market_signal.data.store import Store
from market_signal.ops import profiles
from market_signal.universe import cadence, ledger, source, worker
from market_signal.universe.engine import Market
from market_signal.universe.policy import POLICY

EPOCH = 1_800_000_000


class Finished(Exception):
    pass


@pytest.fixture
def radar(project, monkeypatch):
    path = project / "data" / "radar.duckdb"
    Store(path).close()
    monkeypatch.setenv("PRISM_DB_PATH", str(path))
    monkeypatch.delenv("PRISM_RUNTIME_ROLE", raising=False)
    monkeypatch.setattr(profiles, "current", lambda: SimpleNamespace(explicit=False))
    clock = [float(EPOCH)]

    class Clock:
        @staticmethod
        def now(tz):
            return datetime.fromtimestamp(clock[0], UTC)

    monkeypatch.setattr(universe_cmds, "datetime", Clock)
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    monkeypatch.setattr(
        worker.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    monkeypatch.setattr(
        HttpClient,
        "_http",
        lambda self: SimpleNamespace(event_hooks={"request": [], "response": []}),
    )
    monkeypatch.setattr(HttpClient, "close", lambda self: None)
    return path, clock


def run_worker(radar, monkeypatch, durations, *, restart_at=None):
    _path, clock = radar
    events, in_flight = [], []

    def fetch(http, prepared, now, **kwargs):
        duration = durations[len(events) % len(durations)]
        clock[0] += duration
        at = int(clock[0])
        previous = prepared["previous"] or {}
        first = previous.get("listing_first_seen", {}).get("SAT", at)
        market = Market("SAT", at, first, 100, 10e6, 3e6)
        markets = source.enrich_history([market], prepared["history"], at)
        return {
            "markets": markets,
            "listing_first_seen": {"SAT": first},
            "errors": {},
            "light": [],
            "prepared_at": now,
        }

    def invoke(*args, **kwargs):
        assert not in_flight
        in_flight.append(True)
        try:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                universe_cmds.refresh(warming=False, as_json=True)
            return SimpleNamespace(returncode=0, stdout=stream.getvalue())
        finally:
            in_flight.pop()

    def record(value):
        events.append(json.loads(value))
        if len(events) == 12 or (restart_at and len(events) == restart_at):
            raise Finished

    monkeypatch.setattr(source, "fetch", fetch)
    monkeypatch.setattr(worker.subprocess, "run", invoke)
    monkeypatch.setattr(worker.log, "info", record)
    with pytest.raises(Finished):
        worker.main()
    if restart_at:
        clock[0] += 17
        with pytest.raises(Finished):
            worker.main()
    return events


@pytest.mark.parametrize("duration", [0.1, 36, 299, 599])
def test_fixed_600s_worker_real_cli_accepts_every_slot(radar, monkeypatch, duration):
    events = run_worker(radar, monkeypatch, [duration])
    assert len(events) == 12
    assert all(e["status"] == "ACCEPTED" for e in events)
    assert [e["scheduled_deadline"] for e in events] == [EPOCH + i * 600 for i in range(12)]
    assert all(e["telemetry"]["continuity_gap_seconds"] == 600 for e in events[1:])
    assert all(e["skipped_deadlines"] == 0 for e in events)
    assert all(
        e["telemetry"]["ranking_db_commit_completed_at"] >= e["telemetry"]["evidence_clock"]
        for e in events
    )


def test_restart_waits_for_next_boundary_without_duplicate_or_overlap(radar, monkeypatch):
    events = run_worker(radar, monkeypatch, [36], restart_at=5)
    assert len(events) == 12 and all(e["status"] == "ACCEPTED" for e in events)
    assert all(e["telemetry"]["continuity_gap_seconds"] == 600 for e in events[1:])


@pytest.mark.parametrize("duration", [601, 899])
def test_genuine_overruns_are_explicit_and_reset_continuity(radar, monkeypatch, duration):
    # Exercise deadline overrun handling beyond the production child's300s timeout.
    events = run_worker(radar, monkeypatch, [duration])
    assert all(e["skipped_deadlines"] == 1 for e in events)
    assert all(e["telemetry"]["continuity_gap_seconds"] == 1200 for e in events[1:])
    path, _ = radar
    s = Store(path, read_only=True)
    try:
        assert ledger.latest(s)["states"]["SAT"]["bad"] == 1
        assert not ledger.latest(s)["rows"][0]["market"]["warmup"]["lightweight"]
    finally:
        s.close()


def test_variable_fetch_time_does_not_move_nominal_deadlines(radar, monkeypatch):
    events = run_worker(radar, monkeypatch, [0.1, 299, 36, 120])
    assert all(e["status"] == "ACCEPTED" for e in events)
    assert max(e["telemetry"]["continuity_gap_seconds"] for e in events[1:]) <= 900
    assert [e["next_scheduled_deadline"] for e in events] == [
        EPOCH + (i + 1) * 600 for i in range(12)
    ]


def test_same_slot_retry_is_suppressed_but_next_slot_is_not(radar):
    previous = {"at": EPOCH + 36, "schedule": {"slot": EPOCH, "version": cadence.VERSION}}
    assert not cadence.due(previous, EPOCH + 599)
    assert cadence.due(previous, EPOCH + 600)
    assert cadence.due({"at": EPOCH + 36}, EPOCH + 600)  # legacy snapshot


def test_36_second_fetch_real_cli_does_not_skip_second_slot(radar, monkeypatch):
    """Fails on the rejected eee205f CLI/commit implementation (second call NOT_DUE)."""
    path, clock = radar

    def fetch(http, prepared, now, **kwargs):
        clock[0] += 36
        return {
            "markets": [Market("SAT", int(clock[0]), EPOCH, 100, 10e6, 3e6)],
            "listing_first_seen": {"SAT": EPOCH},
            "errors": {},
            "light": [],
            "prepared_at": now,
        }

    monkeypatch.setattr(source, "fetch", fetch)
    for start in (EPOCH, EPOCH + 600):
        clock[0] = float(start)
        with contextlib.redirect_stdout(io.StringIO()):
            universe_cmds.refresh(warming=False, as_json=True)
    s = Store(path, read_only=True)
    try:
        assert s.con.execute("SELECT count(*) FROM universe_evaluations").fetchone()[0] == 2
        assert ledger.latest(s)["at"] == EPOCH + 636
    finally:
        s.close()


def test_refresh_lock_prevents_overlapping_manual_or_supervised_fetches(radar):
    path, _ = radar
    with cadence.refresh_lock(path), pytest.raises(RuntimeError, match="already running"):
        universe_cmds.refresh(warming=False, as_json=True)
    with cadence.refresh_lock(path):
        pass


@pytest.mark.parametrize(
    "now,expected,skipped",
    [
        (EPOCH + 36, EPOCH + 600, 0),
        (EPOCH + 600, EPOCH + 600, 0),
        (EPOCH + 601, EPOCH + 1200, 1),
        (EPOCH + 1801, EPOCH + 2400, 3),
    ],
)
def test_next_deadline_is_deterministic(now, expected, skipped):
    assert cadence.next_deadline(EPOCH, now) == (expected, skipped)


def test_missed_cycles_are_not_replayed_and_restart_uses_future_boundary():
    assert cadence.first_deadline(EPOCH + 901) == EPOCH + 1200
    assert cadence.first_deadline(EPOCH) == EPOCH
    assert cadence.next_deadline(EPOCH, EPOCH + 1801) == (EPOCH + 2400, 3)


@pytest.mark.parametrize("gap,healthy", [(900, True), (901, False)])
def test_frozen_lightweight_continuity_boundary_is_unchanged(gap, healthy):
    history = [
        {
            "observed_at": EPOCH + i * 600,
            "price": 100 + i,
            "oi_usd": 3e6,
            "volume_usd": 10e6,
            "healthy": True,
        }
        for i in range(6)
    ]
    at = EPOCH + 3000 + gap
    m = source.enrich_history([Market("SAT", at, EPOCH, 107, 10e6, 3e6)], {"SAT": history}, at)[0]
    assert m.warmup["lightweight"] is healthy
    assert POLICY.light_max_gap_seconds == 900 and POLICY.light_observations == 7


def test_timeout_is_recorded_without_overlapping_or_restarting_worker(monkeypatch):
    clock = [float(EPOCH)]
    events = []
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", lambda n: clock.__setitem__(0, clock[0] + n))

    def timeout(cmd, **kw):
        assert kw["timeout"] == 300
        clock[0] += 300
        raise subprocess.TimeoutExpired(cmd, 300)

    def record(value):
        events.append(json.loads(value))
        if len(events) == 2:
            raise Finished

    monkeypatch.setattr(worker.subprocess, "run", timeout)
    monkeypatch.setattr(worker.log, "info", record)
    with pytest.raises(Finished):
        worker.main()
    assert [e["scheduled_deadline"] for e in events] == [EPOCH, EPOCH + 600]
    assert all(e["status"] == "TIMEOUT" for e in events)


def test_late_wakeup_records_missed_slots_without_replaying_them(monkeypatch):
    clock = [float(EPOCH + 1)]
    events = []

    def sleep(seconds):
        clock[0] += seconds + 1200

    def invoke(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout='{"status":"NOT_DUE"}')

    def record(value):
        events.append(json.loads(value))
        raise Finished

    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    monkeypatch.setattr(worker.time, "sleep", sleep)
    monkeypatch.setattr(worker.subprocess, "run", invoke)
    monkeypatch.setattr(worker.log, "info", record)
    with pytest.raises(Finished):
        worker.main()
    assert events[0]["scheduled_deadline"] == EPOCH + 1200
    assert events[0]["missed_before_start"] == 1
    assert events[0]["next_scheduled_deadline"] == EPOCH + 1800
