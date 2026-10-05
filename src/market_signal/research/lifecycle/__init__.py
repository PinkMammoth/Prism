"""Phase 20: time-varying edge and strategy-lifecycle evidence (research only).

Prism's earlier evidence asks whether a strategy worked across all eligible history. This
package adds a second, separate dimension: does the strategy appear to have a credible edge
*now*, is that edge strengthening, stable, decaying or gone, and how would a strictly causal,
hysteretic lifecycle have switched it on and off?

> Phase 20 does not lower Prism's evidence standards. It changes the hypothesis from
> "timeless edge" to "currently credible edge" while retaining full historical context.

> Temporary profitability is not assumed to be durable. Active status must continually be
> re-earned.

Layers (all pure functions over an outcome ledger, plus one governed study and an
append-only profile table):

- ``policy``: the frozen, versioned windows, gates, weights, floors and lifecycle rules;
- ``outcomes``: causal per-event outcomes of a Lab strategy (compiler + Prism perp returns);
- ``estimators``: rolling calendar / event-count / recency-weighted evidence;
- ``market``: coarse market-state and objective stress labels;
- ``changepoint``: online (causal) CUSUM and offline (retrospective) segmentation;
- ``state``: edge state, recent-vs-lifetime divergence, decay and continuous diagnostics;
- ``machine``: the lifecycle state machine and its walk-forward simulation;
- ``profile``: immutable edge-profile snapshots and read-only forward integration;
- ``synthetic``: planted-edge calibration;
- ``study``: the preregistered historical methodology experiment.

Nothing here grants alert, paper or live permissions, and no live consumer imports it.
"""
