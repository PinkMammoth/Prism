"""Reproducibility for structural builds: generated on demand, optionally exported.

Decision (Phase 16): structural events are NOT persisted in the database. Like the Lab
compiler's masks, they are a pure function of (input bars, availability policy, primitive
versions, parameters), are cheap to regenerate (see docs/STRUCTURE.md, Performance), and
a persisted copy could silently go stale. A build can be EXPORTED as an immutable
directory of Parquet tables plus a manifest recording the source hashes, versions,
parameters, software and generation time, with a digest per table. An export directory
is never overwritten; a different build gets a different directory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from market_signal.research.lab.common import canonical_json, content_id
from market_signal.research.structure.registry import REGISTRY_VERSION
from market_signal.research.structure.series import BarSeries

BUILDER = "structure_build_v1"


def table_digest(df: pd.DataFrame) -> str:
    """Order- and value-sensitive SHA-256 of a table (column names included)."""
    h = hashlib.sha256()
    h.update("\0".join(map(str, df.columns)).encode())
    if len(df):
        clean = df.copy()
        for c in clean.columns:
            if clean[c].dtype == object:
                clean[c] = clean[c].map(
                    lambda v: (
                        None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)
                    )
                )
        h.update(pd.util.hash_pandas_object(clean, index=False).to_numpy().tobytes())
    return h.hexdigest()


def input_record(role: str, s: BarSeries) -> dict:
    return {
        "role": role,
        **s.prov.as_dict(),
        "rows": len(s),
        "first_open": None
        if not len(s)
        else pd.Timestamp(int(s.open_time[0]), tz="UTC").isoformat(),
        "last_open": None
        if not len(s)
        else pd.Timestamp(int(s.open_time[-1]), tz="UTC").isoformat(),
        "live_rows": int(s.live.sum()),
        "sha256": s.fingerprint(),
        # Historical (backfilled) bars have no true publication time: every timestamp
        # derived from an "assumed" series rests on the assumed latency.
        "availability_basis": (
            "assumed_latency" if s.prov.availability_mode == "assumed" else "observed"
        ),
    }


def build_manifest(spec: dict, inputs: list[dict], outputs: dict[str, pd.DataFrame],
                   software: dict, generated_at: datetime | None = None) -> dict:  # fmt: skip
    body = {
        "builder": BUILDER,
        "registry_version": REGISTRY_VERSION,
        "spec": spec,
        "inputs": inputs,
        "outputs": {k: {"rows": len(v), "sha256": table_digest(v)} for k, v in outputs.items()},
        "software": software,
    }
    # The ID covers what was computed from what; the generation time is recorded beside it.
    return {"build_id": content_id("sbuild_", body), **body,
            "generated_at": (generated_at or datetime.now(UTC)).isoformat()}  # fmt: skip


def export(out_dir: Path, manifest: dict, outputs: dict[str, pd.DataFrame]) -> Path:
    """Write ``<out_dir>/<build_id>/`` once. An existing build directory is never touched."""
    target = Path(out_dir) / manifest["build_id"]
    if target.exists():
        raise FileExistsError(f"{target} exists; structural exports are immutable")
    target.mkdir(parents=True)
    for name, df in outputs.items():
        df.to_parquet(target / f"{name}.parquet", index=False)
    (target / "manifest.json").write_text(
        json.dumps(json.loads(canonical_json(manifest)), indent=2)
    )
    return target
