"""Orchestrate one Phase 19 evaluation from retained inputs to a deterministic payload.

``evaluate(defn, load)``: ``load(venue, coin)`` returns the ``OiCoinData`` of the frozen
dataset (the governance layer passes a ledger-backed loader; tests and the null calibration
pass synthetic data). The primary venue is analysed on its own; the comparison venue's
snapshots only enter the cross-venue members (gated on coverage) and descriptions. The
payload has no clock and no random draws; runtime and memory go to ``meta``, which is not
part of the result digest.
"""

from __future__ import annotations

import time

from market_signal.research.oiprice.study import analysis as an
from market_signal.research.oiprice.study import collect as cl
from market_signal.research.oiprice.study import verdicts as vd
from market_signal.research.oiprice.study.spec import (
    AVAILABILITY_STATEMENT,
    DIRECTION_LABELS,
    EXPLORATORY_STATEMENT,
    FAMILIES,
    SIGNS,
    OiStudyDefinition,
    family_sizes,
)
from market_signal.research.structure.study.run import _peak_rss_mb, clean, digest

__all__ = ["digest", "evaluate"]


def evaluate(defn: OiStudyDefinition, load) -> tuple[dict, dict]:
    man = defn.manifest
    w, cmp_ = man.window, man.comparison
    t0 = time.perf_counter()
    meta: dict = {"seconds": {}}
    coins = {c: load(w.venue, c) for c in w.coins}
    comparison = {c: load(cmp_.venue, c) for c in cmp_.coins}
    meta["seconds"]["load"] = round(time.perf_counter() - t0, 2)
    ta = time.perf_counter()
    ctx = cl.context(defn, coins, comparison)
    variants = {key: cl.variant(ctx, defn, p) for key, p, _ in man.variants()}
    central = variants.pop("central")
    meta["seconds"]["features"] = round(time.perf_counter() - ta, 2)
    ta = time.perf_counter()
    res = an.analyse(defn, ctx, central, variants)
    meta["seconds"]["analysis"] = round(time.perf_counter() - ta, 2)
    res["event_window"] = [w.event_start.isoformat(), w.event_end.isoformat()]
    res["coins"] = list(w.coins)
    res["excluded"] = dict(w.excluded)
    res["inputs"] = {
        **{f"{w.venue}/{c}": {"dataset_id": d.dataset_id, "bars": len(d.bars) if d.bars else 0,
                              "oi_rows": d.oi_rows, "funding_rows": len(d.funding_ns),
                              "oi_usd_method": d.oi.usd_method} for c, d in coins.items()},
        **{f"{cmp_.venue}/{c}": {"dataset_id": d.dataset_id, "oi_rows": d.oi_rows,
                                 "oi_usd_method": d.oi.usd_method} for c, d in comparison.items()},
    }  # fmt: skip
    res["verdicts"] = _verdicts(res, man.statistics)
    payload = clean({
        "study_id": defn.study_id, "study_version": defn.study_version,
        "evidence_class": defn.evidence_class, "statement": EXPLORATORY_STATEMENT,
        "availability": {"mode": defn.availability_mode,
                         "assumed_bar_latency_s": man.assumed_bar_latency_s,
                         "assumed_oi_latency_s": man.assumed_oi_latency_s,
                         "statement": AVAILABILITY_STATEMENT},
        "timeframe": man.timeframe, "horizons_bars": list(man.horizons),
        "primary_horizon_bars": man.primary_horizon, "central": man.central.model_dump(mode="json"),
        "variants": [k for k, _, _ in man.variants()],
        "family_sizes_preregistered": family_sizes(),
        "datasets": [d.model_dump(mode="json") for d in defn.datasets],
        "families": defn.families,
        "primary": res,
    })  # fmt: skip
    meta["wall_seconds"] = round(time.perf_counter() - t0, 2)
    meta["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    meta["variants"] = len(variants) + 1
    return payload, meta


def _verdicts(res: dict, st) -> list[dict]:
    plateaus = {x["member"]: x for x in res.get("sensitivity", [])}
    out = []
    for fam, ms in FAMILIES.items():
        rows = {r["member"]: r for r in res["families"][fam]["members"]}
        for m in ms:
            s = rows[m.name]
            labels = DIRECTION_LABELS[m.orient]
            for sign, label in zip(SIGNS, labels, strict=True):
                d = an.directional(s, sign)
                pl = (plateaus.get(m.name) or {}).get(f"plateau_{sign}")
                v, why = vd.verdict(d, None, pl, an.floor_of(m, st), an.verdict_kind(m), st)
                out.append({"family": fam, "member": m.name, "kind": m.kind, "target": m.target,
                            "orient": m.orient, "baseline": m.baseline, "direction": label,
                            "sign": sign, "verdict": v, "reasons": why, "plateau": pl,
                            "stat": d.get("stat"), "ci95": d.get("ci95"),
                            "p_one_sided": d.get("p_value"), "q_value": d.get("q_value"),
                            "net_mean": d.get("net_mean"),
                            "untestable_reason": s.get("untestable_reason")})  # fmt: skip
    return out
