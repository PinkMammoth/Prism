"""Feature loading: stored bars → split-adjusted prices → indicator frame.

``FeatureStore`` memoises per (symbol, timeframe) for the lifetime of one command, and
exposes the total-return close alongside for P&L so both price bases stay aligned.
"""

from __future__ import annotations

import pandas as pd

from market_signal.config import Settings
from market_signal.data.prices import PriceBasis, load_bars
from market_signal.data.store import Store
from market_signal.indicators.technical import compute_features
from market_signal.models.domain import Timeframe


class FeatureStore:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self._cache: dict[tuple[str, str], pd.DataFrame | None] = {}

    def features(self, symbol: str, timeframe: Timeframe = Timeframe.D1) -> pd.DataFrame | None:
        key = (symbol, timeframe.value)
        if key not in self._cache:
            asset = self.settings.universe.get(symbol)
            if asset is None or (asset.series_for(timeframe) is None and timeframe != Timeframe.W1):
                self._cache[key] = None
            else:
                bars = load_bars(self.store, asset, timeframe, PriceBasis.SPLIT)
                if bars.empty:
                    self._cache[key] = None
                else:
                    feats = compute_features(bars, asset.asset_class)
                    if timeframe == Timeframe.D1:
                        tr = load_bars(self.store, asset, timeframe, PriceBasis.TOTAL_RETURN)
                        feats["tr_open"] = tr["open"].to_numpy()
                        feats["tr_close"] = tr["close"].to_numpy()
                    self._cache[key] = feats
        return self._cache[key]

    __call__ = features
