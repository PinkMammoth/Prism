"""Shared CLI helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console

from market_signal.config import Settings, get_settings
from market_signal.data.store import Store

console = Console()


@contextmanager
def open_store(read_only: bool = False) -> Iterator[tuple[Settings, Store]]:
    settings = get_settings()
    store = Store(settings.paths.db, settings.paths.raw, read_only=read_only)
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
