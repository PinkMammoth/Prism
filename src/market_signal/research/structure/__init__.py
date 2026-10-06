"""Structural-price and trade-path primitives (Phase 16): descriptive research infrastructure.

These primitives make discretionary trading vocabulary measurable without hindsight. They
do NOT claim that anything predicts returns, and no live consumer (forward tracker,
co-pilot, paper trader) reads them.

- ``registry``: primitive versions (``swing_v1`` ...) and bounded parameter specs; ChainSpec.
- ``series``: the input contract (one venue/coin/timeframe; availability; ATR normaliser).
- ``levels``: STATE: swings, prior extremes, equal-high/low clusters, touches, level state.
- ``events``: EVENTS: breach, breakout outcome, failed breakout, rejection, structure
  shift, retest.
- ``chain``: declarative chains of events and entry-delay analytics.
- ``path``: PATHS: MFE/MAE, thresholds, R, explicit OHLC ambiguity, nested resolution.
- ``regime``: minimal regime measurements (no classifier).
- ``manifest``: on-demand builds, digests and immutable exports.

See docs/STRUCTURE.md.
"""
