"""Phase 19: the preregistered open-interest x price x funding study (EXPLORATORY).

- ``spec``: the frozen definition (window, venues, parameters, families, gates, verdicts).
- ``data``: decoding retained snapshots (bars, funding, Binance OI; Hyperliquid snapshots).
- ``collect``: grid, context, per-variant features, states and events.
- ``analysis``: members, BH families, controls, breadth, stability, sensitivity.
- ``verdicts``: ``phase19_verdicts_v1``.
- ``run``: deterministic evaluation; ``report``: markdown of a stored result.
- ``calibration``: the null calibration on a synthetic market with OI and funding.

Governed by the Phase 17 study adapter (``research.lab.structure_study``).
"""
