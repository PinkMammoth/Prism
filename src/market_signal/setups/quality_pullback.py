"""SETUP A — Quality Pullback.

Find assets in a constructive long-term trend (price > 200DMA, 50DMA > 200DMA, rising
200DMA, positive 6-month momentum) that have pulled back 2–6 ATR from their 3-month high
into the 50DMA area, with RSI cooling to 30–50 and volatility not extreme.

Trigger: first bar all conditions hold (edge-triggered) with a cooldown.
Invalidation: below the 20-bar swing low (−0.5 ATR), capped at 4 ATR.
Optional: a point-in-time fundamental score filter (equities only).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from market_signal.backtest.engine import ExitRules
from market_signal.setups.base import SetupContext, SetupOutput, edge_trigger, register
from market_signal.setups.common import between, class_params, load_setup_config, technical_stop


@register
class QualityPullback:
    name = "quality_pullback"
    title = "Quality Pullback"
    kind = "TRADE"
    needs_fundamentals = False

    def default_params(self, asset_class: str) -> dict[str, Any]:
        return class_params(load_setup_config(self.name), asset_class)

    def conditions(self, f: pd.DataFrame, p: dict[str, Any], ctx: SetupContext) -> pd.DataFrame:
        c = pd.DataFrame(index=f.index)
        if p["require_price_above_sma200"]:
            c["trend_price_above_sma200"] = f["close"] > f["sma_200"]
        if p["require_sma50_above_sma200"]:
            c["trend_sma50_above_sma200"] = f["sma_50"] > f["sma_200"]
        if p["require_sma200_rising"]:
            c["trend_sma200_rising"] = f["sma_200_slope"] > 0
        c["momentum_6m_positive"] = f["roc_6m"] > p["min_roc_6m"]
        c["pullback_depth"] = between(f["pullback_63_atr"], p["pb_min_atr"], p["pb_max_atr"])
        c["near_support_sma50"] = between(
            f["dist_sma_50_atr"], p["near_sma50_min_atr"], p["near_sma50_max_atr"]
        )
        c["rsi_cooled"] = between(f["rsi_14"], p["rsi_min"], p["rsi_max"])
        c["volatility_not_extreme"] = f["rvol_pct"] <= p["max_rvol_pct"]
        if p.get("min_fundamental_score") is not None:
            fs = (
                ctx.fundamentals["fundamental_score"]
                if ctx.fundamentals is not None
                else pd.Series(float("nan"), index=f.index)
            )
            c["fundamentals_ok"] = fs >= p["min_fundamental_score"]
        return c

    def evaluate(
        self, feat: pd.DataFrame, params: dict[str, Any], ctx: SetupContext
    ) -> SetupOutput:
        f = feat
        cond = self.conditions(f, params, ctx)
        needed = [
            "sma_200",
            "sma_200_slope",
            "roc_6m",
            "pullback_63_atr",
            "dist_sma_50_atr",
            "rsi_14",
            "rvol_pct",
            "atr_14",
            "swing_low_20",
        ]
        eligible = f[needed].notna().all(axis=1)
        all_true = cond.all(axis=1) & eligible
        signal = edge_trigger(all_true, int(params["cooldown"]))
        stop = technical_stop(
            f, float(params["stop_swing_buffer_atr"]), float(params["max_stop_atr"])
        )
        return SetupOutput(signal, stop, eligible, cond)

    def exit_rules(self, params: dict[str, Any]) -> ExitRules:
        return ExitRules(max_hold_bars=int(params["max_hold_bars"]))
