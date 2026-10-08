"""Phase 24A: Hyperliquid microstructure collection (data only; docs/MICROSTRUCTURE_COLLECTION.md).

A persistent public-WebSocket collector turns live trades, order books and asset contexts into
deterministic, versioned 1-minute aggregates (``microstructure_1m_v1``) for future causal
research. It is **not** a strategy, a signal or an execution path:

* ``definitions``  frozen feature definitions, sampling cadence and thresholds (versioned)
* ``aggregate``    pure minute arithmetic (trade flow, prints, book samples, dynamics)
* ``engine``       the event-driven collector state machine (no network, injectable clock)
* ``ws``           the asyncio WebSocket runner (reconnect, resubscribe, staleness watchdog)
* ``spool``        append-only spool, short-retention raw archive, status file, collector lock
* ``ingest``       the authoritative runtime's idempotent spool → DuckDB ingest (single writer)
* ``query``        the research loader: known-at causality, completeness, resampling, CVD
* ``health``       collector/data health for the CLI, ``market status`` and runtime alerts

The collector never opens the Prism database: the runtime's scheduled ingest job is the only
writer (see the DuckDB concurrency decision in the docs). Nothing here imports or reaches order
execution, paper, co-pilot, risk, Phase 21/22/23 research state or any credential.
"""
