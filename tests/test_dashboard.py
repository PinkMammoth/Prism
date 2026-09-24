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


@pytest.mark.parametrize("page", ["market", "asset", "portfolio", "hype", "data", "backtest"])
def test_page_renders(demo_db, page):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(REPO / "dashboard" / "views" / f"{page}.py"), default_timeout=300)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.title  # page rendered its heading
    assert any("SYNTHETIC" in w.value for w in at.warning)  # demo banner always shown
