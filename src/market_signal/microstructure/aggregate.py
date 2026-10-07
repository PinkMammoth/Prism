"""Pure, deterministic arithmetic for one (coin, UTC minute) of ``microstructure_1m_v1``.

Inputs are normalized events (plain tuples, exchange/receipt times in epoch milliseconds):

* trade  ``(t, recv, px, sz, is_buy, tid, hkey)``: one fill. ``is_buy`` is Hyperliquid's
  taker side (``side == "B"``), never inferred. ``hkey`` groups fills of one taker transaction
  into a *print* (``None`` when the venue gave no usable hash: the fill is its own print).
* book5  ``(t, recv, bid, ask, bid5_ntl, ask5_ntl)``: one fast 5-level snapshot summary.
* book20 ``(t, recv, bid20_ntl, ask20_ntl)``: one 20-level snapshot summary.
* ctx    ``(recv, oi, mark, oracle, funding, impact_bid, impact_ask)``: asset context.

Book statistics are **time-weighted by construction**: the book is sampled on a fixed 1 s
exchange-time grid (``open + 0..59 s``), each sample taking the latest snapshot at or before
the grid instant, valid only if that snapshot is fresh (``*_MAX_AGE_MS``). A burst of
messages therefore cannot dominate a minute; only the state over time counts.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

import numpy as np

from market_signal.microstructure import definitions as d

TRADE_FIELDS = ("n_buy", "n_sell", "n_buy_prints", "n_sell_prints", "buy_vol", "sell_vol",
                "buy_ntl", "sell_ntl", "first_px", "last_px", "high_px", "low_px",
                "max_print_ntl", "med_print_ntl", "size_hist")  # fmt: skip
LP_FIELDS = ("lp_threshold", "lp_n", "lp_buy_n", "lp_ntl", "lp_buy_ntl")


def status_for(trade_cov: float, book_samples: int, depth20_samples: int) -> str:
    trade_ok = trade_cov >= d.TRADE_COV_MIN
    book_ok = book_samples >= d.BOOK_SAMPLES_MIN and depth20_samples >= d.BOOK_SAMPLES_MIN
    trade_any, book_any = trade_cov > 0, book_samples > 0 or depth20_samples > 0
    if trade_ok and book_ok:
        return "COMPLETE"
    if not trade_any and not book_any:
        return "GAP"
    if trade_ok and not book_any:
        return "TRADE_ONLY"
    if book_ok and not trade_any:
        return "BOOK_ONLY"
    return "PARTIAL"


def prints(trades: Sequence[tuple]) -> list[tuple[float, bool]]:
    """Group fills into taker prints: same tx hash, side and exchange time. Returns
    ``(notional, is_buy)`` per print in first-fill order."""
    out: dict[tuple, list] = {}
    for t, _r, px, sz, is_buy, tid, hkey in trades:
        key = (hkey, is_buy, t) if hkey is not None else ("tid", tid)
        if key in out:
            out[key][0] += px * sz
        else:
            out[key] = [px * sz, is_buy]
    return [(v[0], v[1]) for v in out.values()]


def trade_stats(trades: Sequence[tuple], lp_threshold: float | None) -> tuple[dict, dict]:
    """Trade-flow fields + the sparse fine print histogram (spool only)."""
    out: dict = {}
    b = [x for x in trades if x[4]]
    s = [x for x in trades if not x[4]]
    out["n_buy"], out["n_sell"] = len(b), len(s)
    out["buy_vol"] = float(sum(x[3] for x in b))
    out["sell_vol"] = float(sum(x[3] for x in s))
    out["buy_ntl"] = float(sum(x[2] * x[3] for x in b))
    out["sell_ntl"] = float(sum(x[2] * x[3] for x in s))
    if trades:
        order = sorted(range(len(trades)), key=lambda i: (trades[i][0], i))  # stable by time
        out["first_px"] = float(trades[order[0]][2])
        out["last_px"] = float(trades[order[-1]][2])
        out["high_px"] = float(max(x[2] for x in trades))
        out["low_px"] = float(min(x[2] for x in trades))
    else:
        out["first_px"] = out["last_px"] = out["high_px"] = out["low_px"] = None
    pr = prints(trades)
    out["n_buy_prints"] = sum(1 for _, buy in pr if buy)
    out["n_sell_prints"] = len(pr) - out["n_buy_prints"]
    ntl = np.array([p[0] for p in pr], dtype=float)
    out["max_print_ntl"] = float(ntl.max()) if len(ntl) else None
    out["med_print_ntl"] = float(np.median(ntl)) if len(ntl) else None
    hist = np.bincount(np.searchsorted(d.SIZE_EDGES, ntl, side="right"),
                       minlength=len(d.SIZE_EDGES) + 1) if len(ntl) else np.zeros(len(d.SIZE_EDGES) + 1, int)  # fmt: skip
    out["size_hist"] = [int(x) for x in hist]
    fine: dict[str, int] = {}
    for v in ntl:
        k = str(d.fine_bin(float(v)))
        fine[k] = fine.get(k, 0) + 1
    if lp_threshold is None:
        out.update(lp_threshold=None, lp_n=None, lp_buy_n=None, lp_ntl=None, lp_buy_ntl=None)
    else:
        big = [(v, buy) for v, buy in pr if v >= lp_threshold]
        out["lp_threshold"] = float(lp_threshold)
        out["lp_n"] = len(big)
        out["lp_buy_n"] = sum(1 for _, buy in big if buy)
        out["lp_ntl"] = float(sum(v for v, _ in big))
        out["lp_buy_ntl"] = float(sum(v for v, buy in big if buy))
    return out, fine


def _asof_samples(snaps: Sequence[tuple], open_ms: int, max_age: int) -> list[tuple | None]:
    """For each grid instant g = open + k s: the latest snapshot with t <= g, valid only if the
    NEXT snapshot followed within ``max_age`` (the feed provably continued through g). After a
    disconnect, a stale subscription or at the edge of an outage the sample is invalid, so a
    book that was not being observed never counts as observed."""
    times = [x[0] for x in snaps]
    out: list[tuple | None] = []
    for k in range(d.SAMPLES_PER_MINUTE):
        g = open_ms + k * d.SAMPLE_STEP_MS
        i = bisect_right(times, g) - 1
        ok = 0 <= i < len(times) - 1 and times[i + 1] - times[i] <= max_age
        out.append(snaps[i] if ok else None)
    return out


def _last_before(snaps: Sequence[tuple], close_ms: int, max_age: int) -> tuple | None:
    """End-of-minute state: the latest snapshot strictly before close, valid on the same rule
    (its successor arrived within ``max_age``)."""
    times = [x[0] for x in snaps]
    i = bisect_right(times, close_ms - 1) - 1
    ok = 0 <= i < len(times) - 1 and times[i + 1] - times[i] <= max_age
    return snaps[i] if ok else None


def book_stats(b5: Sequence[tuple], b20: Sequence[tuple], open_ms: int) -> dict:
    """Spread/depth from 1 s samples; dynamics from consecutive in-minute book5 snapshots.
    ``b5``/``b20`` are time-sorted: the last snapshot before the minute (carry-in), those
    inside it, and the first one after it (proof the feed continued past the close)."""
    close = open_ms + d.MINUTE_MS
    out: dict = {}
    s5 = [x for x in _asof_samples(b5, open_ms, d.BOOK5_MAX_AGE_MS) if x is not None]
    out["book_samples"] = len(s5)
    if s5:
        bid = np.array([x[2] for x in s5])
        ask = np.array([x[3] for x in s5])
        spread = ask - bid
        bps = spread / ((ask + bid) / 2) * 1e4
        out["spread_mean"] = float(spread.mean())
        out["spread_bps_mean"] = float(bps.mean())
        out["spread_bps_med"] = float(np.median(bps))
        out["spread_bps_min"] = float(bps.min())
        out["spread_bps_max"] = float(bps.max())
        out["bid5_mean"] = float(np.mean([x[4] for x in s5]))
        out["ask5_mean"] = float(np.mean([x[5] for x in s5]))
    else:
        for k in ("spread_mean", "spread_bps_mean", "spread_bps_med", "spread_bps_min",
                  "spread_bps_max", "bid5_mean", "ask5_mean"):  # fmt: skip
            out[k] = None
    end5 = _last_before(b5, close, d.BOOK5_MAX_AGE_MS)
    out["bid_end"], out["ask_end"] = (end5[2], end5[3]) if end5 else (None, None)
    out["bid5_end"], out["ask5_end"] = (end5[4], end5[5]) if end5 else (None, None)

    s20 = [x for x in _asof_samples(b20, open_ms, d.BOOK20_MAX_AGE_MS) if x is not None]
    out["depth20_samples"] = len(s20)
    out["bid20_mean"] = float(np.mean([x[2] for x in s20])) if s20 else None
    out["ask20_mean"] = float(np.mean([x[3] for x in s20])) if s20 else None
    end20 = _last_before(b20, close, d.BOOK20_MAX_AGE_MS)
    out["bid20_end"], out["ask20_end"] = (end20[2], end20[3]) if end20 else (None, None)

    # dynamics: pairs (previous snapshot, snapshot inside the minute), no pair across a gap
    inside = [i for i, x in enumerate(b5) if open_ms <= x[0] < close]
    out["book_updates"] = len(inside)
    nb = na = nm = 0
    rb = ra = 0.0
    for i in inside:
        if i == 0:
            continue
        p, c = b5[i - 1], b5[i]
        if c[0] - p[0] > d.DYNAMICS_MAX_GAP_MS:
            continue
        nb += c[2] != p[2]
        na += c[3] != p[3]
        nm += (c[2] + c[3]) != (p[2] + p[3])
        if c[2] == p[2]:
            rb += max(0.0, c[4] - p[4])
        if c[3] == p[3]:
            ra += max(0.0, c[5] - p[5])
    has_pairs = any(i > 0 and b5[i][0] - b5[i - 1][0] <= d.DYNAMICS_MAX_GAP_MS for i in inside)
    out["bid_changes"], out["ask_changes"], out["mid_changes"] = (
        (nb, na, nm) if has_pairs else (None,) * 3
    )
    out["bid_replenish"], out["ask_replenish"] = (rb, ra) if has_pairs else (None, None)
    return out


def ctx_stats(ctx: tuple | None, close_ms: int) -> dict:
    keys = ("oi_end", "mark_end", "oracle_end", "funding_end", "impact_bid_end", "impact_ask_end")
    if ctx is None or close_ms - ctx[0] > d.CTX_MAX_AGE_MS:
        return dict.fromkeys(keys)
    return dict(zip(keys, ctx[1:], strict=True))


def latency(trades: Sequence[tuple], b5: Sequence[tuple], open_ms: int) -> dict:
    close = open_ms + d.MINUTE_MS
    lat = [x[1] - x[0] for x in trades] + [x[1] - x[0] for x in b5 if open_ms <= x[0] < close]
    if not lat:
        return {"lat_p50_ms": None, "lat_max_ms": None}
    return {"lat_p50_ms": int(np.median(lat)), "lat_max_ms": int(max(lat))}


def aggregate_minute(open_ms: int, trades: Sequence[tuple], b5: Sequence[tuple],
                     b20: Sequence[tuple], ctx: tuple | None, trade_cov: float,
                     lp_threshold: float | None) -> tuple[dict, dict]:  # fmt: skip
    """All metric fields of one minute (no identity/timing) + its fine print histogram.

    Trade fields are NULL when the trades feed was not live at all (never zero-filled); book
    fields are NULL without a valid sample. A COMPLETE minute with zero trades is a genuinely
    quiet minute, not an outage.
    """
    close = open_ms + d.MINUTE_MS
    out: dict = {"trade_cov": round(float(trade_cov), 4)}
    if trade_cov > 0:
        ts, fine = trade_stats(trades, lp_threshold)
    else:
        ts, fine = dict.fromkeys(TRADE_FIELDS + LP_FIELDS), {}
    out.update(ts)
    out.update(book_stats(b5, b20, open_ms))
    out.update(ctx_stats(ctx, close))
    out.update(latency(trades, b5, open_ms))
    out["status"] = status_for(out["trade_cov"], out["book_samples"], out["depth20_samples"])
    if out["status"] == "GAP":  # nothing observed live: no metric is meaningful
        for k in list(out):
            if k not in ("trade_cov", "book_samples", "depth20_samples", "status"):
                out[k] = None
        fine = {}
    return out, fine
