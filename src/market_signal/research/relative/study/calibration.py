"""Null calibration of the Phase 18 harness on a correlated synthetic market (no real data).

The synthetic market has NO forward-return information, but it has the dependence that
makes relative-strength inference hard:

- a BTC factor with slowly varying volatility regimes and fat tails (Student t, df 4);
- alts = beta x BTC + a shared alt-market factor + idiosyncratic noise (fat tails), with
  alt-specific betas, so BTC-relative spreads of different alts are strongly correlated
  and co-timed;
- one late-listed alt (enters the cross-section only once it has history).

``planted`` > 0 adds residual momentum (each alt's idiosyncratic return loads on its own
previous ``L``-bar idiosyncratic mean), to check that the harness can detect a real effect.

``calibrate`` runs the full evaluation (central parameters) on several seeds and reports
the share of p-values below 0.05 / 0.10 for the block-clustered test and for Phase 17's
independent-draw null, and the BH discoveries per family. A calibrated harness rejects
about 5% / 10% and makes few discoveries; Phase 18 requires this BEFORE the real run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from market_signal.research.relative.study.spec import (
    RelativeStudyDefinition,
    StudyManifest,
    families_spec,
    semantics,
)
from market_signal.research.structure.series import BarSeries, assumed
from market_signal.research.structure.study.data import CoinData
from market_signal.research.structure.study.spec import CostRef, DatasetRef

SIGMA = {"1h": 0.006, "4h": 0.012}
BETAS = {"ETH": 1.1, "SOL": 1.3, "HYPE": 1.4, "LINK": 1.2, "AAVE": 1.25}


def _innov(rng: np.random.Generator, n: int) -> np.ndarray:
    return rng.standard_t(4, n) / np.sqrt(2.0)  # unit variance


def market(coins: tuple[str, ...], n: int, tf: str, seed: int, planted: float = 0.0,
           lookback: int = 18, late: str | None = "HYPE") -> dict[str, np.ndarray]:  # fmt: skip
    """Log-return paths per coin (NaN before a late listing)."""
    rng = np.random.default_rng(seed)
    sig = SIGMA[tf]
    lv = np.zeros(n)
    for i in range(1, n):  # slowly varying log-volatility regime
        lv[i] = 0.995 * lv[i - 1] + 0.07 * rng.normal()
    vol = sig * np.exp(lv - lv.mean())
    xb = vol * _innov(rng, n)
    alt_f = 0.5 * vol * _innov(rng, n)
    out = {"BTC": xb}
    for c in coins:
        if c == "BTC":
            continue
        idio = 0.8 * vol * _innov(rng, n)
        if planted:
            e = idio.copy()
            for i in range(lookback, n):
                e[i] = idio[i] + planted * e[i - lookback : i].mean()
            idio = e
        x = BETAS.get(c, 1.2) * xb + alt_f + idio
        if c == late:
            x[: n // 4] = np.nan
        out[c] = x
    return out


def coin_data(venue: str, coins: tuple[str, ...], tf: str, start, end, seed: int,
              planted: float = 0.0) -> dict[str, CoinData]:  # fmt: skip
    step = pd.Timedelta(tf)
    ot = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq=step, inclusive="left")
    ot = ot[ot + step <= pd.Timestamp(end)]
    paths = market(coins, len(ot), tf, seed, planted)
    out = {}
    for c in coins:
        x = paths[c]
        ok = np.isfinite(x)
        lc = np.log(100.0) + np.cumsum(np.where(ok, x, 0.0))
        cl = np.exp(lc)
        op = np.concatenate([[100.0], cl[:-1]])
        hi = np.maximum(op, cl) * 1.001
        lo = np.minimum(op, cl) * 0.999
        df = pd.DataFrame({"open_time": ot, "close_time": ot + step, "open": op, "high": hi,
                           "low": lo, "close": cl, "volume": 1.0})[ok]  # fmt: skip
        s = BarSeries.from_frame(df.reset_index(drop=True), venue=venue, coin=c, timeframe=tf,
                                 availability=assumed(60.0), dataset_key="dataset_synthetic")  # fmt: skip
        ft = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="8h", inclusive="left")
        out[c] = CoinData(venue, c, "dataset_" + "0" * 64, {tf: s},
                          pd.DatetimeIndex(ft).as_unit("ns").asi8.astype(np.int64),
                          np.full(len(ft), 1e-5))  # fmt: skip
    return out


def definition(man: StudyManifest) -> RelativeStudyDefinition:
    """A synthetic definition: frozen-looking costs and placeholder dataset IDs (never
    registered; calibration and tests only)."""
    vc = man.venue_coins()
    costs = tuple(CostRef(venue=v, coin=c, fee_bps=4.5, slippage_bps=2.0 if c in ("BTC", "ETH") else 6.0)
                  for v, c in vc)  # fmt: skip
    ds = tuple(
        DatasetRef(venue=v, coin=c, dataset_id="dataset_" + f"{i:064x}")
        for i, (v, c) in enumerate(vc)
    )
    return RelativeStudyDefinition(manifest=man, families=families_spec(), costs=costs,
                                   datasets=ds, semantics=semantics())  # fmt: skip


def central_only(man: StudyManifest) -> StudyManifest:
    archs = tuple(a.model_copy(update={"axes": ()}) for a in man.architectures)
    return man.model_copy(update={"architectures": archs})


def run_synthetic(man: StudyManifest, seed: int, planted: float = 0.0) -> dict:
    from market_signal.research.relative.study.run import evaluate

    defn = definition(man)
    cache: dict = {}

    def load(venue: str, coin: str) -> CoinData:
        if venue not in cache:
            series: dict[str, dict] = {}
            for a in man.architectures:
                w = next((w for w in a.windows if w.venue == venue), None)
                if w is None:
                    continue
                cd = coin_data(venue, tuple(w.coins), a.timeframe, w.data_start, w.event_end,
                               seed * 1000 + hash_tf(a.timeframe) + (17 if venue == "binance" else 0),
                               planted)  # fmt: skip
                for c, d in cd.items():
                    series.setdefault(c, {}).update(d.series)
                    series[c]["_funding"] = (d.funding_ns, d.funding_rate)
            cache[venue] = {c: CoinData(venue, c, "dataset_" + "0" * 64,
                                        {k: v for k, v in s.items() if k != "_funding"}, *s["_funding"])
                            for c, s in series.items()}  # fmt: skip
        return cache[venue][coin]

    payload, meta = evaluate(defn, load)
    return {"payload": payload, "meta": meta}


def hash_tf(tf: str) -> int:
    return {"1h": 1, "4h": 4}[tf]


def pvalues(payload: dict) -> pd.DataFrame:
    rows = []
    for an, a in payload["architectures"].items():
        for v, vv in a["venues"].items():
            for fam, fd in (vv.get("families") or {}).items():
                for r in fd["members"]:
                    if r.get("testable"):
                        rows.append({"architecture": an, "venue": v, "family": fam, "member": r["member"],
                                     "kind": r["kind"], "p": r["p_value"], "q": r.get("q_value"),
                                     "p_naive": r.get("p_naive_independent_draws")})  # fmt: skip
    return pd.DataFrame(rows)


def calibrate(man: StudyManifest, seeds=(1, 2, 3, 4), planted: float = 0.0) -> dict:
    man = central_only(man)
    frames, metas = [], []
    for s in seeds:
        out = run_synthetic(man, s, planted)
        p = pvalues(out["payload"])
        p["seed"] = s
        frames.append(p)
        metas.append(out["meta"]["wall_seconds"])
    df = pd.concat(frames, ignore_index=True)
    naive = df["p_naive"].dropna()
    deciles = np.histogram(df["p"], bins=10, range=(0, 1))[0].tolist()
    return {
        "seeds": list(seeds), "planted": planted, "tests": len(df),
        "share_p_below_0.05": float((df["p"] < 0.05).mean()),
        "share_p_below_0.10": float((df["p"] < 0.10).mean()),
        "p_deciles": deciles,
        "naive_tests": len(naive),
        "naive_share_p_below_0.05": float((naive < 0.05).mean()) if len(naive) else None,
        "naive_share_p_below_0.10": float((naive < 0.10).mean()) if len(naive) else None,
        "bh_discoveries": int((df["q"] <= 0.10).sum()),
        "bh_discoveries_by_seed": {int(s): int((g["q"] <= 0.10).sum()) for s, g in df.groupby("seed")},
        "by_kind": {k: {"tests": len(g), "share_p_below_0.05": float((g["p"] < 0.05).mean())}
                    for k, g in df.groupby("kind")},
        "by_family": {k: {"tests": len(g), "share_p_below_0.05": float((g["p"] < 0.05).mean()),
                          "naive_share_p_below_0.05": float((g["p_naive"].dropna() < 0.05).mean())
                          if g["p_naive"].notna().any() else None}
                      for k, g in df.groupby("family")},
        "seconds_per_seed": metas,
        "rows": df.to_dict(orient="records"),
    }  # fmt: skip
