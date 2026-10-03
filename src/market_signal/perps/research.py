"""Perp strategy research: event study, walk-forward, sensitivity, simulation, verdict.

Same statistics and the SAME automatic verdict criteria as spot research
(``research.runner.automatic_verdict``). Nothing here may promote a result:
  - pre-registered defaults (``perps/strategies.py``); walk-forward may only choose
    between declared values, and only on past data;
  - each coin × side is its own series with a same-side random-entry baseline;
  - fees, slippage and funding are in every return; leverage is only in the simulation.
Saved like spot runs (results/perps/<strategy>/<ts>/ + a ``research_runs`` row named
``perp_<strategy>``), so the dashboard's evidence loader reads them unchanged.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from market_signal.backtest.events import (
    AssetEvents,
    EventStudyResult,
    baseline_bars,
    run_event_study,
)
from market_signal.backtest.robustness import (
    make_folds,
    plateau_verdict,
    sensitivity_grid,
    walk_forward,
)
from market_signal.config import Settings, config_hash
from market_signal.data.store import Store, new_id
from market_signal.models.domain import utcnow
from market_signal.perps.backtest import (
    PerpInput,
    PerpRisk,
    PerpRules,
    PerpSimResult,
    load_perp_input,
    perp_asset_events,
    simulate_perps,
)
from market_signal.perps.data import perp_config
from market_signal.perps.strategies import STRATEGIES, PerpStrategy, get_strategy

EVENT_COLUMNS = ["symbol", "asset_class", "horizon", "bar", "signal_time", "ret", "excess", "mae", "mfe",
                 "independent"]  # fmt: skip


def funding_coverage(frame: pd.DataFrame) -> float:
    """Share of days (from the first day with funding) that have a funding value."""
    fd = frame["funding_day"]
    first = fd.first_valid_index()
    return float(fd.loc[first:].notna().mean()) if first is not None else 0.0


@dataclass
class PerpReport:
    strategy: PerpStrategy
    run_id: str
    created_at: str
    params: dict
    horizons: dict[str, int]
    study: EventStudyResult
    sim: PerpSimResult
    wf_table: pd.DataFrame | None
    wf_summary: dict | None
    sens_table: pd.DataFrame | None
    sens_verdict: dict | None
    verdict: dict
    period: tuple[str, str]
    coins: list[str]
    provenance: dict = field(default_factory=dict)
    venue: str = "hyperliquid"
    window_note: str = ""  # e.g. "before Hyperliquid's history starts: unseen by this research"

    @property
    def run_name(self) -> str:
        return f"perp_{self.strategy.name}" + (
            "" if self.venue == "hyperliquid" else f"@{self.venue}"
        )


def _events(
    strategy: PerpStrategy, inputs: list[PerpInput], params: dict, horizons: dict
) -> list[AssetEvents]:
    out = []
    for a in inputs:
        s = strategy.signals(a.frame, params)
        out += perp_asset_events(
            a.coin, a.frame, horizons, a.costs, long_signal=s.long, short_signal=s.short
        )
    return out


def run_perp_research(
    store: Store | None,
    settings: Settings,
    name: str,
    inputs: list[PerpInput] | None = None,
    walk_forward_on: bool = True,
    sensitivity_on: bool = True,
    venue: str = "hyperliquid",
    end: pd.Timestamp | dict[str, pd.Timestamp] | None = None,
    window_note: str = "",
) -> PerpReport:
    """``inputs`` may be passed directly (tests); otherwise they're loaded from ``store`` for
    ``venue``. ``end`` (exclusive) truncates every coin's history first, e.g. to test the
    strategies only on years before the Hyperliquid data they were researched on."""
    strat = get_strategy(name)
    pcfg, bt = perp_config(settings), settings.yaml("backtest.yaml")
    stats = bt["statistics"]
    horizons = {
        k: int(v)
        for k, v in ((pcfg.get("event_study") or {}).get("horizons") or {"1m": 30}).items()
    }
    primary = strat.primary_horizon
    if inputs is None:
        from market_signal.perps.backtest import venue_config

        coins = (
            list(venue_config(pcfg, venue).get("symbols") or {})
            if venue != "hyperliquid"
            else pcfg.get("coins") or []
        )
        inputs = [
            a
            for c in coins
            if (a := load_perp_input(store, settings, str(c).upper(), venue)) is not None
        ]
    if end is not None:  # one date for every coin, or {coin: date}

        def _cut(coin: str) -> pd.Timestamp | None:
            c = end.get(coin) if isinstance(end, dict) else end
            if c is None:
                return None
            c = pd.Timestamp(c)
            return c.tz_localize("UTC") if c.tzinfo is None else c

        inputs = [
            a if (c := _cut(a.coin)) is None
            else dataclasses.replace(a, frame=a.frame[a.frame["close_time"] <= c].reset_index(drop=True))
            for a in inputs
        ]  # fmt: skip
    inputs = [a for a in inputs if len(a.frame) > 60]
    if not inputs:
        raise RuntimeError(
            f"no {venue} perp data stored for this window: run `market update --only perps` first"
        )
    params = dict(strat.defaults)

    aevs = _events(strat, inputs, params, horizons)
    study = run_event_study(aevs, primary, int(stats["bootstrap_samples"]), int(stats["seed"]),
                            int(stats["min_events_for_conclusion"]))  # fmt: skip
    base = baseline_bars(aevs)
    gap = {ae.symbol: horizons[primary] for ae in aevs}

    def events_for(overrides: dict[str, Any]) -> pd.DataFrame:
        ev = run_event_study(
            _events(strat, inputs, {**params, **overrides}, horizons), primary, n_boot=0
        ).events
        return ev if not ev.empty else pd.DataFrame(columns=EVENT_COLUMNS)  # no events ≠ crash

    period = (
        (base["signal_time"].min(), base["signal_time"].max()) if not base.empty else (None, None)
    )
    wf_table = wf_summary = sens_table = sens_verdict = None
    if walk_forward_on and period[0] is not None:
        w = (pcfg.get("research") or {}).get("walk_forward") or {}
        names = list(strat.walk_forward)
        grid = [
            dict(zip(names, c, strict=True))
            for c in pd.MultiIndex.from_product(list(strat.walk_forward.values())).tolist()
        ]
        default_sel = {k: params[k] for k in names}
        folds = make_folds(period[0].normalize(), period[1], float(w.get("train_years", 1)),
                           float(w.get("test_years", 0.5)), bool(w.get("anchored", True)))  # fmt: skip
        if folds:
            wf_table, wf_summary = walk_forward(
                events_for, base, grid, default_sel, folds, primary, gap,
                pd.Timedelta(days=horizons[primary] + 5), int(w.get("min_train_events", 20)),
                w.get("objective", "median_excess"),
            )  # fmt: skip
    if sensitivity_on and strat.sensitivity and not base.empty:
        centre = {k: params[k] for k in strat.sensitivity}
        sens_table = sensitivity_grid(events_for, centre, strat.sensitivity, primary, base, gap)
        sc = bt["sensitivity"]
        sens_verdict = plateau_verdict(sens_table, centre, strat.sensitivity, int(stats["min_events_for_conclusion"]),
                                       float(sc["plateau_min_neighbour_ratio"]), float(sc["fragile_if_neighbours_fail"]))  # fmt: skip

    from market_signal.research.runner import automatic_verdict

    prim_row = (
        study.summary.set_index("horizon").loc[primary]
        if not study.summary.empty
        else pd.Series(dtype=float)
    )
    asset_share = None
    if not study.by_asset.empty:
        pa = study.by_asset[
            (study.by_asset["horizon"] == primary) & (study.by_asset["n_independent"] >= 5)
        ]
        asset_share = float((pa["excess_mean_indep"] > 0).mean()) if len(pa) else None
    verdict = automatic_verdict(prim_row, wf_summary, sens_verdict, asset_share, bt)

    sim_inputs = []
    for a in inputs:
        s = strat.signals(a.frame, params)
        sim_inputs.append(PerpInput(a.coin, a.frame, a.costs, a.venue_max_leverage, s.long, s.short,
                                    s.long_stop, s.short_stop, strat.name, a.maint_rate))  # fmt: skip
    sim = simulate_perps(sim_inputs, PerpRules(max_hold_bars=int(params["max_hold"])), PerpRisk.from_config(pcfg),
                         float((pcfg.get("research") or {}).get("initial_equity", 100_000)))  # fmt: skip
    prov = {
        "config_hash": config_hash(
            {"strategy": strat.name, "params": params, "perps": pcfg, "backtest": bt}
        ),
        "data": [
            {
                "coin": a.coin,
                "bars": len(a.frame),
                "first": str(a.frame["ts"].iloc[0])[:10],
                "last": str(a.frame["ts"].iloc[-1])[:10],
                "funding_days": int(a.frame["funding_day"].notna().sum()),
                "funding_coverage": round(funding_coverage(a.frame), 4),
            }
            for a in inputs
        ],
    }
    try:
        from market_signal.research.runner import _git_commit

        prov["git_commit"] = _git_commit(settings.paths.root)
    except Exception:
        prov["git_commit"] = None
    return PerpReport(strat, new_id("prr_"), utcnow().isoformat(timespec="seconds"), params, horizons, study, sim,
                      wf_table, wf_summary, sens_table, sens_verdict, verdict,
                      (str(period[0])[:10], str(period[1])[:10]), [a.coin for a in inputs], prov,
                      venue, window_note)  # fmt: skip


# --------------------------------------------------------------------------- report


def render_perp_markdown(rep: PerpReport) -> str:
    from market_signal.research.report import fmt, md_table

    s, v = rep.strategy, rep.verdict
    p = s.primary_horizon
    cols = ["horizon", "n_events", "n_independent", "mean_indep", "hit_rate_indep", "baseline_mean", "excess_mean_indep",
            "excess_t_indep", "p_value_random_entry", "mde_80", "avg_mae", "avg_mfe", "verdict_sample"]  # fmt: skip
    lines = [
        f"# Perp research: {s.title} (`{s.name}`)", "",
        f"- **Verdict:** **{v['verdict']}**: {'; '.join(v['reasons'])}",
        f"- **Hypothesis:** {s.hypothesis}",
        f"- **Venue:** {rep.venue}" + (f" · **{rep.window_note}**" if rep.window_note else ""),
        f"- **Coins:** {', '.join(rep.coins)} · **signal period:** {rep.period[0]} → {rep.period[1]} · primary horizon **{p}**",
        f"- **Pre-registered parameters:** `{rep.params}`",
        f"- **Run:** `{rep.run_id}` at {rep.created_at}; config `{rep.provenance.get('config_hash')}`; "
        f"git `{(rep.provenance.get('git_commit') or 'n/a')[:10]}`", "",
        "## Event study (per side, on notional, no leverage)", "",
        "Entry next open, exit at the close h bars later, net of taker fee, slippage and funding. Excess = return "
        "minus random entry on the SAME side of the same coin. `independent` = non-overlapping events.", "",
        md_table(rep.study.summary, [c for c in cols if c in rep.study.summary.columns]), "",
        f"### By side ({p})", "",
    ]  # fmt: skip
    bc = rep.study.by_class
    lines.append(md_table(bc[bc["horizon"] == p] if not bc.empty else bc,
                          ["asset_class", "n_independent", "mean_indep", "hit_rate_indep", "excess_mean_indep", "excess_t_indep"]
                          if not bc.empty else None))  # fmt: skip
    ba = rep.study.by_asset
    lines += ["", f"### By coin and side ({p})", "",
              md_table(ba[ba["horizon"] == p] if not ba.empty else ba,
                       ["symbol", "n_independent", "mean_indep", "hit_rate_indep", "excess_mean_indep", "avg_mae"]
                       if not ba.empty else None)]  # fmt: skip
    for note in rep.study.notes:
        lines.append(f"- {note}")
    m = rep.sim.metrics
    lines += ["", "## Portfolio simulation (isolated margin, risk-sized, leverage ≤ cap, liquidation modelled)", "",
              "| metric | value |", "|---|---|"]  # fmt: skip
    pct_keys = {
        "hit_rate",
        "avg_return",
        "expectancy",
        "cagr",
        "max_drawdown",
        "avg_mae",
        "worst_mae",
        "time_in_market",
    }
    for k in ("trades", "longs", "shorts", "hit_rate", "avg_return", "expectancy_r", "avg_holding_bars", "avg_leverage",
              "liquidations", "funding_paid_total", "fees_total", "cagr", "sharpe", "max_drawdown", "worst_mae",
              "time_in_market", "exit_reasons", "skipped"):  # fmt: skip
        if k in m:
            val = m[k]
            lines.append(
                f"| {k} | {f'{val:+.2%}' if k in pct_keys and isinstance(val, float) and np.isfinite(val) else fmt(val)} |"
            )
    lines += [
        "",
        "## Walk-forward (choose among declared values on the past, test on the next unseen period)",
        "",
    ]
    if rep.wf_table is not None:
        lines += [
            md_table(rep.wf_table),
            "",
            "```",
            *(f"{k}: {fmt(x)}" for k, x in (rep.wf_summary or {}).items()),
            "```",
        ]
    else:
        lines.append("_(not enough history for a fold)_")
    lines += ["", "## Parameter sensitivity", ""]
    if rep.sens_table is not None:
        lines += [
            md_table(rep.sens_table),
            "",
            f"Plateau verdict: **{(rep.sens_verdict or {}).get('verdict')}**",
        ]
    else:
        lines.append("_(not run)_")
    lines += [
        "",
        "## Data",
        "",
        "| coin | bars | first | last | days with funding | funding coverage |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {d['coin']} | {d['bars']} | {d['first']} | {d['last']} | {d['funding_days']} | "
        f"{d.get('funding_coverage', 0):.1%} |"
        for d in rep.provenance["data"]
    ]
    low = [d for d in rep.provenance["data"] if d.get("funding_coverage", 1) < 0.95]
    if low:
        lines += ["", "**Warning:** funding is missing on more than 5% of days for "
                  + ", ".join(f"{d['coin']} ({1 - d['funding_coverage']:.0%})" for d in low)
                  + ". Any return spanning a missing day is excluded, so the sample is smaller than the price "
                  "history suggests."]  # fmt: skip
    return "\n".join(lines) + "\n"


def save_perp_report(store: Store, settings: Settings, rep: PerpReport):
    stamp = rep.created_at.replace(":", "").replace("-", "")[:15]
    folder = (
        stamp if rep.venue == "hyperliquid" else f"{rep.venue}-{stamp}"
    )  # venues never share a folder
    out = settings.paths.results / "perps" / rep.strategy.name / folder
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(render_perp_markdown(rep))
    rep.study.events.to_csv(out / "events.csv", index=False)
    if not rep.sim.trades.empty:
        rep.sim.trades.to_csv(out / "trades.csv", index=False)
    rep.sim.equity.rename("equity").to_csv(out / "equity.csv")
    summary = {
        "name": rep.run_name, "setup": rep.strategy.name, "venue": rep.venue, "window_note": rep.window_note, "verdict": rep.verdict, "period": rep.period,
        "summary": rep.study.summary.replace({np.nan: None}).to_dict("records"),
        "by_side": rep.study.by_class.replace({np.nan: None}).to_dict("records") if not rep.study.by_class.empty else [],
        "simulation": dict(rep.sim.metrics), "walk_forward": rep.wf_summary, "sensitivity": rep.sens_verdict,
        "provenance": rep.provenance, "params": rep.params,
    }  # fmt: skip
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    store.con.execute(
        "INSERT INTO research_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [rep.run_id, rep.run_name, "perp_experiment", utcnow(),
         json.dumps({"strategy": rep.strategy.name, "params": rep.params, "primary_horizon": rep.strategy.primary_horizon,
                     "venue": rep.venue, "period": list(rep.period), "window_note": rep.window_note}),
         rep.provenance["config_hash"], json.dumps(rep.provenance["data"], default=str), "perps-phase3",
         rep.provenance.get("git_commit"), json.dumps({"verdict": rep.verdict}, default=str), str(out)],
    )  # fmt: skip
    return out


def all_strategies() -> list[str]:
    return list(STRATEGIES)
