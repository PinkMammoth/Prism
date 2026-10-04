"""Open-interest collection commands: ``market oi collect`` and ``market oi status``.

Data collection only: nothing here is a feature or a signal."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import typer
from rich.table import Table

from market_signal.cli.common import console, fmt_age, open_store

oi = typer.Typer(
    no_args_is_help=True, help="Perp open interest collection and coverage (data only)."
)

STYLE = {"ok": "green", "unsupported": "dim", "stale": "yellow", "NO DATA": "yellow",
         "STALE": "red", "AT RISK": "red", "LOST": "red"}  # fmt: skip


def render_coverage(cov: pd.DataFrame, title: str = "Open interest coverage") -> None:
    t = Table(title=title)
    for col in ("venue", "coin", "period", "symbol", "rows", "first", "last", "updated", "gaps 30d", "max gap", "status", "note"):  # fmt: skip
        t.add_column(col)
    for _, r in cov.iterrows():
        style = STYLE.get(r["status"], "yellow")
        t.add_row(
            r["venue"], r["coin"], r["period"], r["symbol"], str(r["rows"]),
            "-" if r["first"] is None else f"{pd.Timestamp(r['first']):%Y-%m-%d %H:%M}",
            "-" if r["last"] is None else f"{pd.Timestamp(r['last']):%Y-%m-%d %H:%M}",
            fmt_age(r["age_h"]), str(r["gaps_30d"]),
            "-" if r["max_gap_h"] is None else f"{r['max_gap_h']:.1f}h",
            f"[{style}]{r['status']}[/]", r["note"],
        )  # fmt: skip
    console.print(t)


@oi.command("collect")
def collect() -> None:
    """Hyperliquid OI snapshot + Binance OI rolling backfill (no date arguments; for a scheduler).

    Each run works out from stored coverage what is missing."""
    from market_signal.data.updaters import Updater
    from market_signal.perps.open_interest import collect_hl_snapshot, update_binance_oi

    steps = [
        Updater("oi", "Hyperliquid OI snapshot (prospective; missed captures are not recoverable)",
                lambda s, st: collect_hl_snapshot(s, st)),
        Updater("oi", "Binance perp OI: rolling ~30-day backfill", lambda s, st: update_binance_oi(s, st)),
    ]  # fmt: skip
    with open_store() as (settings, store):
        failed = sum(u.run(settings, store) for u in steps)
    if failed:
        console.print(
            f"[red]{failed} OI step(s) failed; stored data untouched for those. See `market doctor`.[/]"
        )
        raise typer.Exit(1)


@oi.command("status")
def status(
    gaps: bool = typer.Option(False, "--gaps", help="List each gap (last 30 days)"),
    coin: str = typer.Option(None, help="Only this coin"),
) -> None:
    """Latest observation, coverage, gaps and row counts per venue and coin."""
    from market_signal.perps.open_interest import BINANCE, load_oi, oi_config, oi_coverage, oi_gaps

    with open_store(read_only=True) as (settings, store):
        cov = oi_coverage(store, settings)
        if coin:
            cov = cov[cov["coin"] == coin.upper()]
        render_coverage(cov)
        console.print("[dim]Hyperliquid: opportunistic snapshots at irregular times (only while the PC runs); "
                      "gaps cannot be backfilled. Binance: hourly history, backfilled from a ~30-day API window. "
                      "Venues are separate series. Data only, not a signal.[/]")  # fmt: skip
        if not gaps:
            return
        cfg = oi_config(settings)
        since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=cfg.history_days)
        for _, r in cov[cov["rows"] > 0].iterrows():
            df = load_oi(store, r["coin"], r["venue"])
            if r["venue"] == BINANCE:
                df = df[df["period"] == cfg.period]
                g = oi_gaps(df["observed_at"], cfg.step * 1.5, since)
            else:
                g = oi_gaps(df["observed_at"], timedelta(hours=cfg.hl_gap_hours), since)
            for _, x in g.iterrows():
                console.print(f"  {r['venue']:<11} {r['coin']:<5} {x['after']:%Y-%m-%d %H:%M} → "
                              f"{x['before']:%Y-%m-%d %H:%M}  ({x['hours']:.1f}h)")  # fmt: skip


def register(app: typer.Typer) -> None:
    app.add_typer(oi, name="oi")
