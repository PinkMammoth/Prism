"""Offline counterfactual sizing research. Never writes a database; never sizes live trades."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer

from market_signal.cli.common import console

sizing = typer.Typer(
    no_args_is_help=True,
    help="Counterfactual sizing replay (research only; paper execution is unchanged)",
)
JSON = typer.Option(False, "--json")


def _out(value, as_json, path: Path | None = None):
    text = json.dumps(value, indent=2, sort_keys=True, default=str, allow_nan=False)
    if path:
        path.write_text(text + "\n")
        console.print(f"wrote {path}")
    elif as_json:
        print(text)
    else:
        console.print(value)


@sizing.command("policies")
def policies(as_json: bool = JSON):
    """List every versioned sizing policy and its content hash."""
    from market_signal.research.sizing import policy

    rows = [
        {"name": p.name, "status": p.status, "policy_id": p.policy_id} for p in policy.catalogue()
    ]
    _out(rows, as_json)


@sizing.command("audit")
def audit_cmd(as_json: bool = JSON):
    """Stop/invalidation audit of every registered hypothesis."""
    from market_signal.research.sizing import audit

    a = audit.audit()
    if as_json:
        _out(a, True)
        return
    console.print(a["counts"])
    for r in a["rows"]:
        console.print(f"{r['classification']:<40} {r['name']}", markup=False)


@sizing.command("scenario")
def scenario_cmd(name: str, out: Path = typer.Option(..., "--out")):
    """Write a SYNTHETIC scenario trade stream (JSONL). Not market data."""
    from market_signal.research.sizing import ledger, scenario

    trades = scenario.generate(scenario.SCENARIOS[name])
    ledger.write_jsonl(trades, out)
    console.print(f"wrote {len(trades)} SYNTHETIC trades to {out}")


@sizing.command("replay")
def replay_cmd(
    db: Path | None = typer.Option(None, help="OFFLINE backup copy of a Prism DuckDB (read-only)"),
    trades_file: Path | None = typer.Option(None, "--trades", help="TradeRecord JSONL"),
    run: list[str] = typer.Option([], "--run", help="Source run id (repeatable; never pooled)"),
    equity: list[float] = typer.Option([100.0, 200.0], "--equity"),
    policy_name: list[str] = typer.Option([], "--policy", help="Default: whole catalogue"),
    mode: str = typer.Option("both", help="independent | sequential | both"),
    start: str | None = None,
    end: str | None = None,
    hypothesis: str | None = None,
    cohort: str | None = typer.Option(None, help="CORE | SATELLITE"),
    reconcile: bool = typer.Option(False, help="Check the control reproduces recorded sizing"),
    out: Path | None = typer.Option(None, "--out"),
):
    """Replay frozen trades under sizing policies. Results go to a file, never a ledger."""
    from market_signal.config import get_settings
    from market_signal.research.sizing import ledger, metrics, policy, replay
    from market_signal.research.sizing.study import _revision

    if (db is None) == (trades_file is None):
        raise typer.BadParameter("give exactly one of --db or --trades")
    if mode not in ("independent", "sequential", "both"):
        raise typer.BadParameter("mode must be independent, sequential or both")
    if db is not None:
        if db.resolve() == get_settings().paths.db.resolve():
            console.print("Refused: point --db at an offline backup copy, not the live database.")
            raise typer.Exit(2)
        trades, source = ledger.nimble_trades(
            db, run or None, start=start, end=end, hypothesis=hypothesis, cohort=cohort
        )
        if len({t.run_id for t in trades}) > 1 and not run:
            console.print("Refused: several runs found; choose them explicitly with --run.")
            raise typer.Exit(2)
    else:
        trades = ledger.load_jsonl(trades_file)
        trades = [
            t
            for t in trades
            if (not run or t.run_id in run)
            and (not hypothesis or t.hypothesis_id == hypothesis)
            and (not cohort or t.cohort == cohort)
            and (not start or t.decision_at >= start)
            and (not end or t.decision_at <= end)
        ]
        source = {
            "source": str(trades_file),
            "trades": len(trades),
            "synthetic": any(t.synthetic for t in trades),
        }
    pols = [policy.by_name(n) for n in policy_name] if policy_name else list(policy.catalogue())
    results = []
    for p in pols:
        for eq in equity:
            row = {"policy": p.identity(), "starting_equity": eq}
            if mode in ("independent", "both"):
                row["independent"] = metrics.independent_summary(
                    replay.independent(trades, p, eq), eq
                )
            if mode in ("sequential", "both") and trades:
                row["sequential"] = metrics.sequential_summary(replay.sequential(trades, p, eq))
            results.append(row)
    value = {
        "kind": "COUNTERFACTUAL_SIZING_REPLAY (not a strategy backtest; not a ledger)",
        "replayed_at": datetime.now(UTC).isoformat(),
        "revision": _revision(),
        "source": source,
        "trade_ids": sorted(t.trade_id for t in trades),
        "assumptions": "signals, timestamps, prices, exits, fees and funding frozen; "
        "size, leverage and size-induced admission only",
        "reconciliation": ledger.reconcile(trades) if reconcile else None,
        "results": results,
    }
    _out(value, True, out)


@sizing.command("study")
def study_cmd(
    out_dir: Path = typer.Option(Path("docs/evidence/risk-position-sizing-v1"), "--out-dir"),
    bootstrap_n: int = typer.Option(200, "--bootstrap-n"),
    processes: int = typer.Option(4, "--processes"),
):
    """Regenerate the full SYNTHETIC-scenario sizing study evidence."""
    from market_signal.research.sizing import study

    _out(study.run(out_dir, bootstrap_n=bootstrap_n, processes=processes), True)


def register(app):
    app.add_typer(sizing, name="sizing")
