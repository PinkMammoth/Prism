"""Shared dashboard helpers: settings/store access, demo mode, cached scan."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("PRISM_HOME", str(ROOT))

if "--demo" in sys.argv and not os.environ.get("PRISM_SOURCE_OVERRIDE"):
    os.environ["PRISM_DB_PATH"] = str(ROOT / "data" / "demo.duckdb")
    os.environ["PRISM_SOURCE_OVERRIDE"] = "synthetic"

from market_signal.config import get_settings  # noqa: E402
from market_signal.data.store import DatabaseBusy, Store  # noqa: E402


def settings():
    return get_settings(ROOT)


# The dashboard waits only briefly for a busy database (a running update can take minutes);
# pages then show the last answer or a "Prism is updating" notice instead of an error.
LOCK_TIMEOUT = 8.0


@contextmanager
def store(read_only: bool = False, lock_timeout: float = LOCK_TIMEOUT) -> Iterator[Store]:
    """Short-lived connections so the CLI can write between page loads."""
    s = settings()
    st_ = Store(s.paths.db, s.paths.raw, read_only=read_only and s.paths.db.exists(),
                lock_timeout=lock_timeout)  # fmt: skip
    try:
        yield st_
    finally:
        st_.close()


def db_version() -> float:
    p = settings().paths.db
    return p.stat().st_mtime if p.exists() else 0.0


def data_version() -> str:
    """Changes only when the *inputs* to a scan change: a finished ingestion run, a new
    research run, or an edited config file. Unlike the file's mtime, it does not change when
    the dashboard saves its own scan, so a page load no longer triggers another scan."""
    s = settings()
    cfg = max((f.stat().st_mtime for f in s.paths.config.rglob("*.yaml")), default=0.0)
    if not s.paths.db.exists():
        return f"nodb|{cfg}"
    with store(read_only=True) as st_:
        try:
            row = st_.con.execute(
                "SELECT (SELECT max(finished_at) FROM ingestion_runs), "
                "(SELECT max(created_at) FROM research_runs), (SELECT count(*) FROM bars)"
            ).fetchone()
        except Exception:  # an older/empty database: fall back to the file time
            return f"mtime|{db_version()}|{cfg}"
    return f"{row[0]}|{row[1]}|{row[2]}|{cfg}"


def db_available() -> bool:
    try:
        with store(read_only=True, lock_timeout=0):
            return True
    except DatabaseBusy:
        return False


# The last answer Today rendered, shared across sessions, shown while the database is busy.
LAST_TODAY: dict = {}


def busy_notice(exc: DatabaseBusy) -> None:
    """Calm full-page notice + automatic retry while another process holds the database."""
    st.title("Prism is updating")
    st.info(
        f"{exc.holder[0].upper() + exc.holder[1:]} is using Prism's database, usually the daily update "
        "(`market update`) or a scan. That can take a few minutes. This page checks every few "
        "seconds and reloads by itself when the database is free.",
        icon=":material/hourglass_top:",
    )
    st.caption(f"If nothing should be running, check it in a terminal: `ps -fp {exc.pid}`."
               if exc.pid else "If nothing should be running, check for a stuck `market` or streamlit process.")  # fmt: skip
    if st.button("Retry now"):
        st.rerun()

    @st.fragment(run_every="5s")
    def _poll() -> None:
        if db_available():
            st.rerun(scope="app")

    _poll()


def is_demo() -> bool:
    return os.environ.get("PRISM_SOURCE_OVERRIDE") == "synthetic"


def banner() -> None:
    if is_demo():
        st.warning(
            "**SYNTHETIC DEMO DATA** — random-walk series for exercising the app. Prices, scores and "
            "zones are not real and support no conclusions.",
            icon="⚠️",
        )


@st.cache_data(show_spinner="Scoring the universe…")
def cached_scan(_version: str):
    """Score with a read-only connection, then save the scan (track record) and alerts in one
    brief write, so the write lock is held for a moment rather than the whole scan."""
    from market_signal.scoring.engine import persist_scan, run_scan

    with store(read_only=True) as s:
        res = run_scan(s, settings(), persist=False)
    try:
        with store() as s:
            persist_scan(s, settings(), res)
            try:
                from market_signal.portfolio.alerts import evaluate_alerts

                evaluate_alerts(s, settings(), res, notifiers=[])
            except Exception:
                pass
    except DatabaseBusy:
        pass  # shown anyway; the scheduled `market scan` records the day's scan
    return res


def money(x) -> str:
    if x is None:
        return "–"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "–"
    return f"${x:,.2f}" if abs(x) < 1e4 else f"${x:,.0f}"


def pct(x, signed: bool = True) -> str:
    if x is None:
        return "–"
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "–"
    if x != x:
        return "–"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


@st.cache_data(show_spinner=False)
def cached_evidence(_version: str):
    """Latest saved research run per setup (pooled evidence), read-only."""
    from market_signal.presenter import load_evidence

    min_events = int(settings().yaml("backtest.yaml")["statistics"]["min_events_for_conclusion"])
    with store(read_only=True) as s:
        return load_evidence(s, min_events)


def decision_views(res):
    """Presentation views for every assessment, in the engine's ranking order."""
    from market_signal.presenter import build_view

    ev = cached_evidence(data_version())
    return [build_view(a, ev) for a in res.assessments]


def open_asset(symbol: str) -> None:
    st.session_state["asset"] = symbol
    st.switch_page("views/asset.py")


def nav_link(page: str, label: str, icon: str | None = None) -> None:
    """``st.page_link`` that degrades to nothing when a view runs outside ``app.py``
    (e.g. the per-page render tests), where other pages are not registered."""
    from streamlit.errors import StreamlitPageNotFoundError

    with contextlib.suppress(StreamlitPageNotFoundError):
        st.page_link(page, label=label, icon=icon)
