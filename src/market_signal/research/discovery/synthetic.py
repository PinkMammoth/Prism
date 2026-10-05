"""Synthetic intraday markets for the Phase 22 null calibration and planted-edge power tests.

The null market (``discovery_null_v1``) has no edge anywhere (prices are martingales: the
log drift is ``-sigma_t^2 / 2`` so neither side earns a drift) but the texture that makes
intraday research hard: a common BTC factor (alts load on it, so signals co-fire across
coins), volatility clustering (an AR(1) log-volatility state shared by the market plus a
coin-specific part), Student-t(5) innovations, wicks, volume tied to absolute returns and
to the volatility state, and funding that is persistent but unrelated to future returns.
15m bars are generated and aggregated to 1h and 4h exactly as provider bars would be.

Planted effects are **temporary** and act on the harness's own signal definitions: the
strategy's events inside the edge window receive a drift in the trade direction over the
holding window (``plant_drift``), or the volatility after a state onset is scaled
(``plant_volatility``). Everything is seeded and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.research.structure.series import BarSeries, assumed
from market_signal.research.structure.study.data import CoinData

NULL_VERSION = "discovery_null_v1"
M15 = 15 * 60 * 10**9
COINS = ("BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE")
VOL_15M = {"BTC": 0.0026, "ETH": 0.0034, "SOL": 0.0045, "HYPE": 0.0060, "LINK": 0.0045,
           "AAVE": 0.0050}  # fmt: skip


@dataclass
class RawCoin:
    """Log returns, wick sizes and volume of one coin on the 15m grid."""

    coin: str
    open_ns: np.ndarray
    r: np.ndarray
    wick_up: np.ndarray
    wick_dn: np.ndarray
    volume: np.ndarray
    p0: float
    funding_ns: np.ndarray
    funding_rate: np.ndarray


def generate(start, end, seed: int, coins=COINS, funding_hours: int = 8,
             funding_mean: float = 1e-4) -> dict[str, RawCoin]:  # fmt: skip
    rng = np.random.default_rng(seed)
    t0, t1 = _utc(start).value, _utc(end).value
    t0 -= t0 % M15
    open_ns = np.arange(t0, t1, M15, dtype=np.int64)
    n = len(open_ns)
    # market log-vol state: AR(1), half-life ~ 2 days of 15m bars
    phi = 0.5 ** (1 / 192)
    eps = rng.standard_normal(n) * np.sqrt(1 - phi**2) * 0.45
    mv = _ar1(eps, phi)
    f = rng.standard_t(5, n) / np.sqrt(5 / 3) * np.exp(mv)
    out = {}
    for coin in coins:
        e2 = rng.standard_normal(n) * np.sqrt(1 - phi**2) * 0.25
        cv = mv + _ar1(e2, phi)
        s = VOL_15M[coin]
        idio = rng.standard_t(5, n) / np.sqrt(5 / 3) * np.exp(cv)
        if coin == "BTC":
            r = s * f
            var = (s * np.exp(mv)) ** 2
        else:
            r = s * (0.7 * f + np.sqrt(1 - 0.49) * idio)
            var = s * s * (0.49 * np.exp(2 * mv) + 0.51 * np.exp(2 * cv))
        r = r - 0.5 * var  # a martingale price: no drift for either side under the null
        wu = np.abs(rng.standard_normal(n)) * 0.45 * s * np.exp(cv)
        wd = np.abs(rng.standard_normal(n)) * 0.45 * s * np.exp(cv)
        vol = np.exp(rng.standard_normal(n) * 0.35 + 0.8 * np.abs(r) / s + 0.6 * cv) * 1e6
        fh = funding_hours * 3600 * 10**9
        fns = np.arange(t0 - t0 % fh + fh, t1, fh, dtype=np.int64)
        fr = (
            funding_hours
            / 8
            * (funding_mean + 1e-4 * _ar1(rng.standard_normal(len(fns)) * 0.3, 0.95))
        )
        out[coin] = RawCoin(coin, open_ns, r, wu, wd, vol, 100.0 * (1 + list(COINS).index(coin)),
                            fns, fr)  # fmt: skip
    return out


def _ar1(eps: np.ndarray, phi: float) -> np.ndarray:
    """x_t = phi x_{t-1} + eps_t, started in its stationary distribution (an EWM with
    alpha = 1 - phi on eps / alpha, whose first value is set to x_0)."""
    a = 1.0 - phi
    z = eps / a
    z[0] = eps[0] / np.sqrt(1.0 - phi * phi)
    return pd.Series(z).ewm(alpha=a, adjust=False).mean().to_numpy()


def bars_15m(rc: RawCoin) -> pd.DataFrame:
    lc = np.log(rc.p0) + np.cumsum(rc.r)
    c = np.exp(lc)
    o = np.concatenate([[rc.p0], c[:-1]])
    h = np.maximum(o, c) * np.exp(rc.wick_up)
    lo = np.minimum(o, c) * np.exp(-rc.wick_dn)
    return pd.DataFrame({"open_time": pd.to_datetime(rc.open_ns, utc=True),
                         "close_time": pd.to_datetime(rc.open_ns + M15, utc=True),
                         "open": o, "high": h, "low": lo, "close": c, "volume": rc.volume})  # fmt: skip


def aggregate(b15: pd.DataFrame, minutes: int) -> pd.DataFrame:
    step = minutes * 60 * 10**9
    key = b15["open_time"].astype("int64") // step
    g = b15.groupby(key.to_numpy())
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "volume": g["volume"].sum(),
                        "count": g["open"].size()})  # fmt: skip
    out = out[out["count"] == minutes // 15]
    ot = out.index.to_numpy(np.int64) * step
    out["open_time"] = pd.to_datetime(ot, utc=True)
    out["close_time"] = pd.to_datetime(ot + step, utc=True)
    return out.drop(columns="count").reset_index(drop=True)


def coin_data(
    rc: RawCoin, venue: str, starts: dict[str, object], latency_s: float = 60.0
) -> CoinData:
    """15m/1h/4h ``BarSeries`` from the same path; each starts at its manifest data start."""
    b15 = bars_15m(rc)
    series = {}
    for tf, minutes in (("15m", 15), ("1h", 60), ("4h", 240)):
        df = b15 if tf == "15m" else aggregate(b15, minutes)
        df = df[df["open_time"] >= _utc(starts[tf])]
        series[tf] = BarSeries.from_frame(df.reset_index(drop=True), venue=venue, coin=rc.coin,
                                          timeframe=tf, availability=assumed(latency_s),
                                          dataset_key=f"synthetic:{venue}")  # fmt: skip
    return CoinData(venue, rc.coin, f"synthetic:{venue}:{rc.coin}", series, rc.funding_ns,
                    rc.funding_rate)  # fmt: skip


def plant_drift(raw: dict[str, RawCoin], entries: dict[str, list[tuple[int, int]]],
                mu: float, bars: int) -> None:  # fmt: skip
    """Add ``d x mu`` (log) spread over ``bars`` 15m bars from each entry bar (in place)."""
    for coin, items in entries.items():
        rc = raw[coin]
        for e, d in items:
            hi = min(e + bars, len(rc.r))
            if hi > e:
                rc.r[e:hi] += d * mu / bars


def plant_volatility(raw: dict[str, RawCoin], onsets: dict[str, list[int]], factor: float,
                     bars: int) -> None:  # fmt: skip
    """Scale the returns (and wicks) of ``bars`` 15m bars after each onset (in place)."""
    for coin, items in onsets.items():
        rc = raw[coin]
        done = np.zeros(len(rc.r), dtype=bool)
        for e in items:
            sl = slice(e, min(e + bars, len(rc.r)))
            m = ~done[sl]
            idx = np.arange(sl.start, sl.stop)[m]
            rc.r[idx] *= factor
            rc.wick_up[idx] *= factor
            rc.wick_dn[idx] *= factor
            done[idx] = True


__all__ = ["COINS", "NULL_VERSION", "RawCoin", "aggregate", "bars_15m", "coin_data", "generate",
           "plant_drift", "plant_volatility"]  # fmt: skip


def _utc(t) -> pd.Timestamp:
    ts = pd.Timestamp(t)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
