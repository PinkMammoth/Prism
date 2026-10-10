"""Strict read-only connections and server cache for the paper page."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import streamlit as st

import common
from market_signal.data.store import Store
from market_signal.paper.dashboard.report import Report, freshness, runs


def source_path():
    import os

    return Path(os.environ.get("PRISM_PAPER_DB_PATH") or common.settings().paths.db).resolve()


@contextmanager
def connection(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Paper database not found: {path}")
    store = Store(path, read_only=True, lock_timeout=common.LOCK_TIMEOUT)
    try:
        yield store.con
    finally:
        store.close()


def version(path):
    stat = Path(path).stat()
    return stat.st_mtime_ns, stat.st_size


@st.cache_data(ttl=30, show_spinner=False)
def metadata(path, revision):
    with connection(path) as con:
        return runs(con)


@st.cache_resource(ttl=30, max_entries=24, show_spinner=False)
def report(path, revision, run_id, start, end):
    with connection(path) as con:
        run = next(r for r in runs(con) if r["run_id"] == run_id)
        return Report(con, run, start=start, end=end)


@st.cache_data(ttl=30, max_entries=24, show_spinner=False)
def details(path, revision, run_id, start, end):
    r = report(path, revision, run_id, start, end)
    with connection(path) as con:
        return r.curve(con), r.activity(con)


def freshness_state(path, run_metadata):
    import os

    # A production authority claim in a downloaded DB does not make the view live.
    live = os.environ.get("PRISM_PAPER_DATA_MODE") == "production"
    return freshness(Path(path), run_metadata, live=live, synthetic=common.is_demo())


def reload_local():
    metadata.clear()
    report.clear()
    details.clear()
