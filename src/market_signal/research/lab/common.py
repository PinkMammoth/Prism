"""Canonical data-only serialization for Lab governance records (schema v1)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

UTCDateTime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
Text = Annotated[str, Field(strict=True, min_length=1, max_length=10_000)]
Name = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9_]{0,79}$")]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Symbol = Annotated[str, Field(strict=True, pattern=r"^[A-Z0-9][A-Z0-9._/-]{0,39}$")]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Probability = Annotated[Number, Field(ge=0, le=1)]
Count = Annotated[int, Field(strict=True, ge=0)]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]


class LabModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


def canonical_json(value: object) -> str:
    """UTC dates, sorted object keys, finite numbers; no implicit stringification."""

    def normalise(v):
        if isinstance(v, datetime):
            if v.tzinfo is None:
                raise ValueError("naive timestamps cannot be recorded")
            return v.astimezone(UTC).isoformat(timespec="microseconds")
        if isinstance(v, dict):
            if any(not isinstance(k, str) for k in v):
                raise ValueError("JSON object keys must be strings")
            return {k: normalise(x) for k, x in v.items()}
        if isinstance(v, list | tuple):
            return [normalise(x) for x in v]
        if isinstance(v, float) and v == 0:
            return 0.0
        return v

    return json.dumps(normalise(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_id(prefix: str, value: object) -> str:
    return prefix + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def strict_json(text: str):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError(f"duplicate JSON key: {key}")
            out[key] = value
        return out

    def constant(value):
        raise ValueError(f"nonfinite JSON value: {value}")

    result = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    canonical_json(result)  # also rejects overflow such as 1e999
    return result


def revalidate[T: BaseModel](value: T) -> T:
    """Do not trust model_copy/model_construct to have run Pydantic validation."""
    return type(value).model_validate(value.model_dump(mode="python"))
