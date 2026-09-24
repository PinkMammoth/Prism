"""SETUP B — Breakout + Retest.

State machine, evaluated bar by bar using only past data:
  1. Resistance ``L_b`` = highest high of the ``base_len`` bars *before* bar b. The base must
     be tight (range <= max_base_width_atr × ATR).
  2. Breakout at b: close_b > L_b, above the 200DMA (optional), with expansion (true range
     >= expansion_tr_atr × ATR or relative volume >= expansion_rel_volume).
  3. Retest at t in (b + min_bars, b + retest_window]: low_t <= L + touch_atr·ATR (price
     returns to the level), close_t >= L − hold_atr·ATR (the level holds) and
     close_t <= L + max_extension_atr·ATR (not extended). Signal once per breakout.
  4. The breakout is cancelled if any close < L − fail_atr·ATR before a retest.
Invalidation (stop): L − stop_atr·ATR.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from market_signal.backtest.engine import ExitRules
from market_signal.setups.base import SetupContext, SetupOutput, register
from market_signal.setups.common import class_params, load_setup_config


@register
class BreakoutRetest:
    name = "breakout_retest"
    title = "Breakout + Retest"
    kind = "TRADE"
    needs_fundamentals = False

    def default_params(self, asset_class: str) -> dict[str, Any]:
        return class_params(load_setup_config(self.name), asset_class)

    def evaluate(
        self, feat: pd.DataFrame, params: dict[str, Any], ctx: SetupContext
    ) -> SetupOutput:
        p = params
        n = len(feat)
        L_base = int(p["base_len"])
        high, low, close = (feat[c].to_numpy(float) for c in ("high", "low", "close"))
        atr = feat["atr_14"].to_numpy(float)
        sma200 = feat["sma_200"].to_numpy(float)
        relvol = feat["rel_volume"].to_numpy(float)
        prev_close = np.r_[np.nan, close[:-1]]
        tr = np.nanmax(
            np.vstack([high - low, np.abs(high - prev_close), np.abs(low - prev_close)]), axis=0
        )
        # resistance/base from bars strictly before b
        hh = pd.Series(high).rolling(L_base, min_periods=L_base).max().shift(1).to_numpy()
        ll = pd.Series(low).rolling(L_base, min_periods=L_base).min().shift(1).to_numpy()
        atr_prev = np.r_[np.nan, atr[:-1]]

        signal = np.zeros(n, dtype=bool)
        stop = np.full(n, np.nan)
        level = np.full(n, np.nan)
        state_breakout = np.zeros(n, dtype=bool)
        active_level, active_bar = np.nan, -1
        last_signal = -(10**9)
        for i in range(n):
            a = atr[i]
            if np.isnan(a):
                continue
            # 3/4) manage an active breakout
            if active_bar >= 0:
                age = i - active_bar
                if close[i] < active_level - p["fail_atr"] * a or age > p["retest_window"]:
                    active_bar, active_level = -1, np.nan
                elif (
                    age >= p["min_bars_after_breakout"]
                    and low[i] <= active_level + p["touch_atr"] * a
                    and close[i] >= active_level - p["hold_atr"] * a
                    and close[i] <= active_level + p["max_extension_atr"] * a
                    and i - last_signal > p["cooldown"]
                ):
                    signal[i] = True
                    stop[i] = active_level - p["stop_atr"] * a
                    level[i] = active_level
                    last_signal = i
                    active_bar, active_level = -1, np.nan
                    continue
            # 1/2) new breakout (replaces an older active one)
            if np.isnan(hh[i]) or np.isnan(atr_prev[i]):
                continue
            tight = (hh[i] - ll[i]) <= p["max_base_width_atr"] * atr_prev[i]
            broke = close[i] > hh[i]
            trend_ok = (not p["require_trend"]) or (
                not np.isnan(sma200[i]) and close[i] > sma200[i]
            )
            expansion = tr[i] >= p["expansion_tr_atr"] * atr_prev[i] or (
                not np.isnan(relvol[i]) and relvol[i] >= p["expansion_rel_volume"]
            )
            if tight and broke and trend_ok and expansion:
                active_level, active_bar = hh[i], i
                state_breakout[i] = True

        idx = feat.index
        eligible = pd.Series(
            ~np.isnan(hh) & ~np.isnan(atr_prev) & (~np.isnan(sma200) | (not p["require_trend"])),
            index=idx,
        )
        diag = pd.DataFrame(
            {"breakout_bar": state_breakout, "retest_signal": signal, "level": level}, index=idx
        )
        return SetupOutput(pd.Series(signal, index=idx), pd.Series(stop, index=idx), eligible, diag)

    def exit_rules(self, params: dict[str, Any]) -> ExitRules:
        return ExitRules(max_hold_bars=int(params["max_hold_bars"]))
