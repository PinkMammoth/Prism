"""Read-only OI coverage audit for research scoping (Phase 19). Reads; never writes.

Per (venue, coin), inside ``[start, end)``: first/last observation, expected vs actual
observations, gaps, median spacing, whether rows were collected prospectively (ingested
within ``prospective_lag``) or backfilled, and the overlap with stored hourly price bars and
funding of the SAME venue. Binance is judged against its fixed 1h statistics period;
Hyperliquid snapshots against elapsed time (they are irregular by design). Venues are
reported side by side and never combined.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from market_signal.data.store import Store

PROSPECTIVE_LAG = timedelta(hours=2)


def _q(store: Store, sql: str, args: list) -> pd.DataFrame:
    try:
        return store.query(sql, args)
    except Exception:  # a table the database does not have yet (e.g. intraday bars)
        return pd.DataFrame()


def oi_coverage(store: Store, start, end, *, venues=("binance", "hyperliquid"),
                coins=None) -> pd.DataFrame:  # fmt: skip
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    rows, frames = [], {}
    for venue in venues:
        if venue == "binance":
            df = _q(store, "SELECT coin, observed_at AS t, ingested_at FROM perp_oi_history WHERE "
                           "source='binance' AND period='1h' AND observed_at>=? AND observed_at<? "
                           "ORDER BY coin, observed_at", [s, e])  # fmt: skip
        else:
            df = _q(store, "SELECT coin, snapshot_at AS t, snapshot_at AS ingested_at FROM "
                           "perp_snapshots WHERE source='hyperliquid' AND open_interest IS NOT NULL "
                           "AND snapshot_at>=? AND snapshot_at<? ORDER BY coin, snapshot_at",
                    [s, e])  # fmt: skip
        frames[venue] = df
    # A venue with no rows in the window is listed explicitly (zero observations), never
    # dropped: absence is the finding.
    names = sorted({c for df in frames.values() if len(df) for c in df["coin"]})
    for venue, df in frames.items():
        for coin in coins or names:
            g = df[df["coin"] == coin] if len(df) else df
            t = (
                pd.DatetimeIndex(pd.to_datetime(g["t"], utc=True))
                if len(g)
                else pd.DatetimeIndex([])
            )
            row = {"venue": venue, "coin": coin, "observations": len(t),
                   "first": t.min() if len(t) else None, "last": t.max() if len(t) else None}  # fmt: skip
            if len(t) >= 2:
                d = pd.Series(t[1:] - t[:-1]).dt.total_seconds() / 3600
                row["median_spacing_h"] = float(d.median())
                row["max_gap_h"] = float(d.max())
                row["gaps_over_2h"] = int((d > 2).sum())
            if venue == "binance" and len(t):
                expected = int((t.max() - t.min()) / pd.Timedelta("1h")) + 1
                row["expected"] = expected
                row["missing"] = expected - len(t)
                ing = pd.to_datetime(g["ingested_at"], utc=True).to_numpy()
                row["prospective_share"] = float(((ing - t.to_numpy()) <= PROSPECTIVE_LAG).mean())
            elif venue == "hyperliquid":
                row["prospective_share"] = 1.0 if len(t) else None
            bars = _q(store, "SELECT count(*) AS n, min(open_time) AS a, max(close_time) AS b FROM "
                             "perp_intraday_bars WHERE source=? AND coin=? AND timeframe='1h' AND "
                             "close_time>? AND close_time<=?", [venue, coin, s, e])  # fmt: skip
            row["price_bars_1h"] = int(bars["n"].iloc[0]) if len(bars) else 0
            f = _q(store, "SELECT time FROM perp_funding WHERE source=? AND coin=? AND time>=? "
                          "AND time<? ORDER BY time", [venue, coin, s, e])  # fmt: skip
            row["funding_settlements"] = len(f)
            if len(f) >= 2:
                ft = pd.to_datetime(f["time"], utc=True)
                row["funding_cadence_h"] = float(ft.diff().dt.total_seconds().div(3600).median())
            rows.append(row)
    return pd.DataFrame(rows, columns=None if rows else ["venue", "coin", "observations"])


__all__ = ["oi_coverage"]
