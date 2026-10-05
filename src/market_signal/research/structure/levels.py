"""State layer: structural levels and their metadata. A level existing is not an event.

Level table (one row per level, int64 ns times; ``public()`` renders Timestamps):

- ``price``: the level in real prices; ``oprice``: the same in the side's oriented frame
  (``BarSeries.oriented``), where the level is always a HIGH that price breaches upward;
- ``origin_idx`` / ``origin_ns``: the bar whose extreme set the price (the pivot bar);
- ``confirm_idx`` / ``formed_ns``: the last bar the level's definition reads, and its close.
  No price after ``formed_ns`` shaped the level;
- ``available_ns``: ``ready_at[confirm_idx]``, when Prism could know the level;
- ``until_ns``: exclusive bound on the OPEN time of bars that may still reference it (age
  limit or window exit), fixed when the level forms;
- ``retired_ns`` / ``retired_by``: a later supersession (or data gap) that ends its life
  earlier. Learned after the fact, so kept apart from the immutable record. Breaches
  retire a level separately, on whatever bar series the breach is detected.

A bar ``i`` (of any timeframe) may reference a level iff ``formed_ns <= open_time[i] <
min(until_ns, retired_ns)``: the level's price was fully determined before that bar began. Anything derived
from the pair is known no earlier than ``max(ready_at[i], available_ns)``.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from market_signal.research.lab.common import canonical_json
from market_signal.research.structure.registry import (
    ClusterParams,
    PriorExtremeParams,
    SwingLevelParams,
    SwingParams,
    Tolerance,
    TouchParams,
    params_key,
)
from market_signal.research.structure.series import NAT, BarSeries, to_ts

LEVEL_COLUMNS = [
    "level_id", "primitive", "kind", "side", "price", "oprice", "origin_idx", "origin_ns",
    "confirm_idx", "formed_ns", "available_ns", "until_ns", "members", "lower", "upper",
    "center", "tolerance_price", "extends", "atr", "inputs_live", "retired_ns", "retired_by",
]  # fmt: skip
# The record columns never change once a level is available. ``retired_ns``/``retired_by``
# are lifecycle facts learned LATER (an equal bar superseding a prior extreme, a newer
# cluster snapshot, a data gap); they close the referenceable life from that instant on.
RECORD_COLUMNS = [c for c in LEVEL_COLUMNS if c not in ("retired_ns", "retired_by")]
SIDES = ("high", "low")


def sign(side: str) -> int:
    return 1 if side == "high" else -1


def tolerance_price(tol: Tolerance, price: np.ndarray, atr: np.ndarray) -> np.ndarray:
    """Tolerance in price units: bps of |price| or a multiple of ATR (NaN if ATR unknown)."""
    if tol.method == "bps":
        return np.abs(price) * tol.value / 1e4
    return tol.value * atr


def _level_head(series: BarSeries, version: str, params, side: str) -> str:
    return canonical_json({"series": series.prov.as_dict(), "primitive": version,
                           "params": params_key(params), "side": side})  # fmt: skip


def _lid(head: str, key) -> str:
    return "slvl_" + hashlib.sha256(f"{head}\0{canonical_json(key)}".encode()).hexdigest()


def _level_ids(series: BarSeries, version: str, params, side: str, keys) -> list[str]:
    """Level IDs: SHA-256 of the canonical JSON of the static definition (series identity,
    primitive version, parameters, side) and each level's key (its origin bar open time in
    ns, or a cluster's sorted member open times)."""
    head = _level_head(series, version, params, side)
    return [_lid(head, k) for k in keys]


def _frame(rows: dict) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in LEVEL_COLUMNS:
        if col not in df:
            df[col] = (
                NAT
                if col == "retired_ns"
                else (None if col in ("extends", "retired_by") else np.nan)
            )
    return df[LEVEL_COLUMNS]


def empty_levels() -> pd.DataFrame:
    return _frame({c: [] for c in LEVEL_COLUMNS})


# --------------------------------------------------------------------------- swings


def pivot_indices(x: np.ndarray, segment: np.ndarray, left: int, right: int) -> np.ndarray:
    """Pivot highs of ``x``: ``x[p]`` strictly above the ``left`` bars before it and at least
    the ``right`` bars after it, all inside one gap-free segment. In a plateau of equal
    highs only the FIRST bar qualifies (strict left), so one top gives one pivot."""
    width = left + right + 1
    if len(x) < width:
        return np.array([], dtype=np.int64)
    w = sliding_window_view(x, width)
    centre = w[:, left]
    ok = (centre > w[:, :left].max(axis=1)) & (centre >= w[:, left + 1 :].max(axis=1))
    s = sliding_window_view(segment, width)
    ok &= (s[:, 0] == s[:, -1]) & np.isfinite(w).all(axis=1)
    return np.flatnonzero(ok) + left


def swings(series: BarSeries, params: SwingParams, side: str, max_age_bars: int | None = None):
    """``swing_v1`` pivots of one side as a level table.

    The pivot at bar ``p`` is confirmed by bar ``p + right`` and is unknown before
    ``ready_at[p + right]``: a pattern that looks like a swing on a chart is NOT a swing
    for research until its right-side bars have closed (and a higher bar among them
    cancels it). ``max_age_bars`` (level use) bounds how long it can be referenced.
    """
    _, xh, _, _ = series.oriented(side)
    p = pivot_indices(xh, series.segment, params.left, params.right)
    if not len(p):
        return empty_levels()
    j = p + params.right
    formed = series.close_time[j]
    until = (
        formed + max_age_bars * series.step
        if max_age_bars
        else np.full(len(p), np.iinfo(np.int64).max)
    )
    lvl_params = (
        SwingLevelParams(swing=params, max_age_bars=max_age_bars) if max_age_bars else params
    )
    return _frame(
        {
            "level_id": _level_ids(
                series, "swing_v1", lvl_params, side, [int(t) for t in series.open_time[p]]
            ),
            "primitive": "swing_v1",
            "kind": "swing",
            "side": side,
            "price": sign(side) * xh[p],
            "oprice": xh[p],
            "origin_idx": p,
            "origin_ns": series.open_time[p],
            "confirm_idx": j,
            "formed_ns": formed,
            "available_ns": series.ready_at[j],
            "until_ns": until,
            "members": 1,
            "atr": series.atr[j],
            "inputs_live": series.inputs_live(np.maximum(p - params.left, 0), j),
        }
    )


def swing_levels(series: BarSeries, params: SwingLevelParams, side: str) -> pd.DataFrame:
    return swings(series, params.swing, side, params.max_age_bars)


# --------------------------------------------------------------------------- prior extremes


def _next_ge(x: np.ndarray) -> np.ndarray:
    """For each i, the first k > i with x[k] >= x[i] (len(x) if none). Monotonic stack."""
    out = np.full(len(x), len(x), dtype=np.int64)
    stack: list[int] = []
    for k, v in enumerate(x.tolist()):
        while stack and x[stack[-1]] <= v:
            out[stack.pop()] = k
        stack.append(k)
    return out


def prior_extremes(series: BarSeries, params: PriorExtremeParams, side: str) -> pd.DataFrame:
    """``prior_extreme_v1``: the levels behind ``donchian_high_n`` / ``donchian_low_n``.

    After bar j closes, the prior-n extreme for bar j+1 is the max of the ``n`` bars ending
    at j (one gap-free segment); its origin is the LATEST bar attaining it. A bar becomes a
    level at the first such window where it is that origin (``confirm_idx`` = j: possibly
    long after its own bar, once an older higher bar has left the window). It stays the
    level until a bar reaches it: a higher bar breaches it (one event); an EQUAL bar
    supersedes it (``retired_by = superseded_equal``, no event). It leaves the window after
    bar ``origin + n``, and a data gap retires it (the next window is not full).
    """
    n = params.n
    _, xh, _, _ = series.oriented(side)
    if len(xh) < n:
        return empty_levels()
    w = sliding_window_view(xh, n)
    s = sliding_window_view(series.segment, n)
    valid = (s[:, 0] == s[:, -1]) & np.isfinite(w).all(axis=1)
    j = np.flatnonzero(valid) + n - 1  # window end
    org = j - np.argmax(w[valid][:, ::-1], axis=1)  # latest bar attaining the max
    o, first = np.unique(org, return_index=True)
    if not len(o):
        return empty_levels()
    conf = j[first]
    nxt = _next_ge(xh)[o]
    until = series.close_time[o] + n * series.step
    last = len(xh) - 1
    eq = (nxt <= last) & (xh[np.minimum(nxt, last)] == xh[o])
    retired = np.where(eq, series.close_time[np.minimum(nxt, last)], NAT)
    retired_by = np.where(eq, "superseded_equal", None)
    seg_last = np.searchsorted(series.segment, series.segment[o], side="right") - 1
    gap = (seg_last < last) & ((retired == NAT) | (series.close_time[seg_last] < retired))
    retired = np.where(gap, series.close_time[seg_last], retired)
    retired_by = np.where(gap, "data_gap", retired_by)
    return (
        _frame(
            {
                "level_id": _level_ids(
                    series, "prior_extreme_v1", params, side, [int(t) for t in series.open_time[o]]
                ),
                "primitive": "prior_extreme_v1",
                "kind": "prior_extreme",
                "side": side,
                "price": sign(side) * xh[o],
                "oprice": xh[o],
                "origin_idx": o,
                "origin_ns": series.open_time[o],
                "confirm_idx": conf,
                "formed_ns": series.close_time[conf],
                "available_ns": series.ready_at[conf],
                "until_ns": until,
                "members": 1,
                "atr": series.atr[conf],
                "inputs_live": series.inputs_live(conf - n + 1, conf),
                "retired_ns": retired,
                "retired_by": retired_by,
            }
        )
        .sort_values(["confirm_idx", "origin_idx"], kind="stable")
        .reset_index(drop=True)
    )


# --------------------------------------------------------------------------- clusters


def _no_clusters() -> pd.DataFrame:
    return empty_levels().assign(first_member_ns=pd.Series(dtype=np.int64),
                                 latest_member_ns=pd.Series(dtype=np.int64))  # fmt: skip


def clusters(series: BarSeries, params: ClusterParams, side: str) -> pd.DataFrame:
    """``level_cluster_v1``: equal highs/lows as immutable, versioned snapshots.

    Processed in confirmation order (the order Prism learns of swings). When swing S
    (price H, pivot p) is confirmed at bar j, the candidates are earlier CONFIRMED swings
    of the same side, newest first, at most ``max_age_bars`` older than p. Walking back,
    ``between`` is the highest high strictly between the candidate's pivot and p; the walk
    stops once ``between > H + tol`` (no older swing can qualify). A candidate joins if the
    set's width (max - min) stays within ``tol`` and ``between`` does not exceed the set's
    max: a higher high in between separates two tops; they are not "equal highs".
    ``tol`` is fixed at j (bps of H, or ATR at j).

    With at least one member joined, a snapshot {S, members} is emitted at j: reference
    ``upper`` (max member; the level a breach must exceed), ``lower``, ``center`` (mean).
    A snapshot is never edited. If it contains an earlier snapshot's members, it
    ``extends`` it, and the earlier one stops being referenceable at the new one's
    ``formed_ns`` (it is superseded, not rewritten). A snapshot expires ``max_age_bars``
    after its newest member's confirmation.
    """
    sw = swings(series, params.swing, side)
    if len(sw) < 2:
        return _no_clusters()
    _, xh, _, _ = series.oriented(side)
    piv = sw["origin_idx"].to_numpy()
    val = xh[piv]
    conf = sw["confirm_idx"].to_numpy()
    rows: list[dict] = []
    head = _level_head(series, "level_cluster_v1", params, side)
    latest_of: dict[int, int] = {}  # pivot -> index of the newest snapshot containing it
    members_of: list[frozenset[int]] = []
    for m in range(1, len(piv)):
        j = conf[m]
        tol = float(tolerance_price(params.tolerance, val[m : m + 1], series.atr[j : j + 1])[0])
        if not np.isfinite(tol):
            continue
        chosen = [m]
        top, bottom = val[m], val[m]
        between = -np.inf
        for k in range(m - 1, -1, -1):
            if piv[m] - piv[k] > params.max_age_bars:
                break
            lo_ = piv[k] + 1
            hi_ = piv[m] if k == m - 1 else piv[k + 1] + 1
            if hi_ > lo_:
                between = max(between, float(xh[lo_:hi_].max()))
            if between > val[m] + tol:
                break
            new_top, new_bottom = max(top, val[k]), min(bottom, val[k])
            if new_top - new_bottom <= tol and between <= new_top:
                chosen.append(k)
                top, bottom = new_top, new_bottom
        if len(chosen) < 2:
            continue
        mem = frozenset(int(piv[k]) for k in chosen)
        prior = {latest_of[p_] for p_ in mem if p_ in latest_of}
        subsets = [r for r in prior if members_of[r] < mem]
        extends = max(subsets) if subsets else None
        formed = int(series.close_time[j])
        member_times = sorted(int(series.open_time[p_]) for p_ in mem)
        mvals = val[chosen]
        rows.append(
            {
                "level_id": _lid(head, member_times),
                "primitive": "level_cluster_v1",
                "kind": "cluster",
                "side": side,
                "price": sign(side) * top,
                "oprice": top,
                "origin_idx": int(piv[m]),
                "origin_ns": int(series.open_time[piv[m]]),
                "confirm_idx": int(j),
                "formed_ns": formed,
                "available_ns": int(series.ready_at[j]),
                "until_ns": formed + params.max_age_bars * series.step,
                "members": len(mem),
                "center": sign(side) * float(np.mean(mvals)),
                "tolerance_price": tol,
                "extends": None if extends is None else rows[extends]["level_id"],
                "atr": float(series.atr[j]),
                "inputs_live": bool(series.inputs_live(np.array([min(mem)]), np.array([j]))[0]),
                "first_member_ns": member_times[0],
                "latest_member_ns": int(series.open_time[piv[m]]),
                "_top": top,
                "_bottom": bottom,
                "retired_ns": NAT,
                "retired_by": None,
            }
        )
        idx = len(rows) - 1
        if extends is not None and rows[extends]["retired_ns"] == NAT:
            # superseded from the instant the extending snapshot forms (learned then)
            rows[extends]["retired_ns"] = formed
            rows[extends]["retired_by"] = rows[idx]["level_id"]
        members_of.append(mem)
        for p_ in mem:
            latest_of[p_] = idx
    if not rows:
        return _no_clusters()
    df = pd.DataFrame(rows)
    s = sign(side)
    # real-price bounds: for lows the oriented top is the LOWEST low
    df["upper"] = np.where(s > 0, df["_top"], -df["_bottom"])
    df["lower"] = np.where(s > 0, df["_bottom"], -df["_top"])
    out = _frame({c: df[c] for c in LEVEL_COLUMNS if c in df})
    out["first_member_ns"] = df["first_member_ns"].to_numpy(np.int64)
    out["latest_member_ns"] = df["latest_member_ns"].to_numpy(np.int64)
    return out


def levels(series: BarSeries, params, side: str) -> pd.DataFrame:
    """Dispatch on the level kind (swing / prior_extreme / cluster)."""
    if isinstance(params, SwingLevelParams):
        return swing_levels(series, params, side)
    if isinstance(params, PriorExtremeParams):
        return prior_extremes(series, params, side)
    if isinstance(params, ClusterParams):
        return clusters(series, params, side)
    raise TypeError(f"unknown level parameters {type(params).__name__}")


# --------------------------------------------------------------------------- lifecycle


def eligible_range(lv: pd.DataFrame, series: BarSeries) -> tuple[np.ndarray, np.ndarray]:
    """[a, b) bar-index range of ``series`` that may reference each level."""
    until = lv["until_ns"].to_numpy(np.int64)
    retired = lv["retired_ns"].fillna(NAT).to_numpy(np.int64)
    end = np.where(retired != NAT, np.minimum(until, retired), until)
    a = np.searchsorted(series.open_time, lv["formed_ns"].to_numpy(np.int64), side="left")
    b = np.searchsorted(series.open_time, end, side="left")
    return a, b


def lifecycle(
    lv: pd.DataFrame, series: BarSeries, touch: TouchParams
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per level, on ``series`` (its own or a faster one): first breach (a high above it),
    first close beyond it, and separated touches before the breach.

    A touch is a bar whose oriented high is within ``touch.tolerance`` below the level
    (tolerance from the bar's prior ATR, or bps of the level). Consecutive in-zone bars
    count once; another touch needs a bar entirely below the zone first. Touches stop at
    the breach. Each touch and transition is known at ``max(ready_at[bar], level known)``.

    Returns (per-level lifecycle, touches).
    """
    out, touches = [], []
    for side in SIDES:
        part = lv[lv["side"] == side]
        if part.empty:
            continue
        _, xh, _, xc = series.oriented(side)
        a, b = eligible_range(part, series)
        cols = zip(part["oprice"].to_numpy(float), part["price"].to_numpy(float),
                   part["level_id"].to_numpy(), part["available_ns"].to_numpy(np.int64), a, b,
                   strict=True)  # fmt: skip
        for L, price, lid, lavail, a_, b_ in cols:
            seg_h = xh[a_:b_]
            hit = np.flatnonzero(seg_h > L)
            br = a_ + int(hit[0]) if len(hit) else -1
            cb = np.flatnonzero(xc[a_:b_] > L)
            close_beyond = a_ + int(cb[0]) if len(cb) else -1
            stop = br if br >= 0 else b_
            if b_ >= len(series) and br < 0:
                status = "OPEN"  # data ended before the level could expire
            else:
                status = "BREACHED" if br >= 0 else "EXPIRED"
            known = int(lavail)
            if stop > a_:
                tol = tolerance_price(touch.tolerance, np.full(stop - a_, abs(price)),
                                      series.atr_prior[a_:stop])  # fmt: skip
                zone = xh[a_:stop] >= L - tol
                edge = zone & ~np.concatenate([[False], zone[:-1]])
                for k in np.flatnonzero(edge) + a_:
                    touches.append({"level_id": lid, "bar_idx": int(k),
                                    "touch_ns": int(series.open_time[k]),
                                    "available_ns": max(int(series.ready_at[k]), known)})  # fmt: skip
            out.append(
                {
                    "level_id": lid,
                    "status": status,
                    "first_bar": a_,
                    "breach_idx": br,
                    "breach_ns": int(series.open_time[br]) if br >= 0 else NAT,
                    "breach_known_ns": max(int(series.ready_at[br]), known) if br >= 0 else NAT,
                    "close_beyond_idx": close_beyond,
                    "close_beyond_known_ns": (
                        max(int(series.ready_at[close_beyond]), known) if close_beyond >= 0 else NAT
                    ),
                }
            )
    life = pd.DataFrame(out, columns=["level_id", "status", "first_bar", "breach_idx", "breach_ns",
                                      "breach_known_ns", "close_beyond_idx", "close_beyond_known_ns"])  # fmt: skip
    t = pd.DataFrame(touches, columns=["level_id", "bar_idx", "touch_ns", "available_ns"])
    return life, t


def level_state(
    lv: pd.DataFrame, life: pd.DataFrame, touches: pd.DataFrame, series: BarSeries, side: str
) -> pd.DataFrame:
    """Per bar of ``series``: the latest level of ``side`` known by the bar's ``ready_at``
    and its metadata as known then (age, signed distance in price/bps/ATR, touches so far,
    last touch, whether breached / closed beyond yet). Never reads a later bar."""
    part = lv[lv["side"] == side].sort_values(["available_ns", "confirm_idx"], kind="stable")
    t = series.ready_at
    n = len(series)
    cols = {"level_id": np.full(n, None, dtype=object), "price": np.full(n, np.nan)}
    if part.empty or not n:
        return pd.DataFrame(cols, index=to_ts(series.close_time))
    k = np.searchsorted(part["available_ns"].to_numpy(np.int64), t, side="right") - 1
    has = k >= 0
    pick = part.iloc[np.maximum(k, 0)].reset_index(drop=True)
    life_i = life.set_index("level_id").reindex(pick["level_id"])
    price = np.where(has, pick["price"].to_numpy(float), np.nan)
    dist = series.c - price
    formed = pick["formed_ns"].to_numpy(np.int64)
    tc = touches.sort_values("available_ns")
    counts = np.zeros(n, dtype=np.int64)
    last_touch = np.full(n, NAT, dtype=np.int64)
    if not tc.empty:
        g = {lid: grp["available_ns"].to_numpy(np.int64) for lid, grp in tc.groupby("level_id")}
        gt = {lid: grp["touch_ns"].to_numpy(np.int64) for lid, grp in tc.groupby("level_id")}
        ids = pick["level_id"].to_numpy()
        for i in np.flatnonzero(has):
            arr = g.get(ids[i])
            if arr is None:
                continue
            c_ = int(np.searchsorted(arr, t[i], side="right"))
            counts[i] = c_
            if c_:
                last_touch[i] = gt[ids[i]][c_ - 1]
    bk = life_i["breach_known_ns"].to_numpy(np.int64)
    ck = life_i["close_beyond_known_ns"].to_numpy(np.int64)
    out = pd.DataFrame(
        {
            "level_id": np.where(has, pick["level_id"].to_numpy(), None),
            "price": price,
            "origin_time": np.where(has, pick["origin_ns"].to_numpy(np.int64), NAT),
            "available_at": np.where(has, pick["available_ns"].to_numpy(np.int64), NAT),
            "age_bars": np.where(has, (series.close_time - formed) // series.step, -1),
            "age_hours": np.where(has, (t - formed) / 3.6e12, np.nan),
            "distance": dist,
            "distance_bps": dist / np.abs(price) * 1e4,
            "distance_atr": dist / series.atr,
            "touches": np.where(has, counts, 0),
            "last_touch": last_touch,
            "breached": has & (bk != NAT) & (bk <= t),
            "closed_beyond": has & (ck != NAT) & (ck <= t),
        },
        index=to_ts(series.close_time).to_numpy(),
    )
    for c in ("origin_time", "available_at", "last_touch"):
        out[c] = to_ts(out[c].to_numpy(np.int64)).to_numpy()
    out.index.name = "close_time"
    return out


def public(df: pd.DataFrame) -> pd.DataFrame:
    """Render ``*_ns`` columns as UTC Timestamps (``x_ns`` -> ``x``)."""
    out = df.copy()
    for c in [c for c in out.columns if c.endswith("_ns")]:
        out[c[:-3]] = to_ts(out[c].fillna(NAT).astype(np.int64).to_numpy()).to_numpy()
        out = out.drop(columns=c)
    return out
