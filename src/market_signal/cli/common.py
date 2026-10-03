"""Shared CLI helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import typer
from rich.console import Console

from market_signal.config import Settings, get_settings
from market_signal.data.store import DatabaseBusy, Store

console = Console()


# CLI runs (incl. the scheduled daily job) wait for a busy database rather than fail:
# the dashboard only holds its write lock for a moment.
LOCK_TIMEOUT = float(os.environ.get("PRISM_LOCK_TIMEOUT", "600"))


@contextmanager
def open_store(read_only: bool = False) -> Iterator[tuple[Settings, Store]]:
    settings = get_settings()
    try:
        store = Store(settings.paths.db, settings.paths.raw, read_only=read_only,
                      lock_timeout=LOCK_TIMEOUT, on_wait=lambda m: console.print(f"[yellow]{m}[/]"))  # fmt: skip
    except DatabaseBusy as exc:
        console.print(f"[red]Gave up after {exc.waited:.0f}s: {exc.holder} still has the database open. "
                      + (f"If nothing should be running, check it with `ps -fp {exc.pid}`.[/]" if exc.pid else "[/]"))  # fmt: skip
        raise typer.Exit(1) from None
    try:
        yield settings, store
    finally:
        store.close()


def fmt_age(hours: float | None) -> str:
    if hours is None:
        return "never"
    if hours < 1:
        return f"{hours * 60:.0f}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.1f}d ago"
