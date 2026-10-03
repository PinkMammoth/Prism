"""Strategy families: frozen identities, deterministic variants, lineage and batch planning."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.research.lab.batch import freeze_batch, inspect_batch, run_batch
from market_signal.research.lab.compiler import CompileCache, Snapshot, compile_strategy
from market_signal.research.lab.datasets import capture_dataset
from market_signal.research.lab.families import (
    FamilyBatchRequest,
    StrategyFamily,
    assignments,
    generate,
    load_catalogue,
    load_family,
    max_supported_members,
    name_token,
    plan_family_batch,
    register_family_batch,
)
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.spec import load_hypothesis
from tests.test_lab_screen import SW, _plan, _seed, _selections

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_DIR = ROOT / "config/lab/families"

# Frozen catalogue: editing a family without bumping its version must fail here.
PINNED = {
    ("donchian_breakout", 1): ("family_7a33bc657c5d8", {"perp": 10, "spot": 5}),
    ("funding_extreme_fade", 1): ("family_79f72ceb64de3", {"perp": 16}),
    ("funding_momentum_exhaustion", 1): ("family_6b6dca1ab823d", {"perp": 16}),
    ("ma_distance_reversion", 1): ("family_1be815de2e648", {"perp": 12, "spot": 6}),
    ("ma_trend", 1): ("family_411270433a64c", {"perp": 14, "spot": 7}),
    ("rsi_exhaustion", 1): ("family_c952a6e976203", {"perp": 12, "spot": 6}),
    ("trend_pullback", 1): ("family_41262f1986173", {"perp": 16, "spot": 8}),
    ("vol_compression_breakout", 1): ("family_fe8f1920a4af2", {"perp": 16, "spot": 8}),
    ("volume_breakout", 1): ("family_757f7a181083c", {"perp": 16, "spot": 8}),
}


@pytest.fixture(scope="module")
def catalogue():
    return load_catalogue(CATALOGUE_DIR)


def _raw(name="ma_trend"):
    return yaml.safe_load((CATALOGUE_DIR / f"{name}.v1.yaml").read_text())


# --------------------------------------------------------------------------- catalogue


def test_catalogue_is_frozen_and_bounded(catalogue):
    assert set(catalogue) == set(PINNED)
    for key, family in catalogue.items():
        prefix, counts = PINNED[key]
        assert family.family_id.startswith(prefix), key
        assert {m: len(generate(family, m)) for m in family.markets} == counts
        assert all(n <= family.max_variants for n in counts.values())
        assert family.rationale.strip()
    assert sum(len(generate(f, "perp")) for f in catalogue.values()) == 128
    # the whole perp catalogue fits one batch at q = 0.10 with the default 2,000 draws
    assert 128 <= max_supported_members(2000, 0.1) == 200


def test_generation_is_deterministic(catalogue):
    for family in catalogue.values():
        for market in family.markets:
            a, b = generate(family, market), generate(family, market)
            assert [v.strategy_id for v in a] == [v.strategy_id for v in b]
            assert [v.hypothesis for v in a] == [v.hypothesis for v in b]
    again = load_catalogue(CATALOGUE_DIR)
    assert {k: f.family_id for k, f in again.items()} == {
        k: f.family_id for k, f in catalogue.items()
    }


def test_yaml_ordering_does_not_change_identity():
    raw = _raw("vol_compression_breakout")
    family = StrategyFamily.model_validate(raw)
    shuffled = deepcopy(raw)
    shuffled["parameters"] = {k: list(reversed(v)) for k, v in reversed(raw["parameters"].items())}
    shuffled["long"]["conditions"].reverse()
    shuffled["constraints"] = list(reversed(raw["constraints"]))
    shuffled = dict(reversed(list(shuffled.items())))
    other = StrategyFamily.model_validate(shuffled)
    assert other.family_id == family.family_id
    assert [v.strategy_id for v in generate(other, "perp")] == [
        v.strategy_id for v in generate(family, "perp")
    ]


def test_any_frozen_change_creates_a_new_identity():
    base = StrategyFamily.model_validate(_raw()).family_id
    changes = [
        lambda r: r.update(version=2),
        lambda r: r["parameters"].update(fast=[10, 20]),
        lambda r: r["constraints"][0].update(factor=3.0),
        lambda r: r.update(cooldown_bars=5),
        lambda r: r["long"]["conditions"][1].update(op="ge"),
        lambda r: r.update(rationale="A different mechanism."),
    ]
    for change in changes:
        raw = _raw()
        change(raw)
        assert StrategyFamily.model_validate(raw).family_id != base


# --------------------------------------------------------------------------- generation rules


def test_constraints_exclude_invalid_combinations(catalogue):
    family = catalogue[("ma_trend", 1)]
    grid = assignments(family)
    assert all(p["slow"] >= 2.5 * p["fast"] for p in grid)
    assert {(p["fast"], p["slow"]) for p in grid} == {
        (10, 50), (10, 100), (10, 200), (20, 50), (20, 100), (20, 200), (50, 200)
    }  # fmt: skip
    for v in generate(family, "perp"):
        p = dict(v.params)
        assert p["fast"] < p["slow"]


def test_variant_cap_is_enforced_without_truncation():
    raw = _raw()
    raw["parameters"] = {"fast": [5, 10, 15, 20], "slow": [50, 100, 150, 200]}
    with pytest.raises(ValidationError, match="max_variants"):
        StrategyFamily.model_validate(raw)


def test_lineage_names_and_sides(catalogue):
    names = set()
    for family in catalogue.values():
        for market in family.markets:
            for v in generate(family, market):
                lineage = json.loads(v.hypothesis.source)
                assert lineage == {
                    "generator": "lab_family_grid_v1",
                    "family": family.family,
                    "version": family.version,
                    "family_id": family.family_id,
                    "market": market,
                    "side": v.side,
                    "params": dict(v.params),
                }
                assert v.hypothesis.created_at == family.authored_at
                assert v.name.startswith(family.family.split("_")[0])
                assert v.name.endswith("_" + v.side) and v.hypothesis.definition.side == v.side
                assert v.complexity["conditions"] <= 2 and v.complexity["free_parameters"] <= 3
                names.add((market, v.name))
            if market == "spot":
                assert {v.side for v in generate(family, "spot")} == {"long"}
    assert len(names) == 128 + 48  # unique within and across families
    assert generate(catalogue[("ma_trend", 1)], "perp")[0].name == "ma_trend_10_50_long"
    assert [name_token(x) for x in (20, 0.95, 0.1, 1.5, -2, 2.0)] == [
        "20", "p95", "p1", "1p5", "m2", "2"
    ]  # fmt: skip


INVERSE = {"gt": "lt", "lt": "gt", "ge": "le", "le": "ge",
           "crosses_above": "crosses_below", "crosses_below": "crosses_above"}  # fmt: skip


@pytest.mark.parametrize(
    ("family", "mirror_feature", "mirror_number"),
    [
        ("ma_trend", {}, None),
        ("donchian_breakout", {"donchian_high": "donchian_low"}, None),
        ("rsi_exhaustion", {}, lambda x: 100 - x),
        ("ma_distance_reversion", {}, lambda x: -x),
        ("funding_extreme_fade", {}, lambda x: 1 - x),
        ("trend_pullback", {}, lambda x: 100 - x),
    ],
)
def test_short_variants_mirror_long_variants(catalogue, family, mirror_feature, mirror_number):
    variants = generate(catalogue[(family, 1)], "perp")
    longs = {v.params: v for v in variants if v.side == "long"}
    shorts = {v.params: v for v in variants if v.side == "short"}
    assert longs.keys() == shorts.keys()

    def mirrored(cond):
        right = cond.right
        if hasattr(right, "name"):
            stem = right.name.rsplit("_", 1)
            right = (
                (mirror_feature.get(stem[0], stem[0]) + "_" + stem[1])
                if len(stem) > 1
                else right.name
            )
        else:
            right = pytest.approx(mirror_number(right))
        return (cond.left.name, INVERSE[cond.op], right)

    for params, long in longs.items():
        expected = sorted((mirrored(c) for c in long.hypothesis.definition.conditions), key=str)
        actual = sorted(
            ((c.left.name, c.op, c.right.name if hasattr(c.right, "name") else c.right)
             for c in shorts[params].hypothesis.definition.conditions), key=str
        )  # fmt: skip
        assert [a[:2] for a in actual] == [e[:2] for e in expected]
        assert [a[2] for a in actual] == [e[2] for e in expected]


# --------------------------------------------------------------------------- schema


def test_family_schema_is_strict(tmp_path):
    bad = [
        lambda r: r.update(surprise=1),
        lambda r: r["long"]["conditions"][0].update(right="ema_{slow}"),  # no bare strings
        lambda r: r["long"]["conditions"][0].update(left="ema_{medium}"),  # unknown param
        lambda r: r["long"]["conditions"][0].update(left="ema_{fast}_{fast}"),  # bad feature
        lambda r: r.update(name="ma_trend_{fast}_{side}"),  # slow missing from the name
        lambda r: r.update(markets=["perp", "perp"]),
        lambda r: r["parameters"].update(fast=[10, 10]),
        lambda r: r["parameters"].update(extra=[1, 2]),  # unused -> duplicate strategies
        lambda r: r["constraints"].append({"left": "fast", "op": "lt", "right": "nope"}),
        lambda r: r["parameters"].update(fast=[10.5, 20.0]),  # feature periods are integers
    ]
    for change in bad:
        raw = _raw()
        change(raw)
        with pytest.raises((ValidationError, ValueError)):
            StrategyFamily.model_validate(raw)
    path = tmp_path / "ma_trend.v1.yaml"
    path.write_text((CATALOGUE_DIR / "ma_trend.v1.yaml").read_text() + "version: 2\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_family(path)
    renamed = tmp_path / "trend.yaml"
    renamed.write_text((CATALOGUE_DIR / "ma_trend.v1.yaml").read_text())
    with pytest.raises(ValueError, match=r"named ma_trend\.v1\.yaml"):
        load_family(renamed)


def test_every_perp_variant_compiles_on_a_retained_snapshot(store, catalogue):
    _seed(store, coins=("BTC",))
    plan = _plan(costs=[{"symbol": "BTC", "fee_bps": 4.5, "slippage_bps": 2.0}])
    capture = capture_dataset(store, _selections(plan, coins=("BTC",)))
    from market_signal.research.lab.datasets import snapshot_rows

    blobs = dict(capture.blobs)
    snap = Snapshot(capture.manifest.dataset_id, capture.manifest,
                    tuple(snapshot_rows(s, blobs[s.sha256]) for s in capture.manifest.series))  # fmt: skip
    cache = CompileCache(snap)
    for family in catalogue.values():
        for v in generate(family, "perp"):
            out = compile_strategy(v.hypothesis.definition, snap, "BTC", cache=cache)
            assert out.metadata.strategy_id == v.strategy_id
            assert out.metadata.eligible_bars > 0, v.name
    spot = generate(catalogue[("ma_trend", 1)], "spot")[0].hypothesis.definition
    assert spot.exit.atr == "wilder_14" and spot.market == "spot"


# --------------------------------------------------------------------------- batch planning


@pytest.fixture
def env(store):
    _seed(store)
    ledger = Ledger(store)
    plan = _plan(statistics={"min_independent_events": 5, "random_entry_samples": 300})
    plan_id = ledger.register_plan(plan)
    dataset_id = ledger.register_dataset(capture_dataset(store, _selections(plan)))
    return ledger, plan_id, dataset_id


def _request(plan_id, dataset_id, families, **over):
    return FamilyBatchRequest.model_validate(
        {
            "name": "family_batch_v1",
            "description": "families test",
            "plan": plan_id,
            "dataset": dataset_id,
            "role": "discovery",
            "correction": {"q": 0.1},
            "survivor": {"min_pooled_excess": 0.002},
            "families": [{"family": f, "version": 1} for f in families],
            **over,
        }
    )


def _rows(ledger, table):
    return ledger.store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_dry_run_shows_everything_and_writes_nothing(env, catalogue):
    ledger, plan_id, dataset_id = env
    before = {t: _rows(ledger, t) for t in ("lab_strategies", "lab_submissions", "lab_batches")}
    report = plan_family_batch(
        ledger, _request(plan_id, dataset_id, ["donchian_breakout", "rsi_exhaustion"]), catalogue
    )
    assert {t: _rows(ledger, t) for t in before} == before
    assert report["total_variants"] == 22 and report["statistics"]["compatible"]
    assert report["statistics"]["max_supported_members"] == 30  # floor(0.1 * 301)
    assert [f["variants"] for f in report["families"]] == [10, 12]
    assert report["new_registrations"] == 22
    expected = {v.strategy_id for f in ("donchian_breakout", "rsi_exhaustion")
                for v in generate(catalogue[(f, 1)], "perp")}  # fmt: skip
    assert set(report["manifest"]["members"]) == expected
    assert report["manifest"]["primary_horizon"] == "5d"
    member = report["families"][0]["members"][0]
    assert {"name", "strategy_id", "side", "params", "complexity", "registered"} <= set(member)


def test_monte_carlo_limit_refuses_oversized_families(env, catalogue):
    ledger, plan_id, dataset_id = env
    request = _request(plan_id, dataset_id, ["volume_breakout", "trend_pullback"])  # 32 > 30
    report = plan_family_batch(ledger, request, catalogue)
    assert not report["statistics"]["compatible"] and report["manifest"] is None
    assert "exceed the 30" in report["problems"][0]
    with pytest.raises(ValueError, match="cannot generate batch"):
        register_family_batch(ledger, request, catalogue, origin="test")
    assert _rows(ledger, "lab_submissions") == 0


def test_registration_is_idempotent_and_records_lineage(env, catalogue):
    ledger, plan_id, dataset_id = env
    request = _request(plan_id, dataset_id, ["donchian_breakout", "ma_trend"])
    manifest, submitted = register_family_batch(ledger, request, catalogue, origin="test")
    assert submitted == 24 and len(manifest.members) == 24
    again, resubmitted = register_family_batch(ledger, request, catalogue, origin="test")
    assert resubmitted == 0 and again == manifest
    assert _rows(ledger, "lab_strategies") == _rows(ledger, "lab_submissions") == 24
    for v in generate(catalogue[("ma_trend", 1)], "perp"):
        family_id = ledger.store.con.execute(
            "SELECT family_id FROM lab_strategies WHERE strategy_id=?", [v.strategy_id]
        ).fetchone()[0]
        assert family_id == "ma_trend"
    # a strategy already registered under another family is reported, not silently merged
    manual = load_hypothesis(ROOT / "config/lab/examples/daily_trend.yaml")
    receipt = ledger.submit(manual.model_dump_json(), family_id="manual", origin="test")
    assert receipt.accepted
    clash = generate(catalogue[("rsi_exhaustion", 1)], "perp")[0]
    ledger.submit(clash.hypothesis.model_dump_json(), family_id="elsewhere", origin="test")
    report = plan_family_batch(
        ledger, _request(plan_id, dataset_id, ["rsi_exhaustion"], name="clash"), catalogue
    )
    assert any("already registered in family elsewhere" in p for p in report["problems"])


def test_generated_family_batch_runs_end_to_end(env, catalogue):
    ledger, plan_id, dataset_id = env
    request = _request(plan_id, dataset_id, ["donchian_breakout", "rsi_exhaustion"])
    manifest, _ = register_family_batch(ledger, request, catalogue, origin="test")
    batch_id = freeze_batch(ledger, manifest, origin="test")
    run_batch(ledger, batch_id, software=SW)
    analysis = inspect_batch(ledger, batch_id)["analyses"][0]["payload"]
    assert analysis["counts"]["preregistered"] == 22
    assert {f["family_id"] for f in analysis["families"]} == {"donchian_breakout", "rsi_exhaustion"}
    # once screened, the same members cannot be planned into a fresh batch on these inputs
    report = plan_family_batch(ledger, _request(plan_id, dataset_id, ["rsi_exhaustion"],
                                                name="again"), catalogue)  # fmt: skip
    assert any("already preregistered" in p for p in report["problems"])


def test_request_schema_is_strict(env):
    _, plan_id, dataset_id = env
    with pytest.raises(ValidationError):
        _request(plan_id, dataset_id, ["ma_trend", "ma_trend"])
    with pytest.raises(ValidationError):
        _request(plan_id, dataset_id, ["ma_trend"], role="final_holdout")
    with pytest.raises(ValidationError):
        _request(plan_id, dataset_id, ["ma_trend"], surprise=True)


def test_cli_family_and_batch_generation(env, tmp_path):
    from market_signal.cli.main import app

    ledger, plan_id, dataset_id = env
    runner = CliRunner()
    listed = runner.invoke(app, ["lab", "families"])
    assert listed.exit_code == 0 and len(json.loads(listed.output)) == 9
    shown = runner.invoke(app, ["lab", "family", "show", "ma_trend", "--market", "spot"])
    assert json.loads(shown.output)["variants"] == 7
    request = tmp_path / "request.yaml"
    request.write_text(yaml.safe_dump(json.loads(_request(
        plan_id, dataset_id, ["donchian_breakout", "rsi_exhaustion"]).model_dump_json())))  # fmt: skip
    path = ledger.store.path
    ledger.store.close()
    base = ["--db", str(path), "lab", "batch"]
    dry = runner.invoke(app, [*base, "generate", str(request)])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["total_variants"] == 22
    out = tmp_path / "manifest.yaml"
    reg = runner.invoke(app, [*base, "generate", str(request), "--register", "--out", str(out)])
    assert reg.exit_code == 0, reg.output and json.loads(reg.output)["new_submissions"] == 22
    created = runner.invoke(app, [*base, "create", str(out)])
    assert created.exit_code == 0, created.output
    again = runner.invoke(app, [*base, "generate", str(request), "--register", "--out", str(out)])
    assert again.exit_code == 1 and "never overwritten" in again.output
