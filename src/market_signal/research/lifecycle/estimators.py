"""Rolling, event-count and recency-weighted evidence over an outcome ledger.

Every estimate is evaluated *as of* a time T and reads only outcomes that had **resolved**
by T (``t_res <= T``: the exit close of the holding window). A calendar window of W days
holds the outcomes that resolved in ``(T - W, T]``; an event-count window holds the latest N
resolved independent outcomes. Nothing after T can enter, which the no-leakage tests check
by mutating every later outcome.

Units: the effect of one event is its after-cost, after-funding net return on notional at
the study's primary horizon (``net``); ``excess`` subtracts the causal trailing baseline of
random eligible entries on the same asset and side. Times are float days since the Unix
epoch (UTC), so windows are exact elapsed-time intervals.

Uncertainty (``time_block_cluster_robust_v1``). For weights w (1 for unweighted windows):

    mean = sum(w x) / sum(w)
    var_cluster = G / (G - 1) * sum_g (sum_{i in g} w_i (x_i - mean))^2 / sum(w)^2
    var_iid     = n / (n - 1) * sum_i w_i^2 (x_i - mean)^2 / sum(w)^2
    se = sqrt(max(var_cluster, var_iid))

with clusters g = calendar blocks of the signal time (co-timed outcomes on different assets
share a market shock). Kish's effective sample size ``(sum w)^2 / sum(w^2)`` gates weighted
estimates. These are descriptive large-sample approximations; ``t = mean / se`` is reported
as a standardised effect, never converted into a p-value here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np
import pandas as pd

from market_signal.research.lifecycle.policy import CalendarWindow, EventWindow, LifecyclePolicy

DAY_NS = 86_400 * 10**9
REGIME_DIMS = ("trend", "vol", "breadth", "funding")
REGIME_LABELS = {
    "trend": ("down", "neutral", "up"),
    "vol": ("low", "normal", "high"),
    "breadth": ("risk_off", "mixed", "risk_on"),
    "funding": ("low", "normal", "high"),
}
UNKNOWN = -1

OUTCOME_COLUMNS = ("asset", "signal_time", "resolved_at", "net", "gross", "excess", "mae", "mfe",
                   "cost", "funding", "independent", "evaluable", "stress",
                   *(f"regime_{d}" for d in REGIME_DIMS))  # fmt: skip


def to_days(ts) -> float:
    return pd.Timestamp(ts).value / DAY_NS


def to_time(days: float) -> pd.Timestamp:
    return pd.Timestamp(round(days * DAY_NS), tz="UTC")


def iso(days: float | None) -> str | None:
    return None if days is None or not np.isfinite(days) else to_time(days).isoformat()


def _f(x) -> float | None:
    x = float(x)
    return x if np.isfinite(x) else None


@dataclass
class EventSet:
    """Independent evaluable outcomes sorted by (resolved, signal, asset), plus every signal's
    nominal resolution time and evaluability (for the evaluable-outcome gate)."""

    assets: tuple[str, ...]
    t_sig: np.ndarray
    t_res: np.ndarray
    net: np.ndarray
    excess: np.ndarray
    gross: np.ndarray
    mae: np.ndarray
    mfe: np.ndarray
    cost: np.ndarray
    funding: np.ndarray
    asset: np.ndarray
    stress: np.ndarray
    regime: dict[str, np.ndarray]
    all_res: np.ndarray
    all_valid_cum: np.ndarray  # cumulative count of evaluable signals in all_res order
    history_start: float  # earliest time the strategy could have produced an outcome
    block_days: int = 7
    cluster: np.ndarray = field(init=False)
    cum_x: np.ndarray = field(init=False)
    cum_x2: np.ndarray = field(init=False)

    def __post_init__(self):
        self.cluster = np.floor(self.t_sig / self.block_days).astype(np.int64)
        self.cum_x = np.concatenate([[0.0], np.cumsum(self.net)])
        self.cum_x2 = np.concatenate([[0.0], np.cumsum(self.net**2)])

    def __len__(self) -> int:
        return len(self.net)

    @classmethod
    def from_frame(cls, df: pd.DataFrame, *, history_start, block_days: int = 7) -> EventSet:
        """``df``: one row per signal (``OUTCOME_COLUMNS``). Only rows that are independent
        AND evaluable become events; every row counts toward the evaluable share."""
        missing = set(OUTCOME_COLUMNS) - set(df.columns)
        if missing:
            raise ValueError(f"outcome ledger lacks {sorted(missing)}")
        assets = tuple(sorted(df["asset"].unique()))
        code = {a: i for i, a in enumerate(assets)}
        allv = df.assign(_r=pd.to_datetime(df["resolved_at"], utc=True).map(to_days))
        allv = allv.sort_values(["_r"], kind="mergesort")
        keep = df[df["independent"].astype(bool) & df["evaluable"].astype(bool)].copy()
        keep["_s"] = pd.to_datetime(keep["signal_time"], utc=True).map(to_days)
        keep["_r"] = pd.to_datetime(keep["resolved_at"], utc=True).map(to_days)
        keep["_a"] = keep["asset"].map(code)
        keep = keep.sort_values(["_r", "_s", "_a"], kind="mergesort")

        def arr(name, dtype=float):
            return keep[name].to_numpy(dtype=dtype) if len(keep) else np.array([], dtype=dtype)

        regime = {}
        for d in REGIME_DIMS:
            labels = REGIME_LABELS[d]
            regime[d] = np.array([labels.index(v) if v in labels else UNKNOWN
                                  for v in keep[f"regime_{d}"]], dtype=np.int64)  # fmt: skip
        return cls(
            assets=assets,
            t_sig=arr("_s"), t_res=arr("_r"), net=arr("net"), excess=arr("excess"),
            gross=arr("gross"), mae=arr("mae"), mfe=arr("mfe"), cost=arr("cost"),
            funding=arr("funding"), asset=arr("_a", np.int64), stress=arr("stress", bool),
            regime=regime, all_res=allv["_r"].to_numpy(dtype=float),
            all_valid_cum=np.concatenate([[0], np.cumsum(allv["evaluable"].astype(bool))]),
            history_start=to_days(history_start), block_days=block_days,
        )  # fmt: skip

    def subset(self, mask: np.ndarray) -> EventSet:
        """Events where ``mask``; the evaluable-share arrays and history start are kept (a
        regime subset is a conditional view of the same signal stream)."""
        return EventSet(
            assets=self.assets, t_sig=self.t_sig[mask], t_res=self.t_res[mask],
            net=self.net[mask], excess=self.excess[mask], gross=self.gross[mask],
            mae=self.mae[mask], mfe=self.mfe[mask], cost=self.cost[mask],
            funding=self.funding[mask], asset=self.asset[mask], stress=self.stress[mask],
            regime={k: v[mask] for k, v in self.regime.items()}, all_res=self.all_res,
            all_valid_cum=self.all_valid_cum, history_start=self.history_start,
            block_days=self.block_days,
        )  # fmt: skip

    def resolved(self, as_of: float) -> int:
        """Number of events resolved by ``as_of`` (they are the prefix ``[0, k)``)."""
        return int(np.searchsorted(self.t_res, as_of, side="right"))

    def evaluable_share(self, lo_t: float, as_of: float) -> float | None:
        a = int(np.searchsorted(self.all_res, lo_t, side="right"))
        b = int(np.searchsorted(self.all_res, as_of, side="right"))
        total = b - a
        if total == 0:
            return None
        return float(self.all_valid_cum[b] - self.all_valid_cum[a]) / total


# --------------------------------------------------------------------------- core statistics


def weighted_mean_se(x: np.ndarray, w: np.ndarray, cluster: np.ndarray) -> tuple[float, float]:
    """(mean, se) under ``time_block_cluster_robust_v1``; se is NaN below two events."""
    n = len(x)
    if n == 0:
        return np.nan, np.nan
    sw = w.sum()
    mean = float((w * x).sum() / sw)
    if n < 2:
        return mean, np.nan
    r = w * (x - mean)
    var_iid = n / (n - 1) * float((r**2).sum()) / sw**2
    _, inv = np.unique(cluster, return_inverse=True)
    g = int(inv.max()) + 1
    var_cl = 0.0
    if g >= 2:
        sums = np.bincount(inv, weights=r, minlength=g)
        var_cl = g / (g - 1) * float((sums**2).sum()) / sw**2
    return mean, float(np.sqrt(max(var_cl, var_iid)))


def ess(w: np.ndarray) -> float:
    s2 = float((w**2).sum())
    return float(w.sum() ** 2 / s2) if s2 > 0 else 0.0


def recency_weights(t_res: np.ndarray, as_of: float, half_life_days: float) -> np.ndarray:
    return np.power(0.5, (as_of - t_res) / half_life_days)


def drawdown(net: np.ndarray, t_res: np.ndarray) -> dict:
    """Equal-notional cumulative event P&L in resolution order: max drawdown (from a running
    peak that starts at 0), the longest peak-to-recovery time and whether it recovered."""
    if len(net) == 0:
        return {"max_drawdown": None, "recovery_days": None, "recovered": None}
    cum = np.cumsum(net)
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    dd = peak - cum
    longest, start, recovered = 0.0, None, True
    for i in range(len(cum)):
        if dd[i] > 0 and start is None:
            start = t_res[i - 1] if i > 0 else t_res[0]
        elif dd[i] <= 0 and start is not None:
            longest = max(longest, t_res[i] - start)
            start = None
    if start is not None:
        longest, recovered = max(longest, t_res[-1] - start), False
    return {"max_drawdown": _f(dd.max()), "recovery_days": _f(longest), "recovered": recovered}


def tails(ev: EventSet, lo: int, hi: int) -> dict:
    """Pain profile of the events ``[lo, hi)``: drawdown, worst event, worst resolution
    day/week (equal-notional sums), worst stress-affected event."""
    net, t = ev.net[lo:hi], ev.t_res[lo:hi]
    out = drawdown(net, t)
    if len(net) == 0:
        return {**out, "worst_event": None, "worst_day": None, "worst_week": None,
                "worst_stress_event": None}  # fmt: skip
    day = np.floor(t).astype(np.int64)
    week = np.floor(t / 7).astype(np.int64)
    by_day = pd.Series(net).groupby(day).sum()
    by_week = pd.Series(net).groupby(week).sum()
    stress = net[ev.stress[lo:hi]]
    return {
        **out,
        "worst_event": _f(net.min()),
        "worst_event_resolved": iso(t[int(np.argmin(net))]),
        "worst_day": _f(by_day.min()),
        "worst_week": _f(by_week.min()),
        "worst_stress_event": _f(stress.min()) if len(stress) else None,
    }


def stats(ev: EventSet, lo: int, hi: int, weights: np.ndarray | None = None) -> dict:
    """Descriptive statistics of events ``[lo, hi)`` (optionally weighted)."""
    n = hi - lo
    if n <= 0:
        return {"n": 0, "assets": 0}
    sl = slice(lo, hi)
    w = np.ones(n) if weights is None else weights
    x, cl = ev.net[sl], ev.cluster[sl]
    mean, se = weighted_mean_se(x, w, cl)
    exm = np.isfinite(ev.excess[sl])
    ex_mean, ex_se = (weighted_mean_se(ev.excess[sl][exm], w[exm], cl[exm])
                      if exm.any() else (np.nan, np.nan))  # fmt: skip
    sw = w.sum()
    a = ev.asset[sl]
    na = len(ev.assets)
    cnt = np.bincount(a, minlength=na)
    asum = np.bincount(a, weights=w * x, minlength=na)
    aw = np.bincount(a, weights=w, minlength=na)
    with np.errstate(invalid="ignore", divide="ignore"):
        amean = asum / aw
    present = cnt > 0
    per_asset = {ev.assets[i]: {"n": int(cnt[i]), "mean": _f(amean[i])}
                 for i in range(na) if present[i]}  # fmt: skip
    return {
        "n": n,
        "assets": int(present.sum()),
        "ess": _f(ess(w)) if weights is not None else float(n),
        "mean": _f(mean),
        "se": _f(se),
        "t": _f(mean / se) if np.isfinite(se) and se > 0 else None,
        "hit_rate": _f((w * (x > 0)).sum() / sw),
        "excess_mean": _f(ex_mean),
        "excess_t": _f(ex_mean / ex_se) if np.isfinite(ex_se) and ex_se > 0 else None,
        "excess_events": int(exm.sum()),
        "gross_mean": _f((w * ev.gross[sl]).sum() / sw),
        "cost_mean": _f((w * (ev.cost[sl] + ev.funding[sl])).sum() / sw),
        "mae_mean": _f((w * ev.mae[sl]).sum() / sw),
        "mfe_mean": _f((w * ev.mfe[sl]).sum() / sw),
        "positive_asset_share": _f((amean[present] > 0).mean()) if present.any() else None,
        "per_asset": per_asset,
        "first_resolved": iso(ev.t_res[lo]),
        "last_resolved": iso(ev.t_res[hi - 1]),
    }


# --------------------------------------------------------------------------- windows


def lifetime(ev: EventSet, as_of: float, policy: LifecyclePolicy) -> dict:
    k = ev.resolved(as_of)
    s = stats(ev, 0, k)
    g = policy.lifetime
    reasons = []
    if s["n"] < g.min_events:
        reasons.append(f"{s['n']} independent events < {g.min_events}")
    if s.get("assets", 0) < g.min_assets:
        reasons.append(f"{s.get('assets', 0)} assets < {g.min_assets}")
    return {"label": "lifetime", "kind": "lifetime", "adequate": not reasons,
            "insufficient_reasons": reasons, **s}  # fmt: skip


def window(ev: EventSet, as_of: float, w: CalendarWindow | EventWindow,
           policy: LifecyclePolicy) -> dict:  # fmt: skip
    """One rolling window with its gates. Calendar: outcomes resolved in (T - W, T]; the
    strategy's history must cover the whole window. Event-count: the latest N outcomes,
    spanning at most ``max_span_days``."""
    g = policy.gates
    k = ev.resolved(as_of)
    reasons = []
    if isinstance(w, CalendarWindow):
        lo_t = as_of - w.days
        lo = int(np.searchsorted(ev.t_res, lo_t, side="right"))
        kind, span = "calendar", float(w.days)
        if ev.history_start > lo_t:
            reasons.append("history does not cover the whole window")
        share = ev.evaluable_share(lo_t, as_of)
    else:
        lo = max(0, k - w.events)
        kind = "event_count"
        span = float(as_of - ev.t_res[lo]) if k > lo else None
        if k - lo < w.events:
            reasons.append(f"only {k - lo} resolved events < {w.events}")
        elif span is not None and span > w.max_span_days:
            reasons.append(f"latest {w.events} events span {span:.0f} > {w.max_span_days} days")
        share = ev.evaluable_share(ev.t_res[lo] - 1e-9, as_of) if k > lo else None
    s = stats(ev, lo, k)
    if s["n"] < g.min_events:
        reasons.append(f"{s['n']} independent events < {g.min_events}")
    if s.get("assets", 0) < g.min_assets:
        reasons.append(f"{s.get('assets', 0)} assets < {g.min_assets}")
    if share is not None and share < g.min_evaluable_share:
        reasons.append(f"evaluable share {share:.2f} < {g.min_evaluable_share}")
    return {"label": w.label, "kind": kind, "span_days": _f(span) if span is not None else None,
            "evaluable_share": _f(share) if share is not None else None,
            "adequate": not reasons, "insufficient_reasons": reasons, **s}  # fmt: skip


def weighted(ev: EventSet, as_of: float, half_life: int, policy: LifecyclePolicy) -> dict:
    """All resolved outcomes, weighted by 0.5 ** (age / half_life). Adequate when the
    effective sample and asset count pass the gates."""
    k = ev.resolved(as_of)
    if k == 0:
        return {"label": f"hl{half_life}", "kind": "recency_weighted", "half_life_days": half_life,
                "adequate": False, "insufficient_reasons": ["no resolved events"], "n": 0}  # fmt: skip
    w = recency_weights(ev.t_res[:k], as_of, half_life)
    s = stats(ev, 0, k, weights=w)
    age = as_of - ev.t_res[:k]
    edges = (0, *policy.weighting.age_buckets_days, np.inf)
    total = w.sum()
    contrib = {}
    for a, b in pairwise(edges):
        m = (age >= a) & (age < b)
        key = f"{a}-{b}d" if np.isfinite(b) else f"{a}d+"
        contrib[key] = {"weight_share": _f(w[m].sum() / total), "events": int(m.sum()),
                        "mean": _f(ev.net[:k][m].mean()) if m.any() else None}  # fmt: skip
    reasons = []
    if s["ess"] < policy.gates.min_ess:
        reasons.append(f"effective sample {s['ess']:.1f} < {policy.gates.min_ess}")
    if s["assets"] < policy.gates.min_assets:
        reasons.append(f"{s['assets']} assets < {policy.gates.min_assets}")
    return {"label": f"hl{half_life}", "kind": "recency_weighted", "half_life_days": half_life,
            "adequate": not reasons, "insufficient_reasons": reasons, "contribution_by_age": contrib,
            **s}  # fmt: skip


def all_windows(ev: EventSet, as_of: float, policy: LifecyclePolicy) -> dict[str, dict]:
    out = {w.label: window(ev, as_of, w, policy) for w in policy.calendar_windows}
    out.update({w.label: window(ev, as_of, w, policy) for w in policy.event_windows})
    return out


def pick(windows: dict[str, dict], primary: str, fallback: str) -> dict:
    """The calendar window when adequate, else the event-count fallback (recorded)."""
    w = windows[primary]
    if w["adequate"]:
        return {**w, "selected": primary}
    f = windows[fallback]
    if f["adequate"]:
        return {**f, "selected": fallback}
    return {**w, "selected": None}


def eras(ev: EventSet, as_of: float) -> list[dict]:
    """Fixed calendar-year blocks (UTC) of outcomes resolved by ``as_of``: a simple,
    pre-declared chronological segmentation, never a narrative era."""
    k = ev.resolved(as_of)
    if k == 0:
        return []
    years = np.array([to_time(t).year for t in ev.t_res[:k]])
    out = []
    for y in sorted(set(years.tolist())):
        idx = np.flatnonzero(years == y)
        s = stats(ev, int(idx[0]), int(idx[-1]) + 1)
        out.append({"era": str(y), "n": s["n"], "assets": s["assets"], "mean": s["mean"],
                    "se": s["se"], "t": s["t"], "hit_rate": s["hit_rate"],
                    "excess_mean": s["excess_mean"]})  # fmt: skip
    return out
