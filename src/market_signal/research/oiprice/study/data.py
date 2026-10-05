"""Phase 19 study inputs come ONLY from retained Lab snapshots (never today's tables).

Primary venue (Binance) datasets hold three series per coin: ``perp_intraday_bars`` 1h,
``perp_funding`` and ``perp_oi_history`` 1h. Comparison venue (Hyperliquid) datasets hold
one: ``perp_snapshots`` (Prism's prospective OI captures). Each is decoded with its own
venue semantics; nothing is merged across venues.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from market_signal.research.oiprice import primitives as op
from market_signal.research.structure.series import BarSeries
from market_signal.research.structure.study.data import bar_series, funding_arrays


@dataclass(frozen=True)
class OiCoinData:
    venue: str
    coin: str
    dataset_id: str
    bars: BarSeries | None  # primary venue only
    funding_ns: np.ndarray
    funding_rate: np.ndarray
    oi: op.OiSeries
    oi_rows: int


def decode(manifest_series, decoded_rows, *, dataset_id: str, venue: str, coin: str,
           bar_latency_s: float, oi_latency_s: float) -> OiCoinData:  # fmt: skip
    bars = None
    funding = (np.array([], dtype=np.int64), np.array([], dtype=float))
    oi = op.OiSeries.empty(venue, coin)
    n_oi = 0
    for fp, rows in zip(manifest_series, decoded_rows, strict=True):
        sel = fp.selection
        if sel.symbol != coin or sel.source != venue:
            raise ValueError(f"{dataset_id} holds {sel.source}/{sel.symbol}, not {venue}/{coin}")
        if sel.kind == "perp_intraday_bars":
            bars = bar_series(rows, venue=venue, coin=coin, tf=sel.timeframe.value,
                              latency_s=bar_latency_s, dataset_id=dataset_id)  # fmt: skip
        elif sel.kind == "perp_funding":
            funding = funding_arrays(rows, venue=venue, coin=coin)
        elif sel.kind == "perp_oi_history":
            oi, n_oi = op.binance_oi(rows, coin=coin, latency_s=oi_latency_s), len(rows)
        elif sel.kind == "perp_snapshots":
            oi, n_oi = op.hyperliquid_oi(rows, coin=coin), len(rows)
        else:
            raise ValueError(f"unexpected series kind {sel.kind} in a Phase 19 dataset")
    return OiCoinData(venue, coin, dataset_id, bars, *funding, oi, n_oi)


def load_coin(ledger, dataset_id: str, *, venue: str, coin: str, bar_latency_s: float,
              oi_latency_s: float) -> OiCoinData:  # fmt: skip
    """Decode and verify one retained dataset (hash-checked by the Lab ledger)."""
    manifest = ledger.get_dataset(dataset_id)
    return decode(manifest.series, ledger.read_dataset(dataset_id), dataset_id=dataset_id,
                  venue=venue, coin=coin, bar_latency_s=bar_latency_s,
                  oi_latency_s=oi_latency_s)  # fmt: skip
