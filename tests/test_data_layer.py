"""Phase 1: calendars, provider parsing, storage idempotency, validation, updates."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime

import httpx
import numpy as np
import pandas as pd
import pytest

from market_signal.data.calendars import close_times, is_nyse_session, nyse_close_utc, nyse_holidays
from market_signal.data.http import HttpClient, ProviderUnavailable, SchemaError
from market_signal.data.prices import PriceBasis, adjustment_factors, apply_basis
from market_signal.data.providers.base import resample_bars
from market_signal.data.providers.crypto import CoinbaseProvider, HyperliquidProvider
from market_signal.data.providers.equities import StooqProvider, TiingoProvider
from market_signal.data.registry import ProviderRegistry
from market_signal.data.update import update_series
from market_signal.data.validation import BatchRejected, validate_batch, validate_series
from market_signal.models.domain import Calendar, Timeframe
from tests.conftest import coinbase_rows, json_response, make_daily_crypto

# --------------------------------------------------------------------------- calendars


def test_nyse_holidays_2024():
    expected = {
        date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29),
        date(2024, 5, 27), date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2),
        date(2024, 11, 28), date(2024, 12, 25),
    }  # fmt: skip
    assert nyse_holidays(2024) == expected


def test_nyse_observed_rules():
    # 2022-01-01 was a Saturday: NYSE did NOT close on Friday 2021-12-31
    assert is_nyse_session(date(2021, 12, 31))
    # 2026-07-04 is a Saturday -> observed Friday 2026-07-03
    assert not is_nyse_session(date(2026, 7, 3))
    assert not is_nyse_session(date(2025, 1, 9))  # national day of mourning


def test_nyse_close_is_dst_aware():
    assert nyse_close_utc(date(2024, 1, 10)).hour == 21  # EST
    assert nyse_close_utc(date(2024, 7, 10)).hour == 20  # EDT


def test_close_times_crypto_daily():
    ts = pd.Series(pd.date_range("2024-01-01", periods=2, freq="1D", tz="UTC"))
    ct = close_times(ts, Calendar.CRYPTO_24_7, Timeframe.D1)
    assert ct.iloc[0] == pd.Timestamp("2024-01-02", tz="UTC")


# --------------------------------------------------------------------------- parsing


def _client(handler, provider="p"):
    return HttpClient(provider, "https://example.test", requests_per_second=0, backoff_seconds=0,
                      transport=httpx.MockTransport(handler))  # fmt: skip


def test_coinbase_paginates_and_parses():
    df = make_daily_crypto(650, "2022-01-01")
    calls = []

    def handler(req: httpx.Request):
        calls.append(req.url.params)
        s = pd.Timestamp(req.url.params["start"])
        e = pd.Timestamp(req.url.params["end"])
        sub = df[(df.ts >= s) & (df.ts <= e)]
        return json_response(coinbase_rows(sub))

    p = CoinbaseProvider(_client(handler, "coinbase"))
    res = p.get_daily_bars(
        "BTC-USD", datetime(2022, 1, 1, tzinfo=UTC), datetime(2023, 10, 12, tzinfo=UTC)
    )
    assert len(calls) == 3  # 300 candles per request
    assert len(res.bars) == 650
    assert res.bars["ts"].is_monotonic_increasing
    np.testing.assert_allclose(res.bars["close"].to_numpy(), df["close"].to_numpy())
    assert len(res.raw) == 3 and res.raw[0].provider == "coinbase"


def test_coinbase_schema_change_fails_loudly():
    p = CoinbaseProvider(_client(lambda r: json_response([[1, 2, 3]]), "coinbase"))
    with pytest.raises(SchemaError):
        p.get_daily_bars(
            "BTC-USD", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 5, tzinfo=UTC)
        )


def test_coinbase_4h_aggregates_complete_buckets_only():
    ts = pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC")  # 2.5 buckets
    h = pd.DataFrame({"ts": ts, "open": np.arange(10.0) + 1, "high": np.arange(10.0) + 2,
                      "low": np.arange(10.0), "close": np.arange(10.0) + 1.5, "volume": 1.0})  # fmt: skip
    agg = resample_bars(h, "4h", expected_parts=4)
    assert len(agg) == 2  # the partial 3rd bucket is dropped, not built from 2 hours
    first = agg.iloc[0]
    assert first["open"] == 1 and first["close"] == 4.5 and first["high"] == 5 and first["low"] == 0
    assert first["volume"] == 4


def test_hyperliquid_candle_parse_and_pair_resolution():
    meta = {"tokens": [{"index": 0, "name": "USDC"}, {"index": 150, "name": "HYPE"}],
            "universe": [{"name": "@107", "tokens": [150, 0], "index": 107}]}  # fmt: skip
    candles = [{"t": 1735689600000, "T": 1735775999999, "s": "@107", "i": "1d",
                "o": "24.1", "c": "25.0", "h": "25.5", "l": "23.9", "v": "1000.5", "n": 100}]  # fmt: skip

    def handler(req):
        body = json.loads(req.content)
        if body["type"] == "spotMeta":
            return json_response(meta)
        assert body["req"]["coin"] == "@107"
        return json_response(candles)

    p = HyperliquidProvider(_client(handler, "hyperliquid"))
    res = p.get_daily_bars(
        "HYPE", datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 3, tzinfo=UTC)
    )
    assert res.bars.iloc[0]["close"] == 25.0
    assert res.bars.iloc[0]["ts"] == pd.Timestamp("2025-01-01", tz="UTC")


def test_tiingo_parse_raw_bars_and_actions():
    payload = [
        {"date": "2024-06-07T00:00:00.000Z", "open": 1200.0, "high": 1210.0, "low": 1190.0, "close": 1208.9,
         "volume": 4e7, "adjOpen": 120.0, "adjHigh": 121, "adjLow": 119, "adjClose": 120.89, "adjVolume": 4e8,
         "divCash": 0.0, "splitFactor": 1.0},
        {"date": "2024-06-10T00:00:00.000Z", "open": 120.4, "high": 123.1, "low": 117.0, "close": 121.8,
         "volume": 3e8, "adjOpen": 120.4, "adjHigh": 123.1, "adjLow": 117, "adjClose": 121.8, "adjVolume": 3e8,
         "divCash": 0.0, "splitFactor": 10.0},
    ]  # fmt: skip
    bars, actions = TiingoProvider.parse(payload)
    assert list(bars["close"]) == [1208.9, 121.8]  # RAW stored
    assert bars["ts"].iloc[0] == pd.Timestamp("2024-06-07 13:30", tz="UTC")  # 09:30 EDT
    assert len(actions) == 1 and actions.iloc[0]["split_factor"] == 10.0


def test_tiingo_error_payload_raises():
    from market_signal.data.http import ProviderError

    with pytest.raises(ProviderError):
        TiingoProvider.parse({"detail": "Invalid token"})


def test_stooq_parse_and_limit_message():
    from market_signal.data.http import ProviderError

    bars = StooqProvider.parse(b"Date,Open,High,Low,Close,Volume\n2024-01-02,10,11,9,10.5,100\n")
    assert bars.iloc[0]["close"] == 10.5
    with pytest.raises(ProviderError):
        StooqProvider.parse(b"Exceeded the daily hits limit")


def test_network_denial_is_provider_unavailable():
    def handler(req):
        raise httpx.ConnectError("proxy 403")

    p = CoinbaseProvider(_client(handler, "coinbase"))
    with pytest.raises(ProviderUnavailable):
        p.get_latest_price("BTC-USD")


# --------------------------------------------------------------------------- validation


def _crypto_batch(n=10):
    df = make_daily_crypto(n)
    df["ts"] = df["ts"].astype("datetime64[us, UTC]")
    return df


def test_validation_rejects_impossible_ohlc():
    df = _crypto_batch(200)
    df.loc[5, "high"] = df.loc[5, "low"] - 1  # impossible
    vr = validate_batch(
        df,
        symbol="X",
        timeframe=Timeframe.D1,
        source="t",
        calendar=Calendar.CRYPTO_24_7,
        run_id="r",
    )
    assert len(vr.clean) == 199
    assert "ohlc_high" in set(vr.issues["check_name"])


def test_validation_refuses_mostly_bad_batch():
    df = _crypto_batch(50)
    df["close"] = -1.0  # e.g. a units/schema change
    with pytest.raises(BatchRejected):
        validate_batch(
            df,
            symbol="X",
            timeframe=Timeframe.D1,
            source="t",
            calendar=Calendar.CRYPTO_24_7,
            run_id="r",
        )


def test_validation_misaligned_and_duplicates():
    df = _crypto_batch(300)
    df.loc[3, "ts"] = df.loc[3, "ts"] + pd.Timedelta(hours=1)
    dup = df.iloc[[10]].copy()
    df = pd.concat([df, dup], ignore_index=True)
    vr = validate_batch(
        df,
        symbol="X",
        timeframe=Timeframe.D1,
        source="t",
        calendar=Calendar.CRYPTO_24_7,
        run_id="r",
    )
    checks = set(vr.issues["check_name"])
    assert {"ts_misaligned", "duplicate_identical"} <= checks
    assert vr.clean["ts"].is_unique


def test_validation_requires_utc():
    df = _crypto_batch(5)
    df["ts"] = df["ts"].dt.tz_localize(None)
    with pytest.raises(BatchRejected):
        validate_batch(
            df,
            symbol="X",
            timeframe=Timeframe.D1,
            source="t",
            calendar=Calendar.CRYPTO_24_7,
            run_id="r",
        )


def test_series_checks_gaps_jumps_stale():
    df = make_daily_crypto(200)
    df.loc[150:, ["open", "high", "low", "close"]] *= 3.0  # a 3x jump (bad tick / unadjusted split)
    df = df.drop(index=[50, 51, 52]).reset_index(drop=True)
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    issues = validate_series(df, symbol="X", timeframe=Timeframe.D1, source="t", calendar=Calendar.CRYPTO_24_7,
                             stale_hours=48, now=df["close_time"].max() + pd.Timedelta(days=5))  # fmt: skip
    checks = issues.groupby("check_name").size()
    assert checks["missing_bars"] == 1  # one contiguous run of 3
    assert "3 missing" in issues.loc[issues.check_name == "missing_bars", "detail"].iloc[0]
    assert checks["abnormal_jump"] >= 1
    assert checks["stale"] == 1


# --------------------------------------------------------------------------- store


def test_upsert_is_idempotent_and_logs_revisions(store):
    df = _crypto_batch(30)
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    c1 = store.upsert_bars("BTC", Timeframe.D1, "coinbase", df, "run1")
    c2 = store.upsert_bars("BTC", Timeframe.D1, "coinbase", df, "run2")
    assert c1["inserted"] == 30 and c2 == {"inserted": 0, "revised": 0, "unchanged": 30}
    df2 = df.copy()
    df2.loc[29, "close"] = df2.loc[29, "close"] * 1.01
    df2.loc[29, "high"] = max(df2.loc[29, "high"], df2.loc[29, "close"])
    c3 = store.upsert_bars("BTC", Timeframe.D1, "coinbase", df2, "run3")
    assert c3["revised"] == 1
    rev = store.query("SELECT * FROM bar_revisions")
    assert len(rev) == 1 and rev.iloc[0]["old_ingest_run_id"] == "run1"
    assert store.query("SELECT count(*) n FROM bars").iloc[0]["n"] == 30


def test_sources_are_never_stitched(store):
    df = _crypto_batch(10)
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    store.upsert_bars("BTC", Timeframe.D1, "coinbase", df, "r1")
    store.upsert_bars("BTC", Timeframe.D1, "bitstamp", df.iloc[5:], "r2")
    with pytest.raises(ValueError, match="multiple sources"):
        store.get_bars("BTC", Timeframe.D1)
    assert len(store.get_bars("BTC", Timeframe.D1, "bitstamp")) == 5


# --------------------------------------------------------------------------- adjustments


def test_split_and_total_return_adjustment():
    ts = pd.Series([pd.Timestamp(f"2024-06-{d:02d} 13:30", tz="UTC") for d in (5, 6, 7, 10, 11)])
    bars = pd.DataFrame({"ts": ts, "open": [1000, 1010, 1020, 102, 103.0], "high": [1010, 1020, 1030, 104, 105.0],
                         "low": [990, 1000, 1010, 101, 102.0], "close": [1000, 1010, 1020, 102, 104.0],
                         "volume": [1, 1, 1, 10, 10.0]})  # fmt: skip
    actions = pd.DataFrame({"date": [date(2024, 6, 10), date(2024, 6, 11)], "split_factor": [10.0, 1.0],
                            "dividend": [0.0, 1.02]})  # fmt: skip
    f = adjustment_factors(bars, actions, Calendar.NYSE)
    split = apply_basis(bars, f, PriceBasis.SPLIT)
    assert split["close"].tolist() == pytest.approx([100, 101, 102, 102, 104])
    assert split["volume"].tolist() == pytest.approx([10, 10, 10, 10, 10])
    tr = apply_basis(bars, f, PriceBasis.TOTAL_RETURN)
    # dividend 1.02 on prior close 102 = 1% -> prices before ex-date scaled by 0.99
    assert tr["close"].iloc[3] == pytest.approx(102 * 0.99)
    assert tr["close"].iloc[4] == 104
    # total return across ex-date = (104 + 1.02) / 102
    assert tr["close"].iloc[4] / tr["close"].iloc[3] == pytest.approx(104 / (102 * 0.99))


# --------------------------------------------------------------------------- update orchestration


def _coinbase_handler(df):
    def handler(req: httpx.Request):
        if req.url.path.endswith("/ticker"):
            return json_response({"price": "1", "time": "2024-01-01T00:00:00Z"})
        s, e = pd.Timestamp(req.url.params["start"]), pd.Timestamp(req.url.params["end"])
        return json_response(coinbase_rows(df[(df.ts >= s) & (df.ts <= e)]))

    return handler


def test_update_series_end_to_end_idempotent(settings, store):
    now = pd.Timestamp.now(tz="UTC").normalize()
    df = make_daily_crypto(
        120, start=str((now - pd.Timedelta(days=119)).date())
    )  # last bar = today (incomplete)
    reg = ProviderRegistry(settings, transport=httpx.MockTransport(_coinbase_handler(df)))
    settings.providers["backfill"]["1d"] = str(df.ts.min().date())
    btc = settings.asset("BTC")
    r1 = update_series(store, settings, reg, btc, Timeframe.D1)
    assert r1.status == "ok"
    assert r1.inserted == 119  # today's still-open bar is NOT stored
    r2 = update_series(store, settings, reg, btc, Timeframe.D1)
    assert r2.inserted == 0 and r2.revised == 0
    runs = store.query("SELECT * FROM ingestion_runs ORDER BY started_at")
    assert list(runs["status"]) == ["ok", "ok"]
    # raw payload archived with matching hash
    path = json.loads(runs.iloc[0]["raw_paths"])[0]
    with gzip.open(path) as fh:
        envelope = json.loads(fh.readline())
    assert envelope["sha256"] == json.loads(runs.iloc[0]["raw_sha256"])[0]


def test_update_records_failure_without_crashing(settings, store):
    def handler(req):
        raise httpx.ConnectError("denied by proxy")

    reg = ProviderRegistry(settings, transport=httpx.MockTransport(handler))
    r = update_series(store, settings, reg, settings.asset("BTC"), Timeframe.D1)
    assert r.status == "failed" and "cannot connect" in r.message
    assert store.query("SELECT status FROM ingestion_runs").iloc[0]["status"] == "failed"


def test_provider_change_is_logged_not_stitched(settings, store):
    df = _crypto_batch(5)
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    store.upsert_bars("BTC", Timeframe.D1, "bitstamp", df, "old")
    reg = ProviderRegistry(
        settings, transport=httpx.MockTransport(_coinbase_handler(make_daily_crypto(5)))
    )
    update_series(store, settings, reg, settings.asset("BTC"), Timeframe.D1)
    ch = store.query("SELECT * FROM series_changes")
    assert ch.iloc[0]["old_source"] == "bitstamp" and ch.iloc[0]["new_source"] == "coinbase"
    assert len(store.get_bars("BTC", Timeframe.D1, "bitstamp")) == 5  # untouched


def test_missing_tiingo_key_fails_cleanly(settings, store, monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY")
    reg = ProviderRegistry(settings, transport=httpx.MockTransport(lambda r: json_response([])))
    r = update_series(store, settings, reg, settings.asset("SPY"), Timeframe.D1)
    assert r.status == "failed" and "TIINGO_API_KEY" in r.message
