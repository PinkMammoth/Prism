"""Binance perps as a second venue: client pagination and guards, venue isolation, 8h
funding aggregation, and research restricted to the years before Hyperliquid's data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

from market_signal.data.http import HttpClient, ProviderError, SchemaError
from market_signal.perps.backtest import daily_funding
from market_signal.perps.binance import BinanceFuturesProvider, update_binance_perps
from market_signal.perps.data import load_funding, load_perp_bars

DAY = 86_400_000


class FakeBinance:
    def __init__(self, start: datetime, days: int):
        t0 = int(start.timestamp() * 1000)
        self.klines = []
        for d in range(days + 1):  # the last one is today's, still open
            px = 100 + d * 0.1
            self.klines.append([t0 + d * DAY, str(px), str(px + 2), str(px - 2), str(px + 1), "10",
                                t0 + (d + 1) * DAY - 1, "0", 1, "0", "0", "0"])  # fmt: skip
        self.funding = [{"symbol": "BTCUSDT", "fundingTime": t0 + k * 8 * 3_600_000, "fundingRate": "0.0001",
                         "markPrice": "100"} for k in range(days * 3)]  # fmt: skip
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p = dict(req.url.params)
        self.calls.append((req.url.path, p))
        s, e, lim = int(p["startTime"]), int(p["endTime"]), int(p["limit"])
        if req.url.path == "/fapi/v1/klines":
            return httpx.Response(200, json=[k for k in self.klines if s <= k[0] <= e][:lim])
        if req.url.path == "/fapi/v1/fundingRate":
            return httpx.Response(
                200, json=[f for f in self.funding if s <= f["fundingTime"] <= e][:lim]
            )
        raise AssertionError(req.url)


def _prov(handler, **kw) -> BinanceFuturesProvider:
    http = HttpClient("binance_futures", "https://fapi.test", requests_per_second=0, backoff_seconds=0,
                      max_retries=0, transport=httpx.MockTransport(handler))  # fmt: skip
    return BinanceFuturesProvider(http, **kw)


def test_klines_and_funding_paginate():
    start = datetime(2020, 1, 1, tzinfo=UTC)
    fake = FakeBinance(start, 40)
    p = _prov(fake, kline_limit=15, funding_limit=50)
    bars = p.daily_bars("BTCUSDT", start, start + timedelta(days=60))
    assert len(bars) == 41 and bars["ts"].is_monotonic_increasing
    assert bars["close"].iloc[0] == pytest.approx(101.0)
    assert sum(1 for path, _ in fake.calls if path.endswith("klines")) == 3  # 15 + 15 + 11
    f = p.funding_history("BTCUSDT", start, start + timedelta(days=60))
    assert len(f) == 120 and f["funding_rate"].iloc[0] == pytest.approx(0.0001)


def test_restricted_location_is_explained_and_schema_is_checked():
    blocked = _prov(
        lambda req: httpx.Response(
            451, json={"code": 0, "msg": "Service unavailable from a restricted location"}
        )
    )
    with pytest.raises(ProviderError, match="restricted region"):
        blocked.daily_bars(
            "BTCUSDT", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 2, 1, tzinfo=UTC)
        )
    with pytest.raises(SchemaError):
        _prov(lambda req: httpx.Response(200, json={"oops": 1})).funding_history(
            "BTCUSDT", datetime(2020, 1, 1, tzinfo=UTC)
        )


def test_eight_hour_funding_aggregates_per_day():
    ts = pd.date_range("2021-01-01", periods=4, freq="1D", tz="UTC")
    bars = pd.DataFrame({"ts": ts})
    eight = pd.date_range("2021-01-01 08:00", "2021-01-05 00:00", freq="8h", tz="UTC")
    f = pd.Series(0.0001, index=eight)
    f = f.drop(pd.Timestamp("2021-01-03 16:00", tz="UTC"))  # day 3 has only 2 of 3 settlements
    extra = pd.Series(
        0.0001, index=pd.date_range("2021-01-04 04:00", "2021-01-04 20:00", freq="8h", tz="UTC")
    )
    f = pd.concat([f, extra]).sort_index()  # day 4: interval shortened to 4h → 6 settlements
    out = daily_funding(bars, f, min_coverage=0.8)
    assert out[0] == pytest.approx(0.0003) and out[1] == pytest.approx(0.0003)
    assert np.isnan(out[2])  # 2/3 < 80% coverage → missing, never assumed zero
    assert out[3] == pytest.approx(0.0006)  # more settlements than usual: summed as they are


def test_updater_stores_binance_separately(settings, store, monkeypatch):
    import market_signal.perps.binance as bmod

    cfg = {
        "venues": {
            "binance": {
                "enabled": True,
                "history_start": "2020-01-01",
                "symbols": {"BTC": "BTCUSDT"},
            }
        }
    }
    monkeypatch.setattr(bmod, "perp_config", lambda s: cfg)
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    start = (now - timedelta(days=30)).replace(hour=0)
    fake = FakeBinance(start, 30)
    monkeypatch.setattr(bmod, "_ms", lambda dt: int(max(dt, start).timestamp() * 1000))
    res = update_binance_perps(settings, store, provider=_prov(fake))
    assert set(res["status"]) == {"ok"}, res
    assert (
        len(load_perp_bars(store, "BTC", venue="binance")) == 30
    )  # today's open candle not stored
    assert load_perp_bars(store, "BTC").empty  # Hyperliquid loaders never see Binance rows
    assert (
        load_funding(store, "BTC").empty and len(load_funding(store, "BTC", venue="binance")) == 90
    )
    runs = store.query("SELECT DISTINCT provider FROM ingestion_runs")
    assert set(runs["provider"]) == {"binance"}

    cfg["venues"]["binance"]["enabled"] = False  # a disabled venue fetches nothing
    fake.calls.clear()
    assert update_binance_perps(settings, store, provider=_prov(fake)).empty and not fake.calls


def test_research_on_binance_stops_before_hyperliquid(settings, store):
    from market_signal.perps.backtest import PerpCosts, PerpInput
    from market_signal.perps.research import run_perp_research, save_perp_report
    from market_signal.presenter import load_evidence

    rng = np.random.default_rng(2)
    n = 1300
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
    o = np.concatenate([[c[0]], c[:-1]])
    ts = pd.date_range("2019-09-01", periods=n, freq="1D", tz="UTC")
    f = pd.DataFrame({"ts": ts, "close_time": ts + pd.Timedelta(days=1), "open": o, "high": np.maximum(o, c) * 1.01,
                      "low": np.minimum(o, c) * 0.99, "close": c, "volume": 1.0, "funding_day": 0.0003})  # fmt: skip
    cut = pd.Timestamp("2022-06-01", tz="UTC")
    inputs = [PerpInput("BTC", f, PerpCosts(5, 2, 10), 20.0, maint_rate=0.004)]
    rep = run_perp_research(None, settings, "trend_ls", inputs=inputs, walk_forward_on=False, sensitivity_on=False,
                            venue="binance", end=cut, window_note="before Hyperliquid")  # fmt: skip
    assert rep.run_name == "perp_trend_ls@binance"
    assert pd.Timestamp(rep.period[1], tz="UTC") < cut
    out = save_perp_report(store, settings, rep)
    assert (
        out.name.startswith("binance-") and "before Hyperliquid" in (out / "report.md").read_text()
    )
    assert load_evidence(store)["perp_trend_ls@binance"].verdict == rep.verdict["verdict"]


def test_update_only_perps_runs_all_perp_steps(settings, monkeypatch):
    """Regression: the Binance and paper-tracking steps were defined but never registered."""
    import market_signal.data.updaters as up
    from market_signal.cli.data_cmds import update

    ran = []
    fakes = [up.Updater(u.kind, u.title, (lambda t: lambda s, st: (ran.append(t), pd.DataFrame())[1])(u.title))
             for u in up.UPDATERS]  # fmt: skip
    monkeypatch.setattr(up, "UPDATERS", fakes)
    update(symbols=None, timeframes=["1d"], only="perps")
    assert [t.split(":")[0].split(" (")[0] for t in ran] == [
        "Perpetual futures", "Binance perps", "Binance perp open interest", "Perp strategies"]  # fmt: skip


def test_binance_window_is_cut_per_coin(settings):
    from market_signal.perps.backtest import PerpCosts, PerpInput
    from market_signal.perps.research import run_perp_research

    rng = np.random.default_rng(5)
    n = 1500
    ts = pd.date_range("2019-09-01", periods=n, freq="1D", tz="UTC")

    def frame():
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
        o = np.concatenate([[c[0]], c[:-1]])
        return pd.DataFrame({"ts": ts, "close_time": ts + pd.Timedelta(days=1), "open": o,
                             "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99, "close": c,
                             "volume": 1.0, "funding_day": 0.0003})  # fmt: skip

    inputs = [
        PerpInput(c, frame(), PerpCosts(5, 2, 10), 20.0, maint_rate=0.004)
        for c in ("BTC", "ETH", "SOL")
    ]
    cuts = {"BTC": pd.Timestamp("2021-06-01", tz="UTC"), "ETH": pd.Timestamp("2022-06-01", tz="UTC"),
            "SOL": pd.Timestamp("2019-10-01", tz="UTC")}  # SOL: under 60 days → dropped  # fmt: skip
    rep = run_perp_research(None, settings, "breakout_ls", inputs=inputs, walk_forward_on=False,
                            sensitivity_on=False, venue="binance", end=cuts)  # fmt: skip
    last = {d["coin"]: pd.Timestamp(d["last"], tz="UTC") for d in rep.provenance["data"]}
    assert set(last) == {"BTC", "ETH"}
    assert last["BTC"] < cuts["BTC"] and last["ETH"] < cuts["ETH"]
    assert last["ETH"] > cuts["BTC"]  # each coin keeps its own window


def test_hyperliquid_research_period_starts_when_funding_does(store):
    """Pre-launch price bars without funding were never evaluated, so they don't count."""
    from market_signal.perps.binance import hyperliquid_starts
    from market_signal.perps.data import upsert_funding, upsert_perp_bars

    ts = pd.date_range("2020-08-19", periods=1500, freq="1D", tz="UTC")
    bars = pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
                         "close_time": ts + pd.Timedelta(days=1)})  # fmt: skip
    upsert_perp_bars(store, "BTC", bars, "hyperliquid", "t")
    hours = pd.date_range("2023-10-05 01:00", periods=48, freq="1h", tz="UTC")
    upsert_funding(
        store,
        "BTC",
        pd.DataFrame({"time": hours, "funding_rate": 1e-5, "premium": 0.0}),
        "hyperliquid",
        "t",
    )
    assert hyperliquid_starts(store) == {"BTC": pd.Timestamp("2023-10-05", tz="UTC")}


def test_settlement_jitter_does_not_drop_days():
    """Regression: Binance stamps settlements a few ms late; a midnight settlement at
    00:00:00.004 must still count for the day ending at midnight."""
    ts = pd.date_range("2021-01-01", periods=60, freq="1D", tz="UTC")
    t0 = pd.date_range(
        pd.Timestamp("2021-01-01 08:00", tz="UTC"), ts[-1] + pd.Timedelta(days=1), freq="8h"
    )
    jitter = pd.to_timedelta(np.random.default_rng(0).integers(1, 9, len(t0)), unit="ms")
    out = daily_funding(pd.DataFrame({"ts": ts}), pd.Series(0.0001, index=t0 + jitter))
    assert np.isfinite(out).all() and np.allclose(out, 0.0003)


def test_research_with_no_events_reports_instead_of_crashing(settings):
    """Regression: zero events crashed the walk-forward with KeyError 'horizon'."""
    from market_signal.perps.backtest import PerpCosts, PerpInput
    from market_signal.perps.research import run_perp_research

    n = 1200
    ts = pd.date_range("2019-09-01", periods=n, freq="1D", tz="UTC")
    c = 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.03, n)))
    o = np.concatenate([[c[0]], c[:-1]])
    f = pd.DataFrame({"ts": ts, "close_time": ts + pd.Timedelta(days=1), "open": o, "high": np.maximum(o, c) * 1.01,
                      "low": np.minimum(o, c) * 0.99, "close": c, "volume": 1.0, "funding_day": np.nan})  # fmt: skip
    f.loc[:20, "funding_day"] = 0.0003  # funding known only at the very start → no complete returns
    rep = run_perp_research(
        None, settings, "funding_fade", inputs=[PerpInput("BTC", f, PerpCosts(5, 2, 10), 20.0)]
    )
    assert rep.verdict["verdict"] == "INSUFFICIENT_DATA"
    assert rep.provenance["data"][0]["funding_coverage"] < 0.05
