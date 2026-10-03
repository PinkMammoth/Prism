"""Paper-tracking of the perp strategies: a forward test with no money involved.

Every run (inside ``market update``) checks the newest CLOSED perp bar of each coin with
each pre-registered strategy, and stores the check, plus the signal if there is one, in
``perp_paper_checks``. Rules that keep it honest:
  - only a bar that closed recently (``LIVE_WINDOW``) is checked. Bars missed while the PC
    was off are never filled in later: that would be a backtest, not a forward test;
  - a check is written once and never revised;
  - each row stores the hash of the strategy's parameters, so a changed strategy starts a
    new record instead of inheriting the old one.
Evaluation reuses the event study. Signals are the recorded ones, and the random-entry
baseline is drawn ONLY from bars that were checked live, so both come from the same period.
Paper signals are never shown as advice.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from market_signal.backtest.events import run_event_study
from market_signal.config import Settings, config_hash
from market_signal.data.store import Store
from market_signal.models.domain import utcnow
from market_signal.perps.backtest import basket_asset_events, load_perp_input, perp_asset_events
from market_signal.perps.data import perp_config
from market_signal.perps.strategies import STRATEGIES, PerpStrategy

LIVE_WINDOW = pd.Timedelta(hours=36)
VENUE = "hyperliquid"


def params_hash(strategy: PerpStrategy) -> str:
    return config_hash({"strategy": strategy.name, "defaults": strategy.defaults})


def record_paper_signals(
    store: Store, settings: Settings, now: pd.Timestamp | None = None
) -> pd.DataFrame:
    now = pd.Timestamp(now or utcnow())
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    out = []
    coins = [str(c).upper() for c in perp_config(settings).get("coins") or []]
    inputs = {c: load_perp_input(store, settings, c) for c in coins}
    for strat in STRATEGIES.values():
        h = params_hash(strat)
        for coin, a in inputs.items():
            if a is None or a.frame.empty:
                out.append((strat.name, coin, "skipped", "no perp data"))
                continue
            f = a.frame
            bar = pd.Timestamp(f["close_time"].iloc[-1])
            if now - bar > LIVE_WINDOW:
                out.append(
                    (
                        strat.name,
                        coin,
                        "skipped",
                        f"newest bar closed {bar:%d %b %H:%M} (too old to count as live)",
                    )
                )
                continue
            if bar > now:
                out.append((strat.name, coin, "skipped", "newest bar not closed yet"))
                continue
            done = store.con.execute(
                "SELECT 1 FROM perp_paper_checks WHERE strategy=? AND coin=? AND venue=? AND bar_close=?",
                [strat.name, coin, VENUE, bar],
            ).fetchone()
            if done:
                out.append((strat.name, coin, "ok", "already checked"))
                continue
            s = strat.signals(f)
            i = len(f) - 1
            side = "long" if bool(s.long.iloc[i]) else "short" if bool(s.short.iloc[i]) else None
            stop = (
                s.long_stop.iloc[i]
                if side == "long"
                else s.short_stop.iloc[i]
                if side == "short"
                else np.nan
            )
            store.con.execute(
                "INSERT INTO perp_paper_checks VALUES (?,?,?,?,?,?,?,?,?)",
                [strat.name, coin, VENUE, bar, now, side, float(f["close"].iloc[i]),
                 None if not np.isfinite(stop) else float(stop), h],
            )  # fmt: skip
            out.append((strat.name, coin, "ok", f"signal: {side.upper()}" if side else "no signal"))
    return pd.DataFrame(out, columns=["strategy", "coin", "status", "note"])


@dataclass
class PaperResult:
    strategy: str
    first_check: pd.Timestamp | None
    bars_checked: int
    signals: int
    completed: int  # signals whose primary-horizon window has closed
    independent: int
    mean_return: float | None
    excess: float | None
    p_value: float | None
    horizon: str
    min_events: int

    @property
    def too_early(self) -> bool:
        return self.independent < self.min_events


def evaluate_paper(store: Store, settings: Settings, name: str) -> PaperResult:
    strat = STRATEGIES[name]
    stats = settings.yaml("backtest.yaml")["statistics"]
    min_n = int(stats["min_events_for_conclusion"])
    try:
        checks = store.query(
            "SELECT coin, bar_close, side FROM perp_paper_checks WHERE strategy=? AND venue=? AND params_hash=?",
            [name, VENUE, params_hash(strat)],
        )
    except Exception:
        checks = pd.DataFrame()
    empty = PaperResult(name, None, 0, 0, 0, 0, None, None, None, strat.primary_horizon, min_n)
    if checks.empty:
        return empty
    checks["bar_close"] = pd.to_datetime(checks["bar_close"], utc=True)
    horizons = {
        strat.primary_horizon: int(
            (perp_config(settings).get("event_study") or {})
            .get("horizons", {})
            .get(strat.primary_horizon, 30)
        )
    }
    aevs = []
    for coin, g in checks.groupby("coin"):
        a = load_perp_input(store, settings, coin)
        if a is None:
            continue
        ct = pd.to_datetime(a.frame["close_time"], utc=True)
        checked = ct.isin(set(g["bar_close"]))
        longs = ct.isin(set(g.loc[g["side"] == "long", "bar_close"]))
        shorts = ct.isin(set(g.loc[g["side"] == "short", "bar_close"]))
        aevs += perp_asset_events(coin, a.frame, horizons, a.costs, long_signal=pd.Series(longs.to_numpy()),
                                  short_signal=pd.Series(shorts.to_numpy()),
                                  eligible=pd.Series(checked.to_numpy()))  # fmt: skip
    if strat.basket:  # one observation per event date, as in the research verdict
        aevs = basket_asset_events(aevs)
    n_sig = int(checks["side"].notna().sum())
    res = PaperResult(name, checks["bar_close"].min(), len(checks), n_sig, 0, 0, None, None, None,
                      strat.primary_horizon, min_n)  # fmt: skip
    if not aevs or n_sig == 0:
        return res
    study = run_event_study(
        aevs, strat.primary_horizon, int(stats["bootstrap_samples"]), int(stats["seed"]), min_n
    )
    if study.summary.empty:
        return res
    row = study.summary.set_index("horizon").loc[strat.primary_horizon]

    def f(x):
        return None if x is None or not np.isfinite(x) else float(x)

    res.completed = int(row["n_events"])
    res.independent = int(row["n_independent"])
    res.mean_return, res.excess, res.p_value = (
        f(row["mean_indep"]),
        f(row["excess_mean_indep"]),
        f(row["p_value_random_entry"]),
    )
    return res
