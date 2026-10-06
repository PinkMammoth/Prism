"""Null calibration of the Phase 19 harness on a synthetic market (no real data).

The synthetic market has NO forward-return information in OI or funding, but it has the
structure that makes OI inference treacherous:

- a BTC factor with slowly varying volatility regimes and fat tails (Student t, df 4);
- alts = beta x BTC + a shared alt factor + idiosyncratic noise: co-timed signals across
  assets (one market shock moves every coin);
- coin OI as a persistent log random walk (AR(1) increments) whose innovations are
  CONTEMPORANEOUSLY correlated with the coin's own return (0.3) and scale with the current
  volatility regime, i.e. OI that "explains" price after the fact and is louder in volatile
  hours, but never predicts the next return;
- funding as slow, independent regimes settled every 8 h (HYPE every 4 h);
- an optional Hyperliquid comparison series (``hl``: ``"none"``, ``"sparse"``, ``"dense"``).

``planted`` > 0 adds a real effect: each coin's next-hour idiosyncratic return loads on the
mean of its previous six OI innovations, so a harness that works must find it.

``calibrate`` runs the full evaluation (central parameters) on several seeds and reports
the share of p-values below 0.05 / 0.10 for the block-clustered test and for the
event-level iid test, and BH discoveries per family. Phase 19 requires this BEFORE the real
run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.research.oiprice import primitives as op
from market_signal.research.oiprice.study.data import OiCoinData
from market_signal.research.oiprice.study.spec import (
    OiStudyDefinition,
    OiStudyManifest,
    families_spec,
    semantics,
)
from market_signal.research.structure.series import NS, BarSeries, assumed
from market_signal.research.structure.study.spec import CostRef, DatasetRef

SIGMA = 0.006
BETAS = {"ETH": 1.1, "SOL": 1.3, "HYPE": 1.4, "LINK": 1.2, "AAVE": 1.25}


def _innov(rng: np.random.Generator, n: int) -> np.ndarray:
    return rng.standard_t(4, n) / np.sqrt(2.0)


def market(coins, n: int, seed: int, planted: float = 0.0) -> dict[str, dict[str, np.ndarray]]:
    """Per coin: one-bar log returns ``x`` and OI log increments ``oi``."""
    rng = np.random.default_rng(seed)
    lv = np.zeros(n)
    for i in range(1, n):
        lv[i] = 0.995 * lv[i - 1] + 0.07 * rng.normal()
    vol = SIGMA * np.exp(lv - lv.mean())
    xb = vol * _innov(rng, n)
    alt_f = 0.5 * vol * _innov(rng, n)
    out = {}
    for c in coins:
        idio_e = _innov(rng, n)
        eta = rng.normal(size=n)
        x = xb if c == "BTC" else BETAS.get(c, 1.2) * xb + alt_f + 0.8 * vol * idio_e
        own = x / np.maximum(np.abs(vol), 1e-12)
        innov = (0.3 * np.clip(own, -4, 4) / np.sqrt(1.5) + np.sqrt(1 - 0.09) * eta) * (vol / SIGMA)
        oi = np.zeros(n)
        for i in range(1, n):
            oi[i] = 0.5 * oi[i - 1] + 0.003 * innov[i]
        if planted:
            m6 = pd.Series(eta).rolling(6).mean().shift(1).fillna(0.0).to_numpy()
            x = x + planted * vol * m6 * np.sqrt(6)
        out[c] = {"x": x, "oi": oi}
    return out


def coin_data(defn: OiStudyDefinition, seed: int, planted: float = 0.0, hl: str = "none"):
    man = defn.manifest
    w = man.window
    step = pd.Timedelta("1h")
    ot = pd.date_range(pd.Timestamp(w.data_start), pd.Timestamp(w.event_end), freq=step,
                       inclusive="left")  # fmt: skip
    ot = ot[ot + step <= pd.Timestamp(w.event_end)]
    paths = market(w.coins, len(ot), seed, planted)
    rng = np.random.default_rng(seed + 7)
    prim, comp = {}, {}
    for c in w.coins:
        x, inc = paths[c]["x"], paths[c]["oi"]
        cl = np.exp(np.log(100.0) + np.cumsum(x))
        op_ = np.concatenate([[100.0], cl[:-1]])
        hi = np.maximum(op_, cl) * (1 + np.abs(rng.normal(0, 0.002, len(cl))))
        lo = np.minimum(op_, cl) * (1 - np.abs(rng.normal(0, 0.002, len(cl))))
        df = pd.DataFrame({"open_time": ot, "close_time": ot + step, "open": op_, "high": hi,
                           "low": lo, "close": cl, "volume": 1000.0})  # fmt: skip
        bars = BarSeries.from_frame(df, venue=w.venue, coin=c, timeframe="1h",
                                    availability=assumed(man.assumed_bar_latency_s),
                                    dataset_key="dataset_synthetic")  # fmt: skip
        oi_level = 1e6 * np.exp(np.cumsum(inc))
        close_ns = pd.DatetimeIndex(ot + step).as_unit("ns").asi8
        # the retained selection is [oi_start, event_end) on observed_at
        keep = (close_ns >= pd.Timestamp(w.oi_start).value) & (
            close_ns < pd.Timestamp(w.event_end).value
        )
        lat = int(man.assumed_oi_latency_s * NS)
        oi = op.OiSeries(w.venue, c, close_ns[keep], oi_level[keep], (oi_level * cl)[keep],
                         "provider:sumOpenInterestValue", close_ns[keep] + lat)  # fmt: skip
        every = "4h" if c == "HYPE" else "8h"
        ft = pd.date_range(pd.Timestamp(w.funding_start), pd.Timestamp(w.event_end), freq=every,
                           inclusive="left")  # fmt: skip
        f = np.zeros(len(ft))
        for i in range(1, len(ft)):
            f[i] = 0.97 * f[i - 1] + 2e-5 * rng.normal()
        rate = (1e-4 + f) * (0.5 if c == "HYPE" else 1.0)
        prim[c] = OiCoinData(w.venue, c, "dataset_" + "0" * 64, bars,
                             pd.DatetimeIndex(ft).as_unit("ns").asi8.astype(np.int64), rate,
                             oi, int(keep.sum()))  # fmt: skip
        if c in man.comparison.coins:
            comp[c] = _hl(man, c, close_ns[keep], oi_level[keep], cl[keep], rng, hl)
    return prim, comp


def _hl(man, coin, t_ns, oi_bn, px, rng, mode: str) -> OiCoinData:
    if mode == "none" or not len(t_ns):
        s = op.OiSeries.empty("hyperliquid", coin)
    else:
        every = 1 if mode == "dense" else 37
        idx = np.arange(0, len(t_ns), every)
        jitter = rng.integers(-600, 0, len(idx)) * NS
        lvl = 0.2 * oi_bn[idx] * np.exp(rng.normal(0, 0.003, len(idx)))
        tt = t_ns[idx] + jitter
        s = op.OiSeries("hyperliquid", coin, tt, lvl, lvl * px[idx], "open_interest*mark_px", tt)
    return OiCoinData("hyperliquid", coin, "dataset_" + "1" * 64, None,
                      np.array([], np.int64), np.array([]), s, len(s))  # fmt: skip


def definition(man: OiStudyManifest) -> OiStudyDefinition:
    """A synthetic definition: frozen-looking costs and placeholder dataset IDs (never
    registered; calibration and tests only)."""
    vc = man.venue_coins()
    costs = tuple(CostRef(venue=v, coin=c, fee_bps=5.0, slippage_bps=2.0 if c in ("BTC", "ETH") else 6.0)
                  for v, c in vc)  # fmt: skip
    ds = tuple(DatasetRef(venue=v, coin=c, dataset_id="dataset_" + f"{i:064x}")
               for i, (v, c) in enumerate(vc))  # fmt: skip
    return OiStudyDefinition(manifest=man, families=families_spec(), costs=costs, datasets=ds,
                             semantics=semantics())  # fmt: skip


def central_only(man: OiStudyManifest) -> OiStudyManifest:
    return man.model_copy(update={"axes": ()})


def run_synthetic(man: OiStudyManifest, seed: int, planted: float = 0.0, hl: str = "none") -> dict:
    from market_signal.research.oiprice.study.run import evaluate

    defn = definition(man)
    prim, comp = coin_data(defn, seed, planted, hl)

    def load(venue: str, coin: str) -> OiCoinData:
        return prim[coin] if venue == man.window.venue else comp[coin]

    payload, meta = evaluate(defn, load)
    return {"payload": payload, "meta": meta}


def pvalues(payload: dict) -> pd.DataFrame:
    rows = []
    for fam, fd in (payload["primary"].get("families") or {}).items():
        for r in fd["members"]:
            if r.get("testable"):
                rows.append({"family": fam, "member": r["member"], "kind": r["kind"],
                             "target": r["target"], "p": r["p_value"], "q": r.get("q_value"),
                             "p_naive": r.get("p_naive_iid")})  # fmt: skip
    return pd.DataFrame(rows, columns=["family", "member", "kind", "target", "p", "q", "p_naive"])


def calibrate(man: OiStudyManifest, seeds=(1, 2, 3, 4), planted: float = 0.0) -> dict:
    man = central_only(man)
    frames, secs = [], []
    for s in seeds:
        out = run_synthetic(man, s, planted)
        p = pvalues(out["payload"])
        p["seed"] = s
        frames.append(p)
        secs.append(out["meta"]["wall_seconds"])
    df = pd.concat(frames, ignore_index=True)
    naive = df["p_naive"].dropna()
    fams = df.groupby("seed")["family"].nunique().sum()
    return {
        "seeds": list(seeds), "planted": planted, "tests": len(df),
        "share_p_below_0.05": float((df["p"] < 0.05).mean()) if len(df) else None,
        "share_p_below_0.10": float((df["p"] < 0.10).mean()) if len(df) else None,
        "p_deciles": np.histogram(df["p"], bins=10, range=(0, 1))[0].tolist(),
        "naive_tests": len(naive),
        "naive_share_p_below_0.05": float((naive < 0.05).mean()) if len(naive) else None,
        "naive_share_p_below_0.10": float((naive < 0.10).mean()) if len(naive) else None,
        "bh_discoveries": int((df["q"] <= 0.10).sum()),
        "bh_discoveries_by_seed": {int(s): int((g["q"] <= 0.10).sum()) for s, g in df.groupby("seed")},
        "families_tested": int(fams),
        "by_family": {k: {"tests": len(g), "share_p_below_0.05": float((g["p"] < 0.05).mean()),
                          "naive_share_p_below_0.05": float((g["p_naive"].dropna() < 0.05).mean())
                          if g["p_naive"].notna().any() else None}
                      for k, g in df.groupby("family")},
        "seconds_per_seed": secs,
        "rows": df.to_dict(orient="records"),
    }  # fmt: skip
