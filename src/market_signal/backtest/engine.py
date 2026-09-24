"""Event-driven, multi-asset trade simulator.

Execution model (see config/backtest.yaml):
  - Signals are read at bar t's close; entries fill at bar t+1's OPEN with slippage+fees.
    There is no same-close fill path.
  - If bar t+1 opens at/through the stop, the entry is skipped (you would not buy an
    already-invalidated setup).
  - Stops/targets trigger intrabar on bars >= entry bar. A gap through the stop fills at
    the open. If stop and target are both touched in one bar, the STOP is assumed first.
  - Exits decided at a close (time stop, trend exit) fill at the next bar's open.
  - Prices for triggers are split-adjusted; P&L is valued in total-return terms (a
    per-bar dividend factor ``trm`` = tr_close / close), so dividends are credited.
  - Sizing: risk_per_trade / stop distance (TRADE) or a fixed fraction (INVESTMENT), capped
    by max_position_fraction and by max_gross_exposure (no leverage). Orders that cannot
    be funded are skipped and logged, never scaled into leverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from market_signal.backtest.events import Costs
from market_signal.backtest.metrics import curve_metrics, trade_metrics


@dataclass
class ExitRules:
    max_hold_bars: int = 63
    target_r: float | None = None  # take profit at entry + target_r * (entry - stop)
    trend_exit_sma: str | None = None  # e.g. "sma_200": exit next open after close below it
    trend_exit_buffer: float = 0.0


@dataclass
class Sizing:
    method: str = "risk"  # risk | fixed
    risk_per_trade: float = 0.005
    fixed_fraction: float = 0.10
    max_position_fraction: float = 0.25
    max_gross_exposure: float = 1.0
    min_position_fraction: float = 0.002


@dataclass
class AssetInput:
    symbol: str
    asset_class: str
    feat: pd.DataFrame  # ts, close_time, open, high, low, close, tr_close (+ sma cols)
    signal: pd.Series  # bool
    stop: pd.Series  # stop price (split basis) at signal bar; NaN allowed only for fixed sizing
    costs: Costs
    risk_multiplier: pd.Series | None = None  # e.g. from regime policy, aligned
    setup: str = ""
    kind: str = "TRADE"


@dataclass
class _Position:
    symbol: str
    entry_bar: int
    entry_time: pd.Timestamp
    signal_time: pd.Timestamp
    entry_px: float  # split basis, after slippage
    units: float  # total-return units
    stop: float
    target: float
    alloc: float
    risk_frac: float
    min_low: float
    max_high: float
    pending_exit: str | None = None


@dataclass
class SimResult:
    trades: pd.DataFrame
    equity: pd.Series  # daily
    exposure: pd.Series  # daily gross exposure fraction
    skipped: pd.DataFrame
    metrics: dict = field(default_factory=dict)


def simulate(
    inputs: list[AssetInput], rules: ExitRules, sizing: Sizing, initial_equity: float = 100_000.0
) -> SimResult:
    data = {}
    events = []
    for a in inputs:
        f = a.feat.reset_index(drop=True)
        trm = (f["tr_close"] / f["close"]).to_numpy() if "tr_close" in f else np.ones(len(f))
        arrays = {
            "ts": pd.DatetimeIndex(f["ts"]), "close_time": pd.DatetimeIndex(f["close_time"]),
            "open": f["open"].to_numpy(float), "high": f["high"].to_numpy(float),
            "low": f["low"].to_numpy(float), "close": f["close"].to_numpy(float), "trm": trm,
            "signal": a.signal.reset_index(drop=True).fillna(False).to_numpy(bool),
            "stop": a.stop.reset_index(drop=True).to_numpy(float),
            "rmult": (a.risk_multiplier.reset_index(drop=True).to_numpy(float) if a.risk_multiplier is not None else np.ones(len(f))),
            "trend": (f[rules.trend_exit_sma].to_numpy(float) if rules.trend_exit_sma else None),
            "input": a,
        }  # fmt: skip
        data[a.symbol] = arrays
        ct = arrays["close_time"].as_unit("ns").asi8
        events.extend((ct[i], a.symbol, i) for i in range(len(f)))
    events.sort()

    cash = initial_equity
    positions: dict[str, _Position] = {}
    # sym -> (signal_bar, stop, risk multiplier). Same-time orders fill in symbol order.
    pending_entry: dict[str, tuple[int, float, float]] = {}
    last_value: dict[str, float] = {}
    trades, skipped = [], []
    curve_t, curve_eq, curve_exp = [], [], []
    turnover = 0.0

    def equity_now() -> float:
        return cash + sum(last_value.get(s, 0.0) for s in positions)

    def close_position(sym: str, bar: int, px: float, reason: str) -> None:
        nonlocal cash, turnover
        d = data[sym]
        p = positions.pop(sym)
        c = d["input"].costs
        fill = px * (1 - c.slippage_bps / 1e4)
        gross = p.units * fill * d["trm"][bar]
        proceeds = gross * (1 - c.fee_bps / 1e4)
        cash += proceeds
        turnover += gross
        last_value.pop(sym, None)
        ret = proceeds / p.alloc - 1
        r_mult = (
            ret / p.risk_frac
            if p.risk_frac and np.isfinite(p.risk_frac) and p.risk_frac > 0
            else np.nan
        )
        trades.append({
            "symbol": sym, "setup": d["input"].setup, "kind": d["input"].kind,
            "signal_time": p.signal_time, "entry_time": p.entry_time, "entry_price": p.entry_px,
            "stop": p.stop, "target": p.target, "exit_time": d["ts"][bar] if reason != "end_of_data" else d["close_time"][bar],
            "exit_price": fill, "exit_reason": reason, "bars_held": bar - p.entry_bar + 1,
            "ret": ret, "r_multiple": r_mult, "alloc": p.alloc,
            "mae": p.min_low / p.entry_px - 1, "mfe": p.max_high / p.entry_px - 1,
        })  # fmt: skip

    for ct, sym, i in events:
        d = data[sym]
        a: AssetInput = d["input"]
        o, h, lo, cl = d["open"][i], d["high"][i], d["low"][i], d["close"][i]

        # 1) scheduled exit at this bar's open
        if sym in positions and positions[sym].pending_exit:
            close_position(sym, i, o, positions[sym].pending_exit)

        # 2) pending entry fills at this bar's open
        if sym in pending_entry:
            sig_bar, stop, rmult = pending_entry.pop(sym)
            fill = o * (1 + a.costs.slippage_bps / 1e4)
            if np.isfinite(stop) and o <= stop:
                skipped.append({"symbol": sym, "time": d["ts"][i], "reason": "gapped_through_stop"})
            else:
                eq = equity_now()
                risk_frac = (fill - stop) / fill if np.isfinite(stop) else np.nan
                if sizing.method == "risk":
                    if not (np.isfinite(risk_frac) and risk_frac > 0):
                        skipped.append(
                            {"symbol": sym, "time": d["ts"][i], "reason": "invalid_stop"}
                        )
                        risk_frac = None
                    frac = sizing.risk_per_trade * rmult / risk_frac if risk_frac else 0.0
                else:
                    frac = sizing.fixed_fraction * rmult
                frac = min(frac, sizing.max_position_fraction)
                gross_now = sum(last_value.get(s, 0.0) for s in positions)
                room = max(sizing.max_gross_exposure * eq - gross_now, 0.0)
                alloc = min(frac * eq, room, cash)
                if frac > 0 and alloc >= sizing.min_position_fraction * eq:
                    fee = alloc * a.costs.fee_bps / 1e4
                    units = (alloc - fee) / (fill * d["trm"][i])
                    cash -= alloc
                    turnover += alloc
                    target = (
                        fill + rules.target_r * (fill - stop)
                        if rules.target_r and np.isfinite(stop)
                        else np.nan
                    )
                    positions[sym] = _Position(
                        sym, i, d["ts"][i], d["close_time"][sig_bar], fill, units, stop, target, alloc,
                        risk_frac if risk_frac is not None else np.nan, lo, h,
                    )  # fmt: skip
                    last_value[sym] = units * o * d["trm"][i]
                elif frac > 0:
                    skipped.append({"symbol": sym, "time": d["ts"][i], "reason": "no_capital"})

        # 3) intrabar stop / target for open position
        if sym in positions:
            p = positions[sym]
            p.min_low, p.max_high = min(p.min_low, lo), max(p.max_high, h)
            entry_bar = p.entry_bar == i
            if np.isfinite(p.stop) and lo <= p.stop:
                px = p.stop if entry_bar else min(o, p.stop)
                close_position(sym, i, px, "stop")
            elif np.isfinite(p.target) and h >= p.target:
                px = p.target if entry_bar else max(o, p.target)
                close_position(sym, i, px, "target")

        # 4) close-of-bar decisions for open position (executed next open)
        if sym in positions:
            p = positions[sym]
            last_value[sym] = p.units * cl * d["trm"][i]
            held = i - p.entry_bar + 1
            if held >= rules.max_hold_bars:
                p.pending_exit = "time"
            elif (
                d["trend"] is not None
                and np.isfinite(d["trend"][i])
                and cl < d["trend"][i] * (1 - rules.trend_exit_buffer)
            ):
                p.pending_exit = "trend_exit"

        # 5) new signal at this close -> order for next open
        if (
            d["signal"][i]
            and sym not in positions
            and sym not in pending_entry
            and i + 1 < len(d["ts"])
        ):
            pending_entry[sym] = (i, d["stop"][i], d["rmult"][i])

        eq = equity_now()
        curve_t.append(ct)
        curve_eq.append(eq)
        curve_exp.append(sum(last_value.get(s, 0.0) for s in positions) / eq if eq > 0 else np.nan)

    # mark remaining positions at their last close
    for sym in list(positions):
        d = data[sym]
        close_position(sym, len(d["ts"]) - 1, d["close"][-1], "end_of_data")
    if curve_t:
        curve_eq[-1] = cash  # after liquidation (costs included)

    t = pd.to_datetime(np.asarray(curve_t, dtype="int64"), utc=True)
    eq_s = pd.Series(curve_eq, index=t).groupby(t.floor("D")).last()
    exp_s = pd.Series(curve_exp, index=t).groupby(t.floor("D")).last()
    if len(eq_s):
        full = pd.date_range(eq_s.index[0], eq_s.index[-1], freq="D", tz="UTC")
        eq_s = eq_s.reindex(full).ffill()  # portfolio value between marks is unchanged
        exp_s = exp_s.reindex(full).ffill()
    trades_df = pd.DataFrame(trades)
    res = SimResult(trades_df, eq_s, exp_s, pd.DataFrame(skipped))
    res.metrics = {**trade_metrics(trades_df), **curve_metrics(eq_s, exp_s, turnover)}
    res.metrics["skipped"] = (
        res.skipped["reason"].value_counts().to_dict() if not res.skipped.empty else {}
    )
    return res
