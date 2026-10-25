"""Static core-six successor: one exploratory paper account over an evidence-screened allowlist.

Execution, risk, geometry, fee/slippage, 2x cost rule and quote semantics are inherited
unchanged from ``paper_nimble_v1``. Only the entry universe narrows: identities whose latest
completed evidence is REJECTED are excluded (their Lab history is untouched). An official
Hyperliquid outage blocks entry and exit fills, reusing the event/dynamic venue guard.
No hypothesis here has graduated; this is a prospective exploratory validation cohort.
"""

from copy import deepcopy
from functools import cache

from market_signal.paper.nimble import spec
from market_signal.paper.v2 import spec as baseline
from market_signal.research.lab.common import canonical_json, content_id

VERSION = "paper_nimble_core6_v1"
COHORT = "core_six_prospective_exploratory_validation_v1"
CLASSIFICATION = "PROSPECTIVE_EXPLORATORY_VALIDATION_COHORT"

# Latest completed evidence read before the freeze (2026-10-25).
PHASE22 = "Phase 22 frozen intraday study (2026-10-06 real run: NULL, 0 candidates)"
PHASE24B = {
    "study_id": "sstudy_16c5f72d6c4ec79e34d7d7bda50050323219d27df17fc2f76189afcc7684c8a9",
    "study": "microstructure_absorption_v1",
    "checkpoint_id": "mcheck_f75db5b21762478bbd322870033a96fd",
    "as_of": "2026-10-25T00:00:00+00:00",
    "authority": "vps-prism-research",
}

INPUTS = {
    22: {
        "required": "complete Hyperliquid 1h bar at the decision grid, <=20 min old; the rule's "
        "technical primitives finite",
        "missing": "stale/absent bar defers only this candidate (same instant retried); "
        "unready primitives record FEATURE_WARMUP",
    },
    23: {
        "required": "two Hyperliquid hourly OI captures <=80 min apart (latest <=80 min old) and "
        "the snapshot's 1h OI change/predicted funding",
        "missing": "absent/stale capture defers only this candidate; missing OI change, funding "
        "or price progress records FEATURE_WARMUP",
    },
    24: {
        "required": "complete 15m microstructure window with its frozen 288-window warmup",
        "missing": "incomplete window defers only this candidate; ineligible window records "
        "FEATURE_WARMUP",
    },
}
GLOBAL_INPUTS = {
    "required": [
        "fresh executable Hyperliquid bid/ask (<=3 s, future of the intent, uncrossed)",
        "quote session from an active collector process row of the claimed runtime",
        "predicted hourly funding rate (else FUNDING_UNAVAILABLE)",
        "full paper authority and the runtime lock",
    ],
    "enrichment": "Context/news/macro snapshot recorded with every decision; never a veto, "
    "confirmation or size input; absent values stay visibly absent",
    "venue": "active official Hyperliquid major/critical outage: VENUE_UNAVAILABLE, no entry "
    "or exit fill until recovery",
}

_PROBE = ("INTERESTING", PHASE22 + "; long probe verdict INTERESTING, not a candidate")
_POSITIONING = ("NO_VERDICT", "Phase 23 positioning; no completed study verdict")
ALLOWLIST = {
    ("ema_cross_trend[n=20]:1h:long", "long"): _PROBE,
    ("ema_stretch_fade[k=3.0]:1h:long", "long"): _PROBE,
    ("ema_stretch_fade[k=4.0]:1h:long", "long"): _PROBE,
    ("compression_breakout[n=24,pct=0.35]:1h:long", "long"): _PROBE,
    ("expansion_continuation[k=3.0]:1h:long", "long"): _PROBE,
    ("expansion_fade[k=3.0]:1h:long", "long"): _PROBE,
    ("exhaustion[n=24,z=3.0,strong_close=no]:1h:long", "long"): _PROBE,
    ("crowding_vulnerability", "long"): _POSITIONING,
    ("crowding_vulnerability", "short"): _POSITIONING,
    ("oi_price_stall", "long"): _POSITIONING,
    ("oi_price_stall", "short"): _POSITIONING,
    ("oi_directional_pressure", "long"): _POSITIONING,
    ("oi_directional_pressure", "short"): _POSITIONING,
    ("continuation_persistent", "long"): ("NO_EVIDENCE", "Phase 24B EARLY, 216 episodes"),
    ("absorption_book", "long"): ("IMMATURE", "Phase 24B WARMUP, 2 episodes"),
    ("absorption_book", "short"): ("IMMATURE", "Phase 24B WARMUP, 2 episodes"),
}
_CONTROL = ("REJECTED", PHASE22 + "; short control: contemporary net effect credibly negative")
EXCLUDED = {
    **{
        (k.replace(":long", ":short"), "short"): _CONTROL
        for k, side in ALLOWLIST
        if side == "long" and k.endswith(":1h:long")
    },
    ("continuation_core", "long"): ("REJECTED", "Phase 24B EARLY, 272 episodes"),
    ("continuation_core", "short"): ("REJECTED", "Phase 24B EARLY, 302 episodes"),
    ("continuation_persistent", "short"): ("REJECTED", "Phase 24B EARLY, 220 episodes"),
    ("absorption_oi_rising", "long"): ("REJECTED", "Phase 24B EARLY, 54 episodes"),
    ("absorption_oi_rising", "short"): ("REJECTED", "Phase 24B EARLY, 102 episodes"),
}


@cache
def bootstrap():
    by_key = {(h.source_key, h.side): h for h in baseline.bootstrap()}
    if set(ALLOWLIST) | set(EXCLUDED) != set(by_key) or set(ALLOWLIST) & set(EXCLUDED):
        raise ValueError("core-six allowlist must partition the released entry universe")
    return tuple(h for h in baseline.bootstrap() if (h.source_key, h.side) in ALLOWLIST)


def _entry(h, verdict):
    status, basis = verdict
    return {
        "hypothesis_id": h.hypothesis_id,
        "source_key": h.source_key,
        "side": h.side,
        "source_phase": h.source_phase,
        "evidence_status": status,
        "evidence_basis": basis,
        "inputs": INPUTS[h.source_phase],
        "execution_mapping": spec.mapping(h),
    }


@cache
def _definition():
    d = spec.definition()
    hs = bootstrap()
    ids = {h.hypothesis_id for h in hs}
    d["hypotheses"] = [
        dumped
        for h, dumped in zip(baseline.bootstrap(), d["hypotheses"], strict=True)
        if h.hypothesis_id in ids
    ]
    d["mappings"] = {k: v for k, v in d["mappings"].items() if k in ids}
    excluded = [h for h in baseline.bootstrap() if h.hypothesis_id not in ids]
    d["entry_universe_id"] = content_id(
        "paperuniverse_core6_", {"assets": baseline.COINS, "hypotheses": sorted(ids)}
    )
    d.update(
        version=VERSION,
        cohort=COHORT,
        classification=CLASSIFICATION,
        parent_policy_id=spec.policy_id(),
        assets=list(baseline.COINS),
        universe="static_core_six_no_dynamic_universe_admission",
        allowlist=[_entry(h, ALLOWLIST[(h.source_key, h.side)]) for h in hs],
        excluded=[_entry(h, EXCLUDED[(h.source_key, h.side)]) for h in excluded],
        evidence_checkpoints={"phase22": PHASE22, "phase24b": PHASE24B},
        inputs=GLOBAL_INPUTS,
        venue_guard="official_outage_blocks_entry_and_exit_fills",
        sizing="inherited_fixed_20pct_current_equity_no_cohort_no_adaptive",
        interpretation="signal_evidence_and_execution_policy_evidence_reported_separately",
    )
    return d


def definition():
    return deepcopy(_definition())


@cache
def canonical_definition():
    return canonical_json(definition())


@cache
def policy_id():
    return content_id("paperexecution_core6_", definition())


@cache
def universe_id():
    return definition()["entry_universe_id"]
