"""``relative_inference_v1``: block-clustered tests for cross-asset relative signals.

Why not Phase 17's matched random-entry null alone? It draws each event's comparison
independently. Relative signals are co-timed across assets (a BTC move puts every alt's
BTC-relative spread in motion at once) and their forward spreads share the BTC leg, so
events of one timestamp are strongly dependent. Treating them as independent draws
understates the variance, which inflates false discoveries. The null calibration
(``calibration``) measures exactly this on a correlated synthetic market.

Test: group the statistic's observations into calendar blocks shared by all assets
(``block`` = entry bar // primary horizon). The mean is a ratio estimator
``m = sum S_b / sum n_b``, and its variance uses the linearised block contributions
``u_b = (S_b - m n_b) / N``: ``V = sum u_b^2 + 2 sum u_b u_{b+1}`` over adjacent blocks
(forward windows of neighbouring blocks overlap), floored at ``sum u_b^2``. Then
``t = m / sqrt(V)`` against a Student t with ``G - 1`` degrees of freedom (``G`` = blocks
with observations), two-sided. For per-timestamp statistics (IC, buckets, spreads) every
sampled timestamp is its own block.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the incomplete beta (modified Lentz)."""
    tiny, qab, qap, qam = 1e-300, a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lb) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lb) * _betacf(b, a, 1.0 - x) / b


def t_sf(t: float, df: float) -> float:
    """P(T > t) for Student t with ``df`` degrees of freedom."""
    if not math.isfinite(t):
        return 0.0 if t > 0 else 1.0
    tail = 0.5 * betainc(df / 2.0, 0.5, df / (df + t * t))
    return tail if t > 0 else 1.0 - tail


def p_two_sided(t: float, df: float) -> float:
    return float(min(1.0, 2.0 * t_sf(abs(t), df)))


def _contrib(values: np.ndarray, blocks: np.ndarray) -> tuple[float, np.ndarray, np.ndarray, int]:
    s = pd.DataFrame({"b": blocks, "v": values}).groupby("b")["v"].agg(["sum", "size"])
    n_tot = int(s["size"].sum())
    m = float(s["sum"].sum() / n_tot)
    u = (s["sum"].to_numpy() - m * s["size"].to_numpy()) / n_tot
    return m, u, s.index.to_numpy(), n_tot


def _variance(u: np.ndarray, ids: np.ndarray) -> float:
    base = float((u * u).sum())
    if len(u) < 2:
        return base
    adj = np.diff(ids) == 1
    return max(base, base + 2.0 * float((u[:-1] * u[1:])[adj].sum()))


def clustered_mean(values, blocks) -> dict:
    """Mean, block-clustered SE, t, two-sided p, 95% CI and the number of blocks."""
    v = np.asarray(values, float)
    b = np.asarray(blocks)
    ok = np.isfinite(v)
    v, b = v[ok], b[ok]
    if len(v) < 2:
        return {"mean": float(v.mean()) if len(v) else None, "se": None, "t": None,
                "p_value": None, "ci95": [None, None], "clusters": len(np.unique(b))}  # fmt: skip
    m, u, ids, _ = _contrib(v, b)
    return _finish(m, _variance(u, ids), len(ids))


def clustered_difference(v1, b1, v2, b2) -> dict:
    """Difference of two means (group 1 - group 2) with blocks shared between the groups."""
    v1, v2 = np.asarray(v1, float), np.asarray(v2, float)
    b1, b2 = np.asarray(b1), np.asarray(b2)
    k1, k2 = np.isfinite(v1), np.isfinite(v2)
    v1, b1, v2, b2 = v1[k1], b1[k1], v2[k2], b2[k2]
    if len(v1) < 2 or len(v2) < 2:
        return {"mean": None, "se": None, "t": None, "p_value": None, "ci95": [None, None],
                "clusters": 0}  # fmt: skip
    m1, u1, i1, _ = _contrib(v1, b1)
    m2, u2, i2, _ = _contrib(v2, b2)
    ids = np.union1d(i1, i2)
    u = np.zeros(len(ids))
    u[np.searchsorted(ids, i1)] += u1
    u[np.searchsorted(ids, i2)] -= u2
    return _finish(m1 - m2, _variance(u, ids), len(ids))


def _finish(m: float, var: float, g: int) -> dict:
    se = math.sqrt(var) if var > 0 else 0.0
    if se == 0.0:
        t = 0.0 if m == 0 else math.copysign(math.inf, m)
    else:
        t = m / se
    df = max(g - 1, 1)
    p = p_two_sided(t, df)
    # 97.5% Student t quantile by bisection on the survival function
    lo, hi = 0.0, 50.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if t_sf(mid, df) > 0.025:
            lo = mid
        else:
            hi = mid
    q = (lo + hi) / 2
    return {"mean": m, "se": se, "t": t, "p_value": p, "ci95": [m - q * se, m + q * se],
            "clusters": g, "df": df}  # fmt: skip


def one_sided(res: dict) -> tuple[float | None, float | None]:
    """(p for mean > 0, p for mean < 0) from a clustered result."""
    if res.get("t") is None:
        return None, None
    df = res.get("df") or 1
    up = t_sf(res["t"], df)
    return float(up), float(1.0 - up)


__all__ = ["betainc", "clustered_difference", "clustered_mean", "one_sided", "p_two_sided", "t_sf"]
