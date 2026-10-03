"""Evidence profiles: tiers, topology, breadth, horizons, honesty and consumer neutrality."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.research.lab.evidence import (
    EvidencePolicy,
    EvidenceProfile,
    EvidenceSource,
    batch_report,
    build_profiles,
    gather,
    load_profiles,
    record_profiles,
)

PRIMARY = "5d"
ANALYSIS = {
    "primary_horizon": PRIMARY,
    "correction": {"test": "random_entry_mean_excess_v1", "q": 0.1, "method": "benjamini_hochberg"},
    "counts": {"correction_family": 10, "preregistered": 12},
    "batch_id": "batch_" + "1" * 64,
    "run_id": "batchrun_x",
    "analysis_id": "analysis_x",
    "analysis_version": "lab_batch_analysis_v1",
    "plan_id": "plan_" + "2" * 64,
    "dataset_id": "dataset_" + "3" * 64,
    "role": "discovery",
}


def rec(name, params, excess, *, family="fam", side="long", triage="WEAK",
        status="NOT_FDR_SIGNIFICANT", p=0.05, q=0.8, assets=None, horizons=None,
        horizon_bars=None, lineage=True):  # fmt: skip
    """A batch member as ``gather`` returns it, with Phase 4-shaped metrics."""
    assets = assets or {s: (excess, 20) for s in ("BTC", "ETH", "SOL")}
    horizon_bars = horizon_bars or {"1d": 1, "5d": 5, "20d": 20}
    horizons = horizons or {label: excess for label in horizon_bars}
    total = sum(n for _, n in assets.values())
    per_asset = [
        {"symbol": s, "horizon": PRIMARY, "independent_events": n, "excess_mean": e}
        for s, (e, n) in sorted(assets.items())
    ]
    aggregate = [
        {
            "horizon": label,
            "excess_mean": horizons[label],
            "net_mean": horizons[label],
            "gross_mean": horizons[label],
            "hit_rate": 0.55,
            "independent_events": total,
            "evaluable_events": total + 5,
            "raw_signals": total + 9,
            "assets_with_events": sum(n > 0 for _, n in assets.values()),
            "max_asset_event_share": max(n for _, n in assets.values()) / total,
            "p_value_random_entry": p if label == PRIMARY else None,
        }
        for label in horizon_bars
    ]
    sid = "strategy_" + hashlib.sha256(name.encode()).hexdigest()
    return {
        "strategy_id": sid,
        "name": name,
        "family_id": family,
        "market": "perp",
        "side": side,
        "lineage": {
            "generator": "lab_family_grid_v1",
            "family": family,
            "version": 1,
            "family_id": "family_" + family,
            "market": "perp",
            "side": side,
            "params": params,
        }
        if lineage
        else None,
        "metrics": {
            "aggregate": aggregate,
            "per_asset": per_asset,
            "provenance": {"horizons": horizon_bars},
        },
        "experiment_id": "experiment_" + name,
        "result_id": "result_" + name,
        "software_id": "software_" + "4" * 64,
        "versions": {"screen_version": "lab_fast_screen_v1"},
        "raw_p": p if triage in ("WEAK", "INTERESTING") else None,
        "q": q if triage in ("WEAK", "INTERESTING") else None,
        "batch_status": status,
        "phase4_triage": triage,
        "excess_mean": excess if triage in ("WEAK", "INTERESTING") else None,
        "substantive": excess > 0,
    }


def profiles(records, policy=None):
    return {
        p.subject["name"]: p for p in build_profiles(records, ANALYSIS, policy or EvidencePolicy())
    }


def plateau(n=4, effect=0.012, **kw):
    return [
        rec(f"v{a}", {"a": a}, effect + 0.0005 * i, **kw)
        for i, a in enumerate((10, 20, 30, 40)[:n])
    ]


# --------------------------------------------------------------------------- tiers


def test_fdr_honesty_raw_p_alone_never_reaches_research_supported():
    out = profiles(plateau(p=0.05, q=0.8))
    for p in out.values():
        assert p.tier == "EXPLORATORY"
        assert p.statistics["raw_p"] == 0.05 and p.statistics["q"] == 0.8
        assert p.statistics["fdr_survivor"] is False
        assert any("not an FDR survivor (q = 0.800" in r for r in p.limiting)
    survivors = profiles(plateau(p=0.001, q=0.04, status="FDR_SURVIVOR"))
    assert {p.tier for p in survivors.values()} == {"RESEARCH_SUPPORTED"}


def test_isolated_spike_is_flagged_and_blocks_research_support():
    records = [
        rec("v10", {"a": 10}, -0.01),
        rec("v20", {"a": 20}, 0.03, p=0.001, q=0.02, status="FDR_SURVIVOR"),
        rec("v30", {"a": 30}, -0.012),
        rec("v40", {"a": 40}, -0.008),
    ]
    spike = profiles(records)["v20"]
    nb = spike.neighbourhood
    assert nb["neighbours"] == 2 and nb["same_direction"] == 0 and nb["isolated_spike"]
    assert nb["label"] == "isolated" and spike.components["neighbourhood"] == "isolated"
    # strong enough to be worth a look, but a lone spike cannot be RESEARCH_SUPPORTED
    assert spike.tier == "EXPLORATORY"
    assert any("isolated spike" in r for r in spike.limiting)
    assert spike.family_context["target_rank_by_excess"] == 1
    assert "v20" in batch_report([json.loads(p.model_dump_json()) for p in profiles(records).values()])["families"][0]["isolated_spikes"]  # fmt: skip


def test_plateau_gives_neighbourhood_support():
    out = profiles(plateau())
    inner = out["v20"].neighbourhood
    assert inner["label"] == "plateau" and inner["support_share"] == 1.0
    assert inner["neighbours"] == 2 and not inner["isolated_spike"]
    edge = out["v10"].neighbourhood
    assert edge["neighbours"] == 1 and edge["label"] == "mixed"  # one neighbour cannot be a plateau
    report = batch_report([json.loads(p.model_dump_json()) for p in out.values()])
    assert report["families"][0]["pattern"] == "broad_directional"
    assert report["families"][0]["positive_plateaus"] == 2
    assert report["families"][0]["adverse_plateaus"] == 0
    adverse = batch_report(
        [json.loads(p.model_dump_json()) for p in profiles(plateau(effect=-0.03)).values()]
    )["families"][0]
    assert adverse["adverse_plateaus"] == 2 and adverse["pattern"] == "uniformly_weak_or_adverse"


def test_two_parameter_neighbours_are_one_adjacent_step():
    records = [
        rec(f"f{f}_s{s}", {"fast": f, "slow": s}, 0.01)
        for f in (10, 20, 50) for s in (50, 100, 200) if s >= 2.5 * f
    ]  # fmt: skip
    nb = profiles(records)["f20_s100"].neighbourhood
    names = {r["strategy_id"]: r["name"] for r in records}
    got = sorted(names[i] for i in nb["neighbour_strategy_ids"])
    # fast steps 10<->20<->50 at slow=100: only f10_s100 exists (f50_s100 violates the constraint)
    assert got == ["f10_s100", "f20_s200", "f20_s50"]


def test_asset_concentration_is_surfaced():
    assets = {"BTC": (0.08, 50), "ETH": (-0.01, 10), "SOL": (-0.012, 10)}
    records = [rec("conc", {"a": 10}, 0.05, assets=assets), rec("other", {"a": 20}, 0.05)]
    p = profiles(records)["conc"]
    a = p.assets
    assert a["dominated_by_one_asset"] and a["concentrated"]
    assert a["best"]["symbol"] == "BTC" and a["pooled_excess_without_top_contributor"] < 0
    assert p.tier != "EXPLORATORY" and p.components["concentration"] == "concentrated"
    report = batch_report([json.loads(x.model_dump_json()) for x in profiles(records).values()])
    assert "conc" in report["asset_specific"]


def test_horizon_reversal_is_not_consistent_and_stays_descriptive():
    reversing = rec("rev", {"a": 10}, 0.012, horizons={"1d": 0.01, "5d": 0.012, "20d": -0.02})
    steady = rec("rev", {"a": 10}, 0.012, horizons={"1d": 0.004, "5d": 0.012, "20d": 0.03})
    a, b = profiles([reversing])["rev"], profiles([steady])["rev"]
    assert a.horizons["shape"] == "reverses" and a.horizons["sign_consistency_with_primary"] < 1
    assert b.horizons["shape"] == "strengthens" and b.horizons["sign_consistency_with_primary"] == 1
    assert any("reverses" in r for r in a.limiting)
    # non-primary horizons never change statistics or the tier
    assert a.statistics == b.statistics and a.tier == b.tier
    assert [r["raw_p"] for r in a.horizons["rows"] if not r["primary"]] == [None, None]
    assert a.horizons["largest_effect_horizon_descriptive"] == "20d"


def test_insufficient_unavailable_and_negative():
    few = rec("few", {"a": 10}, 0.02, assets={"BTC": (0.02, 8), "ETH": (0.02, 8), "SOL": (0.02, 8)})
    assert profiles([few])["few"].tier == "INSUFFICIENT"
    thin = rec("thin", {"a": 10}, 0.0, triage="INSUFFICIENT_EVENTS")
    assert profiles([thin])["thin"].tier == "INSUFFICIENT"
    error = rec("err", {"a": 10}, 0.0, triage="ERROR", status="ERROR")
    p = profiles([error])["err"]
    assert p.tier == "UNAVAILABLE" and "no evidence either way" in p.limiting[0]
    adverse = profiles(plateau(effect=-0.03))
    assert {x.tier for x in adverse.values()} == {"NEGATIVE"}
    mixed = rec("mixed", {"a": 10}, 0.001)  # positive but below the effect floor
    assert profiles([mixed])["mixed"].tier == "INCONCLUSIVE"


def test_manual_strategies_without_lineage_need_a_strong_effect():
    weak = profiles([rec("manual", {}, 0.004, lineage=False)])["manual"]
    assert weak.neighbourhood == {"available": False, "reason": "no structured-family lineage"}
    assert weak.tier == "INCONCLUSIVE"
    strong = profiles([rec("manual", {}, 0.02, lineage=False, status="FDR_SURVIVOR", q=0.01)])
    # strong enough for EXPLORATORY, but no neighbourhood: never RESEARCH_SUPPORTED
    assert strong["manual"].tier == "EXPLORATORY"


# --------------------------------------------------------------------------- identity & neutrality


def test_profiles_are_deterministic_and_policy_versioned():
    a, b = profiles(plateau()), profiles(plateau())
    assert {k: v.profile_id for k, v in a.items()} == {k: v.profile_id for k, v in b.items()}
    assert a == b
    stricter = EvidencePolicy(version=2, exploratory={"min_effect": 0.02})
    c = profiles(plateau(), stricter)
    assert stricter.policy_id != EvidencePolicy().policy_id
    assert all(c[k].profile_id != a[k].profile_id for k in a)
    assert {p.tier for p in c.values()} == {"INCONCLUSIVE"}


FORBIDDEN = ("alert", "trade", "trading", "execut", "eligib", "sizing", "position_size",
             "approv", "telegram", "recommend", "notional", "leverage")  # fmt: skip


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            yield from _keys(v)


def test_profiles_are_consumer_neutral():
    out = list(profiles(plateau(status="FDR_SURVIVOR", p=0.001, q=0.01)).values()) + list(
        profiles(plateau(p=0.05, q=0.8)).values()
    )
    tiers = {p.tier for p in out}
    assert tiers == {"RESEARCH_SUPPORTED", "EXPLORATORY"}
    for p in out:
        keys = set(_keys(json.loads(p.model_dump_json())))
        assert not [k for k in keys if any(f in k.lower() for f in FORBIDDEN)]
    with pytest.raises(ValidationError):  # no consumer/action fields can be attached
        EvidenceProfile.model_validate({**out[0].model_dump(), "copilot_alert": True})
    # identity depends only on schema, policy, subject and cited records
    p = out[0]
    renamed = p.model_copy(update={"supporting": ("anything",)})
    assert renamed.profile_id == p.profile_id


def test_long_horizons_and_future_stages_fit_the_schema():
    long_horizon = rec(
        "radar", {"a": 10}, 0.05,
        horizon_bars={"30d": 30, "90d": 90, "180d": 180},
        horizons={"30d": 0.03, "90d": 0.05, "180d": 0.08},
    )  # fmt: skip
    p = build_profiles([long_horizon], {**ANALYSIS, "primary_horizon": "90d"}, EvidencePolicy())[0]
    assert p.evaluation["horizons"] == {"30d": 30, "90d": 90, "180d": 180}
    assert [r["horizon"] for r in p.horizons["rows"]] == ["30d", "90d", "180d"]
    # a later governed stage is a NEW profile that extends the earlier one
    later = EvidenceProfile.model_validate(
        {
            **p.model_dump(),
            "sources": [*p.model_dump()["sources"],
                        {"stage": "paper_forward", "records": {"run": "paper_1"}}],
            "extends": p.profile_id,
        }
    )  # fmt: skip
    assert later.profile_id != p.profile_id and p.extends is None
    with pytest.raises(ValidationError, match="VALIDATED requires"):
        EvidenceProfile.model_validate({**p.model_dump(), "tier": "VALIDATED"})
    validated = EvidenceProfile.model_validate(
        {**p.model_dump(), "tier": "VALIDATED",
         "sources": [*p.model_dump()["sources"],
                     {"stage": "validation", "records": {"run": "future"}}]}
    )  # fmt: skip
    assert validated.tier == "VALIDATED"  # only representable with a validation-stage source
    assert EvidenceSource(stage="live_forward", records={"x": "y"}).stage == "live_forward"


# --------------------------------------------------------------------------- governed integration


def test_governed_batch_profiles_cite_records_and_are_append_only(store):
    from market_signal.research.lab.batch import freeze_batch, inspect_batch, run_batch
    from market_signal.research.lab.datasets import capture_dataset
    from market_signal.research.lab.families import load_catalogue, register_family_batch
    from market_signal.research.lab.ledger import Ledger
    from tests.test_lab_families import CATALOGUE_DIR, _request
    from tests.test_lab_screen import SW, _plan, _seed, _selections

    _seed(store)
    ledger = Ledger(store)
    plan = _plan(statistics={"min_independent_events": 5, "random_entry_samples": 300})
    plan_id = ledger.register_plan(plan)
    dataset_id = ledger.register_dataset(capture_dataset(store, _selections(plan)))
    request = _request(plan_id, dataset_id, ["donchian_breakout", "rsi_exhaustion"])
    manifest, _ = register_family_batch(ledger, request, load_catalogue(CATALOGUE_DIR), origin="t")
    batch_id = freeze_batch(ledger, manifest, origin="test")
    run_batch(ledger, batch_id, software=SW)
    before = inspect_batch(ledger, batch_id)

    records, analysis = gather(ledger, batch_id)
    built = build_profiles(records, analysis, EvidencePolicy())
    assert len(built) == 22 and all(r["lineage"] for r in records)
    first = record_profiles(ledger, built, EvidencePolicy())
    again = record_profiles(ledger, build_profiles(*gather(ledger, batch_id), EvidencePolicy()),
                            EvidencePolicy())  # fmt: skip
    assert first["new"] == 22 and again["new"] == 0
    assert inspect_batch(ledger, batch_id) == before  # nothing underneath changed
    stored = load_profiles(ledger, analysis_id=analysis["analysis_id"])
    member = {m["strategy_id"]: m for m in analysis["members"]}
    for p in stored:
        m = member[p["subject"]["strategy_id"]]
        screen, fdr = p["sources"]
        assert screen["stage"] == "fast_screen" and screen["records"]["result_id"] == m["result_id"]
        assert fdr["records"]["analysis_id"] == analysis["analysis_id"]
        assert p["statistics"]["raw_p"] == m["raw_p"] and p["statistics"]["q"] == m["q"]
        assert p["tier"] != "VALIDATED"
        assert p["subject"]["family"] in ("donchian_breakout", "rsi_exhaustion")
    # CLI: build is idempotent; report and show are read-only
    from market_signal.cli.main import app

    path = store.path
    store.close()
    runner = CliRunner()
    base = ["--db", str(path), "lab", "evidence"]
    built_cli = runner.invoke(app, [*base, "build", batch_id])
    assert built_cli.exit_code == 0, built_cli.output
    assert json.loads(built_cli.output)["new"] == 0
    report = runner.invoke(app, [*base, "report", batch_id])
    assert report.exit_code == 0, report.output
    data = json.loads(report.output)
    assert sum(data["tiers"].values()) == 22 and "not a ranking of trades" in data["note"]
    shown = runner.invoke(app, [*base, "show", stored[0]["subject"]["strategy_id"]])
    assert json.loads(shown.output)[0]["profile_id"] == stored[0]["profile_id"]
