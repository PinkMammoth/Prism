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
) -> None:
    if db is not None:
        import os

        os.environ["PRISM_DB_PATH"] = str(db)


if __name__ == "__main__":  # pragma: no cover
    app()
