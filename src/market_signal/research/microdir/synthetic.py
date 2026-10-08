"""Deterministic synthetic 1-minute microstructure markets for calibrating Phase 24B.

Each coin gets the stored ``microstructure_1m_v1`` primitives (taker buy/sell notional and
volume, trade OHLC, end-of-minute bid/ask, top-5/top-20 depth, replenishment, OI, funding,
large prints, status, finalization time), shaped exactly like the research loader's output,
so the study runs its real code path on them.

Market model (per minute): a common market factor and an idiosyncratic factor drive both
order-flow imbalance (AR(1)) and returns; price impact is linear in the flow innovation (Kyle
lambda); volatility clusters (AR(1) log-vol) with a UTC session profile; prices are a
martingale in simple returns; replenishment is proportional to the notional that hit a side
(plus cancel/re-add flicker); OI and funding wander independently of future returns. Outages
produce GAP/PARTIAL minutes.

Scenarios plant a forward drift after chosen 15-minute windows whose microstructure is shaped
to the hypothesis (or, for ``candle``, after windows chosen by an OHLCV-only rule):

* ``null``: nothing planted (flow carries no forward information);
* ``continuation``: strong one-sided flow + efficient response + consumed opposing book ->
  drift WITH the flow;
* ``absorption``: strong one-sided flow + weak response + replenishing opposing book + rising
  OI -> drift AGAINST the flow (both sides: buy absorption -> down, sell absorption -> up);
* ``candle``: drift after every OHLCV candle-momentum window, regardless of flow (the effect
  is fully explained by the candle; microstructure must add nothing);
* ``temporary``: the absorption plant only in the last ``recent_days``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.research.microdir import features as ft
from market_signal.research.microdir import spec as sp

SIGMA = {"BTC": 0.0007, "ETH": 0.0009, "SOL": 0.0012, "HYPE": 0.0015, "LINK": 0.0013,
         "AAVE": 0.0014}  # fmt: skip
ACTIVITY = {"BTC": 2.0e6, "ETH": 5.0e5, "SOL": 9.0e4, "HYPE": 5.0e5, "LINK": 3.0e3,
            "AAVE": 4.4e3}  # fmt: skip
SPREAD_BPS = {"BTC": 0.15, "ETH": 0.4, "SOL": 0.9, "HYPE": 0.3, "LINK": 1.2, "AAVE": 1.1}
PRICE0 = {"BTC": 60000.0, "ETH": 2500.0, "SOL": 150.0, "HYPE": 30.0, "LINK": 15.0, "AAVE": 200.0}
SESSION_VOL = (("asia", 0.9), ("europe", 1.0), ("us", 1.2), ("other", 0.85))
T0 = pd.Timestamp("2026-01-05T00:00:00Z")  # a Monday
LAMBDA = 0.55
RHO = 0.6  # per-minute persistence of order-flow imbalance


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str = "null"  # null | continuation | absorption | candle | temporary
    drift: float = 0.0  # log drift over the next hour after a planted window
    per_coin_day: float = 0.0  # planted windows per coin per day
    recent_days: int = 0  # temporary: plant only in the last N days
    days: int = 40
    coins: tuple[str, ...] = sp.COINS
    seed: int = 0
    extra: dict = field(default_factory=dict)


def _ar(rng: np.random.Generator, n: int, rho: float, sd: float = 1.0) -> np.ndarray:
    """x_t = rho x_{t-1} + sd eps_t."""
    return _ar_from(rng.standard_normal(n) * sd, rho)


def _ar_from(eps: np.ndarray, rho: float) -> np.ndarray:
    """x_t = rho x_{t-1} + eps_t (an EWM of eps / (1 - rho) is exactly this recursion)."""
    return pd.Series(eps / (1 - rho)).ewm(alpha=1 - rho, adjust=False).mean().to_numpy()


def _session_mult(n: int) -> np.ndarray:
    hours = ((np.arange(n) // 60) % 24).astype(int)
    m = np.ones(n)
    for name, mult in SESSION_VOL:
        a, b = next((a, b) for nm, a, b in sp.SESSIONS if nm == name)
        m[(hours >= a) & (hours < b)] = mult
    return m


def _plant_windows(
    rng: np.random.Generator, sc: Scenario, nw: int
) -> tuple[np.ndarray, np.ndarray]:
    if sc.kind in ("null", "candle") or sc.per_coin_day <= 0:
        return np.zeros(0, int), np.zeros(0, int)
    per_day = 1440 // sp.SIGNAL_MINUTES
    first = 4 * per_day  # after the normalization warmup
    if sc.kind == "temporary":
        first = max(first, nw - sc.recent_days * per_day)
    k = round(sc.per_coin_day * (nw - first) / per_day)
    cand = np.arange(first, nw - 6, 6)  # spaced >= 90 min apart: plants never overlap
    w = np.sort(rng.choice(cand, size=min(k, len(cand)), replace=False))
    s = rng.choice([-1, 1], size=len(w))
    return w, s


def _arrays(rng: np.random.Generator, coin: str, n: int, common: dict, sc: Scenario,
            plants: tuple[np.ndarray, np.ndarray]) -> dict:  # fmt: skip
    sig = SIGMA[coin] * np.exp(_ar(rng, n, 0.995, 0.02)) * _session_mult(n)
    act = ACTIVITY[coin] * np.exp(0.5 * rng.standard_normal(n)) * (sig / SIGMA[coin])
    # order-flow imbalance: AR(RHO) common + idiosyncratic + one-minute burst innovations
    spikes = np.where(rng.random(n) < 0.004, rng.choice([-6.0, 6.0], size=n), 0.0)
    burst = spikes != 0  # natural bursts (the persistence contrast)
    z = 0.5 * common["flow"] + 0.87 * _ar(rng, n, RHO, 0.8) + _ar_from(spikes, RHO)
    noise = 0.6 * common["noise"] + 0.8 * rng.standard_normal(n)
    kap_a = np.clip(0.6 + _ar(rng, n, 0.9, 0.1), 0.05, 2.5)
    kap_b = np.clip(0.6 + _ar(rng, n, 0.9, 0.1), 0.05, 2.5)
    d_oi = 0.0002 * rng.standard_normal(n)
    act[burst] *= 5.0
    extra_r = np.zeros(n)
    W = sp.SIGNAL_MINUTES
    w, s = plants
    for wi, si in zip(w, s, strict=True):
        a, b = wi * W, wi * W + W
        z[a:b] = si * 2.5
        act[a:b] *= 2.0
        if sc.kind == "continuation":
            extra_r[a:b] += si * 1.2 * sig[a:b] * np.sqrt(W) / W
            (kap_a if si > 0 else kap_b)[a:b] = 0.1
            d_oi[a:b] += 0.0001
        else:  # absorption / temporary: cancel the impact, opposing side replenishes, OI up
            noise[a:b] *= 0.3
            (kap_a if si > 0 else kap_b)[a:b] = 1.8
            d_oi[a:b] += 0.0004
        e = b  # entry minute (opens at the window close); drift starts after its close
        sign = si if sc.kind == "continuation" else -si
        extra_r[e + 1 : e + 61] += sign * sc.drift / 60
    bf = 1 / (1 + np.exp(-0.9 * z))
    buy, sell = act * bf, act * (1 - bf)
    # Kyle martingale: price responds to the UNEXPECTED flow (the AR innovation), so persistent
    # flow is not, by itself, a forecast of later returns
    innov = z - RHO * np.concatenate([[0.0], z[:-1]])
    impact = np.full(n, LAMBDA)
    if sc.kind != "continuation" and len(w):
        impact[np.isin(np.arange(n) // W, w)] = 0.0  # absorbed: flow buys no price
    r = sig * (impact * innov / 0.8 + np.sqrt(1 - LAMBDA**2) * noise) + extra_r
    if "extra_r" in common:
        r = r + common["extra_r"].get(coin, 0.0)
    return {"sig": sig, "act": act, "bf": bf, "buy": buy, "sell": sell, "r": r,
            "kap_a": kap_a, "kap_b": kap_b, "d_oi": d_oi}  # fmt: skip


def _frame(rng: np.random.Generator, coin: str, a: dict, n: int) -> pd.DataFrame:
    sig, r = a["sig"], a["r"]
    lr = r - 0.5 * sig**2  # martingale in simple returns
    mid = PRICE0[coin] * np.exp(np.cumsum(lr))
    prev = np.concatenate([[PRICE0[coin]], mid[:-1]])
    half = SPREAD_BPS[coin] / 2e4 * mid
    wick = np.abs(rng.standard_normal((2, n))) * 0.4 * sig * mid
    hi = np.maximum(prev, mid) + wick[0]
    lo = np.minimum(prev, mid) - wick[1]
    A = ACTIVITY[coin]
    rep_a = a["kap_a"] * a["buy"] * np.exp(0.3 * rng.standard_normal(n)) + 0.05 * A * np.abs(
        rng.standard_normal(n))  # fmt: skip
    rep_b = a["kap_b"] * a["sell"] * np.exp(0.3 * rng.standard_normal(n)) + 0.05 * A * np.abs(
        rng.standard_normal(n))  # fmt: skip
    la = pd.Series(0.2 * (a["kap_a"] - 0.6) * a["buy"] / A + 0.05 * rng.standard_normal(n))
    lb = pd.Series(0.2 * (a["kap_b"] - 0.6) * a["sell"] / A + 0.05 * rng.standard_normal(n))
    ask5 = 0.5 * A * np.exp(la.ewm(alpha=0.02, adjust=False).mean().to_numpy() * 10)
    bid5 = 0.5 * A * np.exp(lb.ewm(alpha=0.02, adjust=False).mean().to_numpy() * 10)
    oi = 1e9 * A / 2e6 * np.exp(np.cumsum(a["d_oi"]))
    funding = 1.25e-5 + _ar(rng, n, 0.9995, 2e-7)
    opens = T0 + pd.to_timedelta(np.arange(n), unit="min")
    status = np.full(n, "COMPLETE", dtype=object)
    for _ in range(max(1, n // (1440 * 4))):  # an outage every ~4 days
        st = int(rng.integers(0, n - 30))
        status[st : st + int(rng.integers(3, 20))] = "GAP"
    status[(rng.random(n) < 0.002) & (status == "COMPLETE")] = "PARTIAL"
    lp_on = np.arange(n) >= 3 * 1440
    lp_n = (rng.random(n) < 0.1) & lp_on
    lp_ntl = np.where(lp_n, a["act"] * 0.3, 0.0)
    lp_buy = np.where(rng.random(n) < a["bf"], lp_ntl, 0.0)
    fin = opens + pd.Timedelta(minutes=1, seconds=5)
    df = pd.DataFrame({
        "minute_open": opens, "coin": coin, "status": status,
        "buy_ntl": a["buy"], "sell_ntl": a["sell"], "buy_vol": a["buy"] / mid,
        "sell_vol": a["sell"] / mid, "first_px": prev, "last_px": mid, "high_px": hi,
        "low_px": lo, "bid_end": mid - half, "ask_end": mid + half, "bid5_end": bid5,
        "ask5_end": ask5, "bid20_end": 3 * bid5, "ask20_end": 3 * ask5,
        "bid_replenish": rep_b, "ask_replenish": rep_a, "oi_end": oi, "funding_end": funding,
        "lp_threshold": np.where(lp_on, A * 0.25, np.nan), "lp_ntl": np.where(lp_on, lp_ntl, np.nan),
        "lp_buy_ntl": np.where(lp_on, lp_buy, np.nan), "spread_bps_mean": SPREAD_BPS[coin],
        "available_at": fin, "finalized_at": fin,
    })  # fmt: skip
    gap = df["status"] == "GAP"
    keep = [c for c in df.columns if c not in ("minute_open", "coin", "status", "available_at",
                                               "finalized_at")]  # fmt: skip
    df.loc[gap, keep] = np.nan
    return df


def market(sc: Scenario) -> dict[str, pd.DataFrame]:
    """One 1-minute frame per coin (deterministic in ``sc``)."""
    with np.errstate(all="ignore"):
        return _market(sc)


def _market(sc: Scenario) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(sc.seed)
    n = sc.days * 1440
    nw = n // sp.SIGNAL_MINUTES
    common = {"flow": _ar(rng, n, RHO, 0.8), "noise": rng.standard_normal(n)}
    out, arrays = {}, {}
    for coin in sc.coins:
        crng = np.random.default_rng([sc.seed, sp.COINS.index(coin)])
        plants = _plant_windows(crng, sc, nw)
        arrays[coin] = (crng, _arrays(crng, coin, n, common, sc, plants))
    if sc.kind == "candle":
        # drift after every OHLCV candle-momentum window, computed on the unplanted market
        for coin in sc.coins:
            crng, a = arrays[coin]
            base = _frame(np.random.default_rng([sc.seed, 99, sp.COINS.index(coin)]), coin, a, n)
            f, _ = ft.windows(base, coin)
            tw = f["twin_candle_momentum"].to_numpy()
            extra = np.zeros(n)
            for wi in np.flatnonzero(tw != 0):
                e = (wi + 1) * sp.SIGNAL_MINUTES
                extra[e + 1 : e + 61] += tw[wi] * sc.drift / 60
            a["r"] = a["r"] + extra
    for coin in sc.coins:
        crng, a = arrays[coin]
        out[coin] = _frame(np.random.default_rng([sc.seed, 99, sp.COINS.index(coin)]), coin, a, n)
    return out
