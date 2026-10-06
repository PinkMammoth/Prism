"""Signal rules of ``intraday_catalogue_v1``: one implementation per rule, both sides.

``Oriented(ctx, s)`` presents the market so that the long rule reads it directly (``s = +1``)
and the short rule reads the *mirrored* market (``s = -1``): signed features are negated,
range position / close location become ``1 - x``, breakouts above highs become breakdowns
below lows, and the funding percentile becomes ``1 - pct``. A short rule is therefore the
long rule's code applied to the mirror, so the two sides cannot diverge (tested on a
price-mirrored series).

A signal fires at bar ``i`` (its close); every input is a primitive known at ``ready_at[i]``.
Higher-timeframe and BTC context are the newest bars **ready by** the signal bar's ready time
(``align``), never a forming bar. State-like conditions are edge-triggered (true now, false
on the previous bar), so a persisting state is one signal, not one per bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from market_signal.research.discovery.catalogue import PCT_WINDOW, IntradayStrategy, VolTest
from market_signal.research.discovery.primitives import Frame, pct_rank

DAY_NS = 86_400 * 10**9


def align(ready_from: np.ndarray, ready_to: np.ndarray) -> np.ndarray:
    """For each instant in ``ready_to``: index of the newest bar of the other series whose
    ``ready_from`` is at or before it (-1 if none)."""
    return np.searchsorted(ready_from, ready_to, side="right") - 1


def take(x: np.ndarray, idx: np.ndarray) -> np.ndarray:
    out = np.full(len(idx), np.nan)
    ok = idx >= 0
    out[ok] = x[idx[ok]]
    return out


def edge(cond: np.ndarray, seg: np.ndarray | None = None) -> np.ndarray:
    """True where ``cond`` turns on (the previous bar false, or a new data segment)."""
    c = np.asarray(cond, dtype=bool)
    prev = np.concatenate([[False], c[:-1]])
    if seg is not None and len(seg):
        prev &= np.concatenate([[False], seg[1:] == seg[:-1]])
    return c & ~prev


def funding_level(funding_ns: np.ndarray, rate: np.ndarray, ready_at: np.ndarray,
                  hours: int = 24) -> np.ndarray:  # fmt: skip
    """Mean funding rate of settlements in ``(t - hours, t]`` at each instant ``t`` (causal:
    only settlements already paid). NaN when none."""
    out = np.full(len(ready_at), np.nan)
    if not len(funding_ns):
        return out
    cs = np.concatenate([[0.0], np.cumsum(rate)])
    b = np.searchsorted(funding_ns, ready_at, side="right")
    a = np.searchsorted(funding_ns, ready_at - hours * 3600 * 10**9, side="right")
    n = b - a
    covered = (ready_at >= funding_ns[0]) & (ready_at <= funding_ns[-1] + hours * 3600 * 10**9)
    ok = (n > 0) & covered
    out[ok] = (cs[b] - cs[a])[ok] / n[ok]
    return out


@dataclass(eq=False)
class SignalContext:
    """Everything a rule may read for one coin on one signal timeframe."""

    tf: str
    f: Frame
    ready_at: np.ndarray
    htf: Frame | None = None  # context timeframe frame (4h for 1h signals; 1h for 15m)
    htf_idx: np.ndarray | None = None
    btc: Frame | None = None  # BTC on the same timeframe (None for BTC itself)
    btc_idx: np.ndarray | None = None
    funding_pct: np.ndarray | None = None
    _cache: dict = field(default_factory=dict, repr=False)

    def htf_get(self, key: str, fn) -> np.ndarray:
        k = ("htf", key)
        if k not in self._cache:
            self._cache[k] = (
                take(fn(self.htf), self.htf_idx)
                if self.htf is not None
                else np.full(len(self.f), np.nan)
            )
        return self._cache[k]

    def btc_get(self, key: str, fn) -> np.ndarray:
        k = ("btc", key)
        if k not in self._cache:
            self._cache[k] = (
                take(fn(self.btc), self.btc_idx)
                if self.btc is not None
                else np.full(len(self.f), np.nan)
            )
        return self._cache[k]


class Oriented:
    """The market as seen by side ``s`` (+1 long, -1 short)."""

    def __init__(self, ctx: SignalContext, s: int):
        self.x, self.s, self.f = ctx, s, ctx.f

    def z(self, n, w=48):
        return self.s * self.f.ret_z(n, w)

    def ret(self, n):
        return self.s * self.f.ret(n)

    def above_ema(self, n):
        return (self.f.lc > self.f.ema(n)) if self.s > 0 else (self.f.lc < self.f.ema(n))

    def below_ema(self, n):
        return (self.f.lc < self.f.ema(n)) if self.s > 0 else (self.f.lc > self.f.ema(n))

    def slope(self, n, k=3):
        return self.s * self.f.ema_slope(n, k)

    def eff(self, n):
        return self.s * self.f.efficiency(n)

    def breaks_out(self, n):
        return (self.f.c > self.f.roll_high(n)) if self.s > 0 else (self.f.c < self.f.roll_low(n))

    def breaks_down(self, n):
        return (self.f.c < self.f.roll_low(n)) if self.s > 0 else (self.f.c > self.f.roll_high(n))

    def pos(self, n):
        p = self.f.range_pos(n)
        return p if self.s > 0 else 1.0 - p

    def cloc(self):
        c = self.f.close_location
        return c if self.s > 0 else 1.0 - c

    def body(self):
        return self.s * self.f.body_atr

    def ema_dist(self, n):
        return self.s * self.f.ema_dist_atr(n)

    def funding(self):
        p = self.x.funding_pct
        if p is None:
            return np.full(len(self.f), np.nan)
        return p if self.s > 0 else 1.0 - p

    def htf_trend_up(self):
        """Context trend on the context frame: EMA20 above EMA50 (4h) / close above a rising
        EMA20 (1h context for 15m triggers)."""
        x = self.x
        if x.tf == "1h":
            e20 = x.htf_get("ema20", lambda f: f.ema(20))
            e50 = x.htf_get("ema50", lambda f: f.ema(50))
            return (e20 > e50) if self.s > 0 else (e20 < e50)
        c = x.htf_get("lclose", lambda f: f.lc)
        e20 = x.htf_get("ema20", lambda f: f.ema(20))
        sl = x.htf_get("slope20", lambda f: f.ema_slope(20, 3))
        return ((c > e20) & (sl > 0)) if self.s > 0 else ((c < e20) & (sl < 0))

    def htf_close_above_ema20(self):
        c = self.x.htf_get("lclose", lambda f: f.lc)
        e = self.x.htf_get("ema20", lambda f: f.ema(20))
        return (c > e) if self.s > 0 else (c < e)

    def btc_z(self, n):
        return self.s * self.x.btc_get(f"z{n}", lambda f: f.ret_z(n, 48))

    def btc_strong(self):
        c = self.x.btc_get("lclose", lambda f: f.lc)
        e = self.x.btc_get("ema50", lambda f: f.ema(50))
        r = self.x.btc_get("ret24", lambda f: f.ret(24))
        return ((c > e) & (r > 0)) if self.s > 0 else ((c < e) & (r < 0))


def _rule(o: Oriented, rule: str, p: dict) -> np.ndarray:
    f, seg, w = o.f, o.f.segment, PCT_WINDOW[o.x.tf]
    E = lambda c: edge(c, seg)  # noqa: E731
    if rule == "momentum_z":
        return E(o.z(p["n"]) >= p["k"])
    if rule == "ema_cross":
        return E(o.above_ema(p["n"]))
    if rule == "ema_cross_trend":
        return E(o.above_ema(p["n"])) & (o.slope(p["n"]) > 0)
    if rule == "efficiency_trend":
        return E(o.eff(p["n"]) >= p["er"])
    if rule == "momentum_htf":
        return E(o.z(p["n"]) >= p["k"]) & o.htf_close_above_ema20()
    if rule == "momentum_ltf_ctx":
        return E(o.z(p["n"]) >= p["k"]) & o.htf_trend_up()
    if rule == "pullback":
        if p["trend"] == "1h":
            up = (o.s * (f.ema(20) - f.ema(50)) > 0) & (o.slope(50) > 0)
        else:
            up = o.htf_trend_up()
        if p["entry"] == "immediate":  # the close dips below the fast EMA in an uptrend
            return E(o.below_ema(20)) & up
        recent_dip = np.zeros(len(f), dtype=bool)  # recross after a dip within 6 bars
        below = o.below_ema(20)
        for k in range(1, 7):
            recent_dip[k:] |= below[:-k]
        return E(o.above_ema(20)) & up & recent_dip
    if rule == "breakout":
        return E(o.breaks_out(p["n"]))
    if rule == "range_reversion":
        pos = o.pos(p["n"])
        inside_low = (pos >= 0.0) & (pos <= 0.1)
        return E(inside_low) & (f.range_width_atr(p["n"]) <= p["max_width_atr"])
    if rule == "ema_stretch_fade":
        return E(o.ema_dist(20) <= -p["k"])
    if rule in ("compression_breakout", "compression_volume_breakout"):
        comp = np.concatenate([[np.nan], f.compression(p["n"], w)[:-1]])  # before the break
        sig = E(o.breaks_out(p["n"])) & (comp <= p["pct"])
        if rule == "compression_volume_breakout":
            vs = np.concatenate([[np.nan], f.vol_slope_pct(12, w)[:-1]])
            sig &= vs >= p["vslope_pct"]
        return sig
    if rule == "compression_clue":
        onset = E(f.compression(p["n"], w) <= p["pct"])
        clue = p["clue"]
        if clue == "range_pos":
            lean = o.pos(p["n"]) > 0.5
        elif clue == "signed_return":
            lean = o.ret(p["n"]) > 0
        elif clue == "ema_slope":
            lean = o.slope(20) > 0
        else:  # pressure: >= 4 of the last 6 closes in the upper quarter of their bar
            hi = (o.cloc() >= 0.75).astype(int)
            cnt = np.convolve(hi, np.ones(6, dtype=int), mode="full")[: len(hi)]
            lean = cnt >= 4
        return onset & lean
    if rule == "volume_continuation":
        return E(f.rel_volume(20) >= p["r"]) & (o.body() > 0)
    if rule == "volume_fade":
        return E(f.rel_volume(20) >= p["r"]) & (o.body() < 0)
    if rule == "expansion_continuation":
        return E(f.tr_atr >= p["k"]) & (o.cloc() >= 0.7) & (o.body() > 0)
    if rule == "expansion_fade":
        return E(f.tr_atr >= p["k"]) & (o.cloc() <= 0.3) & (o.body() < 0)
    if rule == "exhaustion":  # long after an extreme DECLINE
        sig = E(o.z(p["n"]) <= -p["z"])
        if p["strong_close"] == "yes":
            sig &= o.cloc() >= 0.6
        return sig
    if rule == "funding_trend":
        return E(o.z(4) >= 1.5) & (o.funding() >= p["extreme"])
    if rule == "funding_reversal":
        return E(o.above_ema(20)) & (o.funding() <= 1.0 - p["extreme"])
    if rule == "btc_aligned_momentum":
        bz = o.btc_z(4)
        return E(o.z(4) >= 1.5) & ((bz > 0) if p["btc"] == "aligned" else (bz < 0))
    if rule == "btc_flat_breakout":
        return E(o.breaks_out(p["n"])) & (np.abs(o.btc_z(p["n"])) < 0.5)
    if rule == "btc_strength_breakout":
        return E(o.breaks_out(p["n"])) & o.btc_strong()
    raise KeyError(rule)


def trend_mirror(o: Oriented) -> np.ndarray:
    """Downtrend in the market the short rule trades, expressed on the oriented view: the
    short-only rules are written for s = -1, where ``above_ema`` means below."""
    return o.above_ema(50) & (o.slope(50) > 0)


def signal_mask(ctx: SignalContext, s: IntradayStrategy) -> np.ndarray:
    """Boolean signal bars of one strategy on one coin. Short-only rules are evaluated on the
    oriented (mirrored) market of the SHORT side."""
    if not s.symmetric:
        return np.asarray(_short_only(Oriented(ctx, -1), s.rule, s.p), dtype=bool)
    sign = 1 if s.side == "long" else -1
    return np.asarray(_rule(Oriented(ctx, sign), s.rule, s.p), dtype=bool)


def _short_only(o: Oriented, rule: str, p: dict) -> np.ndarray:
    """The three short-specific rules, read on the short side's oriented market (s = -1):
    there ``breaks_out`` is a break BELOW the low, ``body() >= x`` a large DOWN body,
    ``cloc() >= 0.75`` a close near the LOW, and ``z <= -thr`` an extreme RISE."""
    f, seg = o.f, o.f.segment
    E = lambda c: edge(c, seg)  # noqa: E731
    if rule == "volume_downside_expansion":
        return E(f.rel_volume(20) >= p["r"]) & (o.body() >= p["body"]) & (o.cloc() >= 0.75)
    if rule == "blowoff_exhaustion":
        return E(o.z(p["n"]) <= -p["z"]) & (o.cloc() >= 0.7) & (f.rel_volume(20) >= p["r"])
    if rule == "failed_bounce_breakdown":
        e20 = f.ema(20)
        downtrend = trend_mirror(o)  # on the mirror: "above a rising EMA50" = real downtrend
        rejected = (f.lh >= e20) & (f.lc < e20)  # real high tagged EMA20, real close below it
        recent = np.zeros(len(f), dtype=bool)
        for k in range(1, 7):
            recent[k:] |= rejected[:-k]
        return E(o.breaks_out(p["n"])) & downtrend & recent
    raise KeyError(rule)


def vol_mask(ctx: SignalContext, v: VolTest) -> np.ndarray:
    """Non-directional state onsets (the volatility-forecast family)."""
    f, seg, w, p = ctx.f, ctx.f.segment, PCT_WINDOW[ctx.tf], v.p
    if v.rule == "compression_state":
        return edge(f.compression(p["n"], w) <= p["pct"], seg)
    if v.rule == "compression_volume_state":
        return edge((f.compression(p["n"], w) <= p["pct"])
                    & (f.vol_slope_pct(12, w) >= p["vslope_pct"]), seg)  # fmt: skip
    if v.rule == "volume_spike_state":
        return edge(f.rel_volume(20) >= p["r"], seg)
    if v.rule == "expansion_state":
        return edge(f.tr_atr >= p["k"], seg)
    raise KeyError(v.rule)


def vol_bucket(ctx: SignalContext) -> np.ndarray:
    """Family J volatility state at each bar: realised-vol(24) percentile over 30 days,
    frozen terciles 0 low / 1 normal / 2 high, -1 undefined."""
    r = ctx.f.rv_pct(24, PCT_WINDOW[ctx.tf])
    return np.where(np.isfinite(r), np.minimum(np.floor(r * 3), 2), -1).astype(int)


def funding_pct(level: np.ndarray, tf: str) -> np.ndarray:
    return pct_rank(level, PCT_WINDOW[tf])


__all__ = ["Oriented", "SignalContext", "align", "edge", "funding_level", "funding_pct",
           "signal_mask", "take", "vol_bucket", "vol_mask"]  # fmt: skip
