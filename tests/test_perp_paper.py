"""Perp paper-tracking: live-only checks (no backfill), written once, strategy-version
aware, and evaluated against a baseline drawn from the same live-checked bars."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import market_signal.perps.paper as paper
from market_signal.perps.data import upsert_funding, upsert_perp_bars
from market_signal.perps.strategies import PerpStrategy, Signals


def _store_market(store, coin="BTC", n=900, plant_every=None, seed=1):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.02, n)
    planted = []
    if plant_every:
        for i in range(150, n - 40, plant_every):
            r[i + 1 : i + 31] += 0.004  # the month after a planted bar drifts up
            planted.append(i)
    c = 100 * np.exp(np.cumsum(r))
    o = np.concatenate([[c[0]], c[:-1]])
    ts = pd.date_range("2024-01-01", periods=n, freq="1D", tz="UTC")
    bars = pd.DataFrame({"ts": ts, "open": o, "high": np.maximum(o, c) * 1.005, "low": np.minimum(o, c) * 0.995,
                         "close": c, "volume": 1.0, "close_time": ts + pd.Timedelta(days=1)})  # fmt: skip
    upsert_perp_bars(store, coin, bars, "hyperliquid", "t")
    hours = pd.date_range(ts[0], ts[-1] + pd.Timedelta(days=1), freq="1h", tz="UTC")
    upsert_funding(store, coin, pd.DataFrame({"time": hours, "funding_rate": 0.00001, "premium": 0.0}),
                   "hyperliquid", "t")  # fmt: skip
    return bars, planted


def _always_long() -> PerpStrategy:
    def fn(f, p):
        z = pd.Series(False, index=f.index)
        return Signals(pd.Series(True, index=f.index), z, f["close"] * 0.9, f["close"] * 1.1)

    return PerpStrategy("always_long", "Always long", "test", "1m", {"v": 1}, {}, {}, fn)


@pytest.fixture
def one_strategy(monkeypatch, settings):
    import market_signal.perps.data as pdata

    monkeypatch.setattr(paper, "STRATEGIES", {"always_long": _always_long()})
    monkeypatch.setattr(pdata, "perp_config", lambda s: {"coins": ["BTC"]})
    monkeypatch.setattr(
        paper, "perp_config", lambda s: {"coins": ["BTC"], "event_study": {"horizons": {"1m": 30}}}
    )
    return settings


def test_records_only_live_bars_once(one_strategy, store):
    bars, _ = _store_market(store)
    last_close = bars["close_time"].iloc[-1]
    res = paper.record_paper_signals(store, one_strategy, now=last_close + pd.Timedelta(hours=2))
    assert res["note"].tolist() == ["signal: LONG"]
    again = paper.record_paper_signals(store, one_strategy, now=last_close + pd.Timedelta(hours=5))
    assert again["note"].tolist() == ["already checked"]
    rows = store.query("SELECT * FROM perp_paper_checks")
    assert (
        len(rows) == 1
        and rows["side"].iloc[0] == "long"
        and rows["stop"].iloc[0] < rows["close"].iloc[0]
    )

    late = paper.record_paper_signals(store, one_strategy, now=last_close + pd.Timedelta(days=3))
    assert late["status"].tolist() == ["skipped"] and "too old" in late["note"].iloc[0]


def test_evaluation_uses_live_checked_bars_and_finds_a_planted_edge(store, settings):
    bars, planted = _store_market(store, n=1600, plant_every=100, seed=3)
    strat = paper.STRATEGIES["trend_ls"]
    h = paper.params_hash(strat)
    planted_set = set(planted)
    for i in range(100, len(bars) - 1):  # pretend each of these bars was checked live
        side = "long" if i in planted_set else None
        store.con.execute("INSERT INTO perp_paper_checks VALUES (?,?,?,?,?,?,?,?,?)",
                          ["trend_ls", "BTC", "hyperliquid", bars["close_time"].iloc[i],
                           bars["close_time"].iloc[i] + pd.Timedelta(hours=1), side, 1.0, 0.9, h])  # fmt: skip
    # a check from an OLDER version of the strategy must be ignored
    store.con.execute("INSERT INTO perp_paper_checks VALUES (?,?,?,?,?,?,?,?,?)",
                      ["trend_ls", "BTC", "hyperliquid", bars["close_time"].iloc[50],
                       bars["close_time"].iloc[50], "short", 1.0, 1.1, "old-version"])  # fmt: skip
    r = paper.evaluate_paper(store, settings, "trend_ls")
    assert r.signals == len(planted) and r.bars_checked == len(bars) - 101
    assert r.first_check == bars["close_time"].iloc[100]
    # planted: +0.4%/day for 30 days after each signal (~+12%); random entries catch only
    # part of that, so the same-period baseline is lower and the excess clearly positive
    assert r.independent >= 12 and r.excess > 0.05
    assert r.too_early  # fewer than 30 independent → still "too early"


def test_evaluation_with_no_checks_is_empty(store, settings):
    r = paper.evaluate_paper(store, settings, "funding_fade")
    assert r.bars_checked == 0 and r.signals == 0 and r.excess is None and r.too_early
