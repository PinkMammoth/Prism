"""Open-interest x price x funding primitives v1: causal features on ONE venue's hourly grid.

Every value at grid bar ``t`` (open ``o_t``, close ``c_t = o_t + 1h``) reads data known no
later than ``ready[t]`` and is never forward-filled. Venues are never pooled: a ``Grid``
holds one venue, and Hyperliquid snapshots only ever enter as a separately labelled
comparison series (``align_snapshots``), never as a substitute for a missing Binance value.

Timestamp semantics and availability (``oi_availability_v1``):

- price bar ``t`` is available at its ``BarSeries.ready_at`` (close + assumed latency for
  backfilled history);
- Binance ``openInterestHist`` rows carry the statistics timestamp ``observed_at`` (an
  hour boundary). The row with ``observed_at == c_t`` is the OI matched to bar ``t`` (the
  positioning at the bar's close). Its publication time is not recorded for backfilled rows,
  so it is ASSUMED available at ``observed_at + oi_latency`` (frozen per study). A missing
  row leaves bar ``t`` without OI (never the previous hour's value);
- the signal of bar ``t`` exists only at ``ready[t] = max(price ready, OI ready)``; a trade
  enters at the open of the first bar opening at/after it (``entry_index``). With an
  hourly grid any latency in (0, 1h] enters at bar ``t + 2``: one full bar after the close;
- funding: a settlement is known at its ``time`` (Prism's ``available_at``); features at
  bar ``t`` read settlements with ``time <= c_t``.

Definitions (``L``: lookback bars; ``W``: normalisation window; logs are natural):

- ``ret``   ``c[t] / c[t-L] - 1`` (every bar of the window present);
- ``pz``    ``ret / (sd_1 * sqrt(L))``, ``sd_1`` = std of the ``W`` one-bar returns ending
            at ``t - L`` (the window BEFORE the move, as Phase 18);
- ``oi_chg``  ``log(OI[t] / OI[t-L])`` in COIN units (``sumOpenInterest``): positioning,
            not price. ``usd_chg`` is the same on ``sumOpenInterestValue`` (USD), which also
            moves with price alone. The two are never substituted for each other;
- ``oi_delta`` ``OI[t] - OI[t-L]`` (coins); ``usd_delta`` (USD);
- ``oi_rel``  ``oi_delta / mean(OI over the W bars ending at t - L)``;
- ``oi_s``    ``oi_chg / rms(prior)``: the change scaled by the root-mean-square of the
            ``W`` prior L-bar changes (those ending at ``t-L-W+1 .. t-L``), sign kept, not
            demeaned (so "OI up" still means OI went up). ``usd_s`` likewise;
- ``oi_z``    ``(oi_chg - mean(prior)) / sd(prior)``: the demeaned score of the same prior
            distribution (a SHOCK relative to the recent trend);
- ``oi_pct``  share of the prior values below ``oi_chg`` (ties count half);
- ``oi_accel`` ``oi_chg[t] - oi_chg[t-L]``;
- ``oi_trend`` OLS slope of ``log OI`` over the ``L + 1`` observations ending at ``t``
            (per bar);
- ``oi_to_volume`` ``oi_delta / sum(volume over the L bars ending at t)`` (coins / coins);
- ``fund_24h`` sum of settled rates with ``time`` in ``(c_t - 24h, c_t]`` (cadence-free:
            a 4-hourly and an 8-hourly venue both give one day of carry); ``fund_last`` the
            newest settled rate; ``fund_chg`` ``fund_24h[t] - fund_24h[t-24]``;
            ``fund_pct`` share of the ``fund_window`` prior hourly values of ``fund_24h``
            below the current one (ties half): each coin against its own history, so a
            cadence never has to be inferred;
- ``rv``      std of the 24 one-bar returns ending at ``t``; ``vol_own`` its tercile
            (0/1/2, -1 undefined) by rank among the 720 values ending at ``t`` (30 days).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from market_signal.research.relative.primitives import complete, lag, one_bar_returns, window_return
from market_signal.research.structure.series import NAT, NS, BarSeries

PRIMITIVES_VERSION = "oi_price_primitives_v1"
AVAILABILITY_VERSION = "oi_availability_v1"
HOUR_NS = 3600 * NS
RV_BARS = 24
VOL_RANK_BARS = 720
PZ_EDGES = (0.5, 1.0, 2.0)  # |pz| buckets: [0, .5), [.5, 1), [1, 2), [2, inf)


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, eq=False)
class OiSeries:
    """One venue's OI observations of one coin (exact timestamps, int64 ns)."""

    venue: str
    coin: str
    observed_ns: np.ndarray
    oi: np.ndarray  # base units (coins)
    oi_usd: np.ndarray  # USD value, provider's own (Binance) or Prism-derived (Hyperliquid)
    usd_method: str  # "provider:sumOpenInterestValue" | "open_interest*mark_px" | ""
    ready_ns: np.ndarray  # when each observation is taken to be known

    def __len__(self) -> int:
        return len(self.observed_ns)

    @classmethod
    def empty(cls, venue: str, coin: str) -> OiSeries:
        e = np.array([], dtype=np.int64)
        return cls(venue, coin, e, e.astype(float), e.astype(float), "", e)


def binance_oi(rows: list[dict] | pd.DataFrame, *, coin: str, latency_s: float,
               period: str = "1h") -> OiSeries:  # fmt: skip
    """``perp_oi_history`` rows (one coin, ``source='binance'``) under the assumed latency."""
    df = pd.DataFrame(rows)
    if df.empty:
        return OiSeries.empty("binance", coin)
    if (df["source"] != "binance").any() or (df["coin"] != coin).any():
        raise ValueError("OI rows do not match the selected venue/coin")
    if (df["period"] != period).any():
        raise ValueError(f"OI rows are not all period {period!r}")
    methods = set(df["oi_notional_method"])
    if len(methods) != 1:
        raise ValueError("mixed USD OI methods in one series")
    df = df.sort_values("observed_at")
    t = pd.DatetimeIndex(pd.to_datetime(df["observed_at"], utc=True)).as_unit("ns").asi8
    if len(np.unique(t)) != len(t):
        raise ValueError("duplicate OI timestamps")
    lat = round(latency_s * NS)
    return OiSeries("binance", coin, t, df["open_interest"].to_numpy(float),
                    df["oi_notional"].to_numpy(float), methods.pop(), t + lat)  # fmt: skip


def hyperliquid_oi(rows: list[dict] | pd.DataFrame, *, coin: str) -> OiSeries:
    """``perp_snapshots`` rows (``source='hyperliquid'``): Prism's own capture time is both
    the timestamp and the (observed) availability."""
    df = pd.DataFrame(rows)
    if df.empty:
        return OiSeries.empty("hyperliquid", coin)
    if (df["source"] != "hyperliquid").any() or (df["coin"] != coin).any():
        raise ValueError("snapshot rows do not match the selected venue/coin")
    df = df[df["open_interest"].notna()].sort_values("snapshot_at")
    t = pd.DatetimeIndex(pd.to_datetime(df["snapshot_at"], utc=True)).as_unit("ns").asi8
    return OiSeries("hyperliquid", coin, t, df["open_interest"].to_numpy(float),
                    df["oi_notional"].to_numpy(float), "open_interest*mark_px", t)  # fmt: skip


# --------------------------------------------------------------------------- the grid


@dataclass(frozen=True, eq=False)
class Grid:
    """Hourly bars, OI and funding of several coins of ONE venue on the reference's grid.

    Price arrays are NaN and ``ready`` NAT where a coin has no bar. ``oi``/``oi_usd`` are the
    observations stamped exactly at each bar's close (NaN otherwise); ``oi_ready`` their
    assumed availability."""

    venue: str
    coins: tuple[str, ...]
    open_time: np.ndarray
    close_time: np.ndarray
    o: dict[str, np.ndarray]
    h: dict[str, np.ndarray]
    l: dict[str, np.ndarray]  # noqa: E741
    c: dict[str, np.ndarray]
    v: dict[str, np.ndarray]
    ready: dict[str, np.ndarray]  # price: everything up to bar t available
    oi: dict[str, np.ndarray]
    oi_usd: dict[str, np.ndarray]
    oi_ready: dict[str, np.ndarray]
    usd_method: dict[str, str]
    funding: dict[str, tuple[np.ndarray, np.ndarray]]
    present: dict[str, np.ndarray] = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "present", {k: np.isfinite(v) for k, v in self.c.items()})

    def __len__(self) -> int:
        return len(self.open_time)

    @classmethod
    def build(cls, series: Mapping[str, BarSeries], oi: Mapping[str, OiSeries],
              funding: Mapping[str, tuple[np.ndarray, np.ndarray]], reference: str = "BTC") -> Grid:  # fmt: skip
        if reference not in series:
            raise ValueError(f"the reference {reference} is required")
        venues = {s.prov.venue for s in series.values()} | {x.venue for x in oi.values()}
        if len(venues) != 1:
            raise ValueError("a grid holds ONE venue (venues are never pooled)")
        if {s.prov.timeframe for s in series.values()} != {"1h"}:
            raise ValueError("Phase 19 grids are hourly")
        ref = series[reference]
        step = HOUR_NS
        grid = (np.arange(ref.open_time[0], ref.open_time[-1] + step, step, dtype=np.int64)
                if len(ref) else np.array([], dtype=np.int64))  # fmt: skip
        close = grid + step
        cols: dict[str, dict] = {k: {} for k in ("o", "h", "l", "c", "v", "ready", "oi", "oi_usd",
                                                 "oi_ready")}  # fmt: skip
        methods = {}
        coins = (reference, *sorted(k for k in series if k != reference))
        for coin in coins:
            s = series[coin]
            arr = {k: np.full(len(grid), np.nan) for k in ("o", "h", "l", "c", "v")}
            rr = np.full(len(grid), NAT, dtype=np.int64)
            if len(s) and len(grid):
                pos = (s.open_time - grid[0]) // step
                on = ((s.open_time - grid[0]) % step == 0) & (pos >= 0) & (pos < len(grid))
                p = pos[on]
                for k, src in (("o", s.o), ("h", s.h), ("l", s.l), ("c", s.c), ("v", s.v)):
                    arr[k][p] = src[on]
                rr[p] = s.ready_at[on]
            for k in arr:
                cols[k][coin] = arr[k]
            cols["ready"][coin] = rr
            x = oi.get(coin) or OiSeries.empty(next(iter(venues)), coin)
            a, b = np.full(len(grid), np.nan), np.full(len(grid), np.nan)
            r = np.full(len(grid), NAT, dtype=np.int64)
            if len(x) and len(grid):
                j = np.searchsorted(close, x.observed_ns)
                hit = (j < len(grid)) & (close[np.minimum(j, len(grid) - 1)] == x.observed_ns)
                a[j[hit]], b[j[hit]], r[j[hit]] = x.oi[hit], x.oi_usd[hit], x.ready_ns[hit]
            cols["oi"][coin], cols["oi_usd"][coin], cols["oi_ready"][coin] = a, b, r
            methods[coin] = x.usd_method
        fund = {c: funding.get(c, (np.array([], np.int64), np.array([]))) for c in coins}
        return cls(next(iter(venues)), coins, grid, close, **cols, usd_method=methods,
                   funding=fund)  # fmt: skip


# --------------------------------------------------------------------------- rolling helpers


def _windows(x: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """(windows of ``w`` values ending at each index, mask of fully finite windows)."""
    n = len(x)
    if n < w:
        return np.empty((0, w)), np.zeros(n, dtype=bool)
    win = sliding_window_view(x, w)
    ok = np.zeros(n, dtype=bool)
    ok[w - 1 :] = np.isfinite(win).all(axis=1)
    return win, ok


def rolling_stat(x: np.ndarray, w: int, fn: str) -> np.ndarray:
    """``fn`` (mean / std (ddof 1) / rms) of the ``w`` values ending at each index; NaN unless
    all are finite. Computed per window (two-pass), never from running sums."""
    out = np.full(len(x), np.nan)
    win, ok = _windows(x, w)
    if not ok.any():
        return out
    sel = win[ok[w - 1 :]]
    if fn == "mean":
        v = sel.mean(axis=1)
    elif fn == "std":
        v = sel.std(axis=1, ddof=1)
    elif fn == "rms":
        v = np.sqrt((sel * sel).mean(axis=1))
    else:
        raise ValueError(fn)
    out[ok] = v
    return out


def ols_slope(y: np.ndarray, n: int) -> np.ndarray:
    """Slope per bar of an OLS line through the ``n`` values ending at each index."""
    out = np.full(len(y), np.nan)
    win, ok = _windows(y, n)
    if not ok.any():
        return out
    xs = np.arange(n) - (n - 1) / 2
    sel = win[ok[n - 1 :]]
    out[ok] = (sel * xs).sum(axis=1) / (xs * xs).sum()
    return out


def edge(state: np.ndarray, defined: np.ndarray) -> np.ndarray:
    """``state[t]`` true, ``state[t-1]`` false, both bars defined: a persistent state fires
    once, on the bar it begins."""
    return state & defined & lag(defined, 1) & ~lag(state, 1)


# --------------------------------------------------------------------------- features


@dataclass(frozen=True, eq=False)
class CoinFeatures:
    coin: str
    L: int
    W: int
    ret: np.ndarray
    pz: np.ndarray
    oi_chg: np.ndarray
    usd_chg: np.ndarray
    oi_delta: np.ndarray
    usd_delta: np.ndarray
    oi_rel: np.ndarray
    oi_s: np.ndarray
    usd_s: np.ndarray
    oi_z: np.ndarray
    oi_pct: np.ndarray
    oi_accel: np.ndarray
    oi_trend: np.ndarray
    oi_to_volume: np.ndarray
    ready: np.ndarray  # availability of the signal of bar t (price AND OI)

    @property
    def eligible(self) -> np.ndarray:
        return np.isfinite(self.pz) & np.isfinite(self.oi_s) & np.isfinite(self.oi_z)


def _scaled(chg: np.ndarray, L: int, W: int) -> tuple[np.ndarray, np.ndarray]:
    """(scaled change, demeaned z) against the W prior L-bar changes that end at t - L
    (non-overlapping with the current change)."""
    prior = lag(chg, L)
    rms = rolling_stat(prior, W, "rms")
    mu = rolling_stat(prior, W, "mean")
    sd = rolling_stat(prior, W, "std")
    with np.errstate(invalid="ignore", divide="ignore"):
        s = np.where(rms > 0, chg / rms, np.nan)
        z = np.where(sd > 0, (chg - mu) / sd, np.nan)
    return s, z


def _prior_rank(x: np.ndarray, prior: np.ndarray, w: int) -> np.ndarray:
    """Share of the ``w`` values of ``prior`` ending at ``t`` below ``x[t]`` (ties half)."""
    out = np.full(len(x), np.nan)
    win, ok = _windows(prior, w)
    ok &= np.isfinite(x)
    if not ok.any():
        return out
    idx = np.flatnonzero(ok)
    sel = win[idx - (w - 1)]
    cur = x[idx][:, None]
    out[idx] = ((sel < cur).sum(axis=1) + 0.5 * (sel == cur).sum(axis=1)) / w
    return out


def coin_features(g: Grid, coin: str, L: int, W: int) -> CoinFeatures:
    c = g.c[coin]
    x = one_bar_returns(c)
    ret = window_return(c, L)
    sd1 = lag(rolling_stat(x, W, "std"), L)
    with np.errstate(invalid="ignore", divide="ignore"):
        pz = np.where(sd1 > 0, ret / (sd1 * np.sqrt(L)), np.nan)
    oi, ou = g.oi[coin], g.oi_usd[coin]
    oi_ok = complete(np.isfinite(oi) & (oi > 0), L + 1)
    ou_ok = complete(np.isfinite(ou) & (ou > 0), L + 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        oi_chg = np.where(oi_ok, np.log(oi / lag(oi, L)), np.nan)
        usd_chg = np.where(ou_ok, np.log(ou / lag(ou, L)), np.nan)
    oi_delta = np.where(oi_ok, oi - lag(oi, L), np.nan)
    usd_delta = np.where(ou_ok, ou - lag(ou, L), np.nan)
    base = lag(rolling_stat(oi, W, "mean"), L)
    with np.errstate(invalid="ignore", divide="ignore"):
        oi_rel = np.where(base > 0, oi_delta / base, np.nan)
    oi_s, oi_z = _scaled(oi_chg, L, W)
    usd_s, _ = _scaled(usd_chg, L, W)
    oi_pct = _prior_rank(oi_chg, lag(oi_chg, L), W)
    with np.errstate(invalid="ignore", divide="ignore"):
        logoi = np.where(oi > 0, np.log(oi), np.nan)
        vol_sum = np.where(
            complete(np.isfinite(g.v[coin]), L), rolling_stat(g.v[coin], L, "mean") * L, np.nan
        )
        o2v = np.where(vol_sum > 0, oi_delta / vol_sum, np.nan)
    ready = np.maximum(g.ready[coin], g.oi_ready[coin])
    ready = np.where((g.ready[coin] == NAT) | (g.oi_ready[coin] == NAT), NAT, ready)
    return CoinFeatures(coin=coin, L=L, W=W, ret=ret, pz=pz, oi_chg=oi_chg, usd_chg=usd_chg,
                        oi_delta=oi_delta, usd_delta=usd_delta, oi_rel=oi_rel, oi_s=oi_s,
                        usd_s=usd_s, oi_z=oi_z, oi_pct=oi_pct,
                        oi_accel=oi_chg - lag(oi_chg, L), oi_trend=ols_slope(logoi, L + 1),
                        oi_to_volume=o2v, ready=ready)  # fmt: skip


@dataclass(frozen=True, eq=False)
class Funding:
    fund_last: np.ndarray
    fund_24h: np.ndarray
    fund_chg: np.ndarray
    fund_pct: np.ndarray
    settlements_24h: np.ndarray  # count in the trailing day (cadence, observed causally)


def funding_features(g: Grid, coin: str, window: int) -> Funding:
    """Causal funding on the grid: only settlements with ``time <= close_time[t]``. NaN where
    the stored series does not reach back a full day (never assumed zero)."""
    t_ns, rate = g.funding[coin]
    n = len(g)
    nan = np.full(n, np.nan)
    if not len(t_ns):
        return Funding(nan, nan.copy(), nan.copy(), nan.copy(), np.zeros(n, dtype=int))
    cs = np.concatenate([[0.0], np.cumsum(rate)])
    b = np.searchsorted(t_ns, g.close_time, side="right")
    a = np.searchsorted(t_ns, g.close_time - 24 * HOUR_NS, side="right")
    covered = t_ns[0] <= g.close_time - 24 * HOUR_NS
    f24 = np.where(covered, cs[b] - cs[a], np.nan)
    last = np.where(b > 0, rate[np.maximum(b - 1, 0)], np.nan)
    cnt = np.where(covered, b - a, 0)
    return Funding(last, f24, f24 - lag(f24, 24), _prior_rank(f24, lag(f24, 1), window),
                   cnt.astype(int))  # fmt: skip


def own_vol(g: Grid, coin: str) -> tuple[np.ndarray, np.ndarray]:
    """(rv: std of the 24 one-bar returns ending at t, tercile 0/1/2 or -1) ranked among the
    720 values ending at t (inclusive, as Phase 17's regime rank)."""
    rv = rolling_stat(one_bar_returns(g.c[coin]), RV_BARS, "std")
    r = _prior_rank(rv, rv, VOL_RANK_BARS)
    terc = np.where(np.isfinite(r), np.minimum(np.floor(np.nan_to_num(r) * 3), 2), -1)
    return rv, terc.astype(int)


def pz_bucket(pz: np.ndarray) -> np.ndarray:
    """|pz| bucket 0..3 (``PZ_EDGES``), -1 undefined."""
    a = np.abs(pz)
    b = np.searchsorted(np.asarray(PZ_EDGES), np.nan_to_num(a, nan=-1), side="right")
    return np.where(np.isfinite(a), b, -1).astype(int)


# --------------------------------------------------------------------------- hyperliquid


def align_snapshots(g: Grid, snaps: OiSeries, L: int, max_age_s: float) -> dict[str, np.ndarray]:
    """A Hyperliquid comparison series on another venue's grid, by ACTUAL elapsed time.

    At each bar close: the newest snapshot captured at or before it, if no older than
    ``max_age_s``. ``chg`` is the log change between the values matched at ``t`` and
    ``t - L`` when the two snapshots are ``L`` hours +- ``max_age_s`` apart. Nothing is
    interpolated and nothing is filled from the other venue."""
    n = len(g)
    val, age = np.full(n, np.nan), np.full(n, np.nan)
    at = np.full(n, NAT, dtype=np.int64)
    if len(snaps) and n:
        j = np.searchsorted(snaps.observed_ns, g.close_time, side="right") - 1
        ok = j >= 0
        jj = np.maximum(j, 0)
        a = (g.close_time - snaps.observed_ns[jj]) / NS
        fresh = ok & (a <= max_age_s)
        val[fresh], age[fresh], at[fresh] = (
            snaps.oi[jj[fresh]],
            a[fresh],
            snaps.observed_ns[jj[fresh]],
        )
    prev, prev_at = lag(val, L), lag(at, L)
    elapsed = np.where((at != NAT) & (prev_at != NAT), (at - prev_at) / NS, np.nan)
    good = np.isfinite(elapsed) & (np.abs(elapsed - L * 3600) <= max_age_s) & (val > 0) & (prev > 0)
    with np.errstate(invalid="ignore", divide="ignore"):
        chg = np.where(good, np.log(val / prev), np.nan)
    return {"oi": val, "age_s": age, "chg": chg, "elapsed_s": elapsed}


# --------------------------------------------------------------------------- outcomes


def entry_index(g: Grid, after_ns: np.ndarray) -> np.ndarray:
    """The first grid bar opening at/after each availability instant (NAT -> beyond the end)."""
    a = np.asarray(after_ns, dtype=np.int64)
    k = np.searchsorted(g.open_time, a, side="left")
    return np.where(a == NAT, len(g), k)


@dataclass(frozen=True, eq=False)
class Paths:
    """Forward outcome of a long position entered at the open of bar ``k`` and exited at the
    close of bar ``k + h - 1``: return, log-absolute return, range, max favourable / adverse
    excursion for a long and for a short. NaN unless every bar is present."""

    ret: np.ndarray
    absret: np.ndarray
    rng: np.ndarray
    mfe_long: np.ndarray
    mae_long: np.ndarray
    mfe_short: np.ndarray
    mae_short: np.ndarray
    entry_ns: np.ndarray
    exit_ns: np.ndarray


def forward_paths(g: Grid, coin: str, k: np.ndarray, h: int) -> Paths:
    k = np.asarray(k, dtype=np.int64)
    n = len(g)
    nan = lambda: np.full(len(k), np.nan)  # noqa: E731
    ret, absret, rng_, mfl, mal, mfs, mas = (nan() for _ in range(7))
    ent = np.full(len(k), NAT, dtype=np.int64)
    ext = np.full(len(k), NAT, dtype=np.int64)
    last = k + h - 1
    ok = (k >= 0) & (last < n)
    if ok.any():
        full = complete(g.present[coin], h)
        ok[ok] &= full[last[ok]]
    if ok.any():
        kk, ll = k[ok], last[ok]
        o = g.o[coin][kk]
        c = g.c[coin][ll]
        hw = sliding_window_view(g.h[coin], h)[kk] if n >= h else None
        lw = sliding_window_view(g.l[coin], h)[kk] if n >= h else None
        hi, lo = hw.max(axis=1), lw.min(axis=1)
        ret[ok] = c / o - 1
        absret[ok] = np.abs(np.log(c / o))
        rng_[ok] = (hi - lo) / o
        mfl[ok], mal[ok] = hi / o - 1, lo / o - 1
        mfs[ok], mas[ok] = 1 - lo / o, 1 - hi / o
        ent[ok] = g.open_time[kk]
        ext[ok] = g.close_time[ll]
    return Paths(ret, absret, rng_, mfl, mal, mfs, mas, ent, ext)


__all__ = [
    "AVAILABILITY_VERSION",
    "PRIMITIVES_VERSION",
    "PZ_EDGES",
    "CoinFeatures",
    "Funding",
    "Grid",
    "OiSeries",
    "Paths",
    "align_snapshots",
    "binance_oi",
    "coin_features",
    "edge",
    "entry_index",
    "forward_paths",
    "funding_features",
    "hyperliquid_oi",
    "ols_slope",
    "own_vol",
    "pz_bucket",
    "rolling_stat",
]
