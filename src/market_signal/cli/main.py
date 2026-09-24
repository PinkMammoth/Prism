"""``market`` command-line interface."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from market_signal.cli import data_cmds

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Prism — local, deterministic multi-asset research & signal platform (no execution).",
)
console = Console()

app.command("update")(data_cmds.update)
app.command("doctor")(data_cmds.doctor)
app.command("import-csv")(data_cmds.import_csv)
app.command("assets")(data_cmds.assets)
app.command("demo-data")(data_cmds.demo_data)


def _register_optional() -> None:
    """Commands added by later phases register themselves here (kept import-light)."""
    import importlib

    for mod in ("research_cmds", "scan_cmds", "portfolio_cmds"):
        try:
            m = importlib.import_module(f"market_signal.cli.{mod}")
        except ModuleNotFoundError as exc:
            if exc.name != f"market_signal.cli.{mod}":
                raise
            continue
        m.register(app)


_register_optional()


@app.callback()
def main(
    db: Path | None = typer.Option(None, help="DuckDB path (default data/prism.duckdb)"),
    demo: bool = typer.Option(
        False, "--demo", help="Use the SYNTHETIC demo database (data/demo.duckdb). Not market data."
    ),
) -> None:
    import os

    if demo:
        from market_signal.config import get_settings

        os.environ["PRISM_DB_PATH"] = str(get_settings().paths.data / "demo.duckdb")
        os.environ["PRISM_SOURCE_OVERRIDE"] = "synthetic"
        console.print(
            "[bold black on yellow] SYNTHETIC DEMO DATA — not market data; no conclusions [/]"
        )
    elif db is not None:
        os.environ["PRISM_DB_PATH"] = str(db)


if __name__ == "__main__":  # pragma: no cover
    app()
