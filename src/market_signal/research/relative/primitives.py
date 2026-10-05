"""Relative-strength primitives v1: causal cross-asset quantities on ONE venue and timeframe.

Every quantity at grid bar ``t`` reads bars ``<= t`` only and is known at the availability of
the newest bar it reads (``Panel.ready``, Phase 15/16 semantics: assumed latency for
backfilled history). Nothing is forward-filled: a missing bar makes every window that
contains it undefined (NaN), and an asset that is not listed (or lacks history) at ``t``
is simply absent from that bar's cross-section.

Definitions (``n``-bar windows ending at ``t``; ``x`` = one-bar simple return):

- raw return ``R_a(L) = c_a[t] / c_a[t-L] - 1`` (the Lab's ``ret_L``), every bar of the
  window present;
- BTC-relative return ``RS_a(L) = R_a(L) - R_BTC(L)``;
- market-relative return ``R_a(L) - mean_j R_j(L)`` over the alts with a defined ``R_j(L)``
  at ``t`` (equal weight, the asset itself included: a cross-sectional demeaning);
- rolling beta / correlation to BTC over the ``W`` one-bar returns ending at ``t``
  (sample moments, all ``W`` pairs present). Beta is undefined when BTC's per-bar standard
  deviation is below ``MIN_REF_STD`` (near-zero variance), correlation when either
  variance is zero;
- residual ``Res_a(L) = R_a(L) - beta_a(t-L) * R_BTC(L)``: the beta is estimated on the
  ``W`` bars that END where the residual window starts, so it is the beta "historically
  expected" before the move, never fitted on the move itself;
- z-scores divide each L-bar quantity by its one-bar standard deviation over that same
  prior window, times ``sqrt(L)``: raw ``sd(x_a)``, BTC-relative ``sd(x_a - x_BTC)``,
  residual ``sd(x_a - beta x_BTC) = sd(x_a) sqrt(1 - rho^2)``, market-relative
  ``sd(x_a - x_basket)``.

Cross-sectional ranks cover the eligible alts only (BTC is the reference, never ranked
against itself); rank 1 is the strongest. Ranking by raw, BTC-relative or
market-relative return is IDENTICAL at a timestamp (they differ by a term common to all
alts); only the beta-adjusted residual can reorder the cross-section.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from market_signal.research.structure.series import NAT, BarSeries

PRIMITIVES_VERSION = "relative_strength_primitives_v1"
REFERENCE = "BTC"
MIN_REF_STD = 1e-5  # per-bar BTC return std below which beta is undefined


# --------------------------------------------------------------------------- panel


@dataclass(frozen=True, eq=False)
class Panel:
    """Bars of several coins of ONE venue and timeframe on the reference's regular grid.

    ``o``/``c`` are NaN and ``ready`` is NAT where a coin has no bar (not listed yet,
    missing, or outside its data). The grid spans the reference's first to last bar."""

    venue: str
    timeframe: str
    reference: str
    alts: tuple[str, ...]
    open_time: np.ndarray  # int64 ns
    close_time: np.ndarray
    o: dict[str, np.ndarray]
    c: dict[str, np.ndarray]
    ready: dict[str, np.ndarray]  # per coin: when everything up to bar t was available
    present: dict[str, np.ndarray] = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "present", {k: np.isfinite(v) for k, v in self.c.items()})

    def __len__(self) -> int:
        return len(self.open_time)

    @property
    def coins(self) -> tuple[str, ...]:
        return (self.reference, *self.alts)

    @classmethod
    def from_series(cls, series: Mapping[str, BarSeries], reference: str = REFERENCE) -> Panel:
        if reference not in series:
            raise ValueError(f"the reference {reference} is required")
        ref = series[reference]
        venues = {s.prov.venue for s in series.values()}
        tfs = {s.prov.timeframe for s in series.values()}
        if len(venues) != 1 or len(tfs) != 1:
            raise ValueError("a panel holds ONE venue and ONE timeframe (never pooled)")
        step = ref.step
        if len(ref):
            grid = np.arange(ref.open_time[0], ref.open_time[-1] + step, step, dtype=np.int64)
        else:
            grid = np.array([], dtype=np.int64)
        o, c, ready = {}, {}, {}
        for coin, s in series.items():
            oo, cc = np.full(len(grid), np.nan), np.full(len(grid), np.nan)
            rr = np.full(len(grid), NAT, dtype=np.int64)
            if len(s) and len(grid):
                pos = (s.open_time - grid[0]) // step
                on = ((s.open_time - grid[0]) % step == 0) & (pos >= 0) & (pos < len(grid))
                p = pos[on]
                oo[p], cc[p], rr[p] = s.o[on], s.c[on], s.ready_at[on]
            o[coin], c[coin], ready[coin] = oo, cc, rr
        alts = tuple(sorted(k for k in series if k != reference))
        return cls(venue=next(iter(venues)), timeframe=next(iter(tfs)), reference=reference,
                   alts=alts, open_time=grid, close_time=grid + step, o=o, c=c, ready=ready)  # fmt: skip


# --------------------------------------------------------------------------- windows


def lag(x: np.ndarray, k: int) -> np.ndarray:
    """``out[t] = x[t - k]`` (NaN / NAT / False before the start)."""
    x = np.asarray(x)
    if k == 0:
        return x.copy()
    fill = NAT if x.dtype == np.int64 else (False if x.dtype == bool else np.nan)
    out = np.full(len(x), fill, dtype=x.dtype)
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


def complete(present: np.ndarray, n: int) -> np.ndarray:
    """True where every bar of the ``n``-bar window ending at ``t`` is present."""
    out = np.zeros(len(present), dtype=bool)
    if n <= len(present):
        out[n - 1 :] = sliding_window_view(present, n).all(axis=1)
    return out


def one_bar_returns(c: np.ndarray) -> np.ndarray:
    """``x[t] = c[t] / c[t-1] - 1``; NaN unless both bars are present."""
    return c / lag(c, 1) - 1.0


def window_return(c: np.ndarray, n: int) -> np.ndarray:
    """``c[t] / c[t-n] - 1`` with every bar of ``[t-n, t]`` present (gaps are undefined)."""
    r = c / lag(c, n) - 1.0
    return np.where(complete(np.isfinite(c), n + 1), r, np.nan)


@dataclass(frozen=True)
class Moments:
    """Sample (ddof = 1) moments of the ``w`` one-bar return pairs ending at each ``t``."""

    w: int
    var_a: np.ndarray
    var_b: np.ndarray
    cov: np.ndarray

    @cached_property
    def beta(self) -> np.ndarray:
        ok = np.sqrt(self.var_b) >= MIN_REF_STD
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(ok, self.cov / self.var_b, np.nan)

    @cached_property
    def corr(self) -> np.ndarray:
        den = np.sqrt(self.var_a * self.var_b)
        with np.errstate(invalid="ignore", divide="ignore"):
            r = np.where(den > 0, self.cov / den, np.nan)
        return np.clip(r, -1.0, 1.0)

    def sd_of(self, wa: float, wb: np.ndarray | float) -> np.ndarray:
        """Std of ``wa * x_a + wb * x_b`` over the window."""
        v = wa * wa * self.var_a + wb * wb * self.var_b + 2 * wa * wb * self.cov
        return np.sqrt(np.maximum(v, 0.0))

    @cached_property
    def sd_residual(self) -> np.ndarray:
        """``sd(x_a - beta x_b) = sd(x_a) sqrt(1 - rho^2)`` (closed form for the OLS beta)."""
        r = self.corr
        return np.sqrt(np.maximum(self.var_a * (1.0 - r * r), 0.0))

    def lagged(self, k: int) -> Moments:
        return Moments(self.w, lag(self.var_a, k), lag(self.var_b, k), lag(self.cov, k))


def rolling_moments(xa: np.ndarray, xb: np.ndarray, w: int) -> Moments:
    """Exact two-pass moments of each ``w``-window ending at ``t`` (causal: bars ``<= t``).
    Undefined unless all ``w`` pairs are finite. Computed per window (no running sums), so
    a value never depends on bars outside its own window."""
    n = len(xa)
    va, vb, cv = (np.full(n, np.nan) for _ in range(3))
    if w < 2:
        raise ValueError("a dispersion needs at least two bars")
    if n >= w:
        a, b = sliding_window_view(xa, w), sliding_window_view(xb, w)
        ok = np.isfinite(a).all(axis=1) & np.isfinite(b).all(axis=1)
        a, b = a[ok], b[ok]
        da = a - a.mean(axis=1, keepdims=True)
        db = b - b.mean(axis=1, keepdims=True)
        idx = np.flatnonzero(ok) + w - 1
        va[idx] = (da * da).sum(axis=1) / (w - 1)
        vb[idx] = (db * db).sum(axis=1) / (w - 1)
        cv[idx] = (da * db).sum(axis=1) / (w - 1)
    return Moments(w, va, vb, cv)


# --------------------------------------------------------------------------- pair features


@dataclass(frozen=True, eq=False)
class PairFeatures:
    """One alt versus the reference at lookback ``L`` and beta/correlation window ``W``."""

    coin: str
    L: int
    W: int
    r: np.ndarray  # R_a(L)
    r_ref: np.ndarray  # R_BTC(L)
    rel: np.ndarray  # R_a(L) - R_BTC(L)
    mkt: np.ndarray  # R_a(L) - basket(L)
    res: np.ndarray  # R_a(L) - beta(t-L) R_BTC(L)
    beta: np.ndarray  # beta over the W bars ending at t (forward-target hedge ratio)
    corr: np.ndarray  # corr over the W bars ending at t
    beta_prior: np.ndarray  # beta over the W bars ending at t-L
    corr_prior: np.ndarray  # corr over the W bars ending at t-L
    z_raw: np.ndarray
    z_rel: np.ndarray
    z_mkt: np.ndarray
    z_res: np.ndarray
    ready: np.ndarray  # availability of every value at t (asset and reference)

    @property
    def eligible(self) -> np.ndarray:
        """Defined at ``t``: listed, W + L + 1 contiguous bars, non-degenerate BTC variance."""
        return np.isfinite(self.z_rel) & np.isfinite(self.z_res) & np.isfinite(self.z_raw)

    def z(self, measure: str) -> np.ndarray:
        return {"raw": self.z_raw, "rel": self.z_rel, "mkt": self.z_mkt, "res": self.z_res}[measure]

    def value(self, measure: str) -> np.ndarray:
        return {"raw": self.r, "rel": self.rel, "mkt": self.mkt, "res": self.res}[measure]


def basket(panel: Panel, L: int) -> tuple[np.ndarray, np.ndarray]:
    """(equal-weight alt basket L-bar return, its one-bar return) over the alts defined at t."""
    rs = np.vstack([window_return(panel.c[a], L) for a in panel.alts]) if panel.alts else None
    xs = np.vstack([one_bar_returns(panel.c[a]) for a in panel.alts]) if panel.alts else None
    if rs is None:
        nan = np.full(len(panel), np.nan)
        return nan, nan
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN columns (no alt defined)
        return np.nanmean(rs, axis=0), np.nanmean(xs, axis=0)


def pair_features(panel: Panel, coin: str, L: int, W: int,
                  bask: tuple[np.ndarray, np.ndarray] | None = None) -> PairFeatures:  # fmt: skip
    if coin == panel.reference:
        raise ValueError("the reference is not measured relative to itself")
    ca, cb = panel.c[coin], panel.c[panel.reference]
    xa, xb = one_bar_returns(ca), one_bar_returns(cb)
    ra, rb = window_return(ca, L), window_return(cb, L)
    m = rolling_moments(xa, xb, W)
    mp = m.lagged(L)
    beta_p = mp.beta
    res = ra - beta_p * rb
    rb_bask, xb_bask = bask if bask is not None else basket(panel, L)
    xm = xa - xb_bask
    mm = rolling_moments(xm, xb, W).lagged(L)  # only var_a (= var of x_a - x_basket) is used
    sq = np.sqrt(L)
    with np.errstate(invalid="ignore", divide="ignore"):
        z_raw = np.where(mp.var_a > 0, ra / (np.sqrt(mp.var_a) * sq), np.nan)
        sd_rel = mp.sd_of(1.0, -1.0)
        z_rel = np.where(sd_rel > 0, (ra - rb) / (sd_rel * sq), np.nan)
        sd_res = mp.sd_residual
        z_res = np.where((sd_res > 0) & np.isfinite(beta_p), res / (sd_res * sq), np.nan)
        z_mkt = np.where(mm.var_a > 0, (ra - rb_bask) / (np.sqrt(mm.var_a) * sq), np.nan)
    rd = np.maximum(panel.ready[coin], panel.ready[panel.reference])
    return PairFeatures(coin=coin, L=L, W=W, r=ra, r_ref=rb, rel=ra - rb, mkt=ra - rb_bask,
                        res=res, beta=m.beta, corr=m.corr, beta_prior=beta_p, corr_prior=mp.corr,
                        z_raw=z_raw, z_rel=z_rel, z_mkt=z_mkt, z_res=z_res, ready=rd)  # fmt: skip


# --------------------------------------------------------------------------- events


def cross_up(z: np.ndarray, thr: float) -> np.ndarray:
    """Edge trigger: ``z[t] >= thr`` and ``z[t-1] < thr`` (both defined). A persistent
    excursion fires once, on the bar it begins."""
    prev = lag(z, 1)
    return np.isfinite(z) & np.isfinite(prev) & (z >= thr) & (prev < thr)


def cross_down(z: np.ndarray, thr: float) -> np.ndarray:
    prev = lag(z, 1)
    return np.isfinite(z) & np.isfinite(prev) & (z <= -thr) & (prev > -thr)


def edge(cond: np.ndarray, defined: np.ndarray) -> np.ndarray:
    """``cond[t]`` true, ``cond[t-1]`` false, and both bars defined."""
    pd_ = lag(defined, 1)
    return cond & defined & pd_ & ~lag(cond, 1)


@dataclass(frozen=True, eq=False)
class Breakdown:
    """Correlation breakdown at short window ``S`` after a high long-window correlation:
    ``corr_W(t-S) >= rho_hi`` and ``corr_S(t) <= corr_W(t-S) - delta``, edge-triggered.
    ``sign`` = direction of the residual move over those S bars (vs the prior beta)."""

    event: np.ndarray
    sign: np.ndarray
    rho_prior: np.ndarray
    rho_short: np.ndarray
    res_s: np.ndarray


def breakdown(panel: Panel, coin: str, W: int, S: int, rho_hi: float, delta: float) -> Breakdown:
    ca, cb = panel.c[coin], panel.c[panel.reference]
    xa, xb = one_bar_returns(ca), one_bar_returns(cb)
    long = rolling_moments(xa, xb, W).lagged(S)
    short = rolling_moments(xa, xb, S)
    rho_p, rho_s = long.corr, short.corr
    defined = np.isfinite(rho_p) & np.isfinite(rho_s)
    cond = defined & (rho_p >= rho_hi) & (rho_s <= rho_p - delta)
    res_s = window_return(ca, S) - long.beta * window_return(cb, S)
    sign = np.where(np.isfinite(res_s), np.sign(res_s), 0).astype(int)
    ev = edge(cond, defined) & (sign != 0)
    return Breakdown(ev, sign, rho_p, rho_s, res_s)


# --------------------------------------------------------------------------- cross-section


@dataclass(frozen=True, eq=False)
class CrossSection:
    """Ranks of the eligible alts per grid bar (rank 1 = largest value; 0 = not ranked)."""

    alts: tuple[str, ...]
    rank: np.ndarray  # [T, A] int
    n: np.ndarray  # [T] universe size

    def top(self, min_n: int) -> np.ndarray:
        return (self.rank == 1) & (self.n >= min_n)[:, None]

    def bottom(self, min_n: int) -> np.ndarray:
        return (self.rank == self.n[:, None]) & (self.rank > 0) & (self.n >= min_n)[:, None]

    def percentile(self) -> np.ndarray:
        """0 = weakest, 1 = strongest; NaN where not ranked or the universe has < 2 alts."""
        with np.errstate(invalid="ignore", divide="ignore"):
            p = (self.n[:, None] - self.rank) / (self.n[:, None] - 1)
        return np.where((self.rank > 0) & (self.n[:, None] >= 2), p, np.nan)


def rank_cross_section(values: np.ndarray, eligible: np.ndarray,
                       alts: tuple[str, ...]) -> CrossSection:  # fmt: skip
    """``values``/``eligible`` are [T, A] in ``alts`` order. Ties break by alt order (a
    deterministic, value-free rule). Ineligible alts never occupy a rank."""
    v = np.where(eligible & np.isfinite(values), values, -np.inf)
    ok = eligible & np.isfinite(values)
    order = np.argsort(-v, axis=1, kind="stable")
    rank = np.zeros_like(order)
    rows = np.arange(v.shape[0])[:, None]
    rank[rows, order] = np.arange(1, v.shape[1] + 1)[None, :]
    rank = np.where(ok, rank, 0)
    return CrossSection(alts, rank, ok.sum(axis=1))


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman correlation of two equal-length vectors (average ranks for ties)."""
    if len(a) < 2:
        return np.nan
    ra, rb = _avg_rank(a), _avg_rank(b)
    sa, sb = ra.std(), rb.std()
    if sa == 0 or sb == 0:
        return np.nan
    return float(((ra - ra.mean()) * (rb - rb.mean())).mean() / (sa * sb))


def _avg_rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="stable")
    r = np.empty(len(x))
    r[order] = np.arange(1, len(x) + 1)
    for v in np.unique(x):
        m = x == v
        if m.sum() > 1:
            r[m] = r[m].mean()
    return r


# --------------------------------------------------------------------------- forward targets


def entry_index(panel: Panel, after_ns: np.ndarray) -> np.ndarray:
    """The first grid bar opening at/after each availability instant (conservative entry)."""
    return np.searchsorted(panel.open_time, np.asarray(after_ns, dtype=np.int64), side="left")


def forward_return(panel: Panel, coin: str, k: np.ndarray, h: int) -> np.ndarray:
    """Open of bar ``k`` to the close of bar ``k + h - 1``; NaN unless every bar is present."""
    k = np.asarray(k)
    n = len(panel)
    out = np.full(len(k), np.nan)
    last = k + h - 1
    ok = (k >= 0) & (last < n)
    if not ok.any():
        return out
    full = complete(panel.present[coin], h)  # window of h bars ending at `last`
    ok[ok] &= full[last[ok]]
    out[ok] = panel.c[coin][last[ok]] / panel.o[coin][k[ok]] - 1.0
    return out


__all__ = [
    "MIN_REF_STD",
    "PRIMITIVES_VERSION",
    "REFERENCE",
    "Breakdown",
    "CrossSection",
    "Moments",
    "PairFeatures",
    "Panel",
    "basket",
    "breakdown",
    "complete",
    "cross_down",
    "cross_up",
    "edge",
    "entry_index",
    "forward_return",
    "lag",
    "one_bar_returns",
    "pair_features",
    "rank_cross_section",
    "rolling_moments",
    "spearman",
    "window_return",
]
