from types import SimpleNamespace

import pandas as pd
import pytest

from market_signal.microstructure import health


@pytest.mark.parametrize("active_bad", [False, True])
def test_static_core_health_ignores_retired_satellite_history(monkeypatch, active_bad):
    monkeypatch.setattr(health, "collector_state", lambda *a: {
        "issues": [], "warnings": [], "state": "RUNNING",
        "status": {"dynamic_universe": False, "coins": {"BTC": {}}},
    })
    rows = pd.DataFrame([
        {"coin": "BTC", "newest_age_min": 90 if active_bad else 1,
         "COMPLETE": 60, "PARTIAL": 0, "TRADE_ONLY": 0, "BOOK_ONLY": 0,
         "GAP": 0, "MISSING": 0},
        {"coin": "RETIRED", "newest_age_min": 2000, "COMPLETE": 0,
         "PARTIAL": 0, "TRADE_ONLY": 0, "BOOK_ONLY": 0, "GAP": 0, "MISSING": 60},
    ])
    monkeypatch.setattr(health, "coverage", lambda *a, **kw: rows.copy())
    store = SimpleNamespace(con=SimpleNamespace(
        execute=lambda *a: SimpleNamespace(fetchone=lambda: (0,))))
    result = health.evaluate(store, None)
    assert result["healthy"] is (not active_bad)
    assert all("RETIRED" not in issue for issue in result["issues"])
    if active_bad:
        assert any("BTC" in issue for issue in result["issues"])
