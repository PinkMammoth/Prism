"""``market lab discovery``: Phase 22 intraday strategy discovery (EXPLORATORY, research only).

READ-ONLY:
  catalogue        the frozen intraday_catalogue_v1 (families, variants, long/short, IDs)
  calibrate        null + planted temporary-edge calibration on synthetic markets (no data)
  report RUN       render a stored result (markdown or --json)
  cook RUN         "What did the Lab cook?" — candidates for agents (--json)
  shortlist RUN    the frozen shortlist eligible for a FUTURE intraday incubation universe

WRITES (append-only, the Phase 17 study tables):
  register MANIFEST --reason   capture + retain datasets and freeze the study (evaluates nothing)
  run STUDY [--rerun-of --reason]

No command accepts a threshold, window or date: the frozen manifest decides. Nothing here
places an order, touches the paper account, the co-pilot, risk policy or the frozen Phase 21
incubation baseline, and nothing activates a strategy.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from market_signal.cli.common import console, open_store

discovery = typer.Typer(
    no_args_is_help=True,
    help="Intraday strategy discovery (Phase 22): exploratory research, never a live signal.",
)
JSON = typer.Option(False, "--json", help="Machine-readable output")


def _fail(exc: Exception) -> None:
    console.print(f"Discovery: {exc}", style="red", markup=False)
    raise typer.Exit(1) from None


def _payload(run_id: str) -> tuple[dict, str | None]:
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger

    with open_store(read_only=True) as (_, store):
        led = Ledger(store)
        payload = ss.result_payload(led, run_id)
        row = store.con.execute("SELECT result_digest FROM lab_structure_results WHERE run_id=?",
                                [run_id]).fetchone()  # fmt: skip
    return payload, row[0] if row else None


@discovery.command("catalogue")
def catalogue_cmd(
    as_json: bool = JSON, full: bool = typer.Option(False, help="List every variant")
) -> None:
    """READ-ONLY: the frozen catalogue."""
    from market_signal.research.discovery import catalogue as cat

    out = cat.summary()
    if full:
        out["strategies"] = [{"key": s.key, "strategy_id": s.strategy_id, "bh_family": s.bh_family,
                              "hypothesis": s.hypothesis, "complexity": s.complexity.score,
                              "primary_horizon_minutes": s.primary_minutes, "baseline": s.baseline}
                             for s in cat.strategies()]  # fmt: skip
        out["vol_tests"] = [{"key": v.key, "test_id": v.test_id} for v in cat.vol_tests()]
    console.print_json(json.dumps(out, default=str)) if as_json else console.print(out)


@discovery.command("calibrate")
def calibrate_cmd(
    manifest: str = typer.Option("config/discovery/phase22_intraday_discovery.v1.yaml"),
    workers: int = typer.Option(1, help="Processes (results do not depend on it)"),
    out: str = typer.Option(None, help="Write the full JSON here"),
) -> None:
    """READ-ONLY: run the harness on synthetic null and planted-edge markets."""
    from market_signal.research.discovery.calibration import calibrate
    from market_signal.research.discovery.spec import load_manifest

    res = calibrate(load_manifest(manifest), workers=workers)
    if out:
        Path(out).write_text(json.dumps(res, indent=1, default=str))
    console.print_json(json.dumps({"summary": res["summary"], "wall_seconds": res["wall_seconds"]},
                                  default=str))  # fmt: skip


@discovery.command("register")
def register_cmd(
    manifest: str = typer.Argument(..., help="Phase 22 discovery manifest YAML"),
    reason: str = typer.Option(..., help="Why this study is being frozen"),
) -> None:
    """WRITE: capture + retain the datasets and freeze the study. Evaluates nothing."""
    from market_signal.cli.lab_cmds import _software
    from market_signal.perps.data import perp_config
    from market_signal.research.discovery.spec import load_manifest
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger, LedgerError

    man = load_manifest(manifest)
    with open_store() as (settings, store):
        try:
            defn = ss.register(Ledger(store), man, perps_cfg=perp_config(settings),
                               software=_software(), origin=f"cli:{manifest}", reason=reason)  # fmt: skip
        except (LedgerError, ValueError) as exc:
            _fail(exc)
    console.print(f"Frozen discovery study [bold]{defn.study_id}[/] ({man.name}), EXPLORATORY")
    console.print(f"  catalogue {defn.catalogue['catalogue_id']} "
                  f"({len(defn.catalogue['strategies'])} strategies)")  # fmt: skip
    for d in defn.datasets:
        console.print(f"  dataset {d.venue}/{d.coin}: {d.dataset_id}")


@discovery.command("run")
def run_cmd(
    study_id: str = typer.Argument(...),
    rerun_of: str = typer.Option(None, help="Earlier run of this study (explicit rerun)"),
    reason: str = typer.Option(None, help="Rerun reason (required with --rerun-of)"),
) -> None:
    """WRITE: commit a run row, evaluate from the retained snapshots, record the result."""
    from market_signal.cli.lab_cmds import _software
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


@discovery.command("report")
def report_cmd(
    run_id: str = typer.Argument(...), out: str = typer.Option(None), as_json: bool = JSON
) -> None:
    """READ-ONLY: render a stored result."""
    from market_signal.research.discovery.report import render

    payload, _ = _payload(run_id)
    text = json.dumps(payload, indent=1) if as_json else render(payload)
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


@discovery.command("cook")
def cook_cmd(run_id: str = typer.Argument(...), as_json: bool = JSON,
             candidates_only: bool = typer.Option(False, help="Omit INTERESTING")) -> None:  # fmt: skip
    """READ-ONLY: what did the Lab cook? (structured; thresholds are not parameters)."""
    from market_signal.research.discovery.cook import cook

    payload, _ = _payload(run_id)
    res = cook(payload, include_interesting=not candidates_only)
    if as_json:
        print(json.dumps(res, indent=1, default=str))
        return
    console.print(f"{res['question']} study {res['study_id']}: {res['verdict_counts']}")
    for i in res["items"]:
        console.print(f"- [{i['evidence_state']}] {i['key']} ({i['strategy_id'][:18]}…) "
                      f"{i['frequency']['independent_per_day']:.2f}/day, window {i['evidence_window']}, "
                      f"policy {i['recommended_incubation_policy']}", markup=False)  # fmt: skip


@discovery.command("shortlist")
def shortlist_cmd(run_id: str = typer.Argument(...), out: str = typer.Option(None)) -> None:
    """READ-ONLY: the frozen shortlist (exact IDs, versions, semantics, frequency)."""
    from market_signal.research.discovery.cook import eligibility

    payload, dig = _payload(run_id)
    res = eligibility(payload, dig)
    text = json.dumps(res, indent=1, default=str)
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out} ({res['eligibility_id']})")
    else:
        print(text)
