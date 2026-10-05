"""Phase 21: fast prospective candidate incubation (exploratory paper, research only).

Phase 20 asked "does this strategy deserve capital now?" and answered conservatively: a
planted +3%/event edge took ~180 days to activate. That bar is right for real money and far
too slow for a zero-capital exploratory paper experiment. This package separates the two:

> Prism should be permissive about experimentation and strict about promotion.

> Exploratory paper admission is intentionally permissive. False candidate activations cost
> no capital and are useful observations.

> No Phase 21 state authorizes real trading.

Layers:

- ``policy``: frozen, versioned incubation policies (CONSERVATIVE = the unchanged Phase 20
  lifecycle as a benchmark; BALANCED; AGGRESSIVE), shadow execution and cadence;
- ``evidence``: the small admission rule set, era evidence and structured explanations;
- ``machine``: the fast incubation state machine, episodes and the graduation overlay;
- ``opportunity``: candidate opportunity-rate diagnostics;
- ``pool``: the preregistered candidate pool and its long/short balance audit;
- ``synthetic``: temporary-edge calibration and the edge-lifetime response surface;
- ``replay``: retrospective policy diagnostics (never used to choose thresholds);
- ``prospective``: the freeze, daily candidate snapshots, shadow intents and outcomes;
- ``events``: the future external-event interface (schema only; no event trading).

Nothing here places an order, touches the Phase 12 paper account, or is read by the
co-pilot, the paper engine or any live path.
"""
