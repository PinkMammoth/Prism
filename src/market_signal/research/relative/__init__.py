"""Phase 18: relative strength, BTC beta/correlation, residuals and cross-asset dislocation.

EXPLORATORY research primitives and a preregistered study on backfilled intraday bars whose
historical availability is reconstructed under an assumed latency. Nothing here is a
signal, a strategy family or an input to any live consumer (forward tracker, co-pilot,
paper trader).

- ``primitives``: the causal cross-asset quantities (aligned panel, window returns,
  BTC-relative / market-relative / beta-adjusted residual returns, rolling beta and
  correlation, z-scores, cross-sectional ranks, edge triggers) and forward targets.
- ``study``: the frozen Phase 18 study (spec, collection, inference, analysis, report,
  null calibration), governed by the Phase 17 study adapter.

Four quantities are never conflated (every hypothesis names exactly one):

- the asset's own (USD) return;
- BTC's return;
- the asset's BTC-relative return ``r_asset - r_BTC``;
- the asset's beta-adjusted residual ``r_asset - beta * r_BTC``.

See docs/PHASE18_RELATIVE_STRENGTH.md.
"""
