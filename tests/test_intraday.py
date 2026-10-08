"""Phase 15 intraday substrate: UTC grid, closed-bar guarantee, availability, backfill paging,
incremental overlap, revisions + point-in-time reads, gap triage, causal alignment, dataset
fingerprints, disk guard, runtime schedule, shadow execution and paper continuity."""

# ruff: noqa: F811  (helpers take the imported fixtures' values)

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from market_signal.data.http import RawPayload
from market_signal.intraday import align as al
from market_signal.intraday import bars as ib
from market_signal.intraday import grid
from market_signal.intraday import ingest as ing
from market_signal.models.domain import Timeframe

M15, H1, H4 = Timeframe.M15, Timeframe.H1, Timeframe.H4
T0 = datetime(2026, 9, 1, tzinfo=UTC)
SRC = Path(__file__).resolve().parents[1] / "src" / "market_signal"


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz="UTC")


def synth(tf: Timeframe, start, n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    opens = pd.date_range(pd.Timestamp(start), periods=n, freq=grid.interval(tf), tz="UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.003, n)))
    open_ = np.r_[100.0, close[:-1]]
    hi = np.maximum(open_, close) * (1 + rng.uniform(0, 0.002, n))
    lo = np.minimum(open_, close) * (1 - rng.uniform(0, 0.002, n))
    return pd.DataFrame({"open_time": opens, "open": open_, "high": hi, "low": lo, "close": close,
                         "volume": rng.uniform(10, 20, n), "trades": rng.integers(100, 200, n)})  # fmt: skip


class Clock:
    def __init__(self, t):
        self.t = pd.Timestamp(t, tz="UTC")

    def __call__(self):
        return pd.Timestamp(self.t).tz_convert("UTC").to_pydatetime()


class FakeFeed:
    """Provider stand-in. Serves its 'exchange' bars in [start, end) that have OPENED by the
    clock (so the bar containing now is served still forming, like the real API), minus any
    hidden (not yet published / missing) open times. Records every page request."""

    source = raw_provider = "hyperliquid"

    def __init__(self, clock: Clock, bars: dict, retention: int | None = None):
        self.clock, self.bars, self.retention = clock, bars, retention
        self.hidden: set = set()
        self.calls: list = []
        self._raw: list = []

    def page(self, symbol, tf, start, end):
        self.calls.append((symbol, tf.value, start, end))
        df = self.bars[(symbol, tf.value)]
        now = self.clock.t
        out = df[(df["open_time"] >= start) & (df["open_time"] < end) & (df["open_time"] <= now)]
        if self.retention is not None:
            floor = grid.floor_open(now, tf) - (self.retention - 1) * grid.interval(tf)
            out = out[out["open_time"] >= floor]
        out = out[~out["open_time"].isin(self.hidden)].reset_index(drop=True)
        self._raw.append(RawPayload("hyperliquid", "POST", "https://x/info", {"c": symbol}, 200,
                                    out.to_json(date_format="iso").encode(), now.to_pydatetime()))  # fmt: skip
        return out.copy()

    def drain_raw(self):
        raw, self._raw = self._raw, []
        return raw


@pytest.fixture
def cfg(settings):
    return ing.intraday_config(settings)


def run(store, cfg, feed, plans, clock, mode="update"):
    return ing.run_plans(store, cfg, "hyperliquid", feed, plans, mode=mode, now=clock)


def upd(store, cfg, feed, clock, coins=("BTC",), tfs=(M15,)):
    v = cfg.venues["hyperliquid"]
    plans = [p for c in coins for tf in tfs
             if (p := ing.plan_update(store, v, c, tf, clock.t)) is not None]  # fmt: skip
    return run(store, cfg, feed, plans, clock)


def digest(store, table="perp_intraday_bars") -> str:
    rows = store.con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
    return hashlib.sha256(repr(rows).encode()).hexdigest()


# --------------------------------------------------------------------------- grid


def test_utc_boundaries_are_pinned():
    at = ts("2026-10-05 10:37")
    assert grid.latest_closed_open(at, M15) == ts("2026-10-05 10:15")  # closes 10:30
    assert grid.latest_closed_open(at, H1) == ts("2026-10-05 09:00")  # closes 10:00
    assert grid.latest_closed_open(at, H4) == ts("2026-10-05 04:00")  # closes 08:00
    # exactly at a close the bar has closed; one microsecond earlier it has not
    assert grid.latest_closed_open(ts("2026-10-05 12:00"), H4) == ts("2026-10-05 08:00")
    assert grid.latest_closed_open(ts("2026-10-05 11:59:59.999999"), H4) == ts("2026-10-05 04:00")
    opens = grid.expected_opens(ts("2026-10-05 00:00"), ts("2026-10-06 00:00"), H4)
    assert [t.hour for t in opens] == [0, 4, 8, 12, 16, 20]
    assert [
        t.minute for t in grid.expected_opens(ts("2026-10-05 10:00"), ts("2026-10-05 11:00"), M15)
    ] == [0, 15, 30, 45]
    assert grid.bar_close(ts("2026-10-05 20:00"), H4) == ts("2026-10-06 00:00")
    with pytest.raises(ValueError):
        grid.bar_close(ts("2026-10-05 02:00"), H4)  # off the 4h grid
    with pytest.raises(ValueError):
        grid.floor_open(datetime(2026, 10, 5, 10, 37), M15)  # naive: never guessed
    with pytest.raises(ValueError):
        grid.parse_timeframe("1d")
    # a non-UTC offset is converted, not reinterpreted
    assert grid.floor_open(pd.Timestamp("2026-10-05 12:37", tz="Europe/London"), H1) == ts(
        "2026-10-05 11:00"
    )


# --------------------------------------------------------------------------- closed bars / availability


def test_forming_bar_is_never_stored(store, cfg):
    clock = Clock("2026-09-01 10:37")
    feed = FakeFeed(clock, {("BTC", "15m"): synth(M15, T0, 200)})
    p = ing.plan_backfill(cfg.venues["hyperliquid"], "BTC", M15, T0, clock.t, clock.t)
    assert p.end == ts("2026-09-01 10:30")  # never asks for the forming 10:30 bar
    # even if the provider is asked for it (and serves it), it is dropped
    p.end = ts("2026-09-01 10:45")
    res = run(store, cfg, feed, [p], clock, "backfill")
    assert res.iloc[0]["forming"] == 1
    df = ib.load_bars(store, "hyperliquid", "BTC", M15)
    assert df["open_time"].max() == ts("2026-09-01 10:15") and len(df) == 42
    assert (df["close_time"] <= clock.t).all() and (
        df["first_observed_at"] >= df["close_time"]
    ).all()
    # last line of defence in the writer, and the table CHECK itself
    late = synth(M15, "2026-09-01 10:30", 1)
    n = ib.upsert_bars(store, "hyperliquid", "BTC", M15, late, observed_at=clock(), run_id="r")
    assert n["forming"] == 1 and n["inserted"] == 0
    with pytest.raises(Exception, match=r"CHECK|constraint"):
        store.con.execute("INSERT INTO perp_intraday_bars VALUES ('x','BTC','15m', ?, ?, 1,1,1,1,1,1,"
                          "'native', ?, 'r', true, 0, ?, 'r')",
                          [ts("2026-09-01 10:30"), ts("2026-09-01 10:45"), ts("2026-09-01 10:40"),
                           ts("2026-09-01 10:40")])  # fmt: skip


def test_settle_buffer_excludes_a_bar_that_closed_moments_before_the_request(store, cfg):
    clock = Clock("2026-09-01 11:00:03")  # 3 s after the close; settle is 5 s
    feed = FakeFeed(clock, {("BTC", "1h"): synth(H1, T0, 20)})
    p = ing.plan_backfill(
        cfg.venues["hyperliquid"], "BTC", H1, T0, ts("2026-09-01 12:00"), ts("2026-09-01 12:00")
    )
    run(store, cfg, feed, [p], clock, "backfill")
    assert ib.load_bars(store, "hyperliquid", "BTC", H1)["open_time"].max() == ts(
        "2026-09-01 09:00"
    )


def test_availability_is_first_observation_not_theoretical_close(store, cfg):
    clock = Clock("2026-09-01 10:01")
    bars = synth(M15, T0, 100)
    feed = FakeFeed(clock, {("BTC", "15m"): bars})
    feed.hidden = {ts("2026-09-01 09:45")}  # provider publishes this bar late
    run(
        store,
        cfg,
        feed,
        [ing.plan_backfill(cfg.venues["hyperliquid"], "BTC", M15, T0, clock.t, clock.t)],
        clock,
        "backfill",
    )
    clock.t = ts("2026-09-01 10:16")
    feed.hidden = set()
    upd(store, cfg, feed, clock)
    df = ib.load_bars(store, "hyperliquid", "BTC", M15).set_index("open_time")
    late = df.loc[ts("2026-09-01 09:45")]
    assert late["close_time"] == ts("2026-09-01 10:00") and late["first_observed_at"] == ts(
        "2026-09-01 10:16"
    )
    assert not late["observed_live"]  # first held > one interval after its close
    assert df.loc[ts("2026-09-01 10:00"), "observed_live"]
    # point-in-time: at 10:05 Prism did not have it
    k = ib.load_bars(store, "hyperliquid", "BTC", M15, known_at=ts("2026-09-01 10:05"))
    assert ts("2026-09-01 09:45") not in set(k["open_time"]) and k["open_time"].max() == ts(
        "2026-09-01 09:30"
    )


# --------------------------------------------------------------------------- backfill / update / revisions


def test_paginated_backfill_has_no_duplicates_or_gaps(store, cfg, monkeypatch):
    clock = Clock("2026-09-03 00:07")
    bars = synth(M15, T0, 400)
    feed = FakeFeed(clock, {("BTC", "15m"): bars})
    v = cfg.venues["hyperliquid"]
    monkeypatch.setitem(
        cfg.venues, "hyperliquid", ing.VenueConfig(**{**v.__dict__, "page_bars": 7})
    )
    p = ing.plan_backfill(
        cfg.venues["hyperliquid"], "BTC", M15, T0, ts("2026-09-02 12:00"), clock.t
    )
    res = run(store, cfg, feed, [p], clock, "backfill")
    assert len(feed.calls) == -(-p.bars // 7)
    starts = [c[2] for c in feed.calls]
    assert starts == sorted(starts) and all(
        a[3] == b[2] for a, b in zip(feed.calls, feed.calls[1:], strict=False)
    )
    df = ib.load_bars(store, "hyperliquid", "BTC", M15)
    assert len(df) == p.bars == 144 and res.iloc[0]["inserted"] == 144
    assert (
        df["open_time"].is_unique
        and (df["open_time"].diff().dropna() == pd.Timedelta("15min")).all()
    )
    assert df["open_time"].min() == T0 and df["close_time"].max() == ts("2026-09-02 12:00")
    pd.testing.assert_series_equal(df["close"], bars["close"].iloc[:144], check_names=False)
    run_row = store.con.execute("SELECT status, rows_written, raw_paths, raw_sha256, params FROM ingestion_runs "
                                "WHERE dataset='perp_intraday_bars'").fetchone()  # fmt: skip
    assert (
        run_row[0] == "ok" and run_row[1] == 144 and len(json.loads(run_row[2])) == len(feed.calls)
    )
    params = json.loads(run_row[4])
    assert params["ingest_version"] == ib.INGEST_VERSION and params["plans"][0]["coins"] == ["BTC"]
    archive = Path(json.loads(run_row[2])[0].split("#")[0])
    assert archive.exists() and archive.name.endswith(".jsonl.gz")


def test_retention_limit_is_reported_not_silent(cfg):
    v = cfg.venues["hyperliquid"]
    p = ing.plan_backfill(v, "BTC", M15, ts("2025-01-01"), ts("2026-10-06"), ts("2026-10-05 10:37"))
    assert p.bars == 5000 and "NOT SERVED" in p.notes[0] and p.end == ts("2026-10-05 10:30")


def test_incremental_update_refetches_only_the_overlap_and_is_idempotent(store, cfg):
    clock = Clock("2026-09-01 12:01")
    bars = synth(M15, T0, 300)
    feed = FakeFeed(clock, {("BTC", "15m"): bars})
    upd(store, cfg, feed, clock)  # seeds
    before = digest(store)
    feed.calls.clear()
    assert upd(store, cfg, feed, clock).empty and not feed.calls  # nothing new closed: no request
    clock.t = ts("2026-09-01 12:16")
    res = upd(store, cfg, feed, clock).iloc[0]
    ((_, _, start, end),) = feed.calls
    assert start == ts("2026-09-01 11:45") - 4 * pd.Timedelta("15min") and end == ts(
        "2026-09-01 12:15"
    )
    # the 4 overlap bars before the newest stored one, that one, and the new bar
    assert (res["inserted"], res["revised"], res["unchanged"]) == (1, 0, 5)
    after = digest(store)
    assert after != before
    # a repeat of the same update changes nothing at all
    plan = ing.plan_backfill(cfg.venues["hyperliquid"], "BTC", M15, T0, clock.t, clock.t)
    r2 = run(store, cfg, feed, [plan], clock, "backfill").iloc[0]
    assert (r2["inserted"], r2["revised"]) == (0, 0) and digest(store) == after


def test_revision_is_recorded_and_point_in_time_reads_undo_it(store, cfg):
    clock = Clock("2026-09-01 12:01")
    bars = synth(M15, T0, 300)
    feed = FakeFeed(clock, {("BTC", "15m"): bars})
    upd(store, cfg, feed, clock)
    t = ts("2026-09-01 11:30")
    old = float(bars.loc[bars["open_time"] == t, "close"].iloc[0])
    bars.loc[bars["open_time"] == t, ["close", "high"]] = [old * 1.01, old * 1.02]
    clock.t = ts("2026-09-01 12:16")
    res = upd(store, cfg, feed, clock).iloc[0]
    assert res["revised"] == 1
    row = store.con.execute("SELECT close, revision, updated_at, first_observed_at FROM perp_intraday_bars "
                            "WHERE open_time=?", [t]).fetchone()  # fmt: skip
    assert row[0] == pytest.approx(old * 1.01) and row[1] == 1
    assert pd.Timestamp(row[2]) == ts("2026-09-01 12:16") and pd.Timestamp(row[3]) == ts(
        "2026-09-01 12:01"
    )
    rev = store.query("SELECT * FROM perp_intraday_revisions")
    assert len(rev) == 1 and rev.loc[0, "old_close"] == pytest.approx(old)
    assert rev.loc[0, "revision"] == 1 and pd.Timestamp(rev.loc[0, "old_observed_at"]) == ts(
        "2026-09-01 12:01"
    )
    # what Prism knew at 12:10 is the original value; after 12:16 the revision
    k = ib.load_bars(store, "hyperliquid", "BTC", M15, known_at=ts("2026-09-01 12:10")).set_index(
        "open_time"
    )
    assert k.loc[t, "close"] == pytest.approx(old) and k.loc[t, "revision"] == 0
    k = ib.load_bars(store, "hyperliquid", "BTC", M15, known_at=ts("2026-09-01 12:20")).set_index(
        "open_time"
    )
    assert k.loc[t, "close"] == pytest.approx(old * 1.01)
    # re-observing the revised values is not another revision
    clock.t = ts("2026-09-01 12:31")
    assert upd(store, cfg, feed, clock).iloc[0]["revised"] == 0


def test_conflicting_duplicates_in_a_response_fail_the_series(store, cfg):
    clock = Clock("2026-09-01 12:01")
    b = synth(M15, T0, 10)
    dup = pd.concat([b, b.iloc[[3]].assign(close=b.iloc[3]["close"] * 2)], ignore_index=True)
    feed = FakeFeed(clock, {("BTC", "15m"): dup})
    res = run(
        store,
        cfg,
        feed,
        [ing.plan_backfill(cfg.venues["hyperliquid"], "BTC", M15, T0, clock.t, clock.t)],
        clock,
        "backfill",
    )
    assert (
        res.iloc[0]["status"] == "failed" and ib.load_bars(store, "hyperliquid", "BTC", M15).empty
    )
    assert store.con.execute("SELECT status FROM ingestion_runs").fetchone()[0] == "failed"


def test_impossible_ohlc_rows_are_dropped_and_logged(store, cfg):
    clock = Clock("2026-09-01 03:01")
    b = synth(M15, T0, 12)
    b.loc[5, "high"] = b.loc[5, "low"] * 0.5
    feed = FakeFeed(clock, {("BTC", "15m"): b})
    res = run(
        store,
        cfg,
        feed,
        [ing.plan_backfill(cfg.venues["hyperliquid"], "BTC", M15, T0, clock.t, clock.t)],
        clock,
        "backfill",
    )
    assert res.iloc[0]["invalid"] == 1 and len(ib.load_bars(store, "hyperliquid", "BTC", M15)) == 11
    assert (
        store.con.execute(
            "SELECT count(*) FROM data_quality_issues WHERE check_name='intraday_ohlc'"
        ).fetchone()[0]
        == 1
    )


# --------------------------------------------------------------------------- gaps


def test_gap_categories(store, cfg):
    pol = cfg.policy("hyperliquid")
    pol = ib.SeriesPolicy(live=True, retention_bars=40, stale_after=pol.stale_after)
    clock = Clock("2026-09-01 12:01")
    bars = synth(M15, T0, 300)
    feed = FakeFeed(clock, {("BTC", "15m"): bars})
    feed.hidden = {ts("2026-09-01 11:00"), ts("2026-09-01 11:15")}  # exchange served nothing
    upd(store, cfg, feed, clock)
    # local misses: rows deleted as if never fetched, coverage removed for that range
    store.con.execute(
        "DELETE FROM perp_intraday_bars WHERE open_time IN (?, ?)",
        [ts("2026-09-01 01:00"), ts("2026-09-01 10:00")],
    )
    store.con.execute("DELETE FROM perp_intraday_coverage")
    ib.add_coverage(
        store, "hyperliquid", "BTC", M15, ts("2026-09-01 10:30"), ts("2026-09-01 12:00"), clock()
    )
    g = ib.gaps(store, "hyperliquid", "BTC", M15, pol, clock.t)
    got = {(r.start, r.bars, r.category) for r in g.itertuples()}
    assert got == {(ts("2026-09-01 11:00"), 2, "provider_missing"),
                   (ts("2026-09-01 10:00"), 1, "recoverable"),      # inside the newest 40 bars
                   (ts("2026-09-01 01:00"), 1, "permanent")}  # fmt: skip
    cov = ib.coverage(store, [("hyperliquid", "BTC", M15, pol)], clock.t).iloc[0]
    assert (cov["missing"], cov["provider_missing"], cov["recoverable"], cov["permanent"]) == (
        4,
        2,
        1,
        1,
    )
    assert cov["status"] == "GAPS" and cov["duplicates"] == 0 and cov["malformed"] == 0
    assert cov["expected"] == cov["rows"] + cov["missing"]


def test_coverage_ranges_merge(store):
    a = ts("2026-09-01")
    for s, e in [(0, 4), (8, 12), (4, 8), (20, 24)]:
        ib.add_coverage(
            store, "hyperliquid", "BTC", H1, a + pd.Timedelta(hours=s), a + pd.Timedelta(hours=e), a
        )
    rows = store.con.execute(
        "SELECT covered_from, covered_to FROM perp_intraday_coverage ORDER BY 1"
    ).fetchall()
    assert [(pd.Timestamp(x) - a, pd.Timestamp(y) - a) for x, y in rows] == [
        (pd.Timedelta(0), pd.Timedelta(hours=12)),
        (pd.Timedelta(hours=20), pd.Timedelta(hours=24)),
    ]


def test_staleness_uses_the_timeframes_own_threshold(store, cfg):
    clock = Clock("2026-09-01 12:01")
    feed = FakeFeed(clock, {("BTC", "15m"): synth(M15, T0, 300), ("BTC", "4h"): synth(H4, T0, 10)})
    upd(store, cfg, feed, clock, tfs=(M15, H4))
    two_hours = ts("2026-09-01 14:01")
    cov = ib.coverage(store, [("hyperliquid", "BTC", M15, cfg.policy("hyperliquid")),
                              ("hyperliquid", "BTC", H4, cfg.policy("hyperliquid"))], two_hours)  # fmt: skip
    assert list(cov["status"]) == ["STALE", "OK"]  # 15m two hours old is bad; 4h is not


# --------------------------------------------------------------------------- alignment


def _bars_frame(tf, opens_obs):
    """Bars with given open times and first-observed times."""
    rows = []
    for o, seen in opens_obs:
        o = ts(o)
        rows.append({"open_time": o, "close_time": o + grid.interval(tf), "open": 1.0, "high": 2.0, "low": 0.5,
                     "close": float(o.hour * 100 + o.minute), "volume": 1.0, "trades": 1,
                     "first_observed_at": ts(seen), "observed_live": True, "revision": 0})  # fmt: skip
    return pd.DataFrame(rows)


def test_ltf_never_sees_an_unclosed_or_unobserved_htf_bar():
    h4 = _bars_frame(
        H4,
        [
            ("2026-10-05 00:00", "2026-10-05 04:00:30"),
            ("2026-10-05 04:00", "2026-10-05 08:00:30"),
            ("2026-10-05 08:00", "2026-10-05 12:02:00"),
        ],
    )  # published 2 minutes late
    times = [
        ts("2026-10-05 10:37"),
        ts("2026-10-05 12:00"),
        ts("2026-10-05 12:01"),
        ts("2026-10-05 12:02"),
    ]
    a = al.align(times, h4, H4)
    assert list(a["open_time"]) == [ts("2026-10-05 04:00")] * 3 + [ts("2026-10-05 08:00")]
    assert list(a["bars_behind"]) == [
        0,
        1,
        1,
        0,
    ]  # at 12:00-12:01 the 08:00 bar is closed but unseen
    assert (pd.DatetimeIndex(a["close_time"]) <= pd.DatetimeIndex(times)).all()
    assumed = al.align(times, h4, H4, al.Availability.assumed(timedelta(seconds=30)))
    assert list(assumed["open_time"]) == [ts("2026-10-05 04:00"), ts("2026-10-05 04:00"),
                                          ts("2026-10-05 08:00"), ts("2026-10-05 08:00")]  # fmt: skip
    before_any = al.align([ts("2026-10-05 03:59")], h4, H4)
    assert (
        before_any["open_time"].isna().all() and before_any["close"].isna().all()
    )  # never forward-filled


def test_a_late_published_older_bar_never_displaces_a_newer_available_one():
    h1 = _bars_frame(H1, [("2026-10-05 10:00", "2026-10-05 15:00"),   # gap repaired much later
                          ("2026-10-05 11:00", "2026-10-05 12:01"),
                          ("2026-10-05 14:00", "2026-10-05 15:00:30")])  # fmt: skip
    a = al.align([ts("2026-10-05 15:00:10"), ts("2026-10-05 12:30")], h1, H1)
    assert list(a["open_time"]) == [ts("2026-10-05 11:00"), ts("2026-10-05 11:00")]


def test_no_future_leakage_rows_after_t_cannot_change_the_aligned_view():
    rng = np.random.default_rng(3)
    opens = pd.date_range("2026-10-01", periods=200, freq="1h", tz="UTC")
    seen = opens + pd.Timedelta("1h") + pd.to_timedelta(rng.integers(0, 7200, 200), unit="s")
    full = _bars_frame(H1, list(zip(opens.astype(str), seen.astype(str), strict=True)))
    probe = pd.date_range("2026-10-02", periods=60, freq="37min", tz="UTC")
    ref = al.align(probe, full, H1)
    for t in probe[::7]:
        trimmed = full[full["first_observed_at"] <= t]  # only what existed by t
        one = al.align([t], trimmed, H1)
        pd.testing.assert_frame_equal(
            one.reset_index(drop=True), ref.loc[[t]].reset_index(drop=True), check_dtype=False
        )
        assert pd.isna(ref.loc[t, "available_at"]) or ref.loc[t, "available_at"] <= t


def test_alignment_uses_values_known_at_the_instant(store, cfg):
    clock = Clock("2026-09-01 12:01")
    bars = synth(H1, T0, 30)
    feed = FakeFeed(clock, {("BTC", "1h"): bars})
    upd(store, cfg, feed, clock, tfs=(H1,))
    old = float(bars.loc[11, "close"])
    bars.loc[11, ["close", "high"]] = [old * 1.05, old * 1.06]
    clock.t = ts("2026-09-01 13:01")
    upd(store, cfg, feed, clock, tfs=(H1,))
    snap = al.snapshot(store, "hyperliquid", "BTC", ts("2026-09-01 12:30"), (H1,))
    assert snap.iloc[0]["open_time"] == ts("2026-09-01 11:00") and snap.iloc[0][
        "close"
    ] == pytest.approx(old)
    snap = al.snapshot(store, "hyperliquid", "BTC", ts("2026-09-01 13:30"), (H1,))
    assert snap.iloc[0]["open_time"] == ts("2026-09-01 12:00")


# --------------------------------------------------------------------------- dataset fingerprints


def test_intraday_dataset_fingerprint_is_deterministic_and_carries_pit_columns(store, cfg):
    from market_signal.research.lab.datasets import SeriesSelection, capture_dataset, snapshot_rows

    clock = Clock("2026-09-01 12:01")
    feed = FakeFeed(clock, {("BTC", "15m"): synth(M15, T0, 300)})
    upd(store, cfg, feed, clock)
    sel = (SeriesSelection(kind="perp_intraday_bars", symbol="BTC", source="hyperliquid", timeframe=M15,
                           start=T0, end=ts("2026-09-01 12:00").to_pydatetime()),
           SeriesSelection(kind="perp_intraday_revisions", symbol="BTC", source="hyperliquid", timeframe=M15,
                           start=T0, end=ts("2026-09-01 12:00").to_pydatetime()))  # fmt: skip
    a, b = capture_dataset(store, sel), capture_dataset(store, sel)
    assert a.manifest.dataset_id == b.manifest.dataset_id and a.blobs == b.blobs
    fp = a.manifest.series[0]
    assert fp.strength == "content_sha256" and fp.row_count == 47  # close_time in [start, end)
    assert {"first_observed_at", "observed_live", "revision"} <= {c for c, _ in fp.columns}
    rows = snapshot_rows(fp, a.blobs[0][1])
    assert rows[0]["first_observed_at"] == ts("2026-09-01 12:01").to_pydatetime()
    with pytest.raises(ValueError):
        SeriesSelection(kind="perp_intraday_bars", symbol="BTC", source="hyperliquid", timeframe=Timeframe.D1,
                        start=T0, end=ts("2026-09-02").to_pydatetime())  # fmt: skip
    with pytest.raises(ValueError):
        SeriesSelection(kind="perp_bars", symbol="BTC", source="hyperliquid", timeframe=M15,
                        start=T0, end=ts("2026-09-02").to_pydatetime())  # fmt: skip


# --------------------------------------------------------------------------- disk guard


def test_large_backfill_is_refused_before_writing_when_disk_is_unsafe(
    store, cfg, settings, monkeypatch
):
    from market_signal.ops import runtime as rt

    total = 4 * 2**30
    monkeypatch.setattr(rt, "disk_state", lambda p: rt.DiskState(str(p), total, int(0.16 * total)))
    clock = Clock("2026-10-05 10:37")
    feed = FakeFeed(clock, {})
    with pytest.raises(ing.DiskUnsafe, match="Required capacity"):
        ing.backfill(
            settings, store, venue="binance", start=ts("2019-09-01"), fetcher=feed, now=clock
        )
    assert (
        not feed.calls
        and store.con.execute("SELECT count(*) FROM ingestion_runs").fetchone()[0] == 0
    )
    # plenty of space: allowed
    monkeypatch.setattr(rt, "disk_state", lambda p: rt.DiskState(str(p), total, int(0.9 * total)))
    est = ing.check_disk(store.path, {"rows": 1000, "db_bytes": 40_000, "raw_bytes": 60_000})
    assert est["checked"] and est["free_after"] > est["floor"]


# --------------------------------------------------------------------------- runtime


def test_intraday_schedule_crontab_and_next_run():
    from market_signal.ops import runtime as rt

    tab = rt.crontab()
    for m in (1, 16, 31, 46):
        assert f"{m} * * * * market ops cycle intraday --trigger schedule --wait 600" in tab
    assert rt.next_run("intraday", datetime(2026, 10, 5, 10, 37, tzinfo=UTC)) == datetime(
        2026, 10, 5, 10, 46, tzinfo=UTC
    )
    assert rt.next_run("intraday", datetime(2026, 10, 5, 23, 50, tzinfo=UTC)) == datetime(
        2026, 10, 6, 0, 1, tzinfo=UTC
    )
    # data collection only: no strategy, co-pilot or paper step on the 15-minute cadence
    assert all(args[0] == "bars" for _, args in rt.JOBS["intraday"])
    assert rt.SCHEDULE["prospective"] == ["00:10", "00:45", "03:00", "06:00", "11:00", "17:00"]


def test_concurrent_ingestion_is_serialised_and_idempotent(store, cfg):
    clock = Clock("2026-09-01 12:01")
    feed = FakeFeed(clock, {("BTC", "15m"): synth(M15, T0, 300)})
    v = cfg.venues["hyperliquid"]
    # two schedulers planned the same window before either wrote
    p1, p2 = (ing.plan_update(store, v, "BTC", M15, clock.t) for _ in range(2))
    run(store, cfg, feed, [p1], clock)
    once = digest(store)
    r = run(store, cfg, feed, [p2], clock).iloc[0]
    assert (r["inserted"], r["revised"]) == (0, 0) and digest(store) == once
    n, d = store.con.execute(
        "SELECT count(*), count(DISTINCT open_time) FROM perp_intraday_bars"
    ).fetchone()
    assert n == d


def test_runtime_intraday_job_lock_and_quiet_alerts(project, monkeypatch):
    from market_signal.data.store import Store
    from market_signal.ops import runtime as rt

    monkeypatch.setenv("PRISM_LOCK_TIMEOUT", "5")
    db = project / "data" / "prism.duckdb"
    Store(db).close()
    rt.claim_authority(db, "rt", "test")
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setenv("PRISM_RUNTIME_ID", "rt")
    alerts, calls = [], []
    monkeypatch.setattr(rt, "infra_alert", alerts.append)

    def steps(fail):
        def go(job, name, args):
            calls.append(args)
            return {"step": name, "exit": 1 if fail else 0, "seconds": 0.0}

        return go

    assert (
        rt.run_job("intraday", trigger="test", wait=0, step_runner=steps(False))["status"] == "ok"
    )
    assert calls == [["bars", "update"], ["bars", "shadow", "--record"]]
    rt.run_job("intraday", trigger="test", wait=0, step_runner=steps(True))
    rt.run_job("intraday", trigger="test", wait=0, step_runner=steps(True))
    assert len(alerts) == 1  # only the first failure after a success alerts
    with rt.runtime_lock(db, "prospective", 0), pytest.raises(rt.RuntimeBusy):
        rt.run_job("intraday", trigger="test", wait=0, step_runner=steps(False))


def test_continuity_fingerprint_allows_growth_but_not_loss(store, cfg):
    from market_signal.ops import runtime as rt

    clock = Clock("2026-09-01 12:01")
    feed = FakeFeed(clock, {("BTC", "15m"): synth(M15, T0, 300)})
    before = rt.fingerprint(store)
    upd(store, cfg, feed, clock)
    grown = rt.fingerprint(store)
    assert rt.compare(before, grown) == [] and grown["intraday"]["hyperliquid/15m"]["rows"] == 48
    store.con.execute(
        "DELETE FROM perp_intraday_bars WHERE open_time < ?", [ts("2026-09-01 06:00")]
    )
    assert any("rows decreased" in d for d in rt.compare(grown, rt.fingerprint(store)))


def test_status_shows_intraday_per_timeframe(store, settings, cfg):
    from market_signal.cli.status_cmds import intraday_rows

    clock = Clock("2026-09-01 12:01")
    feeds = {
        (c, tf.value): synth(tf, T0, 60, seed=i)
        for i, c in enumerate(cfg.venues["hyperliquid"].symbols)
        for tf in (M15, H1, H4)
    }
    feed = FakeFeed(clock, feeds)
    upd(store, cfg, feed, clock, coins=tuple(cfg.venues["hyperliquid"].symbols), tfs=(M15, H1, H4))
    rows = {r["component"]: r for r in intraday_rows(store, settings, clock.t)}
    assert set(rows) == {"Intraday 15m", "Intraday 1h", "Intraday 4h"}
    assert all(r["state"] == "OK" for r in rows.values())
    rows = {r["component"]: r for r in intraday_rows(store, settings, ts("2026-09-01 14:01"))}
    assert rows["Intraday 15m"]["state"] == "STALE" and rows["Intraday 4h"]["state"] == "OK"


# --------------------------------------------------------------------------- shadow execution / paper continuity

from market_signal.intraday import shadow  # noqa: E402
from tests.test_lab_forward import SW, day, env  # noqa: E402,F401  (fixture)
from tests.test_paper import _bar, _create, _cycle, _digest, _events, _until, pap  # noqa: E402,F401


def _put_15m(store, coin, open_time, price, observed):
    o = pd.Timestamp(open_time)
    store.con.execute(
        "INSERT INTO perp_intraday_bars VALUES ('hyperliquid', ?, '15m', ?, ?, ?, ?, ?, ?, 1.0, 1, 'native', ?, "
        "'r', true, 0, ?, 'r')",
        [
            coin,
            o,
            o + pd.Timedelta("15min"),
            price,
            price * 1.001,
            price * 0.999,
            price,
            observed,
            observed,
        ],
    )


def test_shadow_records_timing_and_never_touches_the_paper_run(pap):
    run = _create(pap)
    rid = run["run_id"]
    k, sub = _until(pap, rid, "order_submitted")
    order = sub["payload"]["order"]
    coin, side = order["symbol"], order["position_side"]
    intended = pd.Timestamp(day(k))  # the open of the fill day = the signal bar's close
    daily_open = float(_bar(pap, coin, day(k + 1))["open"])
    seen = pd.Timedelta(minutes=15, seconds=40)
    for h in (0, 1, 2, 3, 4):  # 15m bars at the top of each hour after the close
        _put_15m(pap.store, coin, intended + pd.Timedelta(hours=h), daily_open * (1 + 0.001 * h),
                 intended + pd.Timedelta(hours=h) + seen)  # fmt: skip
    _put_15m(
        pap.store,
        coin,
        intended + pd.Timedelta(hours=3, minutes=15),
        daily_open * 1.5,
        intended + pd.Timedelta(hours=3, minutes=15) + seen,
    )  # a later bar must never be picked
    paper_before = _digest(pap, "paper_")
    assert shadow.record(pap.store, now=intended + pd.Timedelta(hours=49)) == []  # prospective only
    new = shadow.record(pap.store, now=intended + pd.Timedelta(hours=4))
    assert len(new) == 1 and shadow.record(pap.store, now=intended + pd.Timedelta(hours=5)) == []
    row = pap.store.query("SELECT * FROM intraday_execution_shadow").iloc[0]
    # decided at 03:00 (cycle time): the first 15m bar opening at/after 03:00 is the reference
    assert pd.Timestamp(row["decision_at"]) == intended + pd.Timedelta(hours=3)
    assert pd.Timestamp(row["ref_open_time"]) == intended + pd.Timedelta(hours=3)
    assert row["ref_price"] == pytest.approx(daily_open * 1.003)
    assert (
        row["latency_seconds"] == (pd.Timedelta(hours=3) + seen).total_seconds()
        and not row["timely"]
    )
    payload = json.loads(row["payload"])
    assert payload["aligned_15m_open_at_intended_entry"] == pytest.approx(daily_open)
    assert payload["rule"]["version"] == shadow.RULE_VERSION == "intraday_exec_timing_v1"
    assert _digest(pap, "paper_") == paper_before  # recording wrote nothing paper-side

    _cycle(pap, rid, k + 1)  # the paper engine fills exactly as before: the stored daily open
    opened = _events(pap, rid, "position_opened")[0]["payload"]
    assert opened["entry_ref"] == pytest.approx(daily_open) != pytest.approx(row["ref_price"])
    rep = shadow.report(pap.store, now=day(k + 1, 4))
    r = rep["recorded"][0]
    assert r["legacy_daily_open"] == pytest.approx(daily_open)
    assert r["adverse_bps_vs_legacy"] == pytest.approx(side * 0.003 * 1e4)
    assert r["legacy_notification_event_at"] == pd.Timestamp(day(k + 1, 3))
    # legacy "opened" event at T+1 03:00 vs reference observable at T 03:15:40
    assert r["notification_lead_h"] == pytest.approx(24 - seen.total_seconds() / 3600, abs=0.01)


def test_shadow_waits_for_the_true_first_bar_rather_than_substituting(pap):
    run = _create(pap)
    k, sub = _until(pap, run["run_id"], "order_submitted")
    coin = sub["payload"]["order"]["symbol"]
    intended = pd.Timestamp(day(k))
    _put_15m(
        pap.store,
        coin,
        intended + pd.Timedelta(hours=3, minutes=15),
        100.0,
        intended + pd.Timedelta(hours=3, minutes=31),
    )  # the 03:00 bar is missing
    assert shadow.record(pap.store, now=intended + pd.Timedelta(hours=4)) == []
    assert shadow.report(pap.store, now=intended + pd.Timedelta(hours=4))["pending"]


def test_paper_and_copilot_never_read_intraday_data():
    for pkg in ("paper", "copilot"):
        for f in (SRC / pkg).rglob("*.py"):
            text = f.read_text()
            assert "intraday" not in text and "perp_intraday" not in text, f
    engine_src = (SRC / "paper" / "engine.py").read_text()
    assert "timeframe='1d'" in engine_src  # fills still read the daily bar


def test_migration_18_is_market_data_only():
    import re

    from market_signal.data.store import MIGRATIONS

    ddl = MIGRATIONS[17]
    assert len(MIGRATIONS) == 24  # Phase 24B appended migration 24 (lab_prospective_*)
    created = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl)
    assert created == ["perp_intraday_bars", "perp_intraday_revisions", "perp_intraday_coverage",
                       "intraday_execution_shadow"]  # fmt: skip
    assert not any(t.startswith(("lab_", "copilot_", "paper_")) for t in created)
    assert "ALTER" not in ddl and "DROP" not in ddl  # existing tables untouched
