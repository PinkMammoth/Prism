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


def research(
    only: list[str] = typer.Option(None, "--only", help="Run only these experiments"),
) -> None:
    """Run ALL experiments and write results/RESULTS_generated.md (the evidence behind RESULTS.md)."""
    from market_signal.demo import is_synthetic
    from market_signal.research.run_all import run_all
    from market_signal.research.runner import ResearchContext

    with open_store() as (settings, store):
        synthetic = is_synthetic(store)
        with console.status("Running all experiments (this can take several minutes)…"):
            path, reps = run_all(ResearchContext(store, settings), only or None, synthetic)
        for r in reps:
            console.print(f"{r.experiment.name:32s} {r.verdict['verdict']}")
        console.print(f"Summary: {path}")


def _pct(v, signed: bool = True) -> str:
    if v is None or v != v:
        return "–"
    return f"{v:+.1%}" if signed else f"{v:.0%}"


def track(
    calls: bool = typer.Option(False, "--calls", help="Also list every call and its outcome"),
) -> None:
    """Live track record: how Prism's recorded ACTIONABLE and WAIT calls actually played out."""
    import pandas as pd

    from market_signal.demo import is_synthetic
    from market_signal.research.track_record import (
        mark_independent,
        scan_coverage,
        score_calls,
        summarise,
    )

    with open_store() as (settings, store):
        if is_synthetic(store):
            console.print("[bold black on yellow] SYNTHETIC DATA — engine demonstration only [/]")
        cov = scan_coverage(store)
        console.print(f"Scans stored on {cov['with_scan']} of the last {cov['days']} days "
                      f"(first {cov['first']:%Y-%m-%d})." if cov["first"] is not None else
                      "No scans stored yet: run `market scan` daily (or open the dashboard).")  # fmt: skip
        sc = score_calls(store, settings)
        if sc.empty:
            console.print("No ACTIONABLE or WAIT calls recorded yet.")
            return
        sc = mark_independent(sc)
        summ = summarise(sc)
        min_n = int(settings.yaml("backtest.yaml")["statistics"]["min_events_for_conclusion"])
        t = Table(
            title="ACTIONABLE calls vs a random pick from the same asset class (net of costs)"
        )
        for c in (
            "horizon",
            "calls",
            "pending",
            "independent",
            "mean return",
            "benchmark",
            "mean excess",
            "beat benchmark",
            "stop hit",
        ):
            t.add_column(c)
        for _, r in summ[summ["group"] == "ACTIONABLE"].iterrows():
            t.add_row(r["horizon"], str(r["calls"]), str(r["pending"]), str(r["independent"]), _pct(r["mean_return"]),
                      _pct(r["mean_benchmark"]), _pct(r["mean_excess"]), _pct(r["beat_benchmark"], False),
                      _pct(r["stop_hit_rate"], False))  # fmt: skip
        console.print(t)
        t = Table(
            title="WAIT calls: did price reach the preferred entry, and did waiting beat buying now?"
        )
        for c in (
            "horizon",
            "calls",
            "pending",
            "independent",
            "filled",
            "median days",
            "after fill",
            "if bought now",
            "wait edge",
        ):
            t.add_column(c)
        for _, r in summ[summ["group"] == "WAIT"].iterrows():
            t.add_row(r["horizon"], str(r["calls"]), str(r["pending"]), str(r["independent"]), _pct(r["fill_rate"], False),
                      "–" if pd.isna(r["median_days_to_fill"]) else f"{r['median_days_to_fill']:.0f}",
                      _pct(r["mean_return_after_fill"]), _pct(r["mean_chase_return"]), _pct(r["mean_wait_edge"]))  # fmt: skip
        console.print(t)
        if summ["independent"].max() < min_n:
            console.print(f"[yellow]Too early to judge: fewer than {min_n} completed independent calls per row. "
                          "Treat these numbers as anecdotes.[/]")  # fmt: skip
        if calls:
            cols = [
                "bar",
                "symbol",
                "group",
                "status",
                "setup",
                "verdict",
                "horizon",
                "state",
                "ret",
                "excess",
                "filled",
                "wait_edge",
            ]
            console.print(sc[cols].sort_values(["bar", "symbol", "horizon"]).to_string(index=False))


def perp_strategies() -> None:
    """List the pre-registered perp strategies and their hypotheses."""
    from market_signal.perps.strategies import STRATEGIES

    for st in STRATEGIES.values():
        console.print(f"[bold]{st.name}[/]: {st.title} (primary horizon {st.primary_horizon})")
        console.print(f"  {st.hypothesis}")
        console.print(f"  defaults: {st.defaults}")


def perp_research(
    name: str = typer.Argument("all", help="Strategy name (see `market perp-strategies`) or 'all'"),
    no_walk_forward: bool = typer.Option(False, "--no-wf", help="Skip walk-forward"),
    no_sensitivity: bool = typer.Option(False, "--no-sens", help="Skip the sensitivity grid"),
) -> None:
    """Research perp strategies on stored perp data: event study, walk-forward, sensitivity,
    simulation and an automatic verdict (same criteria as spot). Reports go to results/perps/."""
    from market_signal.demo import is_synthetic
    from market_signal.perps.research import all_strategies, run_perp_research, save_perp_report
    from market_signal.research.report import fmt

    names = all_strategies() if name == "all" else [name]
    with open_store() as (settings, store):
        synthetic = is_synthetic(store)
        if synthetic:
            console.print("[bold black on yellow] SYNTHETIC DATA: engine demonstration only [/]")
        t = Table(title="Perp strategy research (net of fees, slippage and funding)")
        for c in ("strategy", "verdict", "events", "excess", "p", "WF +", "liq.", "max DD"):
            t.add_column(
                c, no_wrap=True, justify="left" if c in ("strategy", "verdict") else "right"
            )

        def pc(x) -> str:
            return "–" if x is None or x != x else f"{x:+.1%}"

        for n in names:
            with console.status(f"Researching {n}…"):
                try:
                    rep = run_perp_research(store, settings, n, walk_forward_on=not no_walk_forward,
                                            sensitivity_on=not no_sensitivity)  # fmt: skip
                except (RuntimeError, KeyError) as exc:
                    console.print(f"[red]{n}: {exc}[/]")
                    raise typer.Exit(1) from None
                path = save_perp_report(store, settings, rep)
            row = rep.study.summary.set_index("horizon").loc[rep.strategy.primary_horizon]
            wf = rep.wf_summary or {}
            m = rep.sim.metrics
            colour = {"PROMISING": "green", "WEAK_POSITIVE": "cyan", "REJECT": "red"}.get(
                rep.verdict["verdict"], "yellow"
            )
            t.add_row(n, f"[{colour}]{rep.verdict['verdict']}[/]", str(int(row["n_independent"])),
                      pc(row["excess_mean_indep"]), fmt(row["p_value_random_entry"]),
                      f"{wf.get('folds_positive', '–')}/{wf.get('folds_with_events', '–')}",
                      str(m.get("liquidations", 0)), pc(m.get("max_drawdown")))  # fmt: skip
            console.print(
                f"{n}: {rep.verdict['verdict']}: {'; '.join(rep.verdict['reasons'])}  → {path / 'report.md'}"
            )
        console.print(t)
        console.print("Verdicts use the same automatic criteria as spot research. PROMISING is not proven; "
                      "anything else means: don't trade it.")  # fmt: skip


def perp_paper() -> None:
    """Paper-tracking (forward test) of the perp strategies: live-recorded signals and how
    they have done since. Not advice; nothing here was traded."""
    from market_signal.perps.paper import evaluate_paper
    from market_signal.perps.strategies import STRATEGIES

    with open_store(read_only=True) as (settings, store):
        t = Table(title="Perp strategies: paper-tracking since first live check (not traded)")
        for c in ("strategy", "since", "bars", "signals", "done", "indep.", "excess", "p"):
            t.add_column(c, no_wrap=True, justify="left" if c in ("strategy", "since") else "right")
        early = False
        for name in STRATEGIES:
            r = evaluate_paper(store, settings, name)
            early |= r.too_early
            t.add_row(name, "–" if r.first_check is None else f"{r.first_check:%d %b %Y}", str(r.bars_checked),
                      str(r.signals), str(r.completed), str(r.independent),
                      "–" if r.excess is None else f"{r.excess:+.1%}", "–" if r.p_value is None else f"{r.p_value:.2f}")  # fmt: skip
        console.print(t)
        if early:
            console.print("[yellow]Too early to judge: fewer than 30 completed independent signals. "
                          "Checks are recorded daily by `market update`.[/]")  # fmt: skip


def register(app: typer.Typer) -> None:
    app.command("perp-paper")(perp_paper)
    app.command("perp-strategies")(perp_strategies)
    app.command("perp-research")(perp_research)
    app.command("track")(track)
    app.command("backtest")(backtest)
    app.command("experiments")(experiments)
    app.command("research")(research)
