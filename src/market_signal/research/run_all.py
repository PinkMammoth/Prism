"""Run every experiment and write a combined, decision-oriented results summary."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from market_signal.research.report import fmt, md_table
from market_signal.research.runner import (
    ResearchContext,
    ResearchReport,
    load_experiment,
    run_experiment,
    save_report,
)


def run_all(
    ctx: ResearchContext, names: list[str] | None = None, synthetic: bool = False
) -> tuple[Path, list[ResearchReport]]:
    exp_dir = ctx.settings.paths.config / "experiments"
    names = names or sorted(p.stem for p in exp_dir.glob("*.yaml"))
    reports, paths = [], {}
    for n in names:
        exp = load_experiment(ctx.settings, n)
        rep = run_experiment(ctx, exp)
        paths[n] = save_report(ctx, rep)
        reports.append(rep)
    out = ctx.settings.paths.results / (
        "RESULTS_generated_SYNTHETIC.md" if synthetic else "RESULTS_generated.md"
    )
    out.write_text(render_summary(reports, paths, synthetic))
    return out, reports


def _prim(rep: ResearchReport) -> pd.Series:
    prim = rep.experiment.primary_horizon or "1m"
    s = rep.study.summary
    return (
        s.set_index("horizon").loc[prim]
        if not s.empty and prim in set(s["horizon"])
        else pd.Series(dtype=float)
    )


def render_summary(reports: list[ResearchReport], paths: dict[str, Path], synthetic: bool) -> str:
    lines = ["# Generated research results", ""]
    if synthetic:
        lines += ["> **SYNTHETIC DATA.** These numbers come from random-walk demo series and validate the",
                  "> engine only. They say nothing about markets.", ""]  # fmt: skip
    lines += ["Automatic verdicts use the rules in docs/BACKTESTING.md §7. Returns are net of costs; 'excess'",
              "is versus random entry into the same assets over the same period.", "", "## Verdicts", "",
              "| experiment | horizon | indep. events | mean | excess | p (random entry) | MDE 80% | WF OOS excess (default) | WF folds + | sensitivity | sim CAGR | sim maxDD | verdict |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for rep in reports:
        r = _prim(rep)
        wf = rep.wf_summary or {}
        m = rep.sim.metrics
        lines.append(
            f"| {rep.experiment.name} | {rep.experiment.primary_horizon or '1m'} | {int(r.get('n_independent', 0))} "
            f"| {fmt(r.get('mean_indep'), 'mean')} | {fmt(r.get('excess_mean_indep'), 'excess_mean')} "
            f"| {fmt(r.get('p_value_random_entry'))} | {fmt(r.get('mde_80'), 'mde_80')} "
            f"| {fmt(wf.get('oos_default_excess_mean'), 'excess_mean')} ({wf.get('oos_default_n', 0)}) "
            f"| {wf.get('folds_positive', '–')}/{wf.get('folds_with_events', '–')} "
            f"| {(rep.sens_verdict or {}).get('verdict', '–')} | {fmt(m.get('cagr'))} | {fmt(m.get('max_drawdown'))} "
            f"| **{rep.verdict['verdict']}** |"
        )
    for rep in reports:
        prim = rep.experiment.primary_horizon or "1m"
        lines += ["", f"## {rep.experiment.name}", "", f"Verdict **{rep.verdict['verdict']}**: {'; '.join(rep.verdict['reasons'])}", "",
                  f"Signal period {rep.period[0]} → {rep.period[1]}. Full report: `{paths[rep.experiment.name] / 'report.md'}`", ""]  # fmt: skip
        bc = rep.study.by_class
        if not bc.empty:
            lines += [f"By asset class ({prim}):", "", md_table(bc[bc["horizon"] == prim],
                      ["asset_class", "n_independent", "mean_indep", "hit_rate_indep", "excess_mean_indep", "excess_t_indep"])]  # fmt: skip
        for k in ("trend", "regime", "vol", "rates"):
            t = rep.splits.get(k)
            if t is not None and not t.empty:
                lines += [
                    f"Split by {k}:",
                    "",
                    md_table(t, [k, "n_indep", "mean", "hit_rate", "excess_mean", "excess_t"]),
                ]
        ms = {
            k: rep.sim.metrics.get(k)
            for k in (
                "trades",
                "hit_rate",
                "payoff_ratio",
                "expectancy_r",
                "sharpe",
                "max_drawdown",
                "time_in_market",
            )
        }
        lines += [
            "Simulation: "
            + ", ".join(
                f"{k}={fmt(v) if not isinstance(v, float) or np.isfinite(v) else '–'}"
                for k, v in ms.items()
            ),
            "",
        ]
    return "\n".join(lines) + "\n"
