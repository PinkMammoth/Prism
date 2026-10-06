"""Intraday perp market data (Phase 15): substrate only, never a signal.

- ``grid``: canonical UTC timeframes and half-open bar boundaries.
- ``bars``: storage (closed bars only), revisions, point-in-time reads, coverage and gaps.
- ``ingest``: provider adapters (Hyperliquid live, Binance history), backfill, incremental
  update, raw archives and the disk guard.
- ``align``: causal multi-timeframe alignment (only bars closed AND available by a time).
- ``shadow``: observational execution timing around paper orders (never alters the paper run).

See docs/INTRADAY.md.
"""
