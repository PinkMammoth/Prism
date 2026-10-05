"""``market relative``: Phase 18 relative-strength / BTC-dislocation research (EXPLORATORY).

study register   WRITE: capture + retain datasets and freeze a study (evaluates nothing)
study run        WRITE: commit a run row, evaluate from the retained snapshots, record it
study list/show  READ-ONLY
study report     READ-ONLY: render a stored result
study calibrate  READ-ONLY: null calibration on a correlated synthetic market (no data)

Studies are governed by the Phase 17 study adapter (same tables and lifecycle).
"""

from __future__ import annotations

import json

import typer

from market_signal.cli.common import console, open_store

relative = typer.Typer(
    no_args_is_help=True,
    help="Relative strength / BTC dislocation (Phase 18): exploratory research, never a signal.",
)
study = typer.Typer(
    no_args_is_help=True,
    help="Phase 18 preregistered studies (EXPLORATORY; assumed-latency history).",
)
relative.add_typer(study, name="study")


def _software():
    from market_signal.cli.lab_cmds import _software as lab_software

    return lab_software()


def _fail(exc: Exception) -> None:
    console.print(f"Study: {exc}", style="red", markup=False)
    raise typer.Exit(1) from None


@study.command("register")
def study_register(
    manifest: str = typer.Argument(..., help="Phase 18 study manifest YAML"),
    reason: str = typer.Option(..., help="Why this study is being frozen"),
) -> None:
    """WRITE: capture + retain the datasets and freeze the study. Evaluates nothing."""
    from market_signal.perps.data import perp_config
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger, LedgerError
    from market_signal.research.relative.study.spec import family_sizes, load_manifest

    man = load_manifest(manifest)
    with open_store() as (settings, store):
        try:
            defn = ss.register(Ledger(store), man, perps_cfg=perp_config(settings), software=_software(),
                               origin=f"cli:{manifest}", reason=reason)  # fmt: skip
        except (LedgerError, ValueError) as exc:
            _fail(exc)
    console.print(f"Frozen study [bold]{defn.study_id}[/] ({man.name}), EXPLORATORY")
    for d in defn.datasets:
        console.print(f"  dataset {d.venue}/{d.coin}: {d.dataset_id}")
    console.print(f"  preregistered family sizes per venue x architecture: {family_sizes()}")


@study.command("run")
def study_run(
    study_id: str = typer.Argument(...),
    rerun_of: str = typer.Option(None, help="Earlier run of this study (explicit rerun)"),
    reason: str = typer.Option(None, help="Rerun reason (required with --rerun-of)"),
) -> None:
    """WRITE: commit a run row, evaluate from the retained snapshots, record the result."""
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger, LedgerError

    with open_store() as (_, store):
        try:
            out = ss.run(Ledger(store), study_id, software=_software(), rerun_of=rerun_of,
                         rerun_reason=reason)  # fmt: skip
        except (LedgerError, ValueError) as exc:
            _fail(exc)
    console.print(f"run {out['run_id']}: {out['status']} digest={out['result_digest']}")
    console.print(out["meta"])
    if out["status"] != "COMPLETED":
        console.print(out["payload"].get("error", {}).get("message", ""), markup=False)
        raise typer.Exit(1)


@study.command("list")
def study_list() -> None:
    """READ-ONLY: frozen studies (Phase 17 and Phase 18 share the registry)."""
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger

    with open_store(read_only=True) as (_, store):
        console.print_json(json.dumps(ss.list_studies(Ledger(store)), default=str))


@study.command("show")
def study_show(study_id: str = typer.Argument(...)) -> None:
    """READ-ONLY: definition summary, datasets, runs and result digests."""
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger

    with open_store(read_only=True) as (_, store):
        console.print_json(json.dumps(ss.inspect(Ledger(store), study_id), default=str))


@study.command("report")
def study_report(
    run_id: str = typer.Argument(...),
    out: str = typer.Option(None, help="Write markdown here instead of printing"),
    as_json: bool = typer.Option(False, "--json", help="The raw result payload"),
) -> None:
    """READ-ONLY: render a stored result (presentation only; computes nothing new)."""
    from pathlib import Path

    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger
    from market_signal.research.relative.study.report import render

    with open_store(read_only=True) as (_, store):
        payload = ss.result_payload(Ledger(store), run_id)
    text = json.dumps(payload, indent=1) if as_json else render(payload)
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


@study.command("calibrate")
def study_calibrate(
    manifest: str = typer.Argument(..., help="Phase 18 study manifest YAML"),
    seeds: str = typer.Option("1,2,3,4", help="Comma-separated synthetic seeds"),
    planted: float = typer.Option(0.0, help="Planted residual momentum (0 = the null)"),
    out: str = typer.Option(None, help="Write the full JSON (with per-test rows) here"),
) -> None:
    """READ-ONLY: run the harness (central parameters) on a correlated synthetic market and
    report false-positive rates and BH discoveries. Reads no market data."""
    from pathlib import Path

    from market_signal.research.relative.study.calibration import calibrate
    from market_signal.research.relative.study.spec import load_manifest

    res = calibrate(load_manifest(manifest), seeds=tuple(int(s) for s in seeds.split(",")),
                    planted=planted)  # fmt: skip
    if out:
        Path(out).write_text(json.dumps(res, indent=1, default=str))
    res.pop("rows")
    console.print_json(json.dumps(res, default=str))


def register(app: typer.Typer) -> None:
    app.add_typer(relative, name="relative")
