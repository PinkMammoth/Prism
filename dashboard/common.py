"""Shared dashboard helpers: settings/store access, demo mode, cached scan."""

from __future__ import annotations

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
from market_signal.data.store import Store  # noqa: E402


def settings():
    return get_settings(ROOT)


@contextmanager
def store(read_only: bool = False) -> Iterator[Store]:
    """Short-lived connections so the CLI can write between page loads."""
    s = settings()
    st_ = Store(s.paths.db, s.paths.raw, read_only=read_only and s.paths.db.exists())
    try:
        yield st_
    finally:
        st_.close()


def db_version() -> float:
    p = settings().paths.db
    return p.stat().st_mtime if p.exists() else 0.0


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
def cached_scan(_version: float):
    from market_signal.scoring.engine import run_scan

    with store() as s:
        res = run_scan(s, settings(), persist=True)
        try:
            from market_signal.portfolio.alerts import evaluate_alerts

            evaluate_alerts(s, settings(), res, notifiers=[])
        except Exception:
            pass
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
