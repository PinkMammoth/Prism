"""Stop/invalidation audit of every portable/autotrader hypothesis. Read-only over frozen specs.

Classification is about the *loss boundary the sizer can rely on ex ante*, not about whether
the hypothesis is any good. Nothing here fabricates a stop.
"""

from __future__ import annotations

from collections import Counter


def _nimble_row(h, mapping, portability):
    family = mapping["objective"]
    rule = mapping["state_rule"]
    phase = h.source_phase
    if phase == 24:
        cls = "EXPLICIT_INVALIDATION"
        boundary = "prior_15_complete_1m_extreme_plus_one_spread (the setup's own structure)"
        note = "Thesis structure and stop coincide; state rule adds flow/absorption exits."
    elif phase == 23:
        cls = "DERIVABLE_INVALIDATION"
        boundary = (
            "prior_15m_extreme_plus_one_spread (1m microstructure, else latest 15m bar); "
            "the positioning thesis itself fails by state (" + rule + "), which has no price"
        )
        note = "Price stop is a generic structural convention, not the positioning thesis."
    elif phase == 22:
        cls = "DERIVABLE_INVALIDATION"
        boundary = (
            "prior_15m_extreme_plus_one_spread (1m microstructure, else latest 1h bar) "
            "for an hourly signal; state rule price_structure_only"
        )
        note = (
            "Hourly signal, 15-minute stop: frequently far tighter than hourly noise. "
            "Requires the volatility floor before risk-normalized sizing."
        )
    else:
        cls = "EXPLICIT_INVALIDATION"
        boundary = "pre_event_mid_plus_one_entry_spread_against_side; event state/resolution"
        note = "Event stop can be extremely tight when entry is near the pre-event level."
    vol_persisted = family == "volatility"
    return {
        "hypothesis_id": h.hypothesis_id,
        "name": h.name,
        "source_phase": phase,
        "side": h.side,
        "objective_family": family,
        "state_rule": rule,
        "expiry_minutes": mapping["expiry_minutes"],
        "max_hold_minutes": mapping["max_hold_minutes"],
        "classification": cls,
        "price_boundary": boundary,
        "emergency_guard": "10% adverse move (catastrophe guard, not a thesis stop)",
        "causal_volatility_recoverable_from_thesis": vol_persisted,
        "volatility_note": "objective = ref*(1 +/- 0.5*volatility_move): exactly recoverable"
        if vol_persisted
        else "volatility_move is not persisted in the thesis for this family; a prospective "
        "sizing cohort must record it at entry",
        "scientific_verdict": getattr(h, "scientific_verdict", None),
        "portability": portability.get(h.hypothesis_id),
        "note": note,
    }


def audit() -> dict:
    from market_signal.paper.nimble import event_spec, spec
    from market_signal.universe.portability import mapping as portability

    port = {r["hypothesis_id"]: r["classification"] for r in portability()}
    rows = [_nimble_row(h, spec.mapping(h), port) for h in spec.baseline.bootstrap()]
    rows += [_nimble_row(h, event_spec.mapping(h), port) for h in event_spec.bootstrap()]
    for key, c in spec.EVENT_CONTRACTS.items():
        rows.append(
            {
                "hypothesis_id": None,
                "name": key,
                "source_phase": None,
                "side": c["side"],
                "objective_family": c["objective"],
                "state_rule": "event_reaction",
                "classification": "UNSUITABLE_FOR_RISK_NORMALIZED_SIZING",
                "price_boundary": c["invalidation"],
                "note": "Unregistered contract only (registered=False); no trades can exist.",
            }
        )
    rows.append(
        {
            "hypothesis_id": None,
            "name": "paper_exploratory_v2_2 (retired fixed-horizon execution of the same 28)",
            "source_phase": None,
            "side": "both",
            "objective_family": "fixed_horizon",
            "state_rule": None,
            "classification": "EXPIRY_ONLY",
            "price_boundary": "none: exit at the frozen horizon; only the isolated-margin "
            "liquidation guard bounds loss",
            "note": "Retired. 6 closed trades. Usable only with a fallback-notional policy; "
            "excluded from risk-normalized experiments.",
        }
    )
    return {
        "version": "sizing_invalidation_audit_v1",
        "counts": dict(sorted(Counter(r["classification"] for r in rows).items())),
        "rows": rows,
    }
