"""Phase 7: every dashboard page renders without exceptions against a synthetic DB."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    import os

    from market_signal.config import get_settings
    from market_signal.data.store import Store
    from market_signal.demo import build_demo_db

    db = tmp_path_factory.mktemp("dash") / "demo.duckdb"
    old = {k: os.environ.get(k) for k in ("PRISM_DB_PATH", "PRISM_SOURCE_OVERRIDE", "PRISM_HOME")}
    os.environ.update(
        PRISM_DB_PATH=str(db), PRISM_SOURCE_OVERRIDE="synthetic", PRISM_HOME=str(REPO)
    )
    store = Store(db)
    build_demo_db(get_settings(REPO), store, years=3, seed=5)
    store.close()
    sys.path.insert(0, str(REPO / "dashboard"))
    yield db
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.mark.parametrize(
    "page", ["today", "track", "perps", "market", "asset", "portfolio", "hype", "data", "backtest"]
)
def test_page_renders(demo_db, page):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO / "dashboard" / "views" / f"{page}.py"), default_timeout=300)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title  # page rendered its heading
    assert any("SYNTHETIC" in w.value for w in at.warning)  # demo banner always shown


def test_app_navigation_lands_on_today(demo_db):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO / "dashboard" / "app.py"), default_timeout=300)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title[0].value == "Today"


def test_scan_cache_key_ignores_the_dashboards_own_writes(demo_db):
    import common
    from market_signal.scoring.engine import persist_scan, run_scan

    before = common.data_version()
    with common.store() as s:
        persist_scan(s, common.settings(), run_scan(s, common.settings(), persist=False))
    assert common.data_version() == before  # saving a scan must not trigger another scan


def test_busy_database_shows_a_notice_not_a_crash(demo_db):
    import subprocess

    from streamlit.testing.v1 import AppTest

    code = (f"import duckdb, time; c = duckdb.connect({str(demo_db)!r}); "
            "print('locked', flush=True); time.sleep(120)")  # fmt: skip
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        at = AppTest.from_file(str(REPO / "dashboard" / "app.py"), default_timeout=300)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        shown = [t.value for t in at.title] + [i.value for i in at.info]
        assert any("Prism is updating" in x or "is using" in x for x in shown), shown
    finally:
        holder.kill()
        holder.wait()
