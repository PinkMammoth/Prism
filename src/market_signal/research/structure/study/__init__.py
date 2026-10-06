"""Phase 17: preregistered falsification study of discretionary price-action claims.

EXPLORATORY research on backfilled intraday bars whose historical availability is
reconstructed under an assumed latency. Nothing here is a signal, a strategy family or an
input to any live consumer (forward tracker, co-pilot, paper trader).

- ``spec``: the frozen study definition (windows, grids, horizons, families, gates, verdicts).
- ``data``: snapshot rows -> ``BarSeries`` and funding arrays (never the live tables).
- ``populations``: the A->E ladder, held-breakout and simple controls, entries and returns.
- ``analysis``: matched baselines, independent events, tests, BH families, verdicts.
- ``report``: markdown rendering of a stored result.

See docs/PHASE17_FALSIFICATION.md.
"""
