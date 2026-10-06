"""Orchestrate one study evaluation from retained inputs to a deterministic result payload.

``evaluate(defn, load)``: ``load(venue, coin)`` returns the ``CoinData`` of the frozen
dataset (the governance layer passes a ledger-backed loader; tests pass synthetic data).
Venues are analysed separately and only compared afterwards (labels, never pooled
statistics). The payload is deterministic (seeded per hypothesis, no clock); runtime and
memory go to a separate ``meta`` block that is not part of the result digest.
"""

from __future__ import annotations

import hashlib
import resource
import time

import numpy as np

from market_signal.research.lab.common import canonical_json
from market_signal.research.structure.study import verdicts as vd
from market_signal.research.structure.study.analysis import analyse_venue
from market_signal.research.structure.study.collect import collect
from market_signal.research.structure.study.spec import (
    AVAILABILITY_STATEMENT,
    DIRECTIONS,
    EXPLORATORY_STATEMENT,
    HELD,
    LADDER,
    LEVEL_KINDS,
    STRETCH,
    StudyDefinition,
    family_sizes,
)


def clean(v):
    """JSON-safe, deterministic: numpy scalars to Python, non-finite floats to None."""
    if isinstance(v, dict):
        return {str(k): clean(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [clean(x) for x in v]
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float):
        return v if np.isfinite(v) else None
    if v is None or isinstance(v, bool | int | str):
        return v
    try:
        import pandas as pd

        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return str(v)


def digest(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def evaluate(defn: StudyDefinition, load) -> tuple[dict, dict]:
    man = defn.manifest
    st = man.statistics
    t0 = time.perf_counter()
    meta: dict = {"seconds": {}, "seconds_per_coin": {}, "source_bars": {}, "variants": {}}
    cache: dict = {}
    archs: dict = {}
    for arch in man.architectures:
        res: dict = {"timeframes": {"structure": arch.structure_tf, "event": arch.event_tf,
                                    "confirm": arch.confirm_tf, "resolve": arch.resolve_tf},
                     "horizons_bars": list(arch.horizons), "primary_horizon_bars": arch.primary_horizon,
                     "family_sizes_preregistered": family_sizes(arch),
                     "variants_per_kind": {k: len(arch.variants(k)) for k in LEVEL_KINDS},
                     "venues": {}}  # fmt: skip
        meta["variants"][arch.name] = res["variants_per_kind"]
        for w in arch.windows:
            ta = time.perf_counter()
            coins = {}
            for coin in man.coins:
                key = (w.venue, coin)
                if key not in cache:
                    cache[key] = load(w.venue, coin)
                coins[coin] = cache[key]
            col = collect(defn, arch, w.venue, coins)
            out = analyse_venue(defn, arch, col)
            out["event_window"] = [w.event_start.isoformat(), w.event_end.isoformat()]
            res["venues"][w.venue] = out
            meta["seconds"][f"{arch.name}|{w.venue}"] = round(time.perf_counter() - ta, 2)
            meta["seconds_per_coin"][f"{arch.name}|{w.venue}"] = col.seconds
            meta["source_bars"][f"{arch.name}|{w.venue}"] = sum(
                v["rows"] for c in col.inputs.values() for k, v in c.items() if isinstance(v, dict)
            )
        res["cross_venue"], res["verdicts"] = _verdicts(res, arch, st)
        archs[arch.name] = res
    payload = clean({
        "study_id": defn.study_id, "study_version": defn.study_version,
        "evidence_class": defn.evidence_class, "statement": EXPLORATORY_STATEMENT,
        "availability": {"mode": defn.availability_mode,
                         "assumed_latency_s": man.assumed_latency_s,
                         "statement": AVAILABILITY_STATEMENT},
        "datasets": [d.model_dump(mode="json") for d in defn.datasets],
        "architectures": archs,
    })  # fmt: skip
    meta["wall_seconds"] = round(time.perf_counter() - t0, 2)
    meta["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    return payload, meta


def _verdicts(res: dict, arch, st) -> tuple[list, list]:
    venues = res["venues"]
    names = list(venues)
    cross, verdicts = [], []
    combos = [(k, r) for k in LEVEL_KINDS for r in (*LADDER, HELD)]
    if arch.stretch_control is not None:
        combos.append(("none", STRETCH))

    def get(v, kind, rung, which):
        return ((venues.get(v) or {}).get("ladder") or {}).get(kind, {}).get(which, {}).get(rung)

    for kind, rung in combos:
        for which in DIRECTIONS:
            opp = vd.OTHER[which]
            if len(names) == 2:
                a, b = names
                cross.append({"kind": kind, "rung": rung, "direction": which,
                              "label": vd.cross_venue_label(get(a, kind, rung, which), get(b, kind, rung, which),
                                                            get(a, kind, rung, opp), get(b, kind, rung, opp)),
                              **{f"{v}_excess": (get(v, kind, rung, which) or {}).get("excess_mean") for v in names},
                              **{f"{v}_p": (get(v, kind, rung, which) or {}).get("p_value") for v in names}})  # fmt: skip
            for v in names:
                s = get(v, kind, rung, which)
                if s is None:
                    continue
                plateau = next((x["verdict"] for x in venues[v].get("sensitivity", [])
                                if x["kind"] == kind and x["rung"] == rung and x["direction"] == which), None)  # fmt: skip
                other = next((get(o, kind, rung, which) for o in names if o != v), None)
                label, why = vd.verdict(s, get(v, kind, rung, opp), plateau, other, st)
                verdicts.append({"venue": v, "kind": kind, "rung": rung, "direction": which,
                                 "verdict": label, "reasons": why, "plateau": plateau,
                                 "excess_mean": s.get("excess_mean"), "q_value": s.get("q_value"),
                                 "p_value": s.get("p_value"), "independent_events": s.get("independent_events")})  # fmt: skip
    return cross, verdicts
