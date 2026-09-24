"""Research commands: backtest (run an experiment), experiments (list)."""

from __future__ import annotations

import typer
from rich.table import Table

from market_signal.cli.common import console, open_store


def backtest(
    experiment: str = typer.Argument(
        ..., help="Experiment name (config/experiments/<name>.yaml) or path"
    ),
    no_walk_forward: bool = typer.Option(False, "--no-wf", help="Skip walk-forward"),
    no_sensitivity: bool = typer.Option(False, "--no-sens", help="Skip sensitivity grid"),
) -> None:
    """Run a declarative experiment: event study, simulation, splits, walk-forward, sensitivity."""
    from market_signal.demo import is_synthetic
    from market_signal.research.runner import (
        ResearchContext,
        load_experiment,
        run_experiment,
        save_report,
    )

    with open_store() as (settings, store):
        exp = load_experiment(settings, experiment)
        ctx = ResearchContext(store, settings)
        with console.status(f"Running {exp.name} on {len(exp.universe)} assets…"):
            rep = run_experiment(
                ctx, exp, walk_forward_on=not no_walk_forward, sensitivity_on=not no_sensitivity
            )
        path = save_report(ctx, rep)
        if is_synthetic(store):
            console.print("[bold black on yellow] SYNTHETIC DATA — engine demonstration only [/]")
        t = Table(title=f"{exp.name} — event study (net of costs)")
        cols = ["horizon", "n_independent", "mean_indep", "hit_rate_indep", "baseline_mean", "excess_mean_indep",
                "excess_t_indep", "p_value_random_entry"]  # fmt: skip
        for c in cols:
            t.add_column(c)
        from market_signal.research.report import fmt

        for _, r in rep.study.summary.iterrows():
            t.add_row(*[fmt(r[c], c) for c in cols])
        console.print(t)
        m = rep.sim.metrics
        console.print(
            f"Simulation: {m.get('trades', 0)} trades, hit {fmt(m.get('hit_rate'), 'hit_rate')}, "
            f"CAGR {fmt(m.get('cagr'))}, Sharpe {fmt(m.get('sharpe'))}, maxDD {fmt(m.get('max_drawdown'))}"
        )
        if rep.wf_summary:
            w = rep.wf_summary
            console.print(f"Walk-forward: OOS excess {fmt(w.get('oos_excess_mean'), 'excess_mean')} over {w.get('oos_n')} events; "
                          f"{w.get('folds_positive')}/{w.get('folds_with_events')} folds positive")  # fmt: skip
        if rep.sens_verdict:
            console.print(f"Sensitivity: {rep.sens_verdict.get('verdict')}")
        v = rep.verdict
        colour = {"PROMISING": "green", "WEAK_POSITIVE": "yellow", "REJECT": "red"}.get(
            v["verdict"], "yellow"
        )
        console.print(f"[bold {colour}]VERDICT: {v['verdict']}[/] — {'; '.join(v['reasons'])}")
        console.print(f"Report: {path / 'report.md'}")


def experiments() -> None:
    """List available experiments."""
    with open_store(read_only=False) as (settings, _):
        for p in sorted((settings.paths.config / "experiments").glob("*.yaml")):
            console.print(f"- {p.stem}")


def register(app: typer.Typer) -> None:
    app.command("backtest")(backtest)
    app.command("experiments")(experiments)
