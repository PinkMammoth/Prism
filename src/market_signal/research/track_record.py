"""Live track record: score the calls Prism actually made, after the fact.

Backtests are history; this is live evidence. Every persisted scan (``scan_results``) is
a dated, unrevisable record of what Prism said. Nothing here re-runs or backfills a
scan: only calls that were recorded at the time are scored.

Calls
  A *call* is the first scan bar on which an asset entered the ACT group (engine status
  ACTIONABLE / STRONG / EXCEPTIONAL) or the WAIT group. Consecutive scans of the same
  asset in the same group are the same call (an episode), not new evidence. Several
  scans of one bar (dashboard re-loads) collapse to the earliest one.

Timing — identical to the event study (backtest/events.py)
  The call is made on bar t's close. Entry is bar t+1's OPEN plus costs; a horizon of h
  bars exits at bar t+h's CLOSE minus costs; total-return prices. If bar t+h does not
  exist yet the call is *pending*: it is never truncated into the statistics.

Benchmark ("random entry")
  A random pick from the same asset class, entered at the same time: the mean net return
  over the same window of every asset of that class scanned on the same bar. Excess = call
  return − that mean. This isolates selection skill from the market's move.

WAIT calls answer a different question: did the price reach the preferred entry (the
top of the entry zone) within the window, and did waiting beat buying immediately?
  wait edge = (return after filling at the preferred price, or 0 if never filled: cash)
              − (return from buying at the next open anyway)
Positive wait edge = the discipline paid.

Independence: for each horizon, calls on the same asset whose holding windows overlap
(< h bars apart) are dropped from the independent count, as in the backtest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from market_signal.backtest.events import costs_for
from market_signal.config import Settings
from market_signal.data.store import Store
from market_signal.features import FeatureStore
from market_signal.models.domain import Calendar

ACT_STATUSES = ("ACTIONABLE", "STRONG", "EXCEPTIONAL")
HORIZONS = ("1m", "3m")
EPISODE_GAP_DAYS = 5  # a scan gap longer than this starts a new call


def status_group(status: str | None) -> str:
    if status in ACT_STATUSES:
        return "ACTIONABLE"
    return "WAIT" if status == "WAIT" else "OTHER"


# --------------------------------------------------------------------------- calls


def load_scan_rows(store: Store) -> pd.DataFrame:
    """One row per (symbol, scanned bar): the earliest scan of that bar."""
    try:
        df = store.query(
            """SELECT r.scan_id, r.symbol, r.setup, r.score, r.status, r.price, r.payload,
                      s.created_at, s.config_hash
               FROM scan_results r JOIN scan_runs s USING (scan_id)"""
        )
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    rows = []
    for _, r in df.iterrows():
        try:
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else dict(r["payload"])
        except (TypeError, ValueError):
            continue
        setup = p.get("setup") or {}
        zones = p.get("zones") or {}
        ez = zones.get("entry_zone") or [None, None]
        rows.append({
            "scan_id": r["scan_id"], "created_at": pd.Timestamp(r["created_at"]),
            "config_hash": r["config_hash"], "symbol": r["symbol"],
            "asset_class": p.get("asset_class"), "bar": pd.Timestamp(p.get("as_of")),
            "status": r["status"], "group": status_group(r["status"]), "band": p.get("band"),
            "score": r["score"], "price": r["price"], "setup": r["setup"],
            "setup_state": setup.get("state"), "verdict": setup.get("research_verdict"),
            "zone_lo": _f(ez[0]), "zone_hi": _f(ez[1]), "invalidation": _f(zones.get("invalidation")),
        })  # fmt: skip
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.sort_values(["symbol", "bar", "created_at"])
    return out.drop_duplicates(["symbol", "bar"], keep="first").reset_index(drop=True)


def extract_calls(rows: pd.DataFrame) -> pd.DataFrame:
    """Collapse per-bar rows into call episodes (ACTIONABLE and WAIT groups only)."""
    cols = [*rows.columns, "days_in_state", "last_bar"]
    if rows.empty:
        return pd.DataFrame(columns=cols)
    calls = []
    for _, g in rows.groupby("symbol", sort=False):
        g = g.sort_values("bar")
        cur: dict | None = None
        prev_bar, prev_group = None, None
        for _, r in g.iterrows():
            gap = (r["bar"] - prev_bar).days if prev_bar is not None else None
            continuing = (
                cur is not None
                and r["group"] == prev_group
                and gap is not None
                and gap <= EPISODE_GAP_DAYS
            )
            if continuing:
                cur["days_in_state"] += 1
                cur["last_bar"] = r["bar"]
            else:
                if cur is not None:
                    calls.append(cur)
                cur = None
                if r["group"] in ("ACTIONABLE", "WAIT"):
                    cur = {**r.to_dict(), "days_in_state": 1, "last_bar": r["bar"]}
            prev_bar, prev_group = r["bar"], r["group"]
        if cur is not None:
            calls.append(cur)
    return pd.DataFrame(calls, columns=cols).sort_values("bar").reset_index(drop=True)


# --------------------------------------------------------------------------- scoring


@dataclass
class _Series:
    feat: pd.DataFrame
    horizons: dict[str, int]
    cost: float  # per side
    index_by_bar: dict[pd.Timestamp, int]


def _series(fs: FeatureStore, settings: Settings, bt: dict, symbol: str) -> _Series | None:
    asset = settings.universe.get(symbol)
    feat = fs(symbol) if asset is not None else None
    if feat is None or feat.empty or "tr_close" not in feat:
        return None
    key = "crypto" if asset.calendar == Calendar.CRYPTO_24_7 else "nyse"
    hz = {h: int(bt["horizons"][key][h]) for h in HORIZONS}
    cost = costs_for(bt, symbol, asset.asset_class.value).per_side
    ct = pd.to_datetime(feat["close_time"], utc=True)
    return _Series(feat.reset_index(drop=True), hz, cost, {t: i for i, t in enumerate(ct)})


def _locate(s: _Series, bar: pd.Timestamp) -> int | None:
    bar = pd.Timestamp(bar).tz_convert("UTC") if bar.tzinfo else pd.Timestamp(bar, tz="UTC")
    i = s.index_by_bar.get(bar)
    if i is not None:
        return i
    ct = pd.to_datetime(s.feat["close_time"], utc=True)
    prior = np.flatnonzero(ct.to_numpy() <= bar.to_datetime64())
    return int(prior[-1]) if len(prior) else None


def _net_return(s: _Series, t: int, h: int) -> float | None:
    f = s.feat
    if t + h >= len(f):
        return None
    entry = float(f["tr_open"].iloc[t + 1]) * (1 + s.cost)
    exit_ = float(f["tr_close"].iloc[t + h]) * (1 - s.cost)
    return exit_ / entry - 1


def _to_date(s: _Series, t: int) -> float | None:
    f = s.feat
    if t + 1 >= len(f):
        return None
    entry = float(f["tr_open"].iloc[t + 1]) * (1 + s.cost)
    return float(f["tr_close"].iloc[-1]) * (1 - s.cost) / entry - 1


def score_calls(
    store: Store, settings: Settings, calls: pd.DataFrame | None = None
) -> pd.DataFrame:
    """One row per (call, horizon) with outcome columns; pending rows keep NaN outcomes."""
    rows_all = load_scan_rows(store)
    if calls is None:
        calls = extract_calls(rows_all)
    if calls.empty:
        return pd.DataFrame()
    bt = settings.yaml("backtest.yaml")
    fs = FeatureStore(store, settings)
    cache: dict[str, _Series | None] = {}

    def series(sym: str) -> _Series | None:
        if sym not in cache:
            cache[sym] = _series(fs, settings, bt, sym)
        return cache[sym]

    # the benchmark basket for each call = every asset of the same class scanned on that bar
    basket = rows_all.groupby(["bar", "asset_class"])["symbol"].apply(list).to_dict()
    out = []
    for _, c in calls.iterrows():
        s = series(c["symbol"])
        t = _locate(s, c["bar"]) if s is not None else None
        for h in HORIZONS:
            row: dict[str, Any] = {
                **{k: c[k] for k in ("symbol", "asset_class", "group", "status", "setup", "verdict",
                                     "score", "price", "zone_hi", "invalidation", "bar",
                                     "days_in_state", "scan_id")},
                "horizon": h, "bars": None, "state": "no data", "ret": np.nan, "bench": np.nan,
                "excess": np.nan, "to_date": np.nan, "stop_hit": None, "filled": None,
                "days_to_fill": np.nan, "ret_after_fill": np.nan, "wait_edge": np.nan,
            }  # fmt: skip
            if s is None or t is None:
                out.append(row)
                continue
            n = s.horizons[h]
            row["bars"] = n
            ret = _net_return(s, t, n)
            row["to_date"] = _to_date(s, t)
            row["state"] = "pending" if ret is None else "complete"
            row["ret"] = np.nan if ret is None else ret
            end = min(t + n, len(s.feat) - 1)
            window = s.feat.iloc[t + 1 : end + 1]
            if ret is not None:
                peers = []
                for sym in basket.get((c["bar"], c["asset_class"]), [c["symbol"]]):
                    ps = series(sym)
                    pt = _locate(ps, c["bar"]) if ps is not None else None
                    pr = _net_return(ps, pt, ps.horizons[h]) if pt is not None else None
                    if pr is not None:
                        peers.append(pr)
                if peers:
                    row["bench"] = float(np.mean(peers))
                    row["excess"] = ret - row["bench"]
            if c["invalidation"] is not None and not window.empty:
                row["stop_hit"] = bool((window["low"] <= c["invalidation"]).any())
            if c["group"] == "WAIT":
                _score_wait(row, s, t, n, window, c["zone_hi"], ret)
            out.append(row)
    return pd.DataFrame(out)


def _score_wait(row: dict, s: _Series, t: int, n: int, window: pd.DataFrame, pref, chase):
    if pref is None or window.empty:
        return
    hit = np.flatnonzero((window["low"] <= pref).to_numpy())
    if len(hit):
        j = t + 1 + int(hit[0])
        f = s.feat
        fill_px = min(float(f["open"].iloc[j]), pref)  # a gap below the level fills at the open
        tr_fill = fill_px * float(f["tr_close"].iloc[j]) / float(f["close"].iloc[j])
        row["filled"], row["days_to_fill"] = True, j - t
        if t + n < len(f):
            after = float(f["tr_close"].iloc[t + n]) * (1 - s.cost) / (tr_fill * (1 + s.cost)) - 1
            row["ret_after_fill"] = after
    elif t + n < len(s.feat):
        row["filled"] = False  # only "not filled" once the window has closed
    if chase is not None and row["filled"] is not None:
        row["wait_edge"] = (row["ret_after_fill"] if row["filled"] else 0.0) - chase


# --------------------------------------------------------------------------- summary


def mark_independent(scored: pd.DataFrame) -> pd.DataFrame:
    """Flag calls whose holding window does not overlap an earlier kept call (same asset,
    same group, same horizon)."""
    if scored.empty:
        return scored.assign(independent=pd.Series(dtype=bool))
    scored = scored.sort_values("bar").copy()
    keep = pd.Series(False, index=scored.index)
    for _, g in scored.groupby(["symbol", "group", "horizon"]):
        last = None
        for i, r in g.iterrows():
            gap_days = (r["bar"] - last).days if last is not None else None
            span = r["bars"] if r["bars"] is not None else 0
            # calendar days per bar: crypto trades daily, NYSE ~5/7
            span_days = span * (1.0 if r["asset_class"] == "crypto" else 7 / 5)
            if last is None or gap_days >= span_days:
                keep[i] = True
                last = r["bar"]
    return scored.assign(independent=keep)


def summarise(scored: pd.DataFrame, by: str | None = None) -> pd.DataFrame:
    """Completed, independent calls only. ``by`` optionally splits (e.g. 'verdict')."""
    if scored.empty:
        return pd.DataFrame()
    sc = scored if "independent" in scored else mark_independent(scored)
    keys = ["group", "horizon"] + ([by] if by else [])
    out = []
    for k, g in sc.groupby(keys, dropna=False, sort=False):
        done = g[(g["state"] == "complete") & g["independent"]]
        rec = dict(zip(keys, k if isinstance(k, tuple) else (k,), strict=True))
        rec |= {"calls": len(g), "pending": int((g["state"] == "pending").sum()),
                "completed": int((g["state"] == "complete").sum()), "independent": len(done)}  # fmt: skip
        if rec["group"] == "ACTIONABLE":
            rec |= {
                "mean_return": _mean(done["ret"]), "mean_benchmark": _mean(done["bench"]),
                "mean_excess": _mean(done["excess"]), "median_excess": _med(done["excess"]),
                "beat_benchmark": _rate(done["excess"] > 0, done["excess"].notna()),
                "stop_hit_rate": _rate(done["stop_hit"] == True, done["stop_hit"].notna()),  # noqa: E712
            }  # fmt: skip
        else:
            filled = done["filled"].dropna()
            rec |= {
                "fill_rate": _rate(filled == True, filled.notna()),  # noqa: E712
                "median_days_to_fill": _med(done["days_to_fill"]),
                "mean_return_after_fill": _mean(done["ret_after_fill"]),
                "mean_chase_return": _mean(done["ret"]),
                "mean_wait_edge": _mean(done["wait_edge"]),
                "wait_beat_chase": _rate(done["wait_edge"] > 0, done["wait_edge"].notna()),
            }  # fmt: skip
        out.append(rec)
    return pd.DataFrame(out)


def scan_coverage(store: Store, days: int = 30) -> dict[str, Any]:
    """How many of the last ``days`` calendar days have at least one stored scan."""
    try:
        df = store.query("SELECT created_at FROM scan_runs")
    except Exception:
        return {"days": days, "with_scan": 0, "first": None, "last": None}
    if df.empty:
        return {"days": days, "with_scan": 0, "first": None, "last": None}
    d = pd.to_datetime(df["created_at"], utc=True).dt.normalize()
    cutoff = pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=days - 1)
    return {
        "days": days,
        "with_scan": int(d[d >= cutoff].nunique()),
        "first": d.min(),
        "last": d.max(),
    }


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _mean(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.mean()) if len(s) else None


def _med(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.median()) if len(s) else None


def _rate(cond: pd.Series, valid: pd.Series) -> float | None:
    n = int(valid.sum())
    return float((cond & valid).sum()) / n if n else None
