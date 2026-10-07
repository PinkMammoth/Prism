"""Research-facing loader for ``microstructure_1m_v1`` (read-only; research must not query the
tables directly).

    load_microstructure(store, "BTC", start, end, known_at=t, freq="15min")

* **Causality.** ``known_at`` keeps only what Prism held at that instant: rows (and older
  revisions) with ``available_at <= known_at``, where ``available_at`` is the collector's
  finalization time (``availability="finalized"``, default) or, for a consumer that reads the
  database, the ingest time (``availability="ingested"``). Exchange time never grants
  availability. Minutes whose close is after ``known_at`` are not in the grid.
* **Completeness.** Every row has ``status``; minutes with no row are ``MISSING`` (never
  zero-filled). ``complete_only`` keeps COMPLETE rows. Ratios are derived from primitives.
* **Production only** (default): the dataset starts at the immutable production cutover;
  without one (development/scratch databases) nothing is returned unless
  ``production_only=False``.
* **Resampling** (``freq`` = 5min/15min/1h...): flows and counts are summed, ratios recomputed
  from summed primitives, book levels time-weighted by valid 1 s samples, first/last/high/low
  taken correctly, and the bucket is COMPLETE only if every minute is.
"""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd

from market_signal.data.store import Store
from market_signal.microstructure import definitions as d

SUM_COLS = ("n_buy", "n_sell", "n_buy_prints", "n_sell_prints", "buy_vol", "sell_vol",
            "buy_ntl", "sell_ntl", "book_updates", "bid_changes", "ask_changes", "mid_changes",
            "bid_replenish", "ask_replenish", "book_samples", "depth20_samples", "n_dup", "n_late")  # fmt: skip
LP_SUM = ("lp_n", "lp_buy_n", "lp_ntl", "lp_buy_ntl")
W5 = ("spread_mean", "spread_bps_mean", "bid5_mean", "ask5_mean")  # weight: book_samples
W20 = ("bid20_mean", "ask20_mean")  # weight: depth20_samples
LAST_COLS = ("bid_end", "ask_end", "bid5_end", "ask5_end", "bid20_end", "ask20_end", "oi_end",
             "mark_end", "oracle_end", "funding_end", "impact_bid_end", "impact_ask_end",
             "lp_threshold")  # fmt: skip


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def cutover(store: Store, feature_version: str = d.FEATURE_VERSION) -> dict | None:
    try:
        r = store.con.execute("SELECT * FROM microstructure_cutover WHERE feature_version=?",
                              [feature_version]).df()  # fmt: skip
    except Exception:
        return None
    return None if r.empty else json.loads(r.iloc[0].to_json(date_format="iso"))


def _divide(a, b):
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(b > 0, a / np.where(b > 0, b, 1.0), np.nan)


def _imb(bid, ask):
    bid, ask = np.asarray(bid, dtype=float), np.asarray(ask, dtype=float)
    return _divide(bid - ask, bid + ask)


def derive(df: pd.DataFrame) -> pd.DataFrame:
    """Ratios and signed flows from stored primitives (also used after resampling)."""
    out = df.copy()
    out["n_trades"] = out["n_buy"] + out["n_sell"]
    out["n_prints"] = out["n_buy_prints"] + out["n_sell_prints"]
    out["vol"] = out["buy_vol"] + out["sell_vol"]
    out["ntl"] = out["buy_ntl"] + out["sell_ntl"]
    out["delta_vol"] = out["buy_vol"] - out["sell_vol"]
    out["delta_ntl"] = out["buy_ntl"] - out["sell_ntl"]
    out["buy_frac"] = _divide(out["buy_vol"], out["vol"])
    out["sell_frac"] = _divide(out["sell_vol"], out["vol"])
    out["vwap"] = _divide(out["ntl"], out["vol"])
    out["mean_print_ntl"] = _divide(out["ntl"], out["n_prints"])
    out["lp_sell_n"] = out["lp_n"] - out["lp_buy_n"]
    out["lp_sell_ntl"] = out["lp_ntl"] - out["lp_buy_ntl"]
    out["mid_end"] = (out["bid_end"] + out["ask_end"]) / 2
    out["spread_end_bps"] = _divide(out["ask_end"] - out["bid_end"], out["mid_end"]) * 1e4
    out["imb5"] = _imb(out["bid5_mean"], out["ask5_mean"])
    out["imb20"] = _imb(out["bid20_mean"], out["ask20_mean"])
    out["imb5_end"] = _imb(out["bid5_end"], out["ask5_end"])
    out["imb20_end"] = _imb(out["bid20_end"], out["ask20_end"])
    prev = out["mid_end"].shift(1)
    step = out["minute_open"].diff()
    width = out["minute_close"] - out["minute_open"]
    out["ret_mid"] = np.where((step == width) & prev.notna(), out["mid_end"] / prev - 1, np.nan)
    out["complete"] = out["status"] == "COMPLETE"
    return out


def _rows(store: Store, coin: str, start: pd.Timestamp, end: pd.Timestamp, fv: str,
          known_at: pd.Timestamp | None, avail_col: str) -> pd.DataFrame:  # fmt: skip
    cur = store.con.execute(
        "SELECT * FROM microstructure_minutes WHERE feature_version=? AND coin=? AND "
        "minute_open >= ? AND minute_open < ? ORDER BY minute_open",
        [fv, coin, start.to_pydatetime(), end.to_pydatetime()]).df()  # fmt: skip
    if known_at is None or cur.empty:
        return cur
    rev = store.con.execute(
        "SELECT row_json FROM microstructure_revisions WHERE feature_version=? AND coin=? AND "
        "minute_open >= ? AND minute_open < ?",
        [fv, coin, start.to_pydatetime(), end.to_pydatetime()]).fetchall()  # fmt: skip
    if rev:
        old = pd.DataFrame([json.loads(r[0]) for r in rev])
        for c in ("minute_open", "first_recv_at", "last_recv_at", "finalized_at", "ingested_at"):
            old[c] = pd.to_datetime(old[c], utc=True)
        cur = pd.concat([cur, old[cur.columns]], ignore_index=True)
    cur[avail_col] = pd.to_datetime(cur[avail_col], utc=True)
    cur = cur[cur[avail_col] <= known_at]
    cur = cur.sort_values(["minute_open", "revision"]).drop_duplicates("minute_open", keep="last")
    return cur.reset_index(drop=True)


def load_microstructure(store: Store, coin: str, start, end, *, known_at=None,
                        freq: str = "1min", availability: str = "finalized",
                        complete_only: bool = False, fill_missing: bool = True,
                        production_only: bool = True,
                        feature_version: str = d.FEATURE_VERSION) -> pd.DataFrame:  # fmt: skip
    """1-minute primitives (+ derived fields) for ``coin`` in ``[start, end)``, optionally
    resampled. See the module docstring for causality, completeness and cutover rules."""
    if availability not in ("finalized", "ingested"):
        raise ValueError("availability must be 'finalized' or 'ingested'")
    avail_col = "finalized_at" if availability == "finalized" else "ingested_at"
    start, end = _ts(start).floor("min"), _ts(end).ceil("min")
    k = None if known_at is None else _ts(known_at)
    if k is not None:
        # a minute can be known only once it closed and its grace passed (finalization)
        end = min(end, (k - pd.Timedelta(milliseconds=d.GRACE_MS)).floor("min"))
    if production_only:
        co = cutover(store, feature_version)
        if co is None:
            return _empty()
        start = max(start, _ts(co["first_minute"]))
    if end <= start:
        return _empty()
    df = _rows(store, coin, start, end, feature_version, k, avail_col)
    df["minute_open"] = pd.to_datetime(df["minute_open"], utc=True)
    if fill_missing:
        grid = pd.date_range(start, end, freq="1min", inclusive="left", tz="UTC")
        df = df.set_index("minute_open").reindex(grid).rename_axis("minute_open").reset_index()
        df["status"] = df["status"].fillna("MISSING")
        df["coin"] = coin
        df["feature_version"] = feature_version
        for c in ("trade_cov", "book_samples", "depth20_samples"):
            df[c] = df[c].fillna(0)
    df["minute_close"] = df["minute_open"] + pd.Timedelta(minutes=1)
    df["available_at"] = pd.to_datetime(df[avail_col], utc=True)
    df["availability"] = availability
    if freq not in ("1min", "1m", "1T"):
        df = resample(df, freq)
    df = derive(df)
    if complete_only:
        df = df[df["complete"]].reset_index(drop=True)
    return df


def _empty() -> pd.DataFrame:
    cols = [*d.MINUTE_COLUMNS, "minute_close", "available_at", "availability"]
    return derive(pd.DataFrame({c: pd.Series(dtype="object") for c in cols}).assign(
        minute_open=pd.Series(dtype="datetime64[ns, UTC]"),
        minute_close=pd.Series(dtype="datetime64[ns, UTC]"),
        status=pd.Series(dtype="object"),
        **{c: pd.Series(dtype=float) for c in (*SUM_COLS, *LP_SUM, *W5, *W20, *LAST_COLS)}))  # fmt: skip


def resample(df1: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Deterministic aggregation of a 1-minute frame (one coin, fill_missing grid)."""
    width = pd.Timedelta(freq)
    expected = int(width / pd.Timedelta(minutes=1))
    df = df1.copy()
    df["bucket"] = df["minute_open"].dt.floor(freq)
    rows = []
    for b, g in df.groupby("bucket", sort=True):
        r: dict = {"minute_open": b, "minute_close": b + width, "coin": g["coin"].iloc[0],
                   "feature_version": g["feature_version"].iloc[0]}  # fmt: skip
        observed = g[g["status"].isin(["COMPLETE", "PARTIAL", "TRADE_ONLY", "BOOK_ONLY"])]
        n_complete = int((g["status"] == "COMPLETE").sum())
        r["n_minutes"] = int(g["status"].ne("MISSING").sum())
        r["n_complete"] = n_complete
        r["status"] = ("COMPLETE" if n_complete == expected and len(g) == expected
                       else "GAP" if observed.empty else "PARTIAL")  # fmt: skip
        for c in SUM_COLS:
            v = pd.to_numeric(g[c], errors="coerce")
            r[c] = v.sum() if v.notna().any() else np.nan
        traded = g[pd.to_numeric(g["n_buy"], errors="coerce").notna()]
        lp_ok = len(traded) > 0 and pd.to_numeric(traded["lp_n"], errors="coerce").notna().all()
        for c in LP_SUM:
            r[c] = pd.to_numeric(traded[c], errors="coerce").sum() if lp_ok else np.nan
        px = g[pd.to_numeric(g["first_px"], errors="coerce").notna()]
        r["first_px"] = px["first_px"].iloc[0] if len(px) else np.nan
        r["last_px"] = px["last_px"].iloc[-1] if len(px) else np.nan
        r["high_px"] = pd.to_numeric(g["high_px"], errors="coerce").max()
        r["low_px"] = pd.to_numeric(g["low_px"], errors="coerce").min()
        r["max_print_ntl"] = pd.to_numeric(g["max_print_ntl"], errors="coerce").max()
        r["med_print_ntl"] = np.nan  # a median is not recomputable from minute medians
        hists = [h for h in g["size_hist"] if isinstance(h, list | np.ndarray)]
        r["size_hist"] = (
            list(np.sum([np.asarray(h, dtype=int) for h in hists], axis=0)) if hists else None
        )
        for cols, wcol in ((W5, "book_samples"), (W20, "depth20_samples")):
            w = pd.to_numeric(g[wcol], errors="coerce").fillna(0)
            for c in cols:
                v = pd.to_numeric(g[c], errors="coerce")
                m = v.notna() & (w > 0)
                r[c] = float((v[m] * w[m]).sum() / w[m].sum()) if m.any() else np.nan
        r["spread_bps_med"] = np.nan
        r["spread_bps_min"] = pd.to_numeric(g["spread_bps_min"], errors="coerce").min()
        r["spread_bps_max"] = pd.to_numeric(g["spread_bps_max"], errors="coerce").max()
        last = g.iloc[-1]
        for c in LAST_COLS:
            r[c] = last[c]
        r["trade_cov"] = float(
            pd.to_numeric(g["trade_cov"], errors="coerce").fillna(0).sum() / expected
        )
        r["lat_max_ms"] = pd.to_numeric(g["lat_max_ms"], errors="coerce").max()
        r["lat_p50_ms"] = np.nan
        r["revision"] = pd.to_numeric(g["revision"], errors="coerce").max()
        for c in ("finalized_at", "ingested_at", "available_at"):
            r[c] = pd.to_datetime(g[c], utc=True).max() if c in g else pd.NaT
        r["availability"] = g["availability"].iloc[0] if "availability" in g else None
        rows.append(r)
    return pd.DataFrame(rows)


def cvd(df: pd.DataFrame, *, reset: str | None = "daily", window: str | None = None,
        col: str = "delta_ntl") -> pd.DataFrame:  # fmt: skip
    """Cumulative volume delta, derived at query time from stored signed flow.

    ``reset="daily"`` restarts at 00:00 UTC; ``window="1h"`` is a rolling sum over the last
    window of the (gap-filled) grid; ``reset=None`` with no window cumulates from the frame's
    first row (an arbitrary anchor: only differences are meaningful). ``cvd_valid`` is False
    whenever any contributing minute is not COMPLETE or a rolling window is not full.
    Absolute all-time CVD is never stored.
    """
    out = df[["minute_open", "status", col]].copy()
    flow = pd.to_numeric(out[col], errors="coerce").fillna(0.0)
    ok = (out["status"] == "COMPLETE").astype(int)
    if window is not None:
        s = pd.Series(flow.values, index=out["minute_open"])
        o = pd.Series(ok.values, index=out["minute_open"])
        n = pd.Series(1, index=out["minute_open"])
        need = int(
            pd.Timedelta(window) / (out["minute_open"].diff().median() or pd.Timedelta(minutes=1))
        )
        out["cvd"] = s.rolling(window).sum().values
        full = n.rolling(window).sum().values >= need
        out["cvd_valid"] = (o.rolling(window).min().values == 1) & full
        return out
    key = out["minute_open"].dt.floor("D") if reset == "daily" else pd.Series(0, index=out.index)
    out["cvd"] = flow.groupby(key.values).cumsum().values
    out["cvd_valid"] = ok.groupby(key.values).cummin().values.astype(bool)
    return out


def align_asof(left: pd.DataFrame, right: pd.DataFrame, *, right_time: str,
               left_time: str = "available_at", suffix: str = "_ctx") -> pd.DataFrame:  # fmt: skip
    """Attach to each left row the latest right row Prism held at the left row's availability
    (``right_time <= left_time``, backward as-of join). Use it to align context snapshots,
    positioning or volatility states with microstructure without lookahead: e.g. ``right`` =
    context snapshots with ``right_time="recorded_at"``. Rows are never merged in storage."""
    lt = left.copy()
    rt = right.copy()
    lt[left_time] = pd.to_datetime(lt[left_time], utc=True)
    rt[right_time] = pd.to_datetime(rt[right_time], utc=True)
    has = lt[left_time].notna()
    merged = pd.merge_asof(lt[has].sort_values(left_time), rt.sort_values(right_time),
                           left_on=left_time, right_on=right_time, direction="backward",
                           allow_exact_matches=True, suffixes=("", suffix))  # fmt: skip
    return (
        pd.concat([merged, lt[~has]], ignore_index=True)
        .sort_values("minute_open")
        .reset_index(drop=True)
    )


def at(df: pd.DataFrame, when: datetime) -> pd.DataFrame:
    """Rows available at ``when`` (a convenience filter on an already loaded frame)."""
    return df[pd.to_datetime(df["available_at"], utc=True) <= _ts(when)]
