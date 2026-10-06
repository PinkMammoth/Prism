"""Paper-only auto-trader (Strategy Lab Phase 12): a simulated perp account, never real orders.

Nothing in this package can reach an exchange: it imports no network client, holds no
credentials and has exactly one execution adapter, ``execution.PaperExecutionAdapter``,
whose fills are pure arithmetic on stored market data.
"""
