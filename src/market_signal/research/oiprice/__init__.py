"""Phase 19: open interest x price x funding research (EXPLORATORY; never a signal).

- ``primitives``: causal OI / price / funding features on one venue's hourly grid, with
  explicit OI availability semantics.
- ``coverage``: read-only OI coverage audit (per venue/coin, against price and funding).
- ``study``: the preregistered study (spec, collect, analysis, verdicts, run, report,
  calibration), governed by the Phase 17 study adapter.

See docs/PHASE19_OI_PRICE.md.
"""
