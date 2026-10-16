"""Run real prepare/fetch, refuse every writable open, and probe the commit handoff lock."""

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, "/opt/prism/current/src")
from dotenv import dotenv_values

from market_signal.cli import universe_cmds
from market_signal.ops import runtime
from market_signal.universe import source

os.environ.update({k: v for k, v in dotenv_values("/etc/prism/prism.env").items() if v is not None})
result = {"diagnostic_only": True, "live_writes_forbidden": True, "start": time.time()}
real_open = universe_cmds.open_store
real_fetch = source.fetch


class DiagnosticStop(Exception):
    pass


@contextmanager
def read_open(read_only=False):
    assert read_only, "live writable opens forbidden"
    with real_open(read_only=True) as value:
        yield value


def fetch(http, prepared, now, **kwargs):
    result["previous_evidence_clock"] = prepared["previous"]["at"] if prepared["previous"] else None
    result["stage"] = "fetch"
    value = real_fetch(http, prepared, now, **kwargs)
    result["fetch_completed"] = time.time()
    result["markets"] = len(value["markets"])
    result["source_errors"] = len(value["errors"])
    result["stage"] = "commit_handoff"
    return value


@contextmanager
def handoff_probe(db, job, wait):
    path = db.with_name(db.name + ".runtime.lock")
    with path.open("r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result["runtime_lock_busy"] = True
            try:
                result["holder_job"] = json.loads(handle.read()).get("job")
            except (ValueError, TypeError):
                result["holder_job"] = "unknown"
        else:
            result["runtime_lock_busy"] = False
            fcntl.flock(handle, fcntl.LOCK_UN)
    raise DiagnosticStop("STOP BEFORE LIVE WRITER")
    yield


with (
    patch.object(universe_cmds, "open_store", read_open),
    patch.object(source, "fetch", fetch),
    patch.object(runtime, "runtime_lock", handoff_probe),
):
    result["stage"] = "prepare"
    try:
        universe_cmds._refresh_once(False)
    except DiagnosticStop:
        result["diagnostic_completed"] = True
    except Exception as exc:
        result["exception_class"] = type(exc).__name__
        # All network calls are public but avoid arbitrary exception text regardless.
result["end"] = time.time()
print(json.dumps(result, indent=2))
