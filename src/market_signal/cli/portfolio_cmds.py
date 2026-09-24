"""Portfolio / journal / alert commands."""

from __future__ import annotations

import json
from datetime import date

import pandas as pd
import typer
from rich.table import Table

from market_signal.cli.common import console, open_store


def position_open(
    symbol: str = typer.Argument(...),
    price: float = typer.Option(..., help="Entry price"),
    qty: float = typer.Option(..., help="Quantity"),
    kind: str = typer.Option("TRADE", help="TRADE | INVESTMENT"),
    stop: float = typer.Option(None, help="Invalidation price (required for TRADE)"),
    setup: str = typer.Option(
        None, help="quality_pullback | breakout_retest | rerating | discretionary"
    ),
    thesis: str = typer.Option("", help="Why are we entering?"),
    evidence: str = typer.Option("", help="What evidence supports it?"),
    horizon: str = typer.Option("", help="Intended horizon, e.g. 1-3m"),
    followed: bool = typer.Option(
        True, "--followed/--violated", help="Did this follow the system?"
    ),
    book: str = typer.Option("paper", help="paper | real"),
    entry_date: str = typer.Option(None, help="YYYY-MM-DD (default today)"),
    equity: float = typer.Option(None, help="Portfolio equity (to record risk at entry)"),
) -> None:
    """Record a new paper (or real/manual) position with its thesis."""
    from market_signal.portfolio.book import NewPosition, latest_scores, open_position
    from market_signal.scoring.engine import Scanner

    with open_store() as (settings, store):
        regime = None
        try:
            sc = Scanner(store, settings)
            a = settings.asset(symbol)
            regime = sc.regime_for(a, pd.Timestamp.now(tz="UTC"))[0]
        except Exception:
            pass
        pid = open_position(store, settings, NewPosition(
            symbol=symbol.upper(), kind=kind, entry_date=date.fromisoformat(entry_date) if entry_date else date.today(),
            entry_price=price, quantity=qty, book=book, setup=setup, thesis=thesis, evidence=evidence, horizon=horizon,
            invalidation_price=stop, score_at_entry=latest_scores(store).get(symbol.upper()), regime_at_entry=regime,
            followed_system=followed, portfolio_equity=equity,
        ))  # fmt: skip
        console.print(f"Opened {pid}")


def position_close(
    position_id: str = typer.Argument(...),
    price: float = typer.Option(...),
    reason: str = typer.Option(
        ...,
        help="stop | target | time | thesis_invalidated | valuation | better_opportunity | other",
    ),
    outcome: str = typer.Option(""),
    lessons: str = typer.Option(""),
    exit_date: str = typer.Option(None),
) -> None:
    """Close a position; realised return, MAE and MFE are computed from stored bars."""
    from market_signal.portfolio.book import close_position

    with open_store() as (settings, store):
        ret = close_position(store, settings, position_id, date.fromisoformat(exit_date) if exit_date else date.today(),
                             price, reason, outcome, lessons)  # fmt: skip
        console.print(f"Closed {position_id}: realised {ret:+.2%}")


def portfolio(book: str = typer.Option(None, help="paper | real | (all)")) -> None:
    """List positions with marks, scores, invalidation distance, MAE/MFE."""
    from market_signal.portfolio.book import journal_stats, positions_frame

    with open_store() as (settings, store):
        df = positions_frame(store, settings, book)
        if df.empty:
            console.print("No positions recorded.")
            return
        t = Table(title="Positions")
        cols = ["position_id", "book", "kind", "symbol", "setup", "entry_date", "entry_price", "mark", "unrealised_return",
                "realised_return", "score_at_entry", "current_score", "invalidation_price", "to_invalidation", "mae_to_date", "status"]  # fmt: skip
        for c in cols:
            t.add_column(c)
        for _, r in df.iterrows():
            t.add_row(*["–" if pd.isna(r[c]) else (f"{r[c]:+.1%}" if c in ("unrealised_return", "realised_return", "to_invalidation", "mae_to_date")
                        else f"{r[c]:,.4g}" if isinstance(r[c], float) else str(r[c])[:12]) for c in cols])  # fmt: skip
        console.print(t)
        stats = journal_stats(store)
        if stats.get("closed"):
            console.print("[bold]Which setups am I good at?[/]")
            console.print(stats["by_setup"].to_string(index=False))
            console.print("[bold]Do I follow my own rules?[/]")
            console.print(stats["by_discipline"].to_string(index=False))


def journal(
    position_id: str = typer.Argument(None),
    text: str = typer.Option(None),
    followed: bool = typer.Option(None, "--followed/--violated"),
) -> None:
    """Add a journal note (with --text) or show the journal."""
    from market_signal.portfolio.book import add_journal

    with open_store() as (_settings, store):
        if text:
            add_journal(store, position_id, "note", text, followed)
            console.print("Noted.")
            return
        sql = "SELECT created_at, position_id, kind, followed_system, text FROM journal_entries"
        df = store.query(sql + (" WHERE position_id=?" if position_id else "") + " ORDER BY created_at DESC LIMIT 50",
                         [position_id] if position_id else [])  # fmt: skip
        for _, r in df.iterrows():
            console.print(
                f"[dim]{str(r['created_at'])[:16]} {r['position_id']} {r['kind']} followed={r['followed_system']}[/]\n{r['text']}\n"
            )


def alert_add(
    rule: str = typer.Argument(
        ..., help='JSON, e.g. \'{"kind":"price_below","symbol":"HYPE","price":56}\''
    ),
) -> None:
    """Add a local alert rule."""
    from market_signal.portfolio.alerts import add_rule

    with open_store() as (_s, store):
        console.print(f"Added {add_rule(store, json.loads(rule))}")


def alerts(limit: int = typer.Option(30)) -> None:
    """Show alert rules and recent alert events."""
    with open_store() as (_s, store):
        console.print(
            store.query(
                "SELECT rule_id, kind, symbol, params FROM alert_rules WHERE active"
            ).to_string(index=False)
        )
        ev = store.query(
            "SELECT fired_at, symbol, message FROM alert_events ORDER BY fired_at DESC LIMIT ?",
            [limit],
        )
        console.print(ev.to_string(index=False) if not ev.empty else "No alerts fired yet.")


def register(app: typer.Typer) -> None:
    app.command("open")(position_open)
    app.command("close")(position_close)
    app.command("portfolio")(portfolio)
    app.command("journal")(journal)
    app.command("alert-add")(alert_add)
    app.command("alerts")(alerts)
