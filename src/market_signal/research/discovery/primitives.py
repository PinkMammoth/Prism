"""Reusable causal intraday feature primitives (``intraday_features_v1``).

Every primitive is a vectorised function of ONE venue's bars of ONE coin and timeframe and is
**causal**: the value at bar ``i`` reads bars ``<= i`` only, so it is known at that bar's
``ready_at`` (Phase 15/16 availability; ``structure.series.BarSeries``). Windows never span a
data gap (a change of ``segment``): such values are NaN, never guessed. The single deliberate
exception is the trailing percentile rank (``pct_rank``), whose 30-day reference distribution
may straddle a gap; each value inside it was itself computed within one segment.

**Log space.** Every price primitive is computed on LOG prices (``lo, lh, ll, lc``). A
price-mirrored market (``x -> ref^2 / x``) is then exactly the negated log market, so every
signed feature changes sign, every range position ``x`` becomes ``1 - x`` and a short rule
is *exactly* the long rule on the mirror (tested bit for bit).

Exact definitions (``n`` bars; logs of open/high/low/close ``lo/lh/ll/lc``; ``v`` volume):

| primitive | definition |
|---|---|
| ``ret(n)`` | ``c[i] / c[i-n] - 1`` |
| ``rv(n)`` | sample std of one-bar log returns ``lc[j] - lc[j-1]``, ``j = i-n+1..i`` |
| ``ret_z(n, w)`` | ``(lc[i] - lc[i-n]) / (rv(w)[i-n] * sqrt(n))``: the move in units of the volatility measured BEFORE it |
| ``ema(n)`` | EMA of ``lc``, span ``n``, no adjustment, ``n`` bars of warm-up, restarted at a gap |
| ``ema_slope(n, k)`` | ``ema[i] - ema[i-k]`` (log change of the EMA) |
| ``atr`` | Wilder ATR(14) of the log true range ``max(lh-ll, |lh-lc[i-1]|, |ll-lc[i-1]|)``; ``atr_prior[i] = atr[i-1]`` |
| ``efficiency(n)`` | Kaufman directional efficiency ``(lc[i]-lc[i-n]) / sum |lc[j]-lc[j-1]|``, signed, in [-1, 1] |
| ``roll_high(n)`` / ``roll_low(n)`` | max high / min low of the PRIOR ``n`` bars ``i-n..i-1`` |
| ``range_width(n)`` | ``log(roll_high / roll_low)`` (relative width) |
| ``range_width_atr(n)`` | ``range_width / atr_prior`` |
| ``range_mid(n)`` | ``sqrt(roll_high * roll_low)`` (log midpoint) |
| ``range_pos(n)`` | ``(lc - log roll_low) / range_width``; < 0 or > 1 outside the range (a break) |
| ``pct_rank(x, w)`` | share of the trailing ``w`` finite values ``x[i-w+1..i]`` that are ``<= x[i]`` (at least ``w/2`` finite) |
| ``compression(n, w)`` | ``pct_rank(range_width(n), w)``: low = compressed |
| ``rv_pct(n, w)`` | ``pct_rank(rv(n), w)`` |
| ``rel_volume(n)`` | ``v[i] / mean(v[i-n..i-1])`` |
| ``vol_z(n)`` | ``(log v[i] - mean) / std`` of ``log v[i-n..i-1]`` |
| ``vol_slope(n)`` | least-squares slope of ``log v[i-n+1..i]`` per bar |
| ``vol_accel(n)`` | ``vol_slope(n)[i] - vol_slope(n)[i-n]`` |
| ``close_location`` | ``(lc - ll) / (lh - ll)``; 0.5 for a zero-range bar |
| ``body_atr`` | ``(lc - lo) / atr_prior`` (signed) |
| ``tr_atr`` | log true range / ``atr_prior`` |
| ``ema_dist_atr(n)`` | ``(lc - ema(n)) / atr`` |

Volume: zero-volume bars are *placeholders* (Hyperliquid pre-listing bars, Binance
maintenance windows), not observations of no trading. ``volume_valid`` masks them; every
volume primitive is NaN when its window contains one. Volume is only ever used relative to
its own trailing history: absolute magnitudes are never compared across venues.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

FEATURES_VERSION = "intraday_features_v1"
ATR_N = 14


def _same_segment(seg: np.ndarray, k: int) -> np.ndarray:
    """``seg[i] == seg[i-k]`` (False where ``i < k``)."""
    n = len(seg)
    out = np.zeros(n, dtype=bool)
    if 0 <= k < n:
        out[k:] = seg[k:] == seg[: n - k]
    return out


def _shift(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


def _mask(x: np.ndarray, ok: np.ndarray) -> np.ndarray:
    return np.where(ok, x, np.nan)


def _per_segment(x: np.ndarray, seg: np.ndarray, fn) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if not len(x):
        return out
    s = pd.Series(x)
    for g in np.unique(seg):
        m = seg == g
        out[m] = fn(s[m]).to_numpy()
    return out


def ret(c: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    return _mask(c / _shift(c, n) - 1.0, _same_segment(seg, n))


def log_ret1(lc: np.ndarray, seg: np.ndarray) -> np.ndarray:
    return _mask(lc - _shift(lc, 1), _same_segment(seg, 1))


def rv(lc: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    s = pd.Series(log_ret1(lc, seg)).rolling(n, min_periods=n).std().to_numpy()
    return _mask(s, _same_segment(seg, n))


def ret_z(lc: np.ndarray, seg: np.ndarray, n: int, w: int) -> np.ndarray:
    move = lc - _shift(lc, n)
    vol = _shift(rv(lc, seg, w), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = move / (vol * np.sqrt(n))
    return _mask(z, _same_segment(seg, n) & np.isfinite(vol) & (vol > 0))


def ema(lc: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    """Per-segment EMA of log close (a gap restarts the warm-up)."""
    return _per_segment(lc, seg, lambda s: s.ewm(span=n, adjust=False, min_periods=n).mean())


def ema_slope(e: np.ndarray, seg: np.ndarray, k: int) -> np.ndarray:
    return _mask(e - _shift(e, k), _same_segment(seg, k))


def true_range(lh: np.ndarray, ll: np.ndarray, lc: np.ndarray, seg: np.ndarray) -> np.ndarray:
    pc = np.where(_same_segment(seg, 1), _shift(lc, 1), np.nan)
    with np.errstate(invalid="ignore"):
        return np.fmax(lh - ll, np.fmax(np.abs(lh - pc), np.abs(ll - pc)))


def atr(lh, ll, lc, seg, n: int = ATR_N) -> np.ndarray:
    tr = true_range(lh, ll, lc, seg)
    return _per_segment(tr, seg, lambda s: s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean())


def efficiency(lc: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    d = np.abs(lc - _shift(lc, 1))
    path = pd.Series(d).rolling(n, min_periods=n).sum().to_numpy()
    net = lc - _shift(lc, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        er = np.where(path > 0, net / path, 0.0)
    return _mask(er, _same_segment(seg, n) & np.isfinite(path))


def roll_high(h: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    x = pd.Series(h).rolling(n, min_periods=n).max().to_numpy()
    return _mask(_shift(x, 1), _same_segment(seg, n))


def roll_low(lo: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    x = pd.Series(lo).rolling(n, min_periods=n).min().to_numpy()
    return _mask(_shift(x, 1), _same_segment(seg, n))


def pct_rank(x: np.ndarray, w: int) -> np.ndarray:
    """Causal trailing percentile of the current value (``max`` rank: ties count as <=)."""
    s = pd.Series(np.asarray(x, float))
    r = s.rolling(w, min_periods=max(w // 2, 2)).rank(method="max", pct=True).to_numpy()
    return np.where(np.isfinite(x), r, np.nan)


def close_location(lh: np.ndarray, ll: np.ndarray, lc: np.ndarray) -> np.ndarray:
    rng = lh - ll
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(rng > 0, (lc - ll) / rng, 0.5)


def volume_valid(v: np.ndarray) -> np.ndarray:
    return np.isfinite(v) & (v > 0)


def _vol_window_ok(valid: np.ndarray, seg: np.ndarray, start_lag: int, length: int) -> np.ndarray:
    """All bars ``i-start_lag-length+1 .. i-start_lag`` valid and within one segment with i."""
    bad = pd.Series((~valid).astype(float)).rolling(length, min_periods=length).sum().to_numpy()
    ok = _shift(bad, start_lag) == 0
    return ok & _same_segment(seg, start_lag + length - 1)


def rel_volume(v: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    valid = volume_valid(v)
    vv = np.where(valid, v, np.nan)
    m = _shift(pd.Series(vv).rolling(n, min_periods=n).mean().to_numpy(), 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = vv / m
    return _mask(out, valid & _vol_window_ok(valid, seg, 1, n))


def vol_z(v: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    valid = volume_valid(v)
    lv = np.log(np.where(valid, v, np.nan))
    r = pd.Series(lv).rolling(n, min_periods=n)
    m, s = _shift(r.mean().to_numpy(), 1), _shift(r.std().to_numpy(), 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (lv - m) / s
    return _mask(z, valid & _vol_window_ok(valid, seg, 1, n) & (s > 0))


def vol_slope(v: np.ndarray, seg: np.ndarray, n: int) -> np.ndarray:
    """OLS slope of log volume on bar index over ``i-n+1..i`` (closed form, rolling sums)."""
    valid = volume_valid(v)
    lv = np.log(np.where(valid, v, np.nan))
    idx = np.arange(len(v), dtype=float)
    sy = pd.Series(lv).rolling(n, min_periods=n).sum().to_numpy()
    sxy = pd.Series(lv * idx).rolling(n, min_periods=n).sum().to_numpy()
    sx = n * idx - n * (n - 1) / 2.0  # sum of x over i-n+1..i
    denom = n * (n * n - 1) / 12.0
    slope = (sxy - sx * sy / n) / denom
    return _mask(slope, _vol_window_ok(valid, seg, 0, n))


@dataclass(eq=False)
class Frame:
    """Lazily computed, cached primitives of one series (shared by every variant)."""

    o: np.ndarray
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    v: np.ndarray
    segment: np.ndarray
    lo: np.ndarray = field(init=False, repr=False)
    lh: np.ndarray = field(init=False, repr=False)
    ll: np.ndarray = field(init=False, repr=False)
    lc: np.ndarray = field(init=False, repr=False)
    _cache: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        with np.errstate(divide="ignore", invalid="ignore"):
            self.lo, self.lh, self.ll, self.lc = (
                np.log(x) for x in (self.o, self.h, self.l, self.c)
            )

    @classmethod
    def from_series(cls, s) -> Frame:
        return cls(s.o, s.h, s.l, s.c, s.v, s.segment)

    def __len__(self) -> int:
        return len(self.c)

    def get(self, key: tuple, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    @property
    def atr(self):
        return self.get(("atr",), lambda: atr(self.lh, self.ll, self.lc, self.segment))

    @property
    def atr_prior(self):
        return self.get(
            ("atr_prior",), lambda: _mask(_shift(self.atr, 1), _same_segment(self.segment, 1))
        )

    def ret(self, n):
        return self.get(("ret", n), lambda: ret(self.c, self.segment, n))

    def rv(self, n):
        return self.get(("rv", n), lambda: rv(self.lc, self.segment, n))

    def ret_z(self, n, w=48):
        return self.get(("ret_z", n, w), lambda: ret_z(self.lc, self.segment, n, w))

    def ema(self, n):
        return self.get(("ema", n), lambda: ema(self.lc, self.segment, n))

    def ema_slope(self, n, k=3):
        return self.get(("ema_slope", n, k), lambda: ema_slope(self.ema(n), self.segment, k))

    def efficiency(self, n):
        return self.get(("eff", n), lambda: efficiency(self.lc, self.segment, n))

    def roll_high(self, n):
        return self.get(("rh", n), lambda: roll_high(self.h, self.segment, n))

    def roll_low(self, n):
        return self.get(("rl", n), lambda: roll_low(self.l, self.segment, n))

    def range_width(self, n):
        return self.get(("rw", n), lambda: np.log(self.roll_high(n)) - np.log(self.roll_low(n)))

    def range_width_atr(self, n):
        return self.get(("rwa", n), lambda: self.range_width(n) / self.atr_prior)

    def range_mid(self, n):
        return self.get(("rmid", n), lambda: np.sqrt(self.roll_high(n) * self.roll_low(n)))

    def range_pos(self, n):
        def f():
            w = self.range_width(n)
            with np.errstate(divide="ignore", invalid="ignore"):
                return np.where(w > 0, (self.lc - np.log(self.roll_low(n))) / w, np.nan)

        return self.get(("rpos", n), f)

    def compression(self, n, w):
        return self.get(("comp", n, w), lambda: pct_rank(self.range_width(n), w))

    def rv_pct(self, n, w):
        return self.get(("rvpct", n, w), lambda: pct_rank(self.rv(n), w))

    def rel_volume(self, n=20):
        return self.get(("relvol", n), lambda: rel_volume(self.v, self.segment, n))

    def vol_z(self, n=48):
        return self.get(("volz", n), lambda: vol_z(self.v, self.segment, n))

    def vol_slope(self, n=12):
        return self.get(("volslope", n), lambda: vol_slope(self.v, self.segment, n))

    def vol_slope_pct(self, n, w):
        return self.get(("volslopepct", n, w), lambda: pct_rank(self.vol_slope(n), w))

    def vol_accel(self, n=12):
        def f():
            s = self.vol_slope(n)
            return _mask(s - _shift(s, n), _same_segment(self.segment, n))

        return self.get(("volaccel", n), f)

    @property
    def close_location(self):
        return self.get(("cloc",), lambda: close_location(self.lh, self.ll, self.lc))

    @property
    def body_atr(self):
        return self.get(("body",), lambda: (self.lc - self.lo) / self.atr_prior)

    @property
    def tr_atr(self):
        return self.get(
            ("tr",), lambda: true_range(self.lh, self.ll, self.lc, self.segment) / self.atr_prior
        )

    def ema_dist_atr(self, n):
        return self.get(("emad", n), lambda: (self.lc - self.ema(n)) / self.atr)


def mirror_bars(o, h, l, c, ref: float):  # noqa: E741
    """The price-mirrored series ``x -> ref^2 / x`` (log prices reflected about ``log ref``):
    highs become lows, rallies become declines. Used to test long/short symmetry."""
    k = ref * ref
    return k / o, k / l, k / h, k / c


__all__ = [
    "FEATURES_VERSION", "Frame", "atr", "close_location", "efficiency", "ema", "ema_slope",
    "mirror_bars", "pct_rank", "rel_volume", "ret", "ret_z", "roll_high", "roll_low", "rv",
    "true_range", "vol_slope", "vol_z", "volume_valid",
]  # fmt: skip
