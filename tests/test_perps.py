"""Perps Phase 1: Hyperliquid perp client, ingestion (incremental, closed bars only,
provenance) and the funding / OI monitor."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

from market_signal.data.http import HttpClient, SchemaError
from market_signal.data.providers.crypto import HyperliquidProvider
from market_signal.perps.data import load_funding, load_perp_bars, load_snapshots, update_perps
from market_signal.perps.monitor import funding_stats, oi_stats, perp_overview

H = 3_600_000  # one hour in ms


def _provider(handler) -> HyperliquidProvider:
    http = HttpClient("hyperliquid", "https://api.hyperliquid.test", requests_per_second=0,
                      backoff_seconds=0, max_retries=0, transport=httpx.MockTransport(handler))  # fmt: skip
    return HyperliquidProvider(http)


def _json(obj) -> httpx.Response:
    return httpx.Response(
        200, content=json.dumps(obj).encode(), headers={"content-type": "application/json"}
    )


class FakeVenue:
    """A tiny Hyperliquid: hourly funding, daily candles (incl. today's open one), contexts."""

    def __init__(self, now: datetime, days: int = 40):
        self.now = now
        start = int((now - timedelta(days=days)).timestamp() * 1000) // H * H
        self.funding = [{"coin": "BTC", "fundingRate": f"{0.00001 + 0.000001 * (i % 7):.8f}",
                         "premium": "0.0001", "time": start + i * H}
                        for i in range((int(now.timestamp() * 1000) - start) // H)]  # fmt: skip
        day0 = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.candles = []
        for d in range(days + 1):  # the last one is today's, still open
            t = int((day0 + timedelta(days=d)).timestamp() * 1000)
            px = 100 + d
            self.candles.append({"t": t, "T": t + 86_399_999, "s": "BTC", "i": "1d", "o": str(px),
                                 "h": str(px + 2), "l": str(px - 2), "c": str(px + 1), "v": "10", "n": 5})  # fmt: skip
        self.requests: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        self.requests.append(body)
        if body["type"] == "fundingHistory":
            rows = [r for r in self.funding if body["startTime"] <= r["time"] <= body["endTime"]]
            return _json(rows[:500])
        if body["type"] == "candleSnapshot":
            assert body["req"]["coin"] == "BTC"  # perps are addressed by name, no spotMeta lookup
            r = body["req"]
            return _json([c for c in self.candles if r["startTime"] <= c["t"] <= r["endTime"]])
        if body["type"] == "metaAndAssetCtxs":
            return _json([{"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                                        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]},
                          [{"funding": "0.0000125", "openInterest": "1000.5", "prevDayPx": "100",
                            "dayNtlVlm": "5e8", "premium": "0.0002", "oraclePx": "100.9",
                            "markPx": "101.0", "midPx": "101.0"},
                           {"funding": "0.00002", "openInterest": "500", "prevDayPx": "10",
                            "dayNtlVlm": "1e8", "premium": None, "oraclePx": "10", "markPx": "10",
                            "midPx": None}]])  # fmt: skip
        raise AssertionError(body)


# ----------------------------------------------------------------- provider


def test_funding_history_paginates_and_parses():
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    venue = FakeVenue(now, days=50)  # 1,200 hourly rows → pages of 500, 500, 200
    df = _provider(venue).funding_history("BTC", now - timedelta(days=50), now)
    pages = [r for r in venue.requests if r["type"] == "fundingHistory"]
    assert len(pages) == 3
    assert pages[1]["startTime"] == venue.funding[499]["time"] + 1
    assert len(df) == len(venue.funding) and df["time"].is_monotonic_increasing
    assert df["funding_rate"].iloc[0] == pytest.approx(0.00001)
    assert str(df["time"].dt.tz) == "UTC"


def test_perp_contexts_parse_and_schema_guard():
    ctx = _provider(FakeVenue(datetime(2026, 10, 3, tzinfo=UTC))).perp_contexts()
    btc = ctx.set_index("coin").loc["BTC"]
    assert btc["max_leverage"] == 40 and btc["oi_notional"] == pytest.approx(1000.5 * 101.0)
    assert pd.isna(ctx.set_index("coin").loc["ETH", "premium"])
    with pytest.raises(SchemaError):
        _provider(lambda req: _json([{"universe": [{"name": "BTC"}]}, []])).perp_contexts()
    with pytest.raises(SchemaError):
        _provider(lambda req: _json({"oops": 1})).funding_history(
            "BTC", datetime(2026, 1, 1, tzinfo=UTC)
        )


# ----------------------------------------------------------------- ingestion


def test_update_perps_backfills_then_increments(settings, store, monkeypatch):
    import market_signal.perps.data as pdata

    settings_cfg = {"coins": ["BTC"], "funding": {"backfill_days": 30, "page_size": 500}}
    monkeypatch.setattr(pdata, "perp_config", lambda s: settings_cfg)
    now = datetime.now(UTC).replace(minute=30, second=0, microsecond=0)
    venue = FakeVenue(now, days=40)
    prov = _provider(venue)

    res = update_perps(settings, store, provider=prov)
    assert set(res["status"]) == {"ok"}, res
    bars = load_perp_bars(store, "BTC")
    assert len(bars) == 40  # today's still-open candle is NOT stored
    assert (pd.to_datetime(bars["close_time"], utc=True) <= pd.Timestamp(now)).all()
    f = load_funding(store, "BTC")
    assert f.index.min() >= pd.Timestamp(
        now - timedelta(days=30, hours=1)
    )  # backfill window respected
    snaps = load_snapshots(store)
    assert list(snaps["coin"]) == ["BTC"] and snaps["max_leverage"].iloc[0] == 40
    runs = store.query("SELECT dataset, status FROM ingestion_runs WHERE provider='hyperliquid'")
    assert set(runs["dataset"]) == {"perp_bars_1d", "perp_funding", "perp_snapshot"}

    # second run: funding resumes after the last stored time; nothing duplicated
    venue.requests.clear()
    n_before = len(f)
    res2 = update_perps(settings, store, provider=prov)
    first_funding = next(r for r in venue.requests if r["type"] == "fundingHistory")
    assert first_funding["startTime"] == int(f.index.max().timestamp() * 1000) + 1
    assert len(load_funding(store, "BTC")) == n_before
    assert res2.loc[res2["dataset"] == "perp_funding", "new_rows"].iloc[0] == 0


def test_update_perps_records_failures(settings, store, monkeypatch):
    import market_signal.perps.data as pdata

    monkeypatch.setattr(pdata, "perp_config", lambda s: {"coins": ["BTC"]})
    res = update_perps(settings, store, provider=_provider(lambda req: httpx.Response(500)))
    assert set(res["status"]) == {"failed"}
    assert set(store.query("SELECT status FROM ingestion_runs")["status"]) == {"failed"}


# ----------------------------------------------------------------- monitor


def _hourly(values, end="2026-10-03T00:00Z"):
    idx = pd.date_range(end=pd.Timestamp(end), periods=len(values), freq="1h")
    return pd.Series(values, index=idx)


def test_funding_stats_annualises_and_ranks():
    now = pd.Timestamp("2026-10-03T00:00Z")
    flat = _hourly(np.full(24 * 200, 0.00001))
    st = funding_stats(flat, now, 8760, 7, 365, 0.9, 0.1)
    assert st["funding_avg_ann"] == pytest.approx(0.0876)
    assert st["funding_now_ann"] == pytest.approx(0.0876)

    ramp = _hourly(
        np.linspace(-0.00002, 0.00005, 24 * 200)
    )  # rising: today is the most crowded long
    st = funding_stats(ramp, now, 8760, 7, 365, 0.9, 0.1)
    assert st["state"] == "CROWDED LONG" and st["percentile"] >= 0.9
    st = funding_stats(ramp[::-1].set_axis(ramp.index), now, 8760, 7, 365, 0.9, 0.1)
    assert st["state"] == "CROWDED SHORT"

    short = _hourly(np.full(24 * 20, 0.00001))
    assert funding_stats(short, now, 8760, 7, 365, 0.9, 0.1)["state"] == "NOT ENOUGH HISTORY"
    assert funding_stats(pd.Series(dtype=float), now, 8760, 7, 365, 0.9, 0.1)["state"] == "NO DATA"


def test_oi_change_over_seven_days():
    t = pd.date_range("2026-09-20", periods=14, freq="1D", tz="UTC")
    snaps = pd.DataFrame({"snapshot_at": t, "oi_notional": np.linspace(100, 230, 14), "mark_px": 1.0,
                          "premium": 0.0, "max_leverage": 40.0})  # fmt: skip
    st = oi_stats(snaps, pd.Timestamp("2026-10-04T00:00Z"))
    assert st["oi_notional"] == pytest.approx(230)
    assert st["oi_change_7d"] == pytest.approx(230 / 160 - 1)
    assert st["oi_history_days"] == pytest.approx(13)


def test_overview_on_demo_db(tmp_path, monkeypatch):
    from market_signal.config import get_settings
    from market_signal.data.store import Store
    from market_signal.demo import build_demo_db

    s = get_settings()
    st = Store(tmp_path / "d.duckdb")
    try:
        build_demo_db(s, st, years=2, seed=3)
        rows = perp_overview(st, s)
        assert [r.coin for r in rows] == [str(c) for c in s.yaml("perps.yaml")["coins"]]
        assert all(r.state in ("CROWDED LONG", "CROWDED SHORT", "NEUTRAL") for r in rows)
        assert all(r.oi_notional and r.max_leverage for r in rows)
    finally:
        st.close()
