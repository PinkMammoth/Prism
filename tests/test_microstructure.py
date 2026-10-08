"""Phase 24A: Hyperliquid microstructure collection (docs/MICROSTRUCTURE_COLLECTION.md).

Synthetic streams have hand-computed expected aggregates. Times are epoch milliseconds.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from pathlib import Path

import pandas as pd
import pytest

from market_signal.data.store import MIGRATIONS, Store
from market_signal.microstructure import definitions as d
from market_signal.microstructure.engine import Engine, LargePrints, day_str, replay
from market_signal.microstructure.ingest import ingest
from market_signal.microstructure.query import cvd, load_microstructure, resample
from market_signal.microstructure.spool import CollectorBusy, Spool
from market_signal.microstructure.ws import parse

SRC = Path(__file__).resolve().parents[1] / "src" / "market_signal"
M = int(pd.Timestamp("2026-10-07 12:00", tz="UTC").value // 1_000_000)  # a minute open
MIN = d.MINUTE_MS


# --------------------------------------------------------------------------- helpers


def mk(coins=("BTC",), lp=None):
    out: list[dict] = []
    return Engine(coins, out.append, run_id="msrun_test", lp=lp), out


def acks(t, coins=("BTC",), feeds=d.FEEDS):
    evs = [("O", t, "conn1")]
    for c in coins:
        evs += [("A", t, c, f) for f in feeds]
    return evs


def stream(t0, t1, coin="BTC", *, fn=None, lat=100, book5=True, book20=True, ctx=True):
    """Healthy book/ctx traffic over [t0, t1): book5 every 500 ms, book20 every 5 s, ctx 1 s."""
    fn = fn or (lambda t: (100.0, 100.1, 1000.0, 1000.0))
    evs = []
    for t in range(t0, t1, 500):
        bid, ask, b5, a5 = fn(t)
        if book5:
            evs.append(("F", t + lat, coin, t, bid, ask, b5, a5))
        if book20 and (t - t0) % 5000 == 0:
            evs.append(("D", t + lat, coin, t, b5 * 5, a5 * 5))
        if ctx and (t - t0) % 1000 == 0:
            evs.append(("C", t + lat, coin, 1000.0, 100.05, 100.0, 1e-5, 100.0, 100.1))
    return evs


def trade(t, px, sz, buy, tid, h=None, coin="BTC", lat=100):
    return ("T", t + lat, coin, t, px, sz, buy, tid, h)


def run(eng, evs, start=None):
    if start is not None:
        eng.start(start)
    for ev in sorted(evs, key=lambda e: e[1]):  # stable: same-receipt order kept
        eng.feed(ev)


def minute(out, m=M, coin="BTC", rev=0):
    rs = [r for r in out if r["kind"] == "minute" and r["minute_open"] == m and r["coin"] == coin
          and r["revision"] == rev]  # fmt: skip
    assert len(rs) == 1, rs
    return rs[0]


def healthy(extra=(), fn=None, end=M + 70_000, coins=("BTC",)):
    eng, out = mk(coins)
    evs = acks(M - 10_000, coins) + list(extra)
    for c in coins:
        evs += stream(M - 10_000, end, c, fn=fn)
    run(eng, evs, start=M - 10_000)
    return eng, out


def derived(rec):
    df = pd.DataFrame([rec])
    vol = rec["buy_vol"] + rec["sell_vol"]
    return {"delta_ntl": rec["buy_ntl"] - rec["sell_ntl"], "buy_frac": rec["buy_vol"] / vol if vol else None,
            "vwap": (rec["buy_ntl"] + rec["sell_ntl"]) / vol if vol else None, "df": df}  # fmt: skip


# --------------------------------------------------------------------------- trade flow


def test_all_buys_hand_checked():
    _, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1), trade(M + 2000, 101.0, 2.0, True, 2),
                      trade(M + 3000, 102.0, 1.0, True, 3)])  # fmt: skip
    r = minute(out)
    assert r["status"] == "COMPLETE" and r["trade_cov"] == 1.0
    assert (r["n_buy"], r["n_sell"], r["buy_vol"], r["sell_vol"]) == (3, 0, 4.0, 0.0)
    assert r["buy_ntl"] == 404.0 and r["sell_ntl"] == 0.0
    assert (r["first_px"], r["last_px"], r["high_px"], r["low_px"]) == (100.0, 102.0, 102.0, 100.0)
    x = derived(r)
    assert x["delta_ntl"] == 404.0 and x["buy_frac"] == 1.0 and x["vwap"] == 101.0
    assert r["n_buy_prints"] == 3 and r["max_print_ntl"] == 202.0 and r["med_print_ntl"] == 102.0


def test_all_sells_and_balanced_flow():
    _, out = healthy([trade(M + 1000, 50.0, 2.0, False, 1), trade(M + 2000, 50.0, 2.0, False, 2)])
    r = minute(out)
    assert (r["n_buy"], r["n_sell"], r["sell_vol"], r["sell_ntl"]) == (0, 2, 4.0, 200.0)
    assert derived(r)["delta_ntl"] == -200.0 and derived(r)["buy_frac"] == 0.0
    _, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1), trade(M + 2000, 100.0, 1.0, False, 2)])
    r = minute(out)
    assert derived(r)["delta_ntl"] == 0.0 and derived(r)["buy_frac"] == 0.5


def test_aggressor_side_is_the_venue_taker_side_never_inferred():
    # a buy-side taker fill printed at the bid is still a BUY: side comes from the feed
    evs = parse({"channel": "trades", "data": [
        {"coin": "BTC", "side": "B", "px": "99.9", "sz": "1", "time": M + 5, "tid": 7,
         "hash": "0xab" + "1" * 62, "users": ["0xbuyer", "0xseller"]},
        {"coin": "BTC", "side": "A", "px": "100.2", "sz": "2", "time": M + 6, "tid": 8,
         "hash": d.ZERO_HASH, "users": ["0xb", "0xs"]}]}, M + 100)  # fmt: skip
    assert evs[0][:9] == (
        "T",
        M + 100,
        "BTC",
        M + 5,
        99.9,
        1.0,
        True,
        7,
        "0xab1111111111111111"[:18],
    )
    assert evs[1][6] is False and evs[1][8] is None  # zero hash: the fill is its own print
    assert not any("0xbuyer" in str(e) for e in evs)  # addresses are never kept
    assert (
        parse(
            {
                "channel": "trades",
                "data": [
                    {
                        "coin": "BTC",
                        "side": "X",
                        "px": "1",
                        "sz": "1",
                        "time": 1,
                        "tid": 1,
                        "hash": "",
                    }
                ],
            },
            5,
        )[0][0]
        == "?"
    )


def test_fills_of_one_taker_transaction_form_one_print():
    h = "0x" + "c" * 16
    _, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1, h), trade(M + 1000, 100.5, 1.0, True, 2, h),
                      trade(M + 1000, 101.0, 1.0, True, 3, h), trade(M + 2000, 100.0, 0.5, False, 4)])  # fmt: skip
    r = minute(out)
    assert r["n_buy"] == 3 and r["n_buy_prints"] == 1 and r["n_sell_prints"] == 1
    assert r["max_print_ntl"] == 301.5
    # half-decade buckets (0: <$10, 1: [10, 31.6), 2: [31.6, 100), 3: [100, 316), ...)
    assert r["size_hist"][2] == 1 and r["size_hist"][3] == 1 and sum(r["size_hist"]) == 2


def test_duplicate_trade_and_reconnect_replay_never_double_count():
    t1, t2 = trade(M + 1000, 100.0, 1.0, True, 1), trade(M + 2000, 100.0, 1.0, False, 2)
    dup = ("T", M + 1500, "BTC", M + 1000, 100.0, 1.0, True, 1, None)
    # reconnect at +20 s: the venue re-sends recent trades (both already seen)
    replay_evs = [("X", M + 20_000, "test drop"), ("O", M + 20_500, "conn2"),
                  *[("A", M + 20_500, "BTC", f) for f in d.FEEDS],
                  ("T", M + 20_600, "BTC", M + 1000, 100.0, 1.0, True, 1, None),
                  ("T", M + 20_600, "BTC", M + 2000, 100.0, 1.0, False, 2, None)]  # fmt: skip
    eng, out = healthy([t1, t2, dup, *replay_evs])
    r = minute(out)
    assert (r["n_buy"], r["n_sell"], r["n_dup"]) == (1, 1, 3)
    assert eng.counters["dup_trades"] == 3
    assert r["status"] == "PARTIAL" and "disconnect" in r["flags"] and "connect" in r["flags"]
    assert 0.98 < r["trade_cov"] < 1.0  # ~0.5 s without a live trades subscription


def test_duplicate_book_update_is_ignored():
    evs = acks(M - 10_000) + stream(M - 10_000, M + 70_000)
    eng, out = mk()
    evs += [e for e in stream(M - 10_000, M + 70_000) if e[0] == "F"][:50]  # exact re-sends
    run(eng, evs, start=M - 10_000)
    assert eng.counters["dup_book5"] == 50
    assert minute(out)["book_updates"] == 120  # 0.5 s cadence: 120 distinct snapshots


def test_minute_boundary_is_half_open():
    _, out = healthy([trade(M + MIN - 1, 100.0, 1.0, True, 1), trade(M + MIN, 100.0, 2.0, True, 2)],
                     end=M + 2 * MIN + 10_000)  # fmt: skip
    assert minute(out)["buy_vol"] == 1.0
    assert minute(out, M + MIN)["buy_vol"] == 2.0


def test_grace_period_admits_delayed_trades():
    # exchange time 59.9 s, received 4.9 s after close: inside GRACE (5 s), counted normally
    late_ok = ("T", M + MIN + 4_900, "BTC", M + 59_900, 100.0, 3.0, True, 9, None)
    _, out = healthy([late_ok])
    r = minute(out)
    assert r["buy_vol"] == 3.0 and r["n_late"] == 0


def test_late_trade_produces_one_bounded_revision_then_rejection():
    late1 = ("T", M + MIN + 8_000, "BTC", M + 30_000, 100.0, 2.0, True, 21, None)
    late2 = ("T", M + MIN + 20_000, "BTC", M + 31_000, 100.0, 1.0, False, 22, None)
    too_late = ("T", M + MIN + 70_000, "BTC", M + 32_000, 100.0, 5.0, True, 23, None)
    _eng, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1), late1, late2, too_late],
                       end=M + 2 * MIN + 20_000)  # fmt: skip
    r0, r1 = minute(out), minute(out, rev=1)
    assert r0["buy_vol"] == 1.0 and r0["n_late"] == 0  # the finalized row is never mutated
    assert r1["buy_vol"] == 3.0 and r1["sell_vol"] == 1.0 and r1["n_late"] == 2
    assert r1["finalized_ms"] >= M + MIN + d.GRACE_MS + d.REVISION_WINDOW_MS
    assert "late_revision" in r1["flags"] and r1["content_sha"] != r0["content_sha"]
    late = [r for r in out if r["kind"] == "late"]
    assert [(r["tid"], r["disposition"]) for r in late] == [(21, "revised"), (22, "revised"),
                                                           (23, "rejected")]  # fmt: skip
    assert not [r for r in out if r["kind"] == "minute" and r["revision"] > 1]


def test_late_book_update_counted_not_applied():
    late_book = ("F", M + MIN + 9_000, "BTC", M + 30_000, 90.0, 110.0, 1.0, 1.0)
    eng, out = healthy([late_book])
    assert eng.counters["late_book5"] == 1
    assert minute(out)["spread_bps_max"] < 11


# --------------------------------------------------------------------------- book


def test_spread_widening_time_weighted_hand_checked():
    _, out = healthy(fn=lambda t: (100.0, 100.1 if t < M + 30_000 else 100.3, 1000.0, 1000.0))
    r = minute(out)
    assert r["book_samples"] == 60
    assert r["spread_mean"] == pytest.approx(0.2)
    lo, hi = 0.1 / 100.05 * 1e4, 0.3 / 100.15 * 1e4
    assert r["spread_bps_mean"] == pytest.approx((lo + hi) / 2)
    assert r["spread_bps_min"] == pytest.approx(lo) and r["spread_bps_max"] == pytest.approx(hi)
    assert r["ask_end"] == 100.3 and r["ask_changes"] == 1 and r["bid_changes"] == 0
    assert r["mid_changes"] == 1


def test_message_bursts_do_not_dominate_book_averages():
    # 200 snapshots in one second with a wide spread vs one per 0.5 s otherwise
    burst = [("F", M + 10_000 + i * 5 + 100, "BTC", M + 10_000 + i * 5, 100.0, 105.0, 1.0, 1.0)
             for i in range(1, 200)]  # fmt: skip
    _, out = healthy(burst)
    r = minute(out)
    # only grid instants inside the burst see it: at most 1 of 60 samples
    assert r["spread_mean"] < 0.1 + 5.0 * 2 / 60


def test_bid_and_ask_heavy_depth_imbalance():
    _, out = healthy(fn=lambda t: (100.0, 100.1, 3000.0, 1000.0))
    r = minute(out)
    assert (r["bid5_mean"], r["ask5_mean"], r["bid20_mean"], r["ask20_mean"]) == (
        3000.0,
        1000.0,
        15000.0,
        5000.0,
    )
    df = load_like(r)
    assert df["imb5"].iloc[0] == pytest.approx(0.5) and df["imb20_end"].iloc[0] == pytest.approx(
        0.5
    )
    _, out = healthy(fn=lambda t: (100.0, 100.1, 1000.0, 3000.0))
    assert load_like(minute(out))["imb5"].iloc[0] == pytest.approx(-0.5)


def test_replenishment_proxy_counts_depth_added_at_an_unchanged_best():
    # bid5 grows 1000 -> 1500 at +20 s (best bid unchanged), then drops back at +40 s
    fn = lambda t: (100.0, 100.1, 1500.0 if M + 20_000 <= t < M + 40_000 else 1000.0, 1000.0)  # noqa: E731
    _, out = healthy(fn=fn)
    r = minute(out)
    assert r["bid_replenish"] == 500.0 and r["ask_replenish"] == 0.0


def test_end_of_minute_context_from_activeassetctx():
    r = minute(healthy()[1])
    assert (r["oi_end"], r["mark_end"], r["oracle_end"], r["impact_bid_end"]) == (
        1000.0,
        100.05,
        100.0,
        100.0,
    )


def load_like(rec):
    from market_signal.microstructure.query import derive

    df = pd.DataFrame(
        [{**rec, "minute_open": pd.Timestamp(rec["minute_open"], unit="ms", tz="UTC")}]
    )
    df["minute_close"] = df["minute_open"] + pd.Timedelta(minutes=1)
    return derive(df)


# --------------------------------------------------------------------------- completeness


def test_trade_only_book_only_partial_and_gap_minutes():
    # trade-only: trades subscription live, book feed silent (ctx keeps the socket alive)
    eng, out = mk()
    run(eng, acks(M - 10_000) + stream(M - 10_000, M + 70_000, book5=False, book20=False)
        + [trade(M + 1000, 100.0, 1.0, True, 1)], start=M - 10_000)  # fmt: skip
    r = minute(out)
    assert r["status"] == "TRADE_ONLY" and r["buy_vol"] == 1.0 and r["spread_mean"] is None
    # book-only: the trades subscription was never acknowledged
    eng, out = mk()
    run(eng, acks(M - 10_000, feeds=("book5", "book20", "ctx")) + stream(M - 10_000, M + 70_000),
        start=M - 10_000)  # fmt: skip
    r = minute(out)
    assert r["status"] == "BOOK_ONLY" and r["n_buy"] is None and r["book_samples"] == 60
    # stale book mid-minute (connection alive): PARTIAL, never COMPLETE
    eng, out = mk()
    evs = acks(M - 10_000) + [e for e in stream(M - 10_000, M + 70_000)
                              if not (e[0] in "FD" and M + 20_000 <= e[3] < M + 40_000)]  # fmt: skip
    run(eng, evs, start=M - 10_000)
    r = minute(out)
    assert r["status"] == "PARTIAL" and r["trade_cov"] == 1.0 and r["book_samples"] < 57


def test_quiet_minute_is_complete_with_zero_trades_but_offline_minute_is_gap():
    eng, out = healthy()
    r = minute(out)
    assert r["status"] == "COMPLETE" and r["n_buy"] == 0 and r["buy_vol"] == 0.0  # no trading
    eng, out = mk()  # connection drops before the minute and never comes back
    run(eng, acks(M - 30_000) + stream(M - 30_000, M - 5_000) + [("X", M - 4_000, "drop")],
        start=M - 30_000)  # fmt: skip
    eng.tick(M + 2 * MIN)
    r = minute(out)
    assert r["status"] == "GAP" and r["n_buy"] is None and r["buy_vol"] is None  # never zero


def test_restart_writes_explicit_gap_rows_then_partial_first_minute():
    eng, out = mk()
    last = M - 20 * MIN
    eng.start(M + 30_000, {"BTC": last})
    gaps = [r for r in out if r["kind"] == "minute"]
    assert len(gaps) == 19 and all(
        r["status"] == "GAP" and r["flags"] == "collector_down" for r in gaps
    )
    assert gaps[0]["minute_open"] == last + MIN and gaps[-1]["minute_open"] == M - MIN
    run(eng, acks(M + 31_000) + stream(M + 31_000, M + 2 * MIN + 10_000))
    r = minute(out)
    assert r["status"] == "PARTIAL" and r["flags"].startswith("collector_start")
    assert minute(out, M + MIN)["status"] == "COMPLETE"


def test_repeated_flush_emits_each_minute_once():
    eng, out = healthy()
    n = len(out)
    for _ in range(3):
        eng.tick(M + 70_000)
    assert len(out) == n


def test_journal_replay_is_deterministic():
    eng, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1),
                        ("T", M + MIN + 8_000, "BTC", M + 3_000, 100.0, 1.0, False, 2, None)],
                       end=M + 3 * MIN)  # fmt: skip
    again = replay(eng.journal, ("BTC",), "msrun_test")
    strip = [{k: v for k, v in r.items() if k != "computed_ms"} for r in out]
    assert strip == [{k: v for k, v in r.items() if k != "computed_ms"} for r in again]


# --------------------------------------------------------------------------- large prints


def _lp_history(days=3, n=2000, big_every=100):
    lp = LargePrints()
    for i in range(1, days + 1):
        day = day_str(M - i * 86_400_000)
        bins = {}
        for j in range(n):
            ntl = 10_000.0 if j % big_every == 0 else 100.0
            k = str(d.fine_bin(ntl))
            bins[k] = bins.get(k, 0) + 1
        lp.days[day] = {"BTC": {"bins": bins, "minutes": 1440}}
    return lp


def test_large_print_threshold_is_causal_and_needs_warmup():
    assert LargePrints().threshold("BTC", day_str(M))["status"] == "warmup"
    two = _lp_history(days=2)
    assert two.threshold("BTC", day_str(M))["threshold"] is None  # only 2 prior days
    lp = _lp_history(days=3)
    th = lp.threshold("BTC", day_str(M))
    # 1% of prints are $10k: the 99th percentile falls in the $100 bin -> its upper edge
    assert th["status"] == "ok" and th["days_used"] == 3 and th["n_prints"] == 6000
    assert th["threshold"] == pytest.approx(d.fine_upper_edge(d.fine_bin(100.0)))
    # today's data never moves today's threshold
    lp.add("BTC", day_str(M), {str(d.fine_bin(1e9)): 10**6}, True)
    assert lp.threshold("BTC", day_str(M)) == th


def test_one_huge_print_is_a_large_print_after_warmup():
    eng, out = mk(lp=_lp_history(days=3))
    evs = acks(M - 10_000) + stream(M - 10_000, M + 70_000)
    evs += [trade(M + 1000, 100.0, 0.5, True, 1), trade(M + 2000, 100.0, 50.0, False, 2)]
    run(eng, evs, start=M - 10_000)
    r = minute(out)
    assert r["lp_threshold"] is not None and r["lp_n"] == 1 and r["lp_buy_n"] == 0
    assert r["lp_ntl"] == 5000.0 and r["lp_buy_ntl"] == 0.0
    th = [x for x in out if x["kind"] == "lp_threshold"]
    assert th and th[0]["status"] == "ok"
    # without history the same minute reports "unavailable", not zero
    _, out = healthy([trade(M + 2000, 100.0, 50.0, False, 2)])
    assert minute(out)["lp_n"] is None and minute(out)["lp_threshold"] is None


# --------------------------------------------------------------------------- parse


def test_parse_book_depth_classification_and_acks():
    lv = lambda n, base, step: [{"px": str(base + i * step), "sz": "1", "n": 1} for i in range(n)]  # noqa: E731
    fast = parse(
        {
            "channel": "l2Book",
            "data": {"coin": "ETH", "time": 5, "levels": [lv(5, 100, -1), lv(5, 101, 1)]},
        },
        9,
    )
    assert [e[0] for e in fast] == ["F"] and fast[0][4:6] == (100.0, 101.0)
    assert fast[0][6] == sum(100 - i for i in range(5))
    deep = parse(
        {
            "channel": "l2Book",
            "data": {"coin": "ETH", "time": 5, "levels": [lv(20, 100, -1), lv(20, 101, 1)]},
        },
        9,
    )
    assert [e[0] for e in deep] == ["D"] and deep[0][4] == sum(100 - i for i in range(20))
    thin = parse(
        {
            "channel": "l2Book",
            "data": {"coin": "ETH", "time": 5, "levels": [lv(2, 100, -1), lv(3, 101, 1)]},
        },
        9,
    )
    assert [e[0] for e in thin] == ["F", "D"]
    ack = parse({"channel": "subscriptionResponse", "data": {"method": "subscribe", "subscription":
                {"type": "l2Book", "coin": "ETH", "fast": True}}}, 9)  # fmt: skip
    assert ack == [("A", 9, "ETH", "book5")]
    ctx = parse({"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": {
        "openInterest": "10", "markPx": "2", "oraclePx": "3", "funding": "0.0001",
        "impactPxs": ["1.9", "2.1"]}}}, 9)  # fmt: skip
    assert ctx == [("C", 9, "ETH", 10.0, 2.0, 3.0, 0.0001, 1.9, 2.1)]
    assert parse({"channel": "pong"}, 1) == []


# --------------------------------------------------------------------------- spool


def test_spool_crash_tail_is_repaired_and_raw_tolerates_truncation(tmp_path):
    sp = Spool(tmp_path)
    sp.append({"kind": "x", "n": 1}, M)
    f = sp.files()[0]
    with f.open("ab") as fh:
        fh.write(b'{"kind": "minute", "trunc')  # killed mid-write
    recs, off, bad = Spool.read_lines(f)
    assert len(recs) == 1 and bad == 0 and off < f.stat().st_size
    assert sp.repair() > 0 and f.read_bytes().endswith(b"\n")
    sp.archive([("K", 1), ("K", 2)], M)
    raw = next((tmp_path / "raw").rglob("*.gz"))
    first = raw.stat().st_size
    sp.archive([("K", 3)] * 50, M)
    raw.write_bytes(raw.read_bytes()[: first + 12])  # killed while appending the next member
    assert Spool.read_raw(raw) == [("K", 1), ("K", 2)]


def test_collector_lock_allows_one_collector(tmp_path):
    a, b = Spool(tmp_path), Spool(tmp_path)
    a.lock()
    with pytest.raises(CollectorBusy):
        b.lock()
    a.unlock()
    b.lock()
    b.unlock()


def test_prune_never_deletes_unread_spool(tmp_path):
    sp = Spool(tmp_path)
    old = M - 20 * 86_400_000
    sp.append({"kind": "x"}, old)
    sp.archive([("K", 1)], old)
    removed = sp.prune(M, spool_days=10, raw_days=3)
    assert removed == [f"raw/{day_str(old).replace('-', '')}"]  # spool kept: never ingested
    sp.set_ingested_watermark(f"{day_str(M).replace('-', '')}/00.jsonl")
    assert sp.prune(M, spool_days=10, raw_days=3) == [f"spool/{day_str(old).replace('-', '')}"]


def test_large_print_history_rebuilds_from_spool(tmp_path):
    sp = Spool(tmp_path)
    _eng, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1)])
    for r in out:
        sp.append(r, r.get("finalized_ms") or r.get("computed_ms") or M)
    lp = sp.load_large_prints(M + 86_400_000)
    assert lp.days[day_str(M)]["BTC"]["bins"] == {str(d.fine_bin(100.0)): 1}
    assert (tmp_path / "lp" / f"{day_str(M)}.json").is_file()  # completed day cached


# --------------------------------------------------------------------------- ingest / DB


def _spool_from(out, root):
    sp = Spool(root)
    sp.append({"kind": "run_start", "run_id": "msrun_test", "at": M - 10_000, "role": "scratch",
               "runtime_id": "", "git_commit": None}, M)  # fmt: skip
    for r in out:
        sp.append(r, r.get("finalized_ms") or r.get("computed_ms") or M + MIN)
    return sp


def test_migration_columns_match_the_frozen_definition(store):
    cols = [r[0] for r in store.con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name="
        "'microstructure_minutes' ORDER BY ordinal_position").fetchall()]  # fmt: skip
    assert tuple(cols) == d.MINUTE_COLUMNS
    assert "microstructure_minutes" in MIGRATIONS[22]
    assert not any(
        x in MIGRATIONS[22] for x in ("perp_intraday_bars", "paper_", "copilot_", "lab_")
    )


def test_feature_version_is_frozen():
    # Changing any definition constant changes this digest: that requires a NEW feature version
    # (and leaves every stored microstructure_1m_v1 row meaning what it meant).
    assert d.FEATURE_VERSION == "microstructure_1m_v1" and d.LP_VERSION == "lp_p99_prior7d_v1"
    assert d.definition_digest() == FROZEN_DIGEST


FROZEN_DIGEST = "7c299a853f4ba141"


def test_ingest_is_idempotent_and_never_overwrites(store, tmp_path):
    _, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1)], end=M + 3 * MIN)
    sp = _spool_from(out, tmp_path)
    a = ingest(store, sp)
    assert a["status"] == "ok" and a["minutes"] == len([r for r in out if r["kind"] == "minute"])
    before = store.con.execute(
        "SELECT * FROM microstructure_minutes ORDER BY coin, minute_open"
    ).df()
    store.con.execute("DELETE FROM microstructure_ingest_offsets")  # force a full re-read
    b = ingest(store, sp)
    assert b.get("minutes", 0) == 0 and b["duplicates"] == a["minutes"]
    after = store.con.execute(
        "SELECT * FROM microstructure_minutes ORDER BY coin, minute_open"
    ).df()
    pd.testing.assert_frame_equal(before, after)
    assert not before["status"].isna().any()
    # a conflicting record (same key + revision, different content) is refused
    bad = dict(minute(out), buy_vol=999.0)
    bad["content_sha"] = d.content_sha(bad)
    sp.append(bad, M + 5 * MIN)
    c = ingest(store, sp)
    assert c["status"] == "failed" and c["conflicts"] == 1
    assert (
        store.con.execute(
            "SELECT buy_vol FROM microstructure_minutes WHERE minute_open=?",
            [pd.Timestamp(M, unit="ms", tz="UTC").to_pydatetime()],
        ).fetchone()[0]
        == 1.0
    )


def test_revision_is_kept_and_known_at_reconstructs_what_prism_knew(store, tmp_path):
    late = ("T", M + MIN + 8_000, "BTC", M + 30_000, 100.0, 2.0, True, 21, None)
    _, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1), late], end=M + 3 * MIN)
    sp = _spool_from(out, tmp_path)
    res = ingest(store, sp)
    assert res["revisions"] == 1 and res["late_events"] == 1
    r0, r1 = minute(out), minute(out, rev=1)
    t = lambda ms: pd.Timestamp(ms, unit="ms", tz="UTC")  # noqa: E731
    kw = {"production_only": False}
    now = load_microstructure(store, "BTC", t(M), t(M + MIN), **kw)
    assert now["buy_vol"].iloc[0] == 3.0 and now["revision"].iloc[0] == 1
    before_rev = load_microstructure(
        store, "BTC", t(M), t(M + MIN), known_at=t(r1["finalized_ms"] - 1), **kw
    )
    assert before_rev["buy_vol"].iloc[0] == 1.0 and before_rev["revision"].iloc[0] == 0
    before_final = load_microstructure(store, "BTC", t(M), t(M + MIN), known_at=t(r0["finalized_ms"] - 1),
                                       fill_missing=False, **kw)  # fmt: skip
    assert before_final.empty  # not yet finalized: unknown (exchange time grants nothing)
    assert store.con.execute("SELECT count(*) FROM microstructure_revisions").fetchone()[0] == 1


def test_loader_marks_missing_minutes_and_production_only(store, tmp_path):
    _, out = healthy(end=M + 3 * MIN)
    ingest(store, _spool_from(out, tmp_path))
    t = lambda ms: pd.Timestamp(ms, unit="ms", tz="UTC")  # noqa: E731
    assert load_microstructure(store, "BTC", t(M), t(M + 5 * MIN)).empty  # no production cutover
    df = load_microstructure(store, "BTC", t(M - MIN), t(M + 5 * MIN), production_only=False)
    assert list(df["status"]) == [
        "PARTIAL",
        "COMPLETE",
        "COMPLETE",
        "MISSING",
        "MISSING",
        "MISSING",
    ]
    assert df.loc[df["status"] == "MISSING", "n_trades"].isna().all()  # never zero-filled
    cc = load_microstructure(
        store, "BTC", t(M - MIN), t(M + 5 * MIN), production_only=False, complete_only=True
    )
    assert list(cc["status"]) == ["COMPLETE", "COMPLETE"]


def test_resampling_recomputes_ratios_and_propagates_quality():
    t = pd.date_range("2026-10-07 12:00", periods=5, freq="1min", tz="UTC")
    base = {c: 0.0 for c in ("n_buy", "n_sell", "n_buy_prints", "n_sell_prints", "book_updates",
                             "bid_changes", "ask_changes", "mid_changes", "bid_replenish",
                             "ask_replenish", "n_dup", "n_late")}  # fmt: skip
    rows = []
    for i, ts in enumerate(t):
        rows.append({**base, "minute_open": ts, "coin": "BTC", "feature_version": d.FEATURE_VERSION,
                     "status": "COMPLETE", "trade_cov": 1.0, "revision": 0,
                     "buy_vol": [9.0, 1.0, 0, 0, 0][i], "sell_vol": [1.0, 9.0, 0, 0, 0][i],
                     "buy_ntl": [900.0, 100.0, 0, 0, 0][i], "sell_ntl": [100.0, 900.0, 0, 0, 0][i],
                     "first_px": [100.0, 101.0, None, None, None][i],
                     "last_px": [101.0, 99.0, None, None, None][i],
                     "high_px": [102.0, 103.0, None, None, None][i], "low_px": [99.5, 98.0, None, None, None][i],
                     "max_print_ntl": 1.0, "med_print_ntl": 1.0, "size_hist": [1] * 14,
                     "lp_threshold": None, "lp_n": None, "lp_buy_n": None, "lp_ntl": None, "lp_buy_ntl": None,
                     "book_samples": [60, 30, 60, 60, 60][i], "depth20_samples": 60,
                     "spread_mean": [1.0, 4.0, 1.0, 1.0, 1.0][i], "spread_bps_mean": 1.0,
                     "spread_bps_med": 1.0, "spread_bps_min": 0.5, "spread_bps_max": [2.0, 9.0, 2, 2, 2][i],
                     "bid5_mean": 10.0, "ask5_mean": 10.0, "bid20_mean": 10.0, "ask20_mean": 10.0,
                     "bid_end": 100.0, "ask_end": 100.2 + i, "bid5_end": 1.0, "ask5_end": 1.0,
                     "bid20_end": 1.0, "ask20_end": 1.0, "oi_end": 10.0 + i, "mark_end": 1.0,
                     "oracle_end": 1.0, "funding_end": 0.0, "impact_bid_end": 1.0, "impact_ask_end": 1.0,
                     "lat_p50_ms": 1, "lat_max_ms": [5, 50, 5, 5, 5][i],
                     "finalized_at": ts + pd.Timedelta(seconds=65), "ingested_at": ts + pd.Timedelta(minutes=10),
                     "available_at": ts + pd.Timedelta(seconds=65), "availability": "finalized"})  # fmt: skip
    df = pd.DataFrame(rows)
    df["minute_close"] = df["minute_open"] + pd.Timedelta(minutes=1)
    from market_signal.microstructure.query import derive

    b = derive(resample(df, "5min")).iloc[0]
    assert b["status"] == "COMPLETE" and b["n_complete"] == 5
    assert b["buy_frac"] == 0.5  # recomputed from summed volumes (10 / 20), not a mean of ratios
    assert b["delta_ntl"] == 0.0 and b["vwap"] == 100.0
    assert (b["first_px"], b["last_px"], b["high_px"], b["low_px"]) == (100.0, 99.0, 103.0, 98.0)
    # spread time-weighted by valid samples: (1*60 + 4*30 + 1*180) / 270
    assert b["spread_mean"] == pytest.approx(360 / 270)
    assert b["spread_bps_max"] == 9.0 and b["lat_max_ms"] == 50 and math.isnan(b["spread_bps_med"])
    assert b["oi_end"] == 14.0 and b["available_at"] == t[-1] + pd.Timedelta(seconds=65)
    assert b["size_hist"] == [5] * 14
    df.loc[2, "status"] = "PARTIAL"
    assert resample(df, "5min").iloc[0]["status"] == "PARTIAL"
    df["status"] = "GAP"
    assert resample(df, "5min").iloc[0]["status"] == "GAP"


def test_cvd_is_derived_with_validity():
    t = pd.date_range("2026-10-07 23:58", periods=4, freq="1min", tz="UTC")
    df = pd.DataFrame({"minute_open": t, "status": ["COMPLETE", "PARTIAL", "COMPLETE", "COMPLETE"],
                       "delta_ntl": [10.0, 5.0, -3.0, 1.0]})  # fmt: skip
    c = cvd(df, reset="daily")
    assert list(c["cvd"]) == [10.0, 15.0, -3.0, -2.0]  # resets at 00:00 UTC
    assert list(c["cvd_valid"]) == [True, False, True, True]
    r = cvd(df, window="2min")
    assert list(r["cvd"])[1:] == [15.0, 2.0, -2.0] and list(r["cvd_valid"]) == [
        False,
        False,
        False,
        True,
    ]


def test_align_asof_never_looks_ahead():
    from market_signal.microstructure.query import align_asof

    left = pd.DataFrame({"minute_open": pd.to_datetime(["2026-10-07 12:00", "2026-10-07 12:01"], utc=True),
                         "available_at": pd.to_datetime(["2026-10-07 12:01:05", "2026-10-07 12:02:05"], utc=True)})  # fmt: skip
    right = pd.DataFrame({"recorded_at": pd.to_datetime(["2026-10-07 12:01:05", "2026-10-07 12:01:30"], utc=True),
                          "regime": ["a", "b"]})  # fmt: skip
    out = align_asof(left, right, right_time="recorded_at")
    assert list(out["regime"]) == ["a", "b"]


# --------------------------------------------------------------------------- cutover / authority


def test_production_cutover_is_recorded_once_from_the_authoritative_collector(
    project, monkeypatch, tmp_path
):
    from market_signal.ops.runtime import claim_authority

    db = project / "data" / "live.duckdb"
    Store(db).close()
    claim_authority(db, "railway-test", "test")
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
    monkeypatch.setenv("PRISM_RUNTIME_ID", "railway-test")
    _, out = healthy(end=M + 3 * MIN)
    sp = Spool(tmp_path / "prod")
    sp.append({"kind": "run_start", "run_id": "msrun_test", "at": M - 10_000, "role": "authoritative",
               "runtime_id": "railway-test"}, M)  # fmt: skip
    for r in out:
        sp.append(r, r.get("finalized_ms") or M)
    scratch = Spool(tmp_path / "scratch")  # smoke data from a scratch collector
    scratch.append({"kind": "run_start", "run_id": "msrun_smoke", "at": M, "role": "scratch",
                    "runtime_id": ""}, M)  # fmt: skip
    scratch.append(dict(minute(out), session_id="msrun_smoke"), M + MIN)
    scratch.append({"kind": "late", "run_id": "msrun_smoke", "feature_version": d.FEATURE_VERSION,
                    "coin": "BTC", "tid": 99, "t": M, "recv": M + MIN + 9000, "minute_open": M,
                    "px": 1.0, "sz": 1.0, "is_buy": True, "disposition": "rejected"}, M + MIN)  # fmt: skip
    st = Store(db)
    try:
        s = ingest(st, scratch)
        assert (
            s["rejected_not_authoritative"] == 3 and s.get("minutes", 0) == 0
        )  # run, minute, late
        assert st.con.execute("SELECT count(*) FROM microstructure_cutover").fetchone()[0] == 0
        for t in (
            "microstructure_late_events",
            "microstructure_provider_runs",
            "microstructure_minutes",
        ):
            assert st.con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0, t
        res = ingest(st, sp)
        assert res.get("cutover_recorded") == 1, res
        co = st.con.execute(
            "SELECT first_minute, runtime_id FROM microstructure_cutover"
        ).fetchone()
        # the collector_start minute is PARTIAL: the dataset starts at the first COMPLETE minute
        assert (
            pd.Timestamp(co[0]) == pd.Timestamp(M, unit="ms", tz="UTC") and co[1] == "railway-test"
        )
        ingest(st, sp)
        assert st.con.execute("SELECT count(*) FROM microstructure_cutover").fetchone()[0] == 1
        t = pd.Timestamp(M - MIN, unit="ms", tz="UTC")
        df = load_microstructure(st, "BTC", t, t + pd.Timedelta(minutes=3))
        assert df["minute_open"].iloc[0] == pd.Timestamp(
            M, unit="ms", tz="UTC"
        )  # starts at cutover
    finally:
        st.close()


def test_collector_refuses_without_runtime_role(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    monkeypatch.delenv("PRISM_RUNTIME_ROLE", raising=False)
    res = CliRunner().invoke(
        app, ["microstructure", "collect", "--dir", str(tmp_path), "--duration", "1"]
    )
    assert res.exit_code == 2 and not (tmp_path / "spool").exists()


# --------------------------------------------------------------------------- websocket runner


class FakeHL:
    """A local stand-in for the Hyperliquid WS: acks subscriptions, streams books, and can drop
    the connection or go silent."""

    def __init__(self, drop_after=None, silent_after=None):
        self.drop_after, self.silent_after = drop_after, silent_after
        self.subs: list[list[dict]] = []

    async def handler(self, ws):
        subs = []
        self.subs.append(subs)
        n = len(self.subs)

        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                if m.get("method") == "subscribe":
                    subs.append(m["subscription"])
                    await ws.send(json.dumps({"channel": "subscriptionResponse", "data": m}))

        task = asyncio.create_task(reader())
        try:
            i = 0
            while ws.close_code is None:  # until the collector goes away
                await asyncio.sleep(0.05)
                i += 1
                if n == 1 and self.drop_after and i * 0.05 >= self.drop_after:
                    await ws.close()
                    return
                if n == 1 and self.silent_after and i * 0.05 >= self.silent_after:
                    continue
                t = int(pd.Timestamp.now(tz="UTC").value // 1_000_000)
                for s in subs:
                    if s["type"] == "l2Book" and s.get("fast"):
                        await ws.send(json.dumps({"channel": "l2Book", "data": {
                            "coin": s["coin"], "time": t, "levels": [[{"px": "100", "sz": "1", "n": 1}] * 5,
                                                                    [{"px": "101", "sz": "1", "n": 1}] * 5]}}))  # fmt: skip
        except Exception:
            return
        finally:
            task.cancel()


@pytest.mark.parametrize("mode", ["drop", "silent"])
def test_runner_reconnects_and_resubscribes(tmp_path, monkeypatch, mode):
    from websockets.asyncio.server import serve

    from market_signal.microstructure import ws as wsmod

    monkeypatch.setattr(wsmod, "STALE_SOCKET_S", 0.5)
    monkeypatch.setattr(wsmod, "ACK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(wsmod, "STATUS_EVERY_S", 0.2)
    fake = FakeHL(drop_after=0.6) if mode == "drop" else FakeHL(silent_after=0.6)

    async def main():
        async with serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            c = wsmod.Collector(Spool(tmp_path), ("BTC", "ETH"), url=f"ws://127.0.0.1:{port}",
                                role="scratch", duration_s=6.0)  # fmt: skip
            await c.run()
            return c

    c = asyncio.run(main())
    assert c.reconnects >= 1 and len(fake.subs) >= 2
    assert all(len(s) == 8 for s in fake.subs[:2])  # 4 feeds x 2 coins, every connection
    recs = [r for f in Spool(tmp_path).files() for r in Spool.read_lines(f)[0]]
    kinds = [r["kind"] for r in recs]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and kinds.count("conn_open") >= 2
    closes = [r["reason"] for r in recs if r["kind"] == "conn_close"]
    if mode == "silent":
        assert any(r.startswith("stale") for r in closes)
    st = json.loads((tmp_path / "status.json").read_text())
    assert st["reconnects"] >= 1 and st["run_id"] == c.run_id


# --------------------------------------------------------------------------- CLI / status / runtime


def test_cli_json_commands(project, monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    monkeypatch.setenv("PRISM_MICRO_DIR", str(tmp_path / "ms"))
    st = Store(project / "data" / "prism.duckdb")
    _, out = healthy([trade(M + 1000, 100.0, 1.0, True, 1)], end=M + 3 * MIN)
    sp = _spool_from(out, tmp_path / "ms")
    ingest(st, sp)
    st.close()
    r = CliRunner()
    for args in (["status"], ["health"], ["coverage"], ["latest", "BTC"], ["inspect", "BTC"]):
        res = r.invoke(app, ["microstructure", *args, "--json"])
        assert res.exit_code == 0, (args, res.output)
        json.loads(res.output)
    s = json.loads(r.invoke(app, ["microstructure", "status", "--json"]).output)
    assert (
        s["stored"]["rows"] >= 3
        and s["cutover"] is None
        and s["collector"]["state"] == "NOT RUNNING"
    )
    latest = json.loads(r.invoke(app, ["microstructure", "latest", "BTC", "--json"]).output)
    assert latest["stored"]["status"] == "COMPLETE"
    h = r.invoke(app, ["microstructure", "health", "--check"])
    assert h.exit_code == 1  # no collector status file: a dead collector never looks healthy


def test_health_judges_status_file_freshness(tmp_path):
    from market_signal.microstructure.health import collector_state

    sp = Spool(tmp_path)
    now = pd.Timestamp(M + 10 * MIN, unit="ms", tz="UTC")
    base = {"written_ms": M + 10 * MIN - 2000, "started_ms": M, "connected": True,
            "coins": {"BTC": {"subscribed": list(d.FEEDS), "last_book5_age_s": 0.4,
                              "last_trade_age_s": 3, "last_finalized_minute":
                              pd.Timestamp(M + 9 * MIN, unit="ms", tz="UTC").isoformat()}}}  # fmt: skip
    sp.write_status(base)
    assert collector_state(sp, now)["issues"] == []
    sp.write_status({**base, "written_ms": M})
    assert collector_state(sp, now)["state"] == "DOWN"
    sp.write_status({**base, "connected": False, "disconnected_since_ms": M + 4 * MIN})
    assert collector_state(sp, now)["issues"]  # disconnected for 6 min
    sp.write_status({**base, "connected": False, "disconnected_since_ms": M + 10 * MIN - 20_000})
    assert collector_state(sp, now)["issues"] == []  # a 20 s reconnect is not an alert
    sp.write_status({**base, "coins": {"BTC": {**base["coins"]["BTC"], "subscribed": ["book5"]}}})
    assert any("subscription missing" in i for i in collector_state(sp, now)["issues"])


def test_status_row_only_once_deployed(store, tmp_path, monkeypatch):
    from market_signal.cli.status_cmds import microstructure_rows

    now = pd.Timestamp(M + 10 * MIN, unit="ms", tz="UTC")
    monkeypatch.setenv("PRISM_MICRO_DIR", str(tmp_path / "none"))
    assert microstructure_rows(store, now) == []
    _, out = healthy(end=M + 3 * MIN)
    ingest(store, _spool_from(out, tmp_path / "ms"))
    monkeypatch.setenv("PRISM_MICRO_DIR", str(tmp_path / "ms"))
    rows = microstructure_rows(store, now)
    assert rows[0]["component"] == "Microstructure" and rows[0]["state"] == "STALE"


def test_runtime_schedules_ingest_and_health_only():
    from market_signal.ops import runtime as rt

    assert rt.JOBS["microstructure"] == [("ingest", ["microstructure", "ingest"]),
                                         ("health", ["microstructure", "health", "--check"])]  # fmt: skip
    assert "microstructure" in rt.QUIET_JOBS and len(rt.SCHEDULE["microstructure"]) == 4
    assert "ops cycle microstructure " in rt.crontab()
    # the collector is a supervised process in the container, never a cron job
    assert not any(
        args[:2] == ["microstructure", "collect"] for job in rt.JOBS.values() for _, args in job
    )
    ep = (SRC.parents[1] / "deploy" / "railway" / "entrypoint.sh").read_text()
    assert "market microstructure collect" in ep and 'PRISM_RUNTIME_ROLE:-}" = authoritative' in ep


# --------------------------------------------------------------------------- isolation


def test_collector_never_opens_the_database_and_never_reaches_execution():
    pkg = SRC / "microstructure"
    for name in ("engine.py", "ws.py", "spool.py", "aggregate.py", "definitions.py"):
        text = (pkg / name).read_text()
        for banned in ("duckdb", "Store(", "market_signal.data.store", "open_store"):
            assert banned not in text, (name, banned)
    all_src = "\n".join(p.read_text() for p in pkg.rglob("*.py"))
    all_src += (SRC / "cli" / "micro_cmds.py").read_text()
    for banned in ("market_signal.paper", "market_signal.copilot", "market_signal.perps",
                   "market_signal.portfolio", "market_signal.scoring", "research.incubation",
                   "research.lab", "market_signal.context", "telegram", "place_order",
                   "submit_order", "private_key", "PRIVATE_KEY", "eth_account", "secret",
                   "/exchange", "sign_l1_action"):  # fmt: skip
        assert banned not in all_src, banned
    assert not re.search(r'"type":\s*"order"|"action"', all_src)
    for consumer in ("paper", "copilot", "perps", "portfolio", "scoring", "context",
                     "research/incubation", "research/lab", "research/lifecycle"):  # fmt: skip
        for p in (SRC / consumer).rglob("*.py"):
            assert "market_signal.microstructure" not in p.read_text(), p
    for p in pkg.rglob("*.py"):  # only ingest writes, and only microstructure_* tables
        text = p.read_text()
        for m in re.finditer(r"(INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+(\w+)", text):
            assert m.group(2).startswith("microstructure_"), (p, m.group(0))


def test_phase23_context_tables_are_untouched_by_migration_23():
    ddl = MIGRATIONS[22]
    assert "context_" not in ddl and "ALTER" not in ddl and "DROP" not in ddl


def test_health_flags_duplicate_minute_keys(store, tmp_path):
    from market_signal.microstructure.health import evaluate

    _, out = healthy(end=M + 3 * MIN)
    sp = _spool_from(out, tmp_path / "ms")
    ingest(store, sp)
    now = pd.Timestamp(M + 4 * MIN, unit="ms", tz="UTC")
    assert not any("duplicate" in i for i in evaluate(store, sp, now)["issues"])
    store.con.execute(  # what a broken writer would do; the table has no PK by design
        "INSERT INTO microstructure_minutes SELECT * FROM microstructure_minutes LIMIT 1"
    )
    assert any("duplicate" in i for i in evaluate(store, sp, now)["issues"])
