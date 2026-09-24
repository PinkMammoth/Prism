"""SETUP C — Fundamental Re-rating / Dislocation.

Equities (point-in-time, backtestable): business still healthy (TTM revenue growth
>= min, net income growth >= min, profitable) while valuation has compressed (P/E in the
cheapest ``max_pe_pct_5y`` of its own trailing-5y history, as known at the time) and price
has dislocated (>= ``min_drawdown`` below the 52-week high).

Crypto (current/prospective only): fundamentals have no defensible historical
availability, so the historical evaluation marks crypto bars as NOT eligible. Live
evaluation (scanner) uses current fundamentals via ``ctx.extra['current_fundamentals']``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from market_signal.backtest.engine import ExitRules
from market_signal.setups.base import SetupContext, SetupOutput, edge_trigger, register
from market_signal.setups.common import class_params, load_setup_config


@register
class Rerating:
    name = "rerating"
    title = "Fundamental Re-rating / Dislocation"
    kind = "INVESTMENT"
    needs_fundamentals = True

    def default_params(self, asset_class: str) -> dict[str, Any]:
        return class_params(load_setup_config(self.name), asset_class)

    def evaluate(
        self, feat: pd.DataFrame, params: dict[str, Any], ctx: SetupContext
    ) -> SetupOutput:
        idx = feat.index
        nan_stop = pd.Series(np.nan, index=idx)
        if ctx.asset_class == "crypto":
            return self._crypto_current(feat, params, ctx)
        fund = ctx.fundamentals
        if fund is None or fund.empty:
            none = pd.Series(False, index=idx)
            return SetupOutput(none, nan_stop, none, None)
        p = params
        c = pd.DataFrame(index=idx)
        c["revenue_growing"] = fund["rev_growth"] >= p["min_rev_growth"]
        c["earnings_not_deteriorating"] = fund["ni_growth"] >= p["min_ni_growth"]
        if p["require_profitable"]:
            c["profitable"] = fund["net_income_ttm"] > 0
        c["valuation_compressed"] = fund["pe_pct_5y"] <= p["max_pe_pct_5y"]
        c["price_dislocated"] = feat["dist_52w_high"] <= -p["min_drawdown"]
        eligible = (
            fund[["rev_growth", "ni_growth", "pe_pct_5y"]].notna().all(axis=1)
            & feat["dist_52w_high"].notna()
        )
        signal = edge_trigger(c.all(axis=1) & eligible, int(p["cooldown"]))
        return SetupOutput(signal, nan_stop, eligible, c)

    def _crypto_current(
        self, feat: pd.DataFrame, p: dict[str, Any], ctx: SetupContext
    ) -> SetupOutput:
        idx = feat.index
        none = pd.Series(False, index=idx)
        cur = ctx.extra.get("current_fundamentals")
        if not cur:  # historical evaluation: never eligible (no PIT fundamentals)
            return SetupOutput(none, pd.Series(np.nan, index=idx), none, None)
        c = pd.DataFrame(index=idx[-1:])
        by = cur.get("buyback_yield")
        c["fundamental_yield_attractive"] = [
            by is not None and by >= p.get("hype_min_buyback_yield", 0.05)
        ]
        c["price_dislocated"] = [bool(feat["dist_52w_high"].iloc[-1] <= -p["min_drawdown"])]
        sig = none.copy()
        sig.iloc[-1] = bool(c.all(axis=1).iloc[0])
        elig = none.copy()
        elig.iloc[-1] = True
        return SetupOutput(sig, pd.Series(np.nan, index=idx), elig, c)

    def exit_rules(self, params: dict[str, Any]) -> ExitRules:
        return ExitRules(max_hold_bars=int(params["max_hold_bars"]))
