"""Orchestrate one Phase 18 evaluation from retained inputs to a deterministic payload.

``evaluate(defn, load)``: ``load(venue, coin)`` returns the ``CoinData`` of the frozen
dataset (the governance layer passes a ledger-backed loader; tests and the null calibration
pass synthetic data). Venues are analysed separately and compared only afterwards (labels,
never pooled statistics). The payload is deterministic (seeded per hypothesis, no clock);
runtime and memory go to ``meta``, which is not part of the result digest.
"""

from __future__ import annotations

import time

from market_signal.research.relative.study import analysis as an
from market_signal.research.relative.study import verdicts as vd
from market_signal.research.relative.study.collect import collect
from market_signal.research.relative.study.spec import (
    AVAILABILITY_STATEMENT,
    DIRECTIONS,
    EXPLORATORY_STATEMENT,
    FAMILIES,
    RelativeStudyDefinition,
    family_sizes,
)
from market_signal.research.structure.study.run import _peak_rss_mb, clean, digest

__all__ = ["digest", "evaluate"]


def evaluate(defn: RelativeStudyDefinition, load) -> tuple[dict, dict]:
    man = defn.manifest
    st = man.statistics
    t0 = time.perf_counter()
    meta: dict = {"seconds": {}, "source_bars": {}, "variants": {}}
    cache: dict = {}
    archs: dict = {}
    for arch in man.architectures:
        res: dict = {"timeframe": arch.timeframe, "horizons_bars": list(arch.horizons),
                     "primary_horizon_bars": arch.primary_horizon,
                     "central": arch.central.model_dump(mode="json"),
                     "family_sizes_preregistered": family_sizes(),
                     "variants": [k for k, _, _ in arch.variants()], "venues": {}}  # fmt: skip
        meta["variants"][arch.name] = len(res["variants"])
        for w in arch.windows:
            ta = time.perf_counter()
            coins = {}
            for coin in w.coins:
                key = (w.venue, coin)
                if key not in cache:
                    cache[key] = load(w.venue, coin)
                coins[coin] = cache[key]
            col = collect(defn, arch, w.venue, coins)
            out = an.analyse_venue(defn, arch, col)
            out["event_window"] = [w.event_start.isoformat(), w.event_end.isoformat()]
            out["coins"] = list(w.coins)
            out["excluded"] = dict(w.excluded)
            res["venues"][w.venue] = out
            meta["seconds"][f"{arch.name}|{w.venue}"] = round(time.perf_counter() - ta, 2)
            meta["source_bars"][f"{arch.name}|{w.venue}"] = sum(
                v["rows"] for v in col.inputs.values()
            )
        res["cross_venue"], res["verdicts"] = _verdicts(res, st)
        archs[arch.name] = res
    payload = clean({
        "study_id": defn.study_id, "study_version": defn.study_version,
        "evidence_class": defn.evidence_class, "statement": EXPLORATORY_STATEMENT,
        "availability": {"mode": defn.availability_mode,
                         "assumed_latency_s": man.assumed_latency_s,
                         "statement": AVAILABILITY_STATEMENT},
        "datasets": [d.model_dump(mode="json") for d in defn.datasets],
        "families": defn.families,
        "architectures": archs,
    })  # fmt: skip
    meta["wall_seconds"] = round(time.perf_counter() - t0, 2)
    meta["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    return payload, meta


def _verdicts(res: dict, st) -> tuple[list, list]:
    venues = res["venues"]
    names = list(venues)
    cross, verdicts = [], []

    def member(v: str, fam: str, name: str) -> dict | None:
        fams = (venues.get(v) or {}).get("families") or {}
        return next(
            (r for r in (fams.get(fam) or {}).get("members", []) if r["member"] == name), None
        )

    def plateau(v: str, name: str, direction: str) -> str | None:
        return next((x.get(f"plateau_{direction}") for x in venues[v].get("sensitivity", [])
                     if x["member"] == name), None)  # fmt: skip

    for fam, ms in FAMILIES.items():
        for m in ms:
            for direction in DIRECTIONS:
                opp = "reversal" if direction == "continuation" else "continuation"
                dirs = {
                    v: an.directional(s, direction) for v in names if (s := member(v, fam, m.name))
                }
                opps = {v: an.directional(s, opp) for v in names if (s := member(v, fam, m.name))}
                if len(names) == 2:
                    a, b = names
                    cross.append({"family": fam, "member": m.name, "direction": direction,
                                  "label": vd.cross_venue_label(dirs.get(a), dirs.get(b), opps.get(a), opps.get(b)),
                                  **{f"{v}_stat": (dirs.get(v) or {}).get("stat") for v in names},
                                  **{f"{v}_p": (dirs.get(v) or {}).get("p_value") for v in names}})  # fmt: skip
                for v in names:
                    if v not in dirs:
                        continue
                    other = next((dirs.get(o) for o in names if o != v), None)
                    pl = plateau(v, m.name, direction)
                    label, why = vd.verdict(dirs[v], other, pl, an.floor_of(m, st), m.kind, st)
                    s = dirs[v]
                    verdicts.append({"venue": v, "family": fam, "member": m.name, "kind": m.kind,
                                     "target": m.target, "direction": direction, "verdict": label,
                                     "reasons": why, "plateau": pl, "stat": s.get("stat"),
                                     "ci95": s.get("ci95"), "p_one_sided": s.get("p_value"),
                                     "q_value": s.get("q_value"), "net_mean": s.get("net_mean")})  # fmt: skip
    return cross, verdicts
