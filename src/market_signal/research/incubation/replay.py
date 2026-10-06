"""Retrospective policy diagnostics (``incubation_replay_v1``). NOT evidence, NOT tuning.

The three frozen profiles are replayed causally over the frozen candidate pool on historical
daily perp data, stopping at the cutoff (2026-10-01: the Phase 9 validation window and
everything prospective stay untouched). It answers "how would each policy have behaved?":
opportunity rate, trade count, net expectancy, admission delay from WATCH, short bursts,
active fraction, turnover and long/short balance. Thresholds were frozen on synthetic data
before this ran; nothing here may change them (a new policy version would be required).

Read-only on the store (snapshots are captured in memory and fingerprinted); the payload
has no clock and no random draws, and its digest is reproducible.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from market_signal.research.incubation import inputs as ip
from market_signal.research.incubation.machine import replay
from market_signal.research.incubation.opportunity import opportunity_rate
from market_signal.research.incubation.policy import ADMITTED, POLICIES
from market_signal.research.incubation.pool import PoolRule, balance, members, pool_id
from market_signal.research.lab.common import LabModel, Symbol, UTCDateTime
from market_signal.research.lab.datasets import SeriesSelection
from market_signal.research.lab.spec import StrategyDefinition
from market_signal.research.lifecycle import estimators as es
from market_signal.research.lifecycle.estimators import weighted_mean_se
from market_signal.research.structure.study.run import _peak_rss_mb, clean, digest

REPLAY_VERSION = "incubation_replay_v1"
STATEMENT = ("RETROSPECTIVE DIAGNOSTICS: historical replay of policies frozen on synthetic data. "
             "Backfilled data, assumed availability at the close; not prospective evidence, not "
             "used to choose any threshold.")  # fmt: skip


class VenueReplay(LabModel):
    venue: str
    coins: tuple[Symbol, ...]
    bars_start: UTCDateTime
    funding_start: UTCDateTime
    first_evaluation: UTCDateTime


CUTOFF = datetime.fromisoformat("2026-10-01T00:00:00+00:00")
VENUES = (  # the Phase 20 study's venues and fixed calendar start dates
    VenueReplay(venue="hyperliquid", coins=("AAVE", "BTC", "ETH", "HYPE", "LINK", "SOL"),
                bars_start="2022-08-01T00:00:00Z", funding_start="2023-10-01T00:00:00Z",
                first_evaluation="2024-04-01T00:00:00Z"),
    VenueReplay(venue="binance", coins=("AAVE", "BTC", "ETH", "LINK", "SOL"),
                bars_start="2019-09-01T00:00:00Z", funding_start="2019-09-01T00:00:00Z",
                first_evaluation="2020-07-06T00:00:00Z"),
)  # fmt: skip


def selections(v: VenueReplay, coin: str, end) -> tuple[SeriesSelection, ...]:
    return (SeriesSelection(kind="perp_bars", symbol=coin, source=v.venue, timeframe="1d",
                            start=v.bars_start, end=end),
            SeriesSelection(kind="perp_funding", symbol=coin, source=v.venue,
                            start=v.funding_start, end=end))  # fmt: skip


def _drawdown(net: np.ndarray) -> float:
    if not len(net):
        return 0.0
    cum = np.cumsum(net)
    return float((np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:] - cum).max())


def evaluate(store, perps_cfg: dict, catalogue_dir: Path, venues=VENUES, cutoff=CUTOFF,
             horizon: int = 10, limit: int | None = None) -> tuple[dict, dict]:  # fmt: skip
    from market_signal.perps.backtest import perp_costs
    from market_signal.research.lab.forward import _snapshot

    t0 = time.perf_counter()
    rule = PoolRule()
    refs = members(rule, catalogue_dir)[:limit] if limit else members(rule, catalogue_dir)
    lp = POLICIES["CONSERVATIVE"].lifecycle()
    floor = POLICIES["BALANCED"].floor.floor(horizon)
    t_cut = es.to_days(cutoff)
    meta: dict = {"seconds": {}}
    out_venues = {}
    for v in venues:
        tv = time.perf_counter()
        snaps = {c: _snapshot(store, selections(v, c, cutoff)) for c in v.coins}
        vi = ip.venue_inputs(snaps, "BTC", lp)
        costs = {c: (perp_costs(perps_cfg, c, v.venue).fee_bps,
                     perp_costs(perps_cfg, c, v.venue).slippage_bps) for c in v.coins}  # fmt: skip
        first = es.to_days(v.first_evaluation)
        times = ip.daily_times(first, t_cut - 1)
        codes = ip.regime_codes(vi.state, times)
        sig_rows: dict[str, list] = {p: [] for p in POLICIES}
        per: dict[str, list] = {p: [] for p in POLICIES}
        static_net = []
        no_outcomes = []
        for ref in refs:
            d = StrategyDefinition.model_validate(ref.definition)
            _, ev = ip.strategy_ledger(d, vi, costs, horizon, lp)
            if ev is None or not len(ev):
                no_outcomes.append(ref.name)
                continue
            span = ev.t_sig >= first
            static_net.append(ev.net[span])
            for name, pol in POLICIES.items():
                rep = replay(ev, times, pol, floor, strategy_key=f"{v.venue}:{ref.strategy_id}",
                             regime_at=codes)  # fmt: skip
                part = rep.participate & span
                sig_rows[name].append(pd.DataFrame({
                    "t_sig": ev.t_sig[span], "asset": np.array(ev.assets)[ev.asset[span]],
                    "side": ref.side, "admitted": part[span], "net": ev.net[span],
                    "t_res": ev.t_res[span], "cost": ev.cost[span] + ev.funding[span],
                    "confirmed": rep.confirmed[span]}))  # fmt: skip
                per[name].append(_strategy_row(ref, rep, ev, part, times))
        meta["seconds"][v.venue] = round(time.perf_counter() - tv, 1)
        out_venues[v.venue] = {
            "coins": list(v.coins), "first_evaluation": v.first_evaluation.isoformat(),
            "evaluation_days": len(times), "datasets": {c: s.dataset_id for c, s in snaps.items()},
            "costs": costs, "strategies_without_outcomes": no_outcomes,
            "static_all_signals": _static(np.concatenate(static_net) if static_net else np.zeros(0)),
            "policies": {p: _policy_summary(per[p], pd.concat(sig_rows[p], ignore_index=True)
                                            if sig_rows[p] else None, first, t_cut, floor)
                         for p in POLICIES},
        }  # fmt: skip
    payload = clean({
        "version": REPLAY_VERSION, "statement": STATEMENT, "cutoff": cutoff.isoformat(),
        "horizon_bars": horizon, "economic_floor": floor,
        "policies": {n: p.policy_id for n, p in POLICIES.items()},
        "pool": {"pool_id": pool_id(rule, refs), "balance": balance(refs)},
        "venues": out_venues,
    })  # fmt: skip
    meta["wall_seconds"] = round(time.perf_counter() - t0, 1)
    meta["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    return payload, {**meta, "digest": digest(payload)}


def _static(net: np.ndarray) -> dict:
    return {"trades": len(net), "net_mean": float(net.mean()) if len(net) else None,
            "net_sum": float(net.sum())}  # fmt: skip


def _strategy_row(ref, rep, ev, part, times) -> dict:
    lv = np.array(rep.levels, dtype=object)
    # WATCH -> admission delay: days from the start of the WATCH spell preceding an admission
    delays, watch_since = [], None
    for tr in rep.transitions:
        if tr["to"] == "WATCH":
            watch_since = tr["at_days"]
        elif tr["to"] == "EXPLORATORY_PAPER" and tr["from"] == "WATCH" and watch_since is not None:
            delays.append(tr["at_days"] - watch_since)
            watch_since = None
        elif tr["to"] not in ("WATCH",):
            watch_since = None
    ep = rep.episodes
    lengths = [((e["end"] if e["end"] is not None else float(times[-1]) + 1) - e["start"])
               for e in ep]  # fmt: skip
    return {
        "strategy": ref.name, "family": ref.family, "side": ref.side,
        "active_share": float(np.isin(lv, ADMITTED).mean()) if len(lv) else 0.0,
        "episodes": len(ep), "reactivations": sum(bool(e.get("reactivation")) for e in ep),
        "short_bursts_lt_14d": sum(x < 14 for x in lengths),
        "episode_lengths": lengths,
        "negative_episodes": sum(1 for e in ep if (e.get("net_mean") or 0) < 0),
        "confirmed_episodes": sum(1 for e in ep if e.get("confirmed_at")),
        "transitions": len(rep.transitions),
        "watch_to_admission_days": delays,
        "final_level": lv[-1] if len(lv) else None,
        "years": (float(times[-1]) - float(times[0]) + 1) / 365.25 if len(times) else 0.0,
    }  # fmt: skip


def _policy_summary(rows: list[dict], sig: pd.DataFrame | None, first: float, t_cut: float,
                    floor: float) -> dict:  # fmt: skip
    if sig is None or not rows:
        return {"strategies": 0}
    adm = sig[sig["admitted"]].sort_values("t_res", kind="mergesort")
    net = adm["net"].to_numpy(float)
    mean, se = (weighted_mean_se(net, np.ones(len(net)), np.floor(adm["t_sig"].to_numpy() / 7))
                if len(net) else (np.nan, np.nan))  # fmt: skip
    years = sum(r["years"] for r in rows)
    lengths = [x for r in rows for x in r["episode_lengths"]]
    delays = [x for r in rows for x in r["watch_to_admission_days"]]
    eps = sum(r["episodes"] for r in rows)
    by_side = {}
    for side in ("long", "short"):
        rs = [r for r in rows if r["side"] == side]
        a = adm[adm["side"] == side]["net"].to_numpy(float)
        by_side[side] = {"strategies": len(rs),
                         "ever_admitted": sum(r["episodes"] > 0 for r in rs),
                         "admissions": sum(r["episodes"] for r in rs),
                         "admitted_at_cutoff": sum(r["final_level"] in ADMITTED for r in rs),
                         "trades": len(a), "net_mean": float(a.mean()) if len(a) else None}  # fmt: skip
    fam = Counter()
    for r in rows:
        fam[f"{r['family']}:{r['side']}"] += r["episodes"]
    return {
        "strategies": len(rows),
        "ever_admitted": sum(r["episodes"] > 0 for r in rows),
        "admissions": eps,
        "admissions_per_strategy_year": eps / years if years else None,
        "reactivations": sum(r["reactivations"] for r in rows),
        "mean_active_share": float(np.mean([r["active_share"] for r in rows])),
        "trades": len(net),
        "trades_per_year": len(net) / ((t_cut - first) / 365.25),
        "net_mean": float(mean) if len(net) else None,
        "net_se": float(se) if len(net) and np.isfinite(se) else None,
        "net_sum": float(net.sum()) if len(net) else 0.0,
        "cost_mean": float(adm["cost"].mean()) if len(adm) else None,
        "trades_at_least_floor_share": float((net >= floor).mean()) if len(net) else None,
        "pool_max_drawdown": _drawdown(net),
        "confirmed_trades": int(adm["confirmed"].sum()),
        "confirmed_episodes": sum(r["confirmed_episodes"] for r in rows),
        "episode_days_median": float(np.median(lengths)) if lengths else None,
        "short_bursts_lt_14d": sum(r["short_bursts_lt_14d"] for r in rows),
        "negative_episodes": sum(r["negative_episodes"] for r in rows),
        "watch_to_admission_days_median": float(np.median(delays)) if delays else None,
        "transitions_per_strategy_year": sum(r["transitions"] for r in rows) / years if years else None,
        "opportunity": opportunity_rate(sig, first, t_cut),
        "by_side": by_side,
        "admissions_by_family_side": dict(fam),
        "levels_at_cutoff": dict(Counter(r["final_level"] for r in rows)),
    }  # fmt: skip


__all__ = ["CUTOFF", "STATEMENT", "VENUES", "evaluate"]
