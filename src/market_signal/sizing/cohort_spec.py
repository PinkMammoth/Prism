"""Pure, offline checks for the prospective specification; no activation or storage API.

This module is deliberately not imported by any runtime/CLI. Validation is a contract
check, not an implementation of prospective dispatch, admission or sizing.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime

from pydantic import Field

from market_signal.research.lab.common import LabModel, canonical_json, content_id

SPEC_ID = "prospective_sizing_cohort_v2"
FROZEN_SHA256 = "8073b20d4cc61dd15b8e7ab55a12b3b343288a4a4172d1c81203753fbb40df17"


class ExperimentSpec(LabModel):
    schema_version: int = Field(strict=True, ge=1)
    spec_id: str
    scientific_question: str
    scope: dict
    provenance: dict
    cohorts: dict
    shared_components: dict
    execution_cost_contract: dict
    signal_contract: dict
    pairing_contract: dict
    permitted_divergence: list[str]
    protocol_deviations: dict
    start_gate: dict
    commissioning: dict
    evidence: dict
    primary_endpoint: dict
    secondary_metrics: list[str]
    paired_analysis: dict
    attribution: dict
    drawdown: dict
    missing_data: dict
    slippage_limitation: dict
    operational_stop: dict
    immutability: dict
    activation_contract: dict
    analysis_cadence: dict
    future_compatibility: dict
    limitations: list[str]


def spec_hash(spec: dict) -> str:
    """SHA-256 of canonical UTF-8 JSON; the digest is external, never self-referential."""
    return hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()


def validate_frozen_spec(spec: dict) -> ExperimentSpec:
    result = ExperimentSpec.model_validate(spec)
    if spec.get("spec_id") != SPEC_ID or spec_hash(spec) != FROZEN_SHA256:
        raise ValueError("frozen spec mismatch: semantic changes require a new ID/hash")
    return result


def require_successor(original: dict, successor: dict) -> None:
    """A changed specification cannot retain the old name or content identity."""
    if spec_hash(original) != spec_hash(successor) and (
        original["spec_id"] == successor["spec_id"]
        or original["schema_version"] == successor["schema_version"]
    ):
        raise ValueError("semantic changes require a new specification ID and version/hash")


def _timestamp(value) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone aware")
    canonical = parsed.astimezone(UTC).isoformat(timespec="microseconds")
    if value != canonical:
        raise ValueError("timestamp must be canonical UTC with six fractional digits")
    return parsed


def validate_opportunity(spec: dict, record: dict) -> str:
    """Validate a complete source record, then return its cohort-independent identity."""
    validate_frozen_spec(spec)
    contract = spec["pairing_contract"]
    if set(record) != set(contract["source_fields"]):
        raise ValueError("source fields mismatch; cohort/account state is forbidden upstream")
    if record["side"] not in ("long", "short"):
        raise ValueError("invalid side")
    for key in contract["source_nonempty_fields"]:
        if not isinstance(record[key], str) or not record[key]:
            raise ValueError(f"invalid {key}")
    signal = _timestamp(record["signal_timestamp"])
    frontier = _timestamp(record["causal_market_data_frontier"])
    decision = _timestamp(record["eligibility_evaluated_at"])
    if signal > decision or frontier > decision:
        raise ValueError("noncausal opportunity")
    eligibility = record["policy_independent_eligibility"]
    if (
        set(eligibility) != {"eligible", "reason_codes"}
        or type(eligibility["eligible"]) is not bool
    ):
        raise ValueError("invalid policy-independent eligibility")
    if not isinstance(eligibility["reason_codes"], list) or (
        not eligibility["eligible"] and not eligibility["reason_codes"]
    ):
        raise ValueError("unavailable source requires explicit reason codes")
    thesis = record["thesis_evidence"]
    if record["thesis_evidence_hash"] != content_id("", thesis):
        raise ValueError("thesis evidence hash mismatch")
    identity = {key: record[key] for key in contract["identity_fields"]}
    oid = content_id("sourceopportunity_", identity)
    if record["source_opportunity_id"] != oid:
        raise ValueError("opportunity identity mismatch")
    canonical_json(record)  # rejects nonfinite inputs, including nested quote/evidence
    return oid


def _number(value, *, positive=True) -> bool:
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


def candidate_input_rejections(spec: dict, inputs: dict) -> tuple[str, ...]:
    """Prospective precondition contract only. Never sizes, admits, or invents a fallback.

    The historical sizer permits an absent volatility input; the prospective contract
    explicitly requires it. A later adapter must implement this gate before calling size.
    """
    validate_frozen_spec(spec)
    failures = []
    rules = spec["missing_data"]["candidate_required_inputs"]
    for name, rule in rules.items():
        value = inputs.get(name)
        kind = rule["kind"]
        valid = (
            _number(value)
            if kind == "positive"
            else _number(value, positive=False)
            if kind == "nonnegative"
            else type(value) in (int, float) and math.isfinite(value)
            if kind == "finite"
            else type(value) is int and 0 <= value <= 8
            if kind == "size_decimals"
            else isinstance(value, list)
            if kind == "exposures"
            else isinstance(value, dict) and bool(value)
            if kind == "object"
            else isinstance(value, str) and bool(value)
        )
        if not valid:
            failures.append(rule["reason"])
    if not failures:
        side = inputs.get("side")
        if side not in ("long", "short"):
            failures.append("INVALID_SIDE")
        elif inputs["bid"] > inputs["ask"]:
            failures.append("INVALID_QUOTE")
        elif inputs["peak_equity"] < inputs["equity"]:
            failures.append("INVALID_EQUITY_PEAK")
        else:
            # Match sizing v1's initial geometry exactly, before its two impact passes.
            # Checking the bare bid/ask would introduce an additional admission rule.
            sign = 1 if side == "long" else -1
            quote = inputs["ask"] if sign > 0 else inputs["bid"]
            fill = quote * (1 + sign * inputs["slippage_bps"] / 10000)
            if sign * (fill - inputs["invalidation_price"]) <= 0:
                failures.append("INVALIDATION_BREACHED")
    return tuple(dict.fromkeys(failures))
