"""Study inputs come ONLY from retained Lab snapshots (never today's mutable tables).

One retained dataset per (venue, coin) holds four series: ``perp_intraday_bars`` at 4h, 1h
and 15m, and ``perp_funding``. Bars become ``BarSeries`` under the study's assumed latency
(backfilled history has no observed publication time), with the Lab dataset ID as the
series' ``dataset_key``, so every level and event ID carries the exact frozen source.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.research.structure.series import BarSeries, assumed


@dataclass(frozen=True)
class CoinData:
    venue: str
    coin: str
    dataset_id: str
    series: dict[str, BarSeries]  # timeframe -> series
    funding_ns: np.ndarray  # settlement times, int64 ns, ascending
    funding_rate: np.ndarray  # rate per settlement (positive: longs pay shorts)


def bar_series(rows: list[dict], *, venue: str, coin: str, tf: str, latency_s: float,
               dataset_id: str) -> BarSeries:  # fmt: skip
    cols = ["open_time", "close_time", "open", "high", "low", "close", "volume",
            "first_observed_at", "observed_live"]  # fmt: skip
    df = pd.DataFrame(rows, columns=None if rows else cols)
    if len(df):
        if (
            (df["source"] != venue).any()
            or (df["coin"] != coin).any()
            or (df["timeframe"] != tf).any()
        ):
            raise ValueError("snapshot rows do not match the selected venue/coin/timeframe")
        df = df[cols]
    return BarSeries.from_frame(df, venue=venue, coin=coin, timeframe=tf,
                                availability=assumed(latency_s), dataset_key=dataset_id)  # fmt: skip


def funding_arrays(rows: list[dict], *, venue: str, coin: str) -> tuple[np.ndarray, np.ndarray]:
    if not rows:
        return np.array([], dtype=np.int64), np.array([], dtype=float)
    df = pd.DataFrame(rows)
    if (df["source"] != venue).any() or (df["coin"] != coin).any():
        raise ValueError("funding rows do not match the selected venue/coin")
    df = df.sort_values("time")
    t = pd.DatetimeIndex(pd.to_datetime(df["time"], utc=True)).as_unit("ns").asi8
    return t.astype(np.int64), df["funding_rate"].to_numpy(float)


def load_coin(ledger, dataset_id: str, *, venue: str, coin: str, latency_s: float) -> CoinData:
    """Decode and verify one retained dataset (hash-checked by the Lab ledger)."""
    manifest = ledger.get_dataset(dataset_id)
    decoded = ledger.read_dataset(dataset_id)
    series, funding = {}, (np.array([], dtype=np.int64), np.array([], dtype=float))
    for fp, rows in zip(manifest.series, decoded, strict=True):
        sel = fp.selection
        if sel.symbol != coin or sel.source != venue:
            raise ValueError(f"{dataset_id} holds {sel.source}/{sel.symbol}, not {venue}/{coin}")
        if sel.kind == "perp_intraday_bars":
            tf = sel.timeframe.value
            series[tf] = bar_series(rows, venue=venue, coin=coin, tf=tf, latency_s=latency_s,
                                    dataset_id=dataset_id)  # fmt: skip
        elif sel.kind == "perp_funding":
            funding = funding_arrays(rows, venue=venue, coin=coin)
        else:
            raise ValueError(f"unexpected series kind {sel.kind} in a structure-study dataset")
    return CoinData(venue, coin, dataset_id, series, *funding)
