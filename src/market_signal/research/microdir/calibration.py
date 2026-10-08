"""``microdir_calibration_v1``: the frozen harness on synthetic null and planted markets.

Run BEFORE any prospective outcome is read. Each scenario runs ``analysis.evaluate`` (the
real code path) on a deterministic synthetic market and reports, per scenario: verdict
counts, the target members' verdicts, BH discoveries and the raw one-sided p < 0.05 share
over verdict-family members (the false-positive rate under a null), plus the ladder's
layer tests and the key contrasts. ``zero_cost`` nulls check the test itself (costs make any
null look negative, which would hide an anti-conservative test).
"""

from __future__ import annotations

import time
from concurrent.futures import ProcessPoolExecutor

from market_signal.research.microdir import analysis as an
from market_signal.research.microdir import spec as sp
from market_signal.research.microdir import synthetic as sy
from market_signal.research.structure.study.spec import CostRef

VERSION = "microdir_calibration_v1"
DRIFT = 0.008  # planted log drift over the next hour (80 bp; per-episode sigma ~ 60-90 bp)
RATE = 3.0  # planted windows per coin per day

# (scenario, targets that SHOULD be detected, zero cost?)
SCENARIOS: tuple[tuple[sy.Scenario, tuple[str, ...], bool], ...] = (
    *((sy.Scenario(f"null_zero_cost_s{s}", "null", seed=s), (), True) for s in (11, 12, 13)),
    *((sy.Scenario(f"null_s{s}", "null", seed=s), (), False) for s in (21, 22, 23)),
    *((sy.Scenario(f"continuation_s{s}", "continuation", DRIFT, RATE, seed=s),
       ("continuation_book:long", "continuation_book:short"), False) for s in (31, 32)),
    *((sy.Scenario(f"absorption_s{s}", "absorption", DRIFT, RATE, seed=s),
       ("absorption_oi_rising:long", "absorption_oi_rising:short", "absorption_book:long",
        "absorption_book:short"), False) for s in (41, 42)),
    *((sy.Scenario(f"candle_equivalent_s{s}", "candle", 0.004, seed=s), (), False)
      for s in (51, 52)),
    *((sy.Scenario(f"temporary_s{s}", "temporary", DRIFT, RATE, recent_days=30, days=120,
                   seed=s), ("absorption_core:long", "absorption_core:short",
                             "absorption_oi_rising:long", "absorption_oi_rising:short"), False)
      for s in (61, 62)),
)  # fmt: skip


def zero_cost(defn):
    return defn.model_copy(update={"costs": tuple(
        CostRef(venue=c.venue, coin=c.coin, fee_bps=0, slippage_bps=0) for c in defn.costs)})  # fmt: skip


def summarize(name: str, res: dict, targets: tuple[str, ...], seconds: float) -> dict:
    mem = res["members"]
    verdict_members = {k: v for k, v in mem.items() if v["family"] in sp.VERDICT_FAMILIES}
    counts: dict[str, int] = {}
    for v in verdict_members.values():
        counts[v["verdict"]["verdict"]] = counts.get(v["verdict"]["verdict"], 0) + 1
    ps = [(v["stats"].get("net") or {}).get("p_one_sided") for v in verdict_members.values()]
    ps = [p for p in ps if p is not None]
    cand = [
        k for k, v in verdict_members.items() if v["verdict"]["verdict"] in sp.CANDIDATE_VERDICTS
    ]
    bh = [k for k, v in verdict_members.items() if (v.get("bh_q") or 1) <= sp.BH_Q]
    lad = {x["layer"]: (x.get("added_test") or {}).get("p_value") for x in res["ladder"]["layers"]}
    con = {k: {"diff_bps": None if (c.get("difference") or {}).get("mean") is None
               else c["difference"]["mean"] * 1e4, "t": (c.get("difference") or {}).get("t")}
           for k, c in res["contrasts"].items() if k.startswith(("oi_rising_vs_not",
                                                                  "resilient_vs_not",
                                                                  "consumed_vs_not"))}  # fmt: skip
    return {
        "scenario": name, "seconds": round(seconds, 1), "maturity": res["maturity"]["study_level"],
        "verdict_counts": counts, "candidates": cand, "bh_discoveries": bh,
        "raw_p05_share": sum(p < 0.05 for p in ps) / len(ps) if ps else None,
        "targets": {t: {"verdict": mem[t]["verdict"]["verdict"], "route": mem[t]["verdict"]["route"],
                        "net_bps": None if (mem[t]["stats"].get("net") or {}).get("mean") is None
                        else mem[t]["stats"]["net"]["mean"] * 1e4,
                        "t": (mem[t]["stats"].get("net") or {}).get("t"),
                        "recent_t": (mem[t]["stats"].get("recent") or {}).get("t")}
                    for t in targets},
        "detected": [t for t in targets if mem[t]["verdict"]["verdict"] in sp.CANDIDATE_VERDICTS],
        "non_target_candidates": [k for k in cand if k not in targets],
        "ladder_p": lad, "contrasts": con,
        "failure_flags": res["failure_analysis"]["flags"],
    }  # fmt: skip


def run_one(args) -> dict:
    sc, targets, zc, defn_json = args
    defn = sp.MicroStudyDefinition.model_validate_json(defn_json)
    if zc:
        defn = zero_cost(defn)
    t = time.time()
    res = an.evaluate(sy.market(sc), defn)
    return summarize(sc.name, res, targets, time.time() - t)


def calibrate(defn, *, workers: int = 1, only: str | None = None) -> dict:
    todo = [(sc, tg, zc, defn.canonical_json()) for sc, tg, zc in SCENARIOS
            if only is None or sc.name.startswith(only)]  # fmt: skip
    if workers > 1:
        with ProcessPoolExecutor(workers) as ex:
            rows = list(ex.map(run_one, todo))
    else:
        rows = [run_one(a) for a in todo]
    nulls = [r for r in rows if r["scenario"].startswith("null_s")]
    zero = [r for r in rows if r["scenario"].startswith("null_zero")]
    return {
        "version": VERSION, "study": sp.STUDY_NAME, "definition_digest": sp.definition_digest(),
        "drift": DRIFT, "rate_per_coin_day": RATE, "scenarios": rows,
        "null_false_candidates_per_run": (sum(len(r["candidates"]) for r in nulls) / len(nulls))
        if nulls else None,
        "zero_cost_null_raw_p05_share": (sum(r["raw_p05_share"] or 0 for r in zero) / len(zero))
        if zero else None,
        "zero_cost_null_false_candidates_per_run": (sum(len(r["candidates"]) for r in zero)
                                                    / len(zero)) if zero else None,
        "note": "synthetic only; no prospective outcome was read",
    }  # fmt: skip
