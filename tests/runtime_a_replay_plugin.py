"""Local differential replay of existing deterministic execution fixtures.

PRISM_RUNTIME_A_REPLAY_MODE=before|after PRISM_RUNTIME_A_REPLAY_OUT=/tmp/result.json
pytest -p tests.runtime_a_replay_plugin ...
This plugin is opt-in and never imported by production code or ordinary pytest.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from market_signal.data.store import Store
from tests.test_runtime_optimisation_a import baseline, reference

PATCHES = []
RESULTS = {}
COUNTER = 0


def frozen_clock():
    return datetime(2026, 10, 11, 12, tzinfo=UTC)


def next_uuid():
    global COUNTER
    COUNTER += 1
    return UUID(int=COUNTER)


def install(target, name, value):
    p = patch.object(target, name, value)
    p.start()
    PATCHES.append(p)


def pytest_configure(config):
    import uuid

    from market_signal.context.gateway import spool as gateway_spool
    from market_signal.data import store as store_module
    from market_signal.models import domain
    from market_signal.paper.nimble import data as nimble_data
    from market_signal.paper.nimble import event_spec, worker
    from market_signal.paper.nimble import spec as nimble_spec
    from market_signal.paper.v2 import data, engine, spec

    gateway_now = gateway_spool.now
    install(gateway_spool, "now", frozen_clock)
    for name, module in list(sys.modules.items()):
        if name.startswith("market_signal.") and getattr(module, "now", None) is gateway_now:
            install(module, "now", frozen_clock)
    # Measurement clocks/RSS are injected too: preserve complete evidence bytes,
    # including telemetry, without dropping fields from comparison.
    install(engine, "time", SimpleNamespace(perf_counter=lambda: 0.0, process_time=lambda: 0.0))
    install(
        engine,
        "resource",
        SimpleNamespace(getrusage=lambda _: SimpleNamespace(ru_maxrss=1024), RUSAGE_SELF=0),
    )
    original = domain.utcnow
    for name, module in list(sys.modules.items()):
        if name.startswith("market_signal.") and getattr(module, "utcnow", None) is original:
            install(module, "utcnow", frozen_clock)
    install(uuid, "uuid4", next_uuid)
    # Store truncates UUIDs to 16 leading hex digits; tiny counters collide there.
    install(store_module, "new_id", lambda prefix: prefix + next_uuid().hex[-16:])
    for name, module in list(sys.modules.items()):
        if name.startswith("market_signal.") and hasattr(module, "uuid4"):
            install(module, "uuid4", next_uuid)
    if os.environ["PRISM_RUNTIME_A_REPLAY_MODE"] == "before":
        oracle = baseline()
        old_spec = reference("baseline_spec", "market_signal.paper.v2")
        old_nimble = reference("baseline_nimble_spec", "market_signal.paper.nimble")
        old_nimble.baseline = old_spec
        old_event = reference("baseline_event_spec", "market_signal.paper.nimble")
        old_event.spec = old_nimble
        legacy_module = reference("baseline_nimble_data", "market_signal.paper.nimble")
        # Keep fixture-injected loaders visible to both versions of the adapter.
        oracle.load_bars = lambda *a, **kw: data.load_bars(*a, **kw)
        oracle.load_microstructure = lambda *a, **kw: data.load_microstructure(*a, **kw)
        legacy_module.load_bars = lambda *a, **kw: nimble_data.load_bars(*a, **kw)
        legacy_module.load_microstructure = lambda *a, **kw: nimble_data.load_microstructure(
            *a, **kw
        )
        legacy = legacy_module.Sources
        legacy.__bases__ = (oracle.Sources,)
        install(data, "Sources", oracle.Sources)
        install(engine, "Sources", oracle.Sources)
        install(nimble_data, "Sources", legacy)
        install(worker, "Sources", legacy)
        install(spec, "bootstrap", old_spec.bootstrap)
        install(spec, "definition", old_spec.definition)
        install(spec, "canonical_definition", lambda: canonical(spec.definition()))
        install(nimble_spec, "definition", old_nimble.definition)
        install(nimble_spec, "canonical_definition", lambda: canonical(nimble_spec.definition()))
        install(event_spec, "definition", old_event.definition)
        install(event_spec, "canonical_definition", lambda: canonical(event_spec.definition()))


def canonical(value):
    from market_signal.research.lab.common import canonical_json

    return canonical_json(value)


def pytest_runtest_setup(item):
    global COUNTER
    COUNTER = 0


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.passed:
        return
    stores = []

    def collect(value):
        if isinstance(value, Store) and value not in stores:
            stores.append(value)
        elif isinstance(value, (tuple, list)):
            for member in value:
                collect(member)

    for value in item.funcargs.values():
        collect(value)
    result = {}
    for index, store in enumerate(stores):
        connection = store.con
        reopened = None
        try:
            connection.execute("SELECT 1")
        except Exception:
            reopened = Store(store.path, read_only=True)
            connection = reopened.con
        try:
            tables = [
                r[0]
                for r in connection.execute("SHOW TABLES").fetchall()
                if r[0].startswith(("paper_", "context_snapshots"))
            ]
            result[str(index)] = {
                table: hashlib.sha256(
                    repr(
                        connection.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
                    ).encode()
                ).hexdigest()
                for table in tables
            }
        finally:
            if reopened:
                reopened.close()
    RESULTS[item.nodeid] = result


def pytest_sessionfinish(session, exitstatus):
    path = Path(os.environ["PRISM_RUNTIME_A_REPLAY_OUT"])
    worker = getattr(session.config, "workerinput", {}).get("workerid")
    if worker:
        path = path.with_name(path.stem + "." + worker + path.suffix)
    elif hasattr(session.config, "workerinput") is False and session.config.option.numprocesses:
        return
    path.write_text(json.dumps(RESULTS, sort_keys=True) + "\n")
    for p in reversed(PATCHES):
        p.stop()
