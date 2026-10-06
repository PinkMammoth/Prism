"""Phase 23 context intelligence layer: data, semantics, provenance, researchability.

Context creates the trade thesis; positioning shows where the pain may be; price/volume/
microstructure help with timing and execution. Nothing in this package places orders, sizes
positions, changes risk, or is read by the forward tracker, co-pilot, paper trader or
incubation runner. Every directional thesis must still earn evidence in the Lab.

- ``taxonomy``: versioned categories, confidence ladder, relevance windows, materiality v1.
- ``model``: canonical observation/attribute schemas (``context_observation_v1``).
- ``entities``: explicit, versioned asset/entity resolver.
- ``ledger``: append-only ingest → dedupe → first observation + updates; as-of folds.
- ``macro``: scheduled-catalyst state, blackout-window flags and objective surprise.
- ``providers``: ``ContextProvider`` implementations and the external (ChatGPT Work) importer.
- ``positioning``: OI/funding/basis/long-short context, crowding and vulnerability states.
- ``opportunity``: non-directional activity state (Phase 22's validated volatility signals).
- ``snapshot``: point-in-time ``context_snapshot(asset, t)``.
- ``thesis``: research-only ``TradeThesis`` with full evidence provenance.
- ``event_study`` / ``probes``: descriptive event-study, reaction-time and Phase 22 probe adapters.
"""
