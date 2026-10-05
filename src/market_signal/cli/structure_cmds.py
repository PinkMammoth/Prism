"""``market structure``: structural-price primitives (Phase 16). Read-only research tooling.

catalog  the primitive registry: versions, layers and bounded parameter schemas
smoke    descriptive detector counts on stored intraday bars (no returns, no ranking)
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


def register(app: typer.Typer) -> None:
    app.add_typer(structure, name="structure")
