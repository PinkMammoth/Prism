"""``market structure``: structural-price primitives (Phase 16). Read-only research tooling.

catalog  the primitive registry: versions, layers and bounded parameter schemas
smoke    descriptive detector counts on stored intraday bars (no returns, no ranking)
study    Phase 17 preregistered falsification studies: register | run | list | show | report
"""

from __future__ import annotations

import json
import time

import typer
from rich.table import Table

from market_signal.cli.common import console, open_store

structure = typer.Typer(
    no_args_is_help=True,
    help="Structural-price primitives (Phase 16): descriptive research only, never a signal.",
)


@structure.command("catalog")
def catalog(as_json: bool = typer.Option(False, "--json")) -> None:
    """List primitive versions (state / event / path layers)."""
    from market_signal.research.structure.registry import REGISTRY_VERSION
    from market_signal.research.structure.registry import catalog as cat

    rows = cat()
    if as_json:
        console.print_json(json.dumps({"registry": REGISTRY_VERSION, "primitives": rows}))
        return
    t = Table(title=f"Structural primitives ({REGISTRY_VERSION})")
    for c in ("version", "layer", "meaning"):
        t.add_column(c)
    for r in rows:
        t.add_row(r["version"], r["layer"], r["meaning"])
    console.print(t)


@structure.command("smoke")
def smoke(
    venue: list[str] = typer.Option(["hyperliquid"], help="Venue(s); never mixed in one series"),
    coin: list[str] = typer.Option(None, help="Coins (default: the configured perp coins)"),
    timeframe: list[str] = typer.Option(
        ["15m", "1h", "4h", "mtf"], "--tf", help="15m, 1h, 4h and/or mtf (4h/1h/15m chain)"
    ),
    start: str = typer.Option(None, help="UTC start (bar open), inclusive"),
    end: str = typer.Option(None, help="UTC end (bar open), exclusive"),
    latency: float = typer.Option(60.0, help="Assumed publication latency (s) for history"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """READ-ONLY: detector counts per venue/coin/timeframe. A sanity check, not research."""
    from market_signal.cli.bars_cmds import _ts
    from market_signal.perps.data import perp_config
    from market_signal.research.structure.smoke import peak_rss_mb, run_smoke

    with open_store(read_only=True) as (settings, store):
        coins = (
            [c.upper() for c in coin]
            if coin
            else [str(c).upper() for c in perp_config(settings).get("coins") or []]
        )
        t0 = time.perf_counter()
        df = run_smoke(store, venue, coins, timeframe, _ts(start), _ts(end), latency)
        wall = time.perf_counter() - t0
    meta = {"wall_seconds": round(wall, 2), "peak_rss_mb": round(peak_rss_mb(), 1),
            "assumed_latency_s": latency, "rows": int(df["bars"].sum()) if len(df) else 0}  # fmt: skip
    if as_json:
        console.print_json(json.dumps({"meta": meta, "rows": df.to_dict(orient="records")},
                                      default=str))  # fmt: skip
        return
    t = Table(title="Structural smoke (descriptive counts; assumed-latency availability)")
    cols = [c for c in df.columns]
    for c in cols:
        t.add_column(c)
    for _, r in df.iterrows():
        t.add_row(*["-" if str(r[c]) == "nan" else str(r[c]) for c in cols])
    console.print(t)
    console.print(meta)


study = typer.Typer(
    no_args_is_help=True,
    help="Phase 17 preregistered falsification studies (EXPLORATORY; assumed-latency history).",
)
structure.add_typer(study, name="study")


def _software():
    from market_signal.cli.lab_cmds import _software as lab_software

    return lab_software()


def _fail(exc: Exception) -> None:
    console.print(f"Study: {exc}", style="red", markup=False)
    raise typer.Exit(1) from None


@study.command("register")
def study_register(
    manifest: str = typer.Argument(..., help="Study manifest YAML"),
    reason: str = typer.Option(..., help="Why this study is being frozen"),
) -> None:
    """WRITE: capture + retain the datasets and freeze the study. Evaluates nothing."""
    from market_signal.perps.data import perp_config
    from market_signal.research.lab import structure_study as ss
    from market_signal.research.lab.ledger import Ledger, LedgerError
    from market_signal.research.structure.study.spec import family_sizes, load_manifest

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
    for a in man.architectures:
        console.print(f"  {a.name}: preregistered family sizes {family_sizes(a)}")


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
    """READ-ONLY: frozen studies."""
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
    from market_signal.research.structure.study.report import render

    with open_store(read_only=True) as (_, store):
        payload = ss.result_payload(Ledger(store), run_id)
    text = json.dumps(payload, indent=1) if as_json else render(payload)
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


def register(app: typer.Typer) -> None:
    app.add_typer(structure, name="structure")
