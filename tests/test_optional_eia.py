"""Disabling optional EIA must preserve real FRED acquisition and configured failures."""

from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd

from market_signal.data import updaters
from market_signal.data.http import ProviderError


def test_missing_eia_does_not_fail_or_attempt_fetch(monkeypatch):
    fred = Mock()
    fred.get_series.return_value = pd.DataFrame()
    eia = Mock()
    store = Mock()
    settings = SimpleNamespace(
        yaml=lambda _: {"series": {"DGS10": {}}, "eia": {"WCESTUS1": {}}},
        secret=lambda name: "fred-test-key" if name == "FRED_API_KEY" else None,
    )
    monkeypatch.setattr(updaters, "_http", lambda *args: None)
    monkeypatch.setattr(updaters, "FredProvider", lambda *args: fred)
    monkeypatch.setattr(updaters, "EiaProvider", lambda *args: eia)
    monkeypatch.setattr(updaters, "_last_obs", lambda *args: None)
    result = updaters.update_macro(settings, store).set_index("series")
    assert result.loc["DGS10", "status"] == "ok"
    assert result.loc["WCESTUS1", "status"] == "disabled"
    fred.get_series.assert_called_once()
    eia.get_series.assert_not_called()
    assert store.start_run.call_count == 1


def test_configured_eia_failure_remains_visible(monkeypatch):
    eia = Mock()
    eia.get_series.side_effect = ProviderError("upstream unavailable")
    settings = SimpleNamespace(
        yaml=lambda _: {"eia": {"WCESTUS1": {"route": "PET.WCESTUS1.W"}}},
        secret=lambda name: "test-key",
    )
    monkeypatch.setattr(updaters, "_http", lambda *args: None)
    monkeypatch.setattr(updaters, "EiaProvider", lambda *args: eia)
    monkeypatch.setattr(updaters, "_last_obs", lambda *args: None)
    result = updaters.update_macro(settings, Mock())
    assert result.iloc[0]["status"] == "failed"
    eia.get_series.assert_called_once()
