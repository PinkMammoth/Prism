"""Perp open interest collection: Binance parsing/backfill/dedup/gap recovery, venue
separation, irregular Hyperliquid snapshots, coverage reporting. No live network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pandas as pd
import pytest

from market_signal.data.http import HttpClient, SchemaError
from market_signal.perps.binance import BinanceFuturesProvider
from market_signal.perps.open_interest import (
    BINANCE,
    HYPERLIQUID,
    load_oi,
    oi_config,
    oi_coverage,
    oi_gaps,
    plan_stop,
    update_binance_oi,
)

HOUR = 3_600_000
NOW = datetime(2026, 10, 3, 21, 18, tzinfo=UTC)


class FakeOi:
    """Mimics /futures/data/openInterestHist as observed live on 2026-10-03: only the latest
    30 days are served, an older startTime is HTTP 400 (-1130), a window wider than
    ``limit`` returns its LATEST rows, and an unknown symbol is an empty list."""

    def __init__(self, now: datetime = NOW, symbols=("BTCUSDT",), fail: set[str] | None = None):
        self.now_ms = int(now.timestamp() * 1000)
        last = self.now_ms - self.now_ms % HOUR
        oldest = last - 30 * 24 * HOUR + HOUR  # ~29.96 days, like the live API
        self.rows = {s: [{"symbol": s, "sumOpenInterest": f"{1000 + k}.5", "sumOpenInterestValue": f"{(1000 + k) * 50}.25",
                          "CMCCirculatingSupply": "1", "timestamp": t}
                         for k, t in enumerate(range(oldest, last + 1, HOUR))] for s in symbols}  # fmt: skip
        self.fail = fail or set()
        self.calls: list[dict] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/futures/data/openInterestHist"
        p = dict(req.url.params)
        self.calls.append(p)
        if p["symbol"] in self.fail:
            return httpx.Response(503)
        if "startTime" in p and int(p["startTime"]) < self.now_ms - 30 * 24 * HOUR:
            return httpx.Response(
                400, json={"code": -1130, "msg": "parameter 'startTime' is invalid."}
            )
        end = int(p.get("endTime", self.now_ms))
        start = int(p.get("startTime", 0))
        sel = [r for r in self.rows.get(p["symbol"], []) if start <= r["timestamp"] <= end]
        return httpx.Response(200, json=sel[-int(p.get("limit", 30)) :])


def _prov(handler, **kw) -> BinanceFuturesProvider:
    http = HttpClient("binance_futures", "https://fapi.test", requests_per_second=0, backoff_seconds=0,
                      max_retries=0, transport=httpx.MockTransport(handler))  # fmt: skip
    return BinanceFuturesProvider(http, **kw)


@pytest.fixture
def oi_settings(settings, monkeypatch):
    import market_signal.perps.data as pdata

    cfg = pdata.perp_config(settings)
    cfg = {**cfg, "coins": ["BTC", "ETH", "HYPE", "XYZ"]}
    cfg["open_interest"] = {"binance": {**cfg["open_interest"]["binance"],
                                        "symbols": {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "HYPE": "HYPEUSDT"}},
                            "hyperliquid": cfg["open_interest"]["hyperliquid"]}  # fmt: skip
    monkeypatch.setattr(pdata, "perp_config", lambda s: cfg)
    import market_signal.perps.open_interest as oimod

    monkeypatch.setattr(oimod, "perp_config", lambda s: cfg)
    return settings


def _bn(store, coin="BTC") -> pd.DataFrame:
    return store.query("SELECT * FROM perp_oi_history WHERE coin=? ORDER BY observed_at", [coin])


# ----------------------------------------------------------------- parsing + pagination


def test_parse_known_response():
    body = [{"symbol": "BTCUSDT", "sumOpenInterest": "97118.36200000", "sumOpenInterestValue": "8245466286.77272200",
             "CMCCirculatingSupply": "20092634.00000000", "timestamp": 1791054000000}]  # fmt: skip
    df = _prov(lambda req: httpx.Response(200, json=body)).open_interest_history(
        "BTCUSDT", "1h", NOW, NOW
    )
    assert list(df.columns) == ["observed_at", "open_interest", "oi_notional"]
    assert df["observed_at"].iloc[0] == pd.Timestamp("2026-10-03T19:00Z")
    assert df["open_interest"].iloc[0] == pytest.approx(97118.362)
    assert df["oi_notional"].iloc[0] == pytest.approx(8245466286.772722)


def test_schema_errors_fail_loudly():
    with pytest.raises(SchemaError):
        _prov(lambda req: httpx.Response(200, json={"code": -1})).open_interest_history(
            "BTCUSDT", "1h", NOW
        )
    bad = [
        {"symbol": "BTCUSDT", "timestamp": 1, "sumOpenInterest": "x", "sumOpenInterestValue": "1"}
    ]
    with pytest.raises(SchemaError):
        _prov(lambda req: httpx.Response(200, json=bad)).open_interest_history("BTCUSDT", "1h", NOW)
    other = [
        {"symbol": "ETHUSDT", "timestamp": 1, "sumOpenInterest": "1", "sumOpenInterestValue": "1"}
    ]
    with pytest.raises(SchemaError):
        _prov(lambda req: httpx.Response(200, json=other)).open_interest_history(
            "BTCUSDT", "1h", NOW
        )


def test_pages_backward_to_full_window_without_start_time():
    fake = FakeOi()
    df = _prov(fake, oi_limit=200).open_interest_history(
        "BTCUSDT", "1h", NOW - timedelta(days=30), NOW
    )
    assert len(df) == 720 and df["observed_at"].is_monotonic_increasing
    assert (df["observed_at"].diff().dropna() == pd.Timedelta(hours=1)).all()  # complete, no holes
    assert len(fake.calls) == 4  # 200 + 200 + 200 + 120 (short page ends it)
    assert all("startTime" not in c for c in fake.calls)  # never trips the 30-day 400


def test_stops_paging_once_stop_at_is_reached_and_asks_only_for_what_is_needed():
    fake = FakeOi()
    stop = NOW - timedelta(hours=150)
    df = _prov(fake, oi_limit=100).open_interest_history("BTCUSDT", "1h", stop, NOW)
    assert len(fake.calls) == 2
    assert [int(c["limit"]) for c in fake.calls] == [100, 52]  # second page sized to the remainder
    assert df["observed_at"].min() <= pd.Timestamp(stop)
    assert (df["observed_at"].diff().dropna() == pd.Timedelta(hours=1)).all()
    small = FakeOi()
    _prov(small).open_interest_history("BTCUSDT", "1h", NOW - timedelta(hours=3), NOW)
    assert [int(c["limit"]) for c in small.calls] == [4]  # routine top-up: one tiny request


# ----------------------------------------------------------------- backfill / dedup / gaps


def test_first_run_backfills_window_and_second_run_is_noop(oi_settings, store):
    fake = FakeOi(symbols=("BTCUSDT", "ETHUSDT"))
    prov = _prov(fake)
    res = update_binance_oi(oi_settings, store, provider=prov, now=NOW)
    st = dict(zip(res["coin"], res["status"], strict=True))
    assert st == {"BTC": "ok", "ETH": "ok", "HYPE": "unavailable", "XYZ": "unsupported"}
    bn = _bn(store)
    assert len(bn) == 720
    assert set(bn["source"]) == {"binance"} and set(bn["provider_symbol"]) == {"BTCUSDT"}
    assert set(bn["period"]) == {"1h"} and set(bn["market_type"]) == {"usdm_perpetual"}
    assert set(bn["oi_notional_method"]) == {"provider:sumOpenInterestValue"}
    runs = store.query(
        "SELECT entity, status, raw_paths FROM ingestion_runs WHERE dataset='perp_oi_1h' ORDER BY entity"
    )
    assert list(runs["entity"]) == ["BTC", "ETH", "HYPE"] and set(runs["status"]) == {"ok"}
    assert all(p != "[]" for p in runs["raw_paths"])  # raw payloads archived

    fake.calls.clear()
    first_ingested = bn["ingested_at"].iloc[0]
    res2 = update_binance_oi(oi_settings, store, provider=prov, now=NOW)
    assert res2.set_index("coin").loc["BTC", "new_rows"] == 0
    assert len(_bn(store)) == 720  # no duplicates
    assert _bn(store)["ingested_at"].iloc[0] == first_ingested  # unchanged rows untouched
    btc_calls = [c for c in fake.calls if c["symbol"] == "BTCUSDT"]
    assert len(btc_calls) == 1  # only a short overlap is re-read


def test_downtime_is_recovered_by_next_ordinary_run(oi_settings, store):
    early = NOW - timedelta(days=21)
    update_binance_oi(oi_settings, store, provider=_prov(FakeOi(now=early)), now=early)
    assert _bn(store)["observed_at"].max() <= pd.Timestamp(early)
    # 21 days offline, then an ordinary run with no arguments
    res = update_binance_oi(oi_settings, store, provider=_prov(FakeOi(now=NOW)), now=NOW)
    assert res.set_index("coin").loc["BTC", "status"] == "ok"
    bn = _bn(store)
    recent = bn[bn["observed_at"] >= pd.Timestamp(NOW - timedelta(days=29))]
    assert (recent["observed_at"].diff().dropna() == pd.Timedelta(hours=1)).all()
    assert bn["observed_at"].duplicated().sum() == 0


def test_interior_gap_is_refilled(oi_settings, store):
    update_binance_oi(oi_settings, store, provider=_prov(FakeOi()), now=NOW)
    hole = (pd.Timestamp(NOW - timedelta(days=10)), pd.Timestamp(NOW - timedelta(days=8)))
    store.con.execute("DELETE FROM perp_oi_history WHERE observed_at BETWEEN ? AND ?", list(hole))
    assert len(_bn(store)) < 720
    fake = FakeOi()
    update_binance_oi(oi_settings, store, provider=_prov(fake), now=NOW)
    bn = _bn(store)
    assert len(bn) == 720 and (bn["observed_at"].diff().dropna() == pd.Timedelta(hours=1)).all()


def test_revisions_replace_values_without_duplicating(oi_settings, store):
    update_binance_oi(oi_settings, store, provider=_prov(FakeOi()), now=NOW)
    fake = FakeOi()
    fake.rows["BTCUSDT"][-1]["sumOpenInterest"] = "5.0"  # newest point revised by the provider
    res = update_binance_oi(oi_settings, store, provider=_prov(fake), now=NOW)
    assert "1 revised" in res.set_index("coin").loc["BTC", "note"]
    bn = _bn(store)
    assert len(bn) == 720 and bn["open_interest"].iloc[-1] == 5.0


def test_failure_keeps_existing_data_and_other_coins(oi_settings, store):
    update_binance_oi(
        oi_settings, store, provider=_prov(FakeOi(symbols=("BTCUSDT", "ETHUSDT"))), now=NOW
    )
    before_btc = _bn(store, "BTC")
    later = NOW + timedelta(hours=5)
    res = update_binance_oi(oi_settings, store,
                            provider=_prov(FakeOi(now=later, symbols=("BTCUSDT", "ETHUSDT"), fail={"BTCUSDT"})), now=later)  # fmt: skip
    st = res.set_index("coin")["status"]
    assert st["BTC"] == "failed" and st["ETH"] == "ok"
    pd.testing.assert_frame_equal(_bn(store, "BTC"), before_btc)
    assert _bn(store, "ETH")["observed_at"].max() > before_btc["observed_at"].max()
    failed = store.query("SELECT entity FROM ingestion_runs WHERE status='failed'")
    assert list(failed["entity"]) == ["BTC"]


def test_plan_stop_never_reaches_before_api_window():
    class C:
        history_days, overlap_periods, step = 30.0, 3, timedelta(hours=1)

    assert plan_stop(pd.Series([], dtype="datetime64[us, UTC]"), NOW, C) == NOW - timedelta(days=30)
    old = pd.Series(pd.date_range(end=NOW - timedelta(days=40), periods=10, freq="1h"))
    assert plan_stop(old, NOW, C) == NOW - timedelta(days=30)
    recent = pd.Series(pd.date_range(end="2026-10-03T21:00Z", periods=48, freq="1h"))
    assert plan_stop(recent, NOW, C) == datetime(2026, 10, 3, 18, tzinfo=UTC)


# ----------------------------------------------------------------- venues + Hyperliquid


def _hl_snapshot(store, coin, at, oi=10.0, mark=100.0):
    store.con.execute(
        "INSERT INTO perp_snapshots (coin, source, snapshot_at, mark_px, open_interest, oi_notional, ingest_run_id) "
        "VALUES (?, 'hyperliquid', ?, ?, ?, ?, 'r')",
        [coin, at, mark, oi, oi * mark],
    )


def test_venues_stay_separate(oi_settings, store):
    update_binance_oi(oi_settings, store, provider=_prov(FakeOi()), now=NOW)
    _hl_snapshot(store, "BTC", NOW - timedelta(hours=2))
    hl, bn = load_oi(store, "BTC", HYPERLIQUID), load_oi(store, "BTC", BINANCE)
    assert (
        len(hl) == 1 and set(hl["venue"]) == {"hyperliquid"} and set(hl["period"]) == {"snapshot"}
    )
    assert set(hl["oi_notional_method"]) == {"open_interest*mark_px"}
    assert len(bn) == 720 and set(bn["venue"]) == {"binance"}
    both = store.query(
        "SELECT venue, count(*) AS n FROM perp_oi_observations GROUP BY venue ORDER BY venue"
    )
    assert dict(zip(both["venue"], both["n"], strict=True)) == {"binance": 720, "hyperliquid": 1}


def test_irregular_hyperliquid_snapshots_keep_exact_times(oi_settings, store):
    # a realistic local-PC day: logon, scattered runs, off overnight, back late evening
    times = [pd.Timestamp(t) for t in ("2026-10-01T06:16:41Z", "2026-10-01T10:02:13Z", "2026-10-01T14:00:59Z",
                                       "2026-10-01T18:07:30Z", "2026-10-01T21:15:02Z", "2026-10-02T06:21:44Z",
                                       "2026-10-02T09:59:01Z")]  # fmt: skip
    for t in times:
        _hl_snapshot(store, "BTC", t.to_pydatetime())
    hl = load_oi(store, "BTC", HYPERLIQUID)
    assert list(pd.to_datetime(hl["observed_at"], utc=True)) == times  # nothing re-gridded
    g = oi_gaps(hl["observed_at"], timedelta(hours=20))
    assert g.empty  # overnight PC-off (~9h) is not a reportable gap
    g = oi_gaps(hl["observed_at"], timedelta(hours=8))
    assert len(g) == 1 and g["hours"].iloc[0] == pytest.approx(9.11, abs=0.01)


def test_hl_snapshot_min_gap_prevents_back_to_back_duplicates(oi_settings, store):
    from market_signal.perps.open_interest import collect_hl_snapshot

    class Prov:
        calls = 0

        def perp_contexts(self):
            Prov.calls += 1
            return pd.DataFrame([{"coin": "BTC", "mark_px": 1.0, "oracle_px": 1.0, "mid_px": 1.0, "prev_day_px": 1.0,
                                  "funding_rate": 0.0, "premium": 0.0, "open_interest": 5.0, "oi_notional": 5.0,
                                  "day_ntl_vlm": 1.0, "max_leverage": 40.0}])  # fmt: skip

        def drain_raw(self):
            return []

    r1 = collect_hl_snapshot(oi_settings, store, Prov())
    r2 = collect_hl_snapshot(oi_settings, store, Prov())
    assert (
        r1["new_rows"].iloc[0] == 1
        and r2["new_rows"].iloc[0] == 0
        and "skipped" in r2["note"].iloc[0]
    )
    assert Prov.calls == 1
    assert len(load_oi(store, "BTC", HYPERLIQUID)) == 1


# ----------------------------------------------------------------- coverage


def test_coverage_reports_latest_gaps_and_status(oi_settings, store):
    update_binance_oi(oi_settings, store, provider=_prov(FakeOi()), now=NOW)
    store.con.execute("DELETE FROM perp_oi_history WHERE observed_at BETWEEN ? AND ?",
                      [NOW - timedelta(days=5, hours=4), NOW - timedelta(days=5)])  # fmt: skip
    _hl_snapshot(store, "BTC", NOW - timedelta(days=3))
    _hl_snapshot(store, "BTC", NOW - timedelta(hours=2))
    _hl_snapshot(store, "ETH", NOW - timedelta(days=2))
    cov = oi_coverage(store, oi_settings, now=NOW).set_index(["venue", "coin"])
    b = cov.loc[("binance", "BTC")]
    assert b["status"] == "ok" and b["gaps_30d"] == 1 and b["max_gap_h"] == pytest.approx(5.0)
    assert b["last"] == pd.Timestamp("2026-10-03T21:00Z") and b["rows"] == 716
    assert cov.loc[("binance", "XYZ"), "status"] == "unsupported"
    assert cov.loc[("binance", "ETH"), "status"] == "NO DATA"
    h = cov.loc[("hyperliquid", "BTC")]
    assert h["status"] == "ok" and h["gaps_30d"] == 1 and h["rows"] == 2
    assert cov.loc[("hyperliquid", "ETH"), "status"] == "STALE"
    assert cov.loc[("hyperliquid", "HYPE"), "status"] == "NO DATA"

    later = cov_at(store, oi_settings, NOW + timedelta(days=22))
    assert later.loc[("binance", "BTC"), "status"] == "AT RISK"
    assert (
        cov_at(store, oi_settings, NOW + timedelta(days=31)).loc[("binance", "BTC"), "status"]
        == "LOST"
    )
    assert (
        cov_at(store, oi_settings, NOW + timedelta(hours=14)).loc[("binance", "BTC"), "status"]
        == "stale"
    )


def cov_at(store, settings, now):
    return oi_coverage(store, settings, now=now).set_index(["venue", "coin"])


def test_real_config_maps_every_perp_coin():
    from market_signal.config import get_settings

    cfg = oi_config(get_settings())
    assert cfg.enabled and cfg.period == "1h" and cfg.history_days == 30
    assert set(cfg.coins) <= set(cfg.symbols)
