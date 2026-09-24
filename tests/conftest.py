from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from market_signal.config import get_settings
from market_signal.data.store import Store

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated project root with a copy of the real config/."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("PRISM_HOME", str(tmp_path))
    monkeypatch.delenv("PRISM_DB_PATH", raising=False)
    monkeypatch.setenv("TIINGO_API_KEY", "test-key")
    return tmp_path


@pytest.fixture
def settings(project: Path):
    s = get_settings(project)
    # tests never sleep for rate limits
    for cfg in (s.providers.get("providers") or {}).values():
        cfg["requests_per_second"] = 0
    s.providers.setdefault("http", {})["backoff_seconds"] = 0
    return s


@pytest.fixture
def store(project: Path):
    st = Store(project / "data" / "test.duckdb", project / "data" / "raw")
    yield st
    st.close()


def make_daily_crypto(
    n: int = 400, start: str = "2023-01-01", seed: int = 0, drift: float = 0.0
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, periods=n, freq="1D", tz="UTC")
    rets = rng.normal(drift, 0.03, n)
    close = 100 * np.exp(np.cumsum(rets))
    open_ = np.r_[100.0, close[:-1]]
    spread = np.abs(rng.normal(0, 0.01, n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = rng.uniform(1e3, 2e3, n)
    return pd.DataFrame(
        {"ts": ts, "open": open_, "high": high, "low": low, "close": close, "volume": vol}
    )


def coinbase_rows(df: pd.DataFrame) -> list[list[float]]:
    """Coinbase candle schema: [time, low, high, open, close, volume], newest first."""
    rows = [
        [int(r.ts.timestamp()), r.low, r.high, r.open, r.close, r.volume] for r in df.itertuples()
    ]
    return rows[::-1]


def mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def json_response(obj, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(obj).encode(), headers={"content-type": "application/json"}
    )
