"""Trade-path layer (``trade_path_v1``): what price did after an event. Descriptive only;
nothing here constructs or optimises a strategy.

Conventions (one set, used by every output):

- **Entry.** The open of the first bar of the path series opening at or after the
  entry instant (normally the event's ``available_ns``): an action that was possible.
  The path is that bar plus the next ``H - 1`` bars (horizon ``H``). A path with a data
  gap or running past the data is INCOMPLETE and all its measures are NaN (never
  truncated).
- **Direction.** ``+1`` long, ``-1`` short. A short is measured on the negated frame, so
  favourable is always "up" in the oriented frame: long favourable = high, short
  favourable = low.
- **MFE / MAE are non-negative fractions of the entry price.** MFE = the largest favourable
  move, MAE = the largest adverse move, each floored at 0. ``*_close`` uses closes only;
  the unsuffixed (intrabar) versions use highs/lows. They are never mixed.
- **Time to excursion.** ``bars_to_*`` is the 0-based offset of the FIRST bar reaching the
  extreme (NaN if the excursion is 0); ``*_by_ns`` is that bar's close: the extreme
  happened at or before it. OHLC does not say when inside the bar.
- **Ordering.** FAVOURABLE_FIRST / ADVERSE_FIRST / NEITHER, or
  ``AMBIGUOUS_INTRABAR_ORDER`` when both are first reached inside the same bar. The only
  same-bar resolution OHLC supports is the open: a bar that OPENS beyond a level touched
  that level first. Otherwise the order is unknown and stays unknown, unless complete
  lower-timeframe bars resolve it (``resolve_with``), which is recorded as such.
- **R.** Only with an objective ex-ante invalidation level (e.g. the swept extreme). risk =
  direction x (entry - invalidation) > 0; otherwise R is unavailable, never invented.
  Targets +kR versus the -1R stop use the same first-touch ordering.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from market_signal.research.structure.registry import TradePathParams
from market_signal.research.structure.series import NAT, BarSeries

FAV, ADV, NEITHER = "FAVOURABLE_FIRST", "ADVERSE_FIRST", "NEITHER"
AMBIGUOUS = "AMBIGUOUS_INTRABAR_ORDER"
UNDEFINED = "UNDEFINED"


def first_touch(xo, xh, xl, fav_px, adv_px):
    """Vectorised first-touch over (E, H) oriented windows.

    Returns (fav_bar, adv_bar, order): bar offsets (H = never) and the order label. A bar
    reaching both levels is AMBIGUOUS unless its open is already at/beyond one of them."""
    e, h = xh.shape
    fav_px = np.asarray(fav_px, float)
    adv_px = np.asarray(adv_px, float)
    fh = xh >= fav_px[:, None]
    ah = xl <= adv_px[:, None]
    f = np.where(fh.any(axis=1), fh.argmax(axis=1), h)
    a = np.where(ah.any(axis=1), ah.argmax(axis=1), h)
    order = np.full(e, NEITHER, dtype=object)
    order[f < a] = FAV
    order[a < f] = ADV
    same = (f == a) & (f < h)
    o_at = xo[np.arange(e), np.minimum(f, h - 1)]
    order[same] = AMBIGUOUS
    order[same & (o_at >= fav_px)] = FAV
    order[same & (o_at <= adv_px)] = ADV
    order[~(np.isfinite(fav_px) & np.isfinite(adv_px))] = UNDEFINED
    return f, a, order


def _oriented_windows(series: BarSeries, side: str, idx: np.ndarray, h: int):
    xo, xh, xl, xc = series.oriented(side)
    return tuple(sliding_window_view(x, h)[idx] for x in (xo, xh, xl, xc))


def trade_paths(
    series: BarSeries,
    entries: pd.DataFrame,
    params: TradePathParams,
    *,
    resolve_with: BarSeries | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Path measures for each entry and horizon.

    ``entries``: ``event_id``, ``entry_after_ns`` (instant), ``direction`` (+1/-1) and
    optionally ``atr`` (price units, for ATR thresholds) and ``invalidation`` (price).
    Returns (paths, thresholds): one row per (event, horizon), and one per (event,
    horizon, criterion) with first-reach bars and the ordering of each symmetric pair
    (+x vs -x for pct and ATR levels, +kR vs -1R).
    """
    if resolve_with is not None and resolve_with.step >= series.step:
        raise ValueError("ambiguity can only be resolved with a FASTER timeframe")
    n = len(series)
    ent = entries.reset_index(drop=True)
    e_idx = np.searchsorted(series.open_time, ent["entry_after_ns"].to_numpy(np.int64), side="left")
    direction = ent["direction"].to_numpy(int)
    if not set(np.unique(direction)) <= {1, -1}:
        raise ValueError("direction must be +1 (long) or -1 (short)")
    atr = ent["atr"].to_numpy(float) if "atr" in ent else np.full(len(ent), np.nan)
    inval = (
        ent["invalidation"].to_numpy(float) if "invalidation" in ent else np.full(len(ent), np.nan)
    )
    paths, thresholds = [], []
    for h in params.horizons:
        for d, side in ((1, "high"), (-1, "low")):
            rows = np.flatnonzero(direction == d)
            if not len(rows):
                continue
            e = e_idx[rows]
            ok = e + h <= n
            ok[ok] &= series.segment[e[ok]] == series.segment[e[ok] + h - 1]
            base = pd.DataFrame({
                "event_id": ent["event_id"].to_numpy()[rows], "direction": d, "horizon": h,
                "entry_idx": np.where(e < n, e, -1),
                "entry_ns": np.where(e < n, series.open_time[np.minimum(e, n - 1)], NAT),
                "complete": ok,
            })  # fmt: skip
            good = np.flatnonzero(ok)
            cols = {k: np.full(len(rows), np.nan) for k in (
                "entry_price", "mfe", "mae", "mfe_close", "mae_close", "bars_to_mfe",
                "bars_to_mae", "terminal_return", "risk", "risk_bps", "risk_pct", "risk_atr",
                "r_terminal", "r_mfe", "r_mae")}  # fmt: skip
            obj = {
                k: np.full(len(rows), None, dtype=object) for k in ("mfe_mae_order", "risk_status")
            }
            ts = {k: np.full(len(rows), NAT, dtype=np.int64)
                  for k in ("mfe_by_ns", "mae_by_ns", "path_end_ns", "path_available_ns")}  # fmt: skip
            if len(good):
                ge = e[good]
                xo, xh, xl, xc = _oriented_windows(series, side, ge, h)
                entry = xo[:, 0]
                scale = np.abs(entry)
                fav = (xh - entry[:, None]) / scale[:, None]
                adv = (entry[:, None] - xl) / scale[:, None]
                favc = (xc - entry[:, None]) / scale[:, None]
                mfe = np.maximum(fav.max(axis=1), 0.0)
                mae = np.maximum(adv.max(axis=1), 0.0)
                bm = np.where(mfe > 0, fav.argmax(axis=1), -1)
                ba = np.where(mae > 0, adv.argmax(axis=1), -1)
                cols["entry_price"][good] = d * entry
                cols["mfe"][good], cols["mae"][good] = mfe, mae
                cols["mfe_close"][good] = np.maximum(favc.max(axis=1), 0.0)
                cols["mae_close"][good] = np.maximum((-favc).max(axis=1), 0.0)
                cols["bars_to_mfe"][good] = np.where(bm >= 0, bm, np.nan)
                cols["bars_to_mae"][good] = np.where(ba >= 0, ba, np.nan)
                cols["terminal_return"][good] = favc[:, -1]
                ts["mfe_by_ns"][good] = np.where(
                    bm >= 0, series.close_time[ge + np.maximum(bm, 0)], NAT
                )
                ts["mae_by_ns"][good] = np.where(
                    ba >= 0, series.close_time[ge + np.maximum(ba, 0)], NAT
                )
                ts["path_end_ns"][good] = series.close_time[ge + h - 1]
                ts["path_available_ns"][good] = series.ready_at[ge + h - 1]
                # order of the extremes themselves
                fpx = np.where(mfe > 0, entry + mfe * scale, np.inf)
                apx = np.where(mae > 0, entry - mae * scale, -np.inf)
                _, _, order = first_touch(xo, xh, xl, fpx, apx)
                order = np.where((mfe == 0) & (mae == 0), "NONE", order)
                order = np.where((mfe > 0) & (mae == 0), FAV, order)
                order = np.where((mfe == 0) & (mae > 0), ADV, order)
                obj["mfe_mae_order"][good] = order

                crit: list[tuple[str, float, np.ndarray, np.ndarray]] = []
                for x in params.pct_levels:
                    crit.append((f"pct_{x:g}", x, entry + scale * x / 100, entry - scale * x / 100))
                a_ = atr[rows][good]
                for x in params.atr_levels:
                    crit.append((f"atr_{x:g}", x, entry + x * a_, entry - x * a_))
                # R: ex-ante invalidation only
                inv = d * inval[rows][good]
                risk = entry - inv
                rstat = np.where(~np.isfinite(inv), "UNAVAILABLE",
                                 np.where(risk > 0, "OK", "INVALID_AT_ENTRY"))  # fmt: skip
                obj["risk_status"][good] = rstat
                rk = np.where(rstat == "OK", risk, np.nan)
                cols["risk"][good] = rk
                cols["risk_bps"][good] = rk / scale * 1e4
                cols["risk_pct"][good] = rk / scale * 100
                cols["risk_atr"][good] = rk / a_
                cols["r_terminal"][good] = (xc[:, -1] - entry) / rk
                cols["r_mfe"][good] = mfe * scale / rk
                cols["r_mae"][good] = mae * scale / rk
                for k in params.r_targets:
                    crit.append((f"r_{k:g}", k, entry + k * rk, entry - rk))
                for name, level, fpx, apx in crit:
                    fb, ab, order = first_touch(xo, xh, xl, fpx, apx)
                    t = pd.DataFrame({
                        "event_id": base["event_id"].to_numpy()[good], "direction": d,
                        "horizon": h, "criterion": name, "level": level,
                        "favourable_price": d * fpx, "adverse_price": d * apx,
                        "favourable_bar": np.where(fb < h, fb, -1),
                        "adverse_bar": np.where(ab < h, ab, -1),
                        "favourable_by_ns": np.where(fb < h, series.close_time[ge + np.minimum(fb, h - 1)], NAT),
                        "adverse_by_ns": np.where(ab < h, series.close_time[ge + np.minimum(ab, h - 1)], NAT),
                        "order": order, "resolution": None, "resolution_tf": None,
                        "coverage_complete": None, "resolved_available_ns": NAT,
                        "_entry_idx": ge, "_side": side,
                    })  # fmt: skip
                    if resolve_with is not None:
                        t["coverage_complete"] = t["coverage_complete"].astype(object)
                        _resolve(t, series, resolve_with)
                    thresholds.append(t.drop(columns=["_entry_idx", "_side"]))
            base = base.assign(**cols, **obj, **ts)
            paths.append(base)
    pcols = None
    out = pd.concat(paths, ignore_index=True) if paths else pd.DataFrame(columns=pcols)
    thr = pd.concat(thresholds, ignore_index=True) if thresholds else pd.DataFrame()
    if not out.empty:
        out = out.sort_values(["event_id", "horizon"], kind="stable").reset_index(drop=True)
    if not thr.empty:
        thr = thr.sort_values(["event_id", "horizon", "criterion"], kind="stable").reset_index(
            drop=True
        )
    return out, thr


def _resolve(t: pd.DataFrame, parent: BarSeries, child: BarSeries) -> None:
    """Resolve AMBIGUOUS rows with the child bars of the ambiguous parent bar (in place).

    Coverage must be complete (every child bar of the parent interval present, one
    segment) and consistent (the children reach both levels too); otherwise the row stays
    ambiguous and the reason is recorded. The child sequence is ordered with the same
    first-touch rule, so a child bar touching both is still ambiguous."""
    per = int(parent.step // child.step)
    for i in np.flatnonzero(t["order"].to_numpy() == AMBIGUOUS):
        side = t["_side"].iat[i]
        k = int(t["favourable_bar"].iat[i])
        p = int(t["_entry_idx"].iat[i]) + k
        a = int(np.searchsorted(child.open_time, parent.open_time[p], side="left"))
        b = int(np.searchsorted(child.open_time, parent.close_time[p], side="left"))
        t.at[t.index[i], "resolution_tf"] = child.prov.timeframe
        complete = (
            b - a == per
            and child.open_time[a] == parent.open_time[p]
            and child.segment[a] == child.segment[b - 1]
        )
        t.at[t.index[i], "coverage_complete"] = bool(complete)
        if not complete:
            t.at[t.index[i], "resolution"] = "INCOMPLETE_COVERAGE"
            continue
        xo, xh, xl, _ = child.oriented(side)
        d = int(t["direction"].iat[i])
        fpx = d * float(t["favourable_price"].iat[i])
        apx = d * float(t["adverse_price"].iat[i])
        if xh[a:b].max() < fpx or xl[a:b].min() > apx:
            t.at[t.index[i], "resolution"] = "INCONSISTENT"
            continue
        _, _, order = first_touch(xo[None, a:b], xh[None, a:b], xl[None, a:b], [fpx], [apx])
        t.at[t.index[i], "resolved_available_ns"] = int(child.ready_at[b - 1])
        if order[0] == AMBIGUOUS:
            t.at[t.index[i], "resolution"] = "STILL_AMBIGUOUS"
        else:
            t.at[t.index[i], "resolution"] = "RESOLVED"
            t.at[t.index[i], "order"] = order[0]
