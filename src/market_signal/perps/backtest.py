"""Perp-aware backtesting: longs AND shorts, funding, perp fees, margin and liquidation.

Separate from the spot engine (``backtest/engine.py``), which stays long-only and unchanged.
Two layers, mirroring the spot research:

1. Event study (``perp_asset_events`` → the existing ``run_event_study``)
   Forward returns per side on *notional* (no leverage), so the edge is measured cleanly:
     entry  = open[t+1] (signal read at bar t's close; no same-close fills)
     exit   = close[t+h]
     return = side × (exit / entry − 1) − 2 × (fee + slippage) − funding paid
   funding paid = side × Σ_{d=t+1..t+h} funding_day[d] × close[d] / entry
   (longs pay positive funding, shorts receive it; notional follows the price.)
   Each side of each coin is its own "asset" (``BTC:long``, ``BTC:short``), so its baseline
   is *random entry on the same side*. A short strategy is not credited for a market that
   simply fell, and a long one pays the same funding as its baseline. Any missing funding
   day inside a window makes that return missing (never assumed zero).

2. Portfolio simulation (``simulate_perps``): isolated margin per position.
   - Size from risk: notional = risk_per_trade × equity / stop distance (capped).
   - Leverage is an OUTPUT, never an input: the highest leverage ≤ ``leverage_cap`` and ≤
     the venue's maximum at which the liquidation price still sits ≥ ``liq_buffer`` × the stop
     distance away. If even 1× can't satisfy that, the trade is skipped.
   - Each bar, in this order: scheduled exit at the open, then pending entry at the open
     (skipped if it gaps through the stop), then the adverse intrabar move, then the
     favourable target, then funding, then close-of-bar decisions.
   - Adverse move, checked along the path from the open: a gap through the liquidation
     price liquidates at the open; a gap through the stop fills at the open. Otherwise,
     whichever of stop or liquidation is nearer the open is hit first. Funding paid erodes
     the position's margin each day, so the liquidation price drifts toward the entry and
     can overtake the stop.
   - Liquidation loses the position's whole remaining margin (conservative).
   - Stops fill with extra ``stop_slippage_bps``; every fill pays the taker fee on notional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from market_signal.backtest.events import AssetEvents
from market_signal.backtest.metrics import curve_metrics, trade_metrics

# --------------------------------------------------------------------------- costs & margin


@dataclass(frozen=True)
class PerpCosts:
    fee_bps: float
    slippage_bps: float
    stop_slippage_bps: float = 0.0

    @property
    def per_side(self) -> float:
        return (self.fee_bps + self.slippage_bps) / 1e4


def venue_config(cfg: dict, venue: str) -> dict:
    return ((cfg.get("venues") or {}).get(venue) or {}) if venue != "hyperliquid" else {}


def perp_costs(cfg: dict, coin: str, venue: str = "hyperliquid") -> PerpCosts:
    c = cfg.get("costs") or {}
    slip = c.get("slippage_bps") or {}
    v = venue_config(cfg, venue)
    return PerpCosts(
        float(v.get("taker_fee_bps", c.get("taker_fee_bps", 4.5))),
        float(slip.get(coin, slip.get("default", 8))),
        float(c.get("stop_slippage_bps", 10)),
    )


def venue_max_leverage(cfg: dict, coin: str, snapshot_value: float | None = None) -> float:
    if snapshot_value and snapshot_value > 0:
        return float(snapshot_value)
    d = (cfg.get("margin") or {}).get("default_max_leverage") or {}
    return float(d.get(coin, d.get("default", 10)))


def maintenance_rate(max_leverage: float) -> float:
    """Hyperliquid: maintenance margin = half the initial margin at max leverage."""
    return 1.0 / (2.0 * max_leverage)


def choose_leverage(
    stop_distance: float, maint: float, cap: float, venue_max: float, liq_buffer: float
) -> float | None:
    """Highest leverage ≤ cap/venue max whose liquidation sits ≥ liq_buffer × stop distance
    away. None if even 1× is not safe (or the stop is invalid)."""
    if not (np.isfinite(stop_distance) and 0 < stop_distance < 1):
        return None
    safe = 1.0 / (liq_buffer * stop_distance + maint)
    lev = min(cap, venue_max, safe)
    return float(lev) if lev >= 1.0 else None


def liquidation_price(
    entry: float, side: int, units: float, margin_balance: float, maint: float
) -> float:
    """Price at which isolated position equity falls to maintenance margin.

    equity(p) = margin + side·u·(p − e);  liquidated when equity(p) <= maint·u·p
    """
    u, e = units, entry
    p = (side * u * e - margin_balance) / (u * (side - maint))
    return float(max(p, 0.0))


# --------------------------------------------------------------------------- data alignment


def daily_funding(
    bars: pd.DataFrame,
    funding: pd.Series,
    min_coverage: float = 0.8,
    per_day: np.ndarray | None = None,
) -> np.ndarray:
    """Funding per daily bar: sum of the rates settled in (ts, ts + 1 day].

    Works for any settlement frequency (Hyperliquid hourly, Binance every 8h): the expected
    settlements per day come from the series' median spacing. A day with fewer settlements
    than that but at least ``min_coverage`` of them is scaled up pro rata. Below that, the
    day is missing (NaN), never assumed zero.

    ``per_day`` optionally supplies the expected settlements for each bar instead (NaN =
    unknown, so the day is missing). The Strategy Lab passes a causal estimate; the
    default full-history median is unchanged for existing research.
    """
    ts = pd.DatetimeIndex(pd.to_datetime(bars["ts"], utc=True))
    out = np.full(len(ts), np.nan)
    if funding is None or funding.empty or not len(ts):
        return out
    f = funding.sort_index()
    # snap to the nearest minute: venues stamp settlements with a few ms of jitter (Binance:
    # "00:00:00.004"), which would otherwise push a midnight settlement into the next day
    t = pd.DatetimeIndex(pd.to_datetime(f.index, utc=True)).round("min").as_unit("ns").asi8
    v = f.to_numpy(float)
    if per_day is None:
        spacing = float(np.median(np.diff(t))) / 1e9 if len(t) > 1 else 3600.0
        per_day = max(round(86400 / spacing), 1) if spacing > 0 else 24
    expected = np.broadcast_to(np.asarray(per_day, dtype=float), (len(ts),))
    starts = ts.as_unit("ns").asi8
    ends = starts + pd.Timedelta(days=1).value
    lo = np.searchsorted(t, starts, side="right")  # strictly after the bar open
    hi = np.searchsorted(t, ends, side="right")  # up to and including the bar close
    cs = np.concatenate([[0.0], np.cumsum(v)])
    n = hi - lo
    total = cs[hi] - cs[lo]
    full = n >= expected
    partial = ~full & (n >= min_coverage * expected) & (n > 0)
    out[full] = total[full]
    out[partial] = total[partial] * expected[partial] / n[partial]
    return out


def perp_frame(store: Any, settings: Any, coin: str, venue: str = "hyperliquid") -> pd.DataFrame:
    """Daily perp bars + per-bar funding (``funding_day``) for one venue, ready for both engines."""
    from market_signal.perps.data import load_funding, load_perp_bars, perp_config

    bars = load_perp_bars(store, coin, venue=venue)
    if bars.empty:
        return bars
    bars = bars.copy()
    bars["ts"] = pd.to_datetime(bars["ts"], utc=True)
    bars["close_time"] = pd.to_datetime(bars["close_time"], utc=True)
    es = perp_config(settings).get("event_study") or {}
    cov = float(es.get("min_funding_coverage", 0.8))
    bars["funding_day"] = daily_funding(bars, load_funding(store, coin, venue=venue), cov)
    from market_signal.perps.macro_events import attach_macro_events, load_event_table

    # point-in-time macro events (DGS2, CPI) for event-driven strategies; NaN if not stored
    return attach_macro_events(bars.reset_index(drop=True), load_event_table(store))


# --------------------------------------------------------------------------- event study


def side_forward_returns(
    frame: pd.DataFrame, horizons: dict[str, int], side: int, costs: PerpCosts
) -> pd.DataFrame:
    """Per-bar forward returns for one side, on notional, net of fees, slippage and funding."""
    o = frame["open"].to_numpy(float)
    h_ = frame["high"].to_numpy(float)
    lo = frame["low"].to_numpy(float)
    c = frame["close"].to_numpy(float)
    fd = (
        frame["funding_day"].to_numpy(float)
        if "funding_day" in frame
        else np.full(len(frame), np.nan)
    )
    n = len(frame)
    entry = np.full(n, np.nan)
    entry[:-1] = o[1:]
    g = fd * c  # funding × notional-in-price-units per day
    cs = np.concatenate([[0.0], np.cumsum(np.nan_to_num(g))])
    nn = np.concatenate([[0], np.cumsum(np.isnan(g))])
    out = pd.DataFrame(index=frame.index)
    rt = 2 * costs.per_side
    for name, h in horizons.items():
        ret = np.full(n, np.nan)
        mae = np.full(n, np.nan)
        mfe = np.full(n, np.nan)
        fund = np.full(n, np.nan)
        t = np.arange(n - h)  # needs bar t+h
        if len(t):
            e = entry[t]
            ex = c[t + h]
            # funding over bars t+1 .. t+h  →  cumsum indices (t+1 .. t+h] = cs[t+h+1] - cs[t+1]
            fsum = cs[t + h + 1] - cs[t + 1]
            missing = (nn[t + h + 1] - nn[t + 1]) > 0
            paid = side * fsum / e
            paid[missing] = np.nan
            ret[t] = side * (ex / e - 1) - rt - paid
            fund[t] = paid
            win_lo = pd.Series(lo).rolling(h, min_periods=h).min().shift(-h).to_numpy()[t]
            win_hi = pd.Series(h_).rolling(h, min_periods=h).max().shift(-h).to_numpy()[t]
            if side > 0:
                mae[t], mfe[t] = win_lo / e - 1, win_hi / e - 1
            else:
                mae[t], mfe[t] = -(win_hi / e - 1), -(win_lo / e - 1)
        out[f"ret_{name}"], out[f"mae_{name}"], out[f"mfe_{name}"], out[f"funding_{name}"] = (
            ret,
            mae,
            mfe,
            fund,
        )
    return out


def perp_asset_events(
    coin: str,
    frame: pd.DataFrame,
    horizons: dict[str, int],
    costs: PerpCosts,
    long_signal: pd.Series | None = None,
    short_signal: pd.Series | None = None,
    eligible: pd.Series | None = None,
) -> list[AssetEvents]:
    """One ``AssetEvents`` per traded side, for ``run_event_study``. The baseline of each is
    random entry on that same side of that coin (bars where funding is known)."""
    elig = eligible if eligible is not None else pd.Series(True, index=frame.index)
    elig = elig & frame["funding_day"].notna() if "funding_day" in frame else elig
    out = []
    for side, sig, label in ((1, long_signal, "long"), (-1, short_signal, "short")):
        if sig is None:
            continue
        out.append(AssetEvents(f"{coin}:{label}", f"perp_{label}", frame, side_forward_returns(frame, horizons, side, costs),
                               sig.reindex(frame.index).fillna(False).astype(bool), elig, dict(horizons)))  # fmt: skip
    return out


def basket_asset_events(per_coin: list[AssetEvents], name: str = "BASKET") -> list[AssetEvents]:
    """Equal-weight basket per side: on each date, the mean of the coins' forward returns (each
    coin only where it is eligible). One observation per date, so an event that fires on every
    coin at once counts once, and its random-entry baseline draws random *dates*, keeping the
    coins' co-movement. A coin with no data on a date simply isn't in that day's mean."""
    out = []
    for cls in dict.fromkeys(a.asset_class for a in per_coin):
        group = [a for a in per_coin if a.asset_class == cls]
        horizons = dict(group[0].horizons)
        cts = [pd.DatetimeIndex(pd.to_datetime(a.feat["close_time"], utc=True)) for a in group]
        idx = cts[0]
        for c in cts[1:]:
            idx = idx.union(c)
        n = len(idx)
        cols = [f"{k}_{h}" for h in horizons for k in ("ret", "mae", "mfe")]
        tot = {c: np.zeros(n) for c in cols}
        cnt = {c: np.zeros(n) for c in cols}
        sig = np.zeros(n, bool)
        elig = np.zeros(n, bool)
        for a, ct in zip(group, cts, strict=True):
            pos = idx.get_indexer(ct)
            el = a.eligible.fillna(False).to_numpy(bool)
            for c in cols:
                v = a.fwd[c].to_numpy(float)
                ok = el & np.isfinite(v)
                np.add.at(tot[c], pos[ok], v[ok])
                np.add.at(cnt[c], pos[ok], 1)
            sig[pos] |= a.signal.fillna(False).to_numpy(bool) & el
            elig[pos] |= el
        fwd = pd.DataFrame(
            {c: np.where(cnt[c] > 0, tot[c] / np.maximum(cnt[c], 1), np.nan) for c in cols}
        )
        side = cls.removeprefix("perp_")
        out.append(AssetEvents(f"{name}:{side}", cls, pd.DataFrame({"close_time": idx}), fwd,
                               pd.Series(sig), pd.Series(elig), horizons))  # fmt: skip
    return out


# --------------------------------------------------------------------------- simulator


@dataclass
class PerpInput:
    coin: str
    frame: pd.DataFrame  # ts, close_time, open, high, low, close, funding_day
    costs: PerpCosts
    venue_max_leverage: float
    long_signal: pd.Series | None = None
    short_signal: pd.Series | None = None
    long_stop: pd.Series | None = None  # stop price set at the signal bar
    short_stop: pd.Series | None = None
    setup: str = ""
    maint_rate: float | None = None  # venue maintenance margin rate; default 1/(2 × max leverage)


@dataclass
class PerpRules:
    max_hold_bars: int = 30
    target_r: float | None = None  # take profit at entry ± target_r × stop distance


@dataclass
class PerpRisk:
    risk_per_trade: float = 0.005
    leverage_cap: float = 3.0
    liq_buffer: float = 2.0
    max_gross_notional: float = 2.0
    max_position_notional: float = 0.5
    min_position_notional: float = 0.002

    @classmethod
    def from_config(cls, cfg: dict) -> PerpRisk:
        r = cfg.get("risk") or {}
        return cls(**{k: float(v) for k, v in r.items() if k in cls.__dataclass_fields__})


@dataclass
class _Pos:
    coin: str
    side: int
    entry_bar: int
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry: float
    units: float
    notional: float
    margin: float  # posted
    margin_bal: float  # posted − funding paid
    leverage: float
    maint: float
    stop: float
    target: float
    liq_at_entry: float
    fees: float
    funding_paid: float = 0.0
    funding_imputed_days: int = 0
    worst: float = 0.0  # adverse excursion (fraction of entry)
    best: float = 0.0
    risk_amount: float = 0.0
    pending_exit: str | None = None

    def liq(self) -> float:
        return liquidation_price(self.entry, self.side, self.units, self.margin_bal, self.maint)

    def value(self, px: float) -> float:
        return max(self.margin_bal + self.side * self.units * (px - self.entry), 0.0)


@dataclass
class PerpSimResult:
    trades: pd.DataFrame
    equity: pd.Series
    exposure: pd.Series  # gross notional / equity, daily
    skipped: pd.DataFrame
    metrics: dict = field(default_factory=dict)


def _aligned(s: pd.Series | None, n: int, fill: Any) -> np.ndarray:
    return (
        s.reset_index(drop=True).reindex(range(n)).fillna(fill).to_numpy()
        if s is not None
        else np.full(n, fill)
    )


def simulate_perps(
    inputs: list[PerpInput], rules: PerpRules, risk: PerpRisk, initial_equity: float = 100_000.0
) -> PerpSimResult:
    data: dict[str, dict] = {}
    events = []
    for a in inputs:
        f = a.frame.reset_index(drop=True)
        n = len(f)

        fd = f["funding_day"].to_numpy(float) if "funding_day" in f else np.full(n, np.nan)
        data[a.coin] = {
            "ts": pd.DatetimeIndex(f["ts"]), "close_time": pd.DatetimeIndex(f["close_time"]),
            "open": f["open"].to_numpy(float), "high": f["high"].to_numpy(float),
            "low": f["low"].to_numpy(float), "close": f["close"].to_numpy(float), "funding": fd,
            "long": _aligned(a.long_signal, n, False).astype(bool), "short": _aligned(a.short_signal, n, False).astype(bool),
            "long_stop": _aligned(a.long_stop, n, np.nan).astype(float), "short_stop": _aligned(a.short_stop, n, np.nan).astype(float),
            "input": a, "maint": a.maint_rate or maintenance_rate(a.venue_max_leverage),
        }  # fmt: skip
        ct = data[a.coin]["close_time"].as_unit("ns").asi8
        events.extend((ct[i], a.coin, i) for i in range(n))
    events.sort()

    cash = initial_equity
    pos: dict[str, _Pos] = {}
    pending: dict[str, tuple[int, int, float]] = {}  # coin -> (signal bar, side, stop)
    last_px: dict[str, float] = {}
    last_funding: dict[str, float] = {}
    trades: list[dict] = []
    skipped: list[dict] = []
    curve_t, curve_eq, curve_exp = [], [], []
    turnover = 0.0

    def equity() -> float:
        return cash + sum(p.value(last_px.get(c, p.entry)) for c, p in pos.items())

    def gross() -> float:
        return sum(p.units * last_px.get(c, p.entry) for c, p in pos.items())

    def close(coin: str, bar: int, px: float, reason: str, extra_slip_bps: float = 0.0) -> None:
        nonlocal cash, turnover
        d = data[coin]
        p = pos.pop(coin)
        c = d["input"].costs
        if reason == "liquidation":
            fill, fee, value = px, 0.0, 0.0
        else:
            fill = px * (1 - p.side * (c.slippage_bps + extra_slip_bps) / 1e4)
            fee = p.units * fill * c.fee_bps / 1e4
            value = max(p.value(fill) - fee, 0.0)
        cash += value
        turnover += p.units * fill
        pnl = value - p.margin  # includes fees on exit, funding, price; entry fee booked at entry
        pnl_total = pnl - p.fees
        trades.append({
            "coin": coin, "setup": d["input"].setup, "side": "long" if p.side > 0 else "short",
            "signal_time": p.signal_time, "entry_time": p.entry_time, "entry_price": p.entry,
            "stop": p.stop, "target": p.target, "leverage": p.leverage, "notional": p.notional,
            "margin": p.margin, "liq_price_at_entry": p.liq_at_entry,
            "exit_time": d["ts"][bar] if reason != "end_of_data" else d["close_time"][bar],
            "exit_price": fill, "exit_reason": reason, "bars_held": bar - p.entry_bar + 1,
            "funding_paid": p.funding_paid, "funding_imputed_days": p.funding_imputed_days,
            "fees": p.fees + fee, "pnl": pnl_total,
            "ret": pnl_total / p.notional,  # on notional: comparable with spot, leverage-free
            "ret_on_margin": pnl_total / p.margin,
            "r_multiple": pnl_total / p.risk_amount if p.risk_amount else np.nan,
            "mae": -p.worst, "mfe": p.best,
        })  # fmt: skip
        last_px.pop(coin, None)

    for ct, coin, i in events:
        d = data[coin]
        a: PerpInput = d["input"]
        o, hi, lo, cl = d["open"][i], d["high"][i], d["low"][i], d["close"][i]
        if np.isfinite(d["funding"][i]):
            last_funding[coin] = d["funding"][i]

        # 1) scheduled exit at the open
        if coin in pos and pos[coin].pending_exit:
            close(coin, i, o, pos[coin].pending_exit)

        # 2) pending entry at the open
        if coin in pending:
            sig_bar, side, stop = pending.pop(coin)
            c = a.costs
            if not np.isfinite(stop) or (side > 0 and o <= stop) or (side < 0 and o >= stop):
                skipped.append({"coin": coin, "time": d["ts"][i], "side": side,
                                "reason": "invalid_stop" if not np.isfinite(stop) else "gapped_through_stop"})  # fmt: skip
            else:
                fill = o * (1 + side * c.slippage_bps / 1e4)
                dist = side * (fill - stop) / fill
                lev = choose_leverage(
                    dist, d["maint"], risk.leverage_cap, a.venue_max_leverage, risk.liq_buffer
                )
                eq = equity()
                if lev is None:
                    skipped.append(
                        {
                            "coin": coin,
                            "time": d["ts"][i],
                            "side": side,
                            "reason": "unsafe_leverage",
                        }
                    )
                else:
                    notional = min(risk.risk_per_trade * eq / dist, risk.max_position_notional * eq,
                                   max(risk.max_gross_notional * eq - gross(), 0.0))  # fmt: skip
                    margin = notional / lev
                    fee = notional * c.fee_bps / 1e4
                    if margin + fee > cash:
                        notional = max((cash - 1e-9) / (1 / lev + c.fee_bps / 1e4), 0.0)
                        margin, fee = notional / lev, notional * c.fee_bps / 1e4
                    if notional < risk.min_position_notional * eq:
                        skipped.append(
                            {"coin": coin, "time": d["ts"][i], "side": side, "reason": "no_capital"}
                        )
                    else:
                        units = notional / fill
                        cash -= margin + fee
                        turnover += notional
                        tgt = (
                            fill + side * rules.target_r * dist * fill if rules.target_r else np.nan
                        )
                        p = _Pos(coin, side, i, d["close_time"][sig_bar], d["ts"][i], fill, units, notional,
                                 margin, margin, lev, d["maint"], stop, tgt, 0.0, fee,
                                 risk_amount=risk.risk_per_trade * eq)  # fmt: skip
                        p.liq_at_entry = p.liq()
                        pos[coin] = p
                        last_px[coin] = o

        # 3) adverse move, along the path from the open
        if coin in pos:
            p = pos[coin]
            adverse = lo if p.side > 0 else hi
            favour = hi if p.side > 0 else lo
            p.worst = max(p.worst, -p.side * (adverse / p.entry - 1))
            p.best = max(p.best, p.side * (favour / p.entry - 1))
            liq = p.liq()

            def beyond(
                x: float, level: float, side: int = p.side
            ) -> bool:  # x at/through level, adversely
                return x <= level if side > 0 else x >= level

            if beyond(o, liq):
                close(coin, i, liq, "liquidation")
            elif beyond(o, p.stop):
                close(coin, i, o, "stop", a.costs.stop_slippage_bps)
            elif beyond(adverse, p.stop) or beyond(adverse, liq):
                # nearer level (to the open) is hit first
                stop_first = (p.stop >= liq) if p.side > 0 else (p.stop <= liq)
                if stop_first:
                    close(coin, i, p.stop, "stop", a.costs.stop_slippage_bps)
                else:
                    close(coin, i, liq, "liquidation")

        # 4) favourable target
        if coin in pos and np.isfinite(pos[coin].target):
            p = pos[coin]
            if (p.side > 0 and hi >= p.target) or (p.side < 0 and lo <= p.target):
                px = (
                    p.target
                    if p.entry_bar == i
                    else (max(o, p.target) if p.side > 0 else min(o, p.target))
                )
                close(coin, i, px, "target")

        # 5) funding for this bar, then close-of-bar marks and decisions
        if coin in pos:
            p = pos[coin]
            f = d["funding"][i]
            if not np.isfinite(f):
                f = last_funding.get(coin, 0.0)  # carry the last known rate; counted per trade
                p.funding_imputed_days += 1
            paid = p.side * p.units * cl * f
            p.margin_bal -= paid
            p.funding_paid += paid
            last_px[coin] = cl
            if p.margin_bal <= p.maint * p.units * cl:  # funding alone exhausted the margin
                close(coin, i, cl, "liquidation")
            elif i - p.entry_bar + 1 >= rules.max_hold_bars:
                p.pending_exit = "time"

        # 6) new signal at this close → order for the next open
        if (
            coin not in pos
            and coin not in pending
            and i + 1 < len(d["ts"])
            and np.isfinite(d["funding"][i])
        ):
            want_long, want_short = d["long"][i], d["short"][i]
            if want_long and not want_short:
                pending[coin] = (i, 1, d["long_stop"][i])
            elif want_short and not want_long:
                pending[coin] = (i, -1, d["short_stop"][i])

        eq = equity()
        curve_t.append(ct)
        curve_eq.append(eq)
        curve_exp.append(gross() / eq if eq > 0 else np.nan)

    for coin in list(pos):
        d = data[coin]
        close(coin, len(d["ts"]) - 1, d["close"][-1], "end_of_data")
    if curve_t:
        curve_eq[-1] = cash

    t = pd.to_datetime(np.asarray(curve_t, dtype="int64"), utc=True)
    eq_s = pd.Series(curve_eq, index=t).groupby(t.floor("D")).last()
    ex_s = pd.Series(curve_exp, index=t).groupby(t.floor("D")).last()
    tr = pd.DataFrame(trades)
    res = PerpSimResult(tr, eq_s, ex_s, pd.DataFrame(skipped))
    m = {**trade_metrics(tr), **curve_metrics(eq_s, ex_s, turnover)}
    if not tr.empty:
        m |= {
            "liquidations": int((tr["exit_reason"] == "liquidation").sum()),
            "avg_leverage": float(tr["leverage"].mean()),
            "funding_paid_total": float(tr["funding_paid"].sum()),
            "fees_total": float(tr["fees"].sum()),
            "longs": int((tr["side"] == "long").sum()), "shorts": int((tr["side"] == "short").sum()),
        }  # fmt: skip
    m["skipped"] = res.skipped["reason"].value_counts().to_dict() if not res.skipped.empty else {}
    res.metrics = m
    return res


def load_perp_input(
    store: Any, settings: Any, coin: str, venue: str = "hyperliquid", **signals: Any
) -> PerpInput | None:
    """A ``PerpInput`` from stored data for one venue: perp bars + daily funding, that venue's
    costs and margin rules (Hyperliquid: max leverage from the latest snapshot, config default
    otherwise). Pass ``long_signal`` / ``short_signal`` / ``long_stop`` / ``short_stop`` /
    ``setup`` as needed."""
    from market_signal.perps.data import load_snapshots, perp_config

    f = perp_frame(store, settings, coin, venue)
    if f.empty:
        return None
    cfg = perp_config(settings)
    v = venue_config(cfg, venue)
    if v:
        lev = float(
            (v.get("max_leverage") or {}).get(
                coin, (v.get("max_leverage") or {}).get("default", 20)
            )
        )
        mr = v.get("maintenance_rate") or {}
        maint = float(mr.get(coin, mr.get("default", 0.01)))
        return PerpInput(coin, f, perp_costs(cfg, coin, venue), lev, maint_rate=maint, **signals)
    snaps = load_snapshots(store, coin)
    snap_lev = (
        float(snaps["max_leverage"].dropna().iloc[-1])
        if not snaps.empty and snaps["max_leverage"].notna().any()
        else None
    )
    return PerpInput(
        coin, f, perp_costs(cfg, coin), venue_max_leverage(cfg, coin, snap_lev), **signals
    )
