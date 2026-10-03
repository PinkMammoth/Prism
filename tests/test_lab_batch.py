"""Search batches: frozen testing families, BH correction, survivor rules and governance."""

from __future__ import annotations

import json
import random
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.research.lab import screen as screen_mod
from market_signal.research.lab.batch import (
    BatchDefinition,
    BatchError,
    BatchManifest,
    benjamini_hochberg,
    classify,
    freeze_batch,
    inspect_batch,
    list_batches,
    load_manifest,
    run_batch,
)
from market_signal.research.lab.datasets import capture_dataset
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.provenance import SoftwareIdentity
from tests.test_lab_compiler import _definition
from tests.test_lab_screen import P0, P1, SW, _hypothesis, _plan, _seed, _selections

# --------------------------------------------------------------------------- BH


def test_bh_matches_hand_calculation():
    p = [0.001, 0.008, 0.039, 0.041, 0.042, 0.06, 0.074, 0.205, 0.212, 0.216]
    # p * m / rank: .01 .04 .13 .1025 .084 .1 .10571 .25625 .23556 .216, then a running
    # minimum from the largest rank down
    expected = [0.01, 0.04, 0.084, 0.084, 0.084, 0.1, 0.074 * 10 / 7, 0.216, 0.216, 0.216]
    q = benjamini_hochberg({f"s{i}": v for i, v in enumerate(p)})
    assert [q[f"s{i}"] for i in range(10)] == pytest.approx(expected, rel=1e-12)
    assert benjamini_hochberg({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.005}) == pytest.approx(
        {"a": 0.02, "b": 0.04, "c": 0.04, "d": 0.02}
    )
    assert benjamini_hochberg({"only": 0.3}) == {"only": 0.3}
    assert benjamini_hochberg({"a": 0.9, "b": 0.95}) == pytest.approx({"a": 0.95, "b": 0.95})
    assert benjamini_hochberg({}) == {}
    with pytest.raises(ValueError):
        benjamini_hochberg({"a": 1.2})


def test_bh_ties_and_order_invariance():
    q = benjamini_hochberg({"a": 0.02, "b": 0.02, "c": 0.5})
    assert q["a"] == q["b"] == pytest.approx(0.03) and q["c"] == pytest.approx(0.5)
    rng = random.Random(1)
    p = {f"s{i}": round(rng.random() ** 3, 3) for i in range(40)}  # many ties
    base = benjamini_hochberg(p)
    for _ in range(5):
        keys = list(p)
        rng.shuffle(keys)
        assert benjamini_hochberg({k: p[k] for k in keys}) == base


def test_bh_rejections_equal_the_step_up_rule():
    rng = random.Random(7)
    for _ in range(200):
        m = rng.randint(1, 30)
        p = {f"s{i}": rng.random() ** rng.choice([1, 2, 4]) for i in range(m)}
        alpha = rng.choice([0.05, 0.1, 0.2])
        ordered = sorted(p.values())
        k = max((i for i in range(1, m + 1) if ordered[i - 1] <= alpha * i / m), default=0)
        step_up = {key for key, v in p.items() if k and v <= ordered[k - 1]}
        q = benjamini_hochberg(p)
        assert {key for key, v in q.items() if v <= alpha} == step_up


# --------------------------------------------------------------------------- identity


def _manifest(default_members, /, **over):
    members = over.pop("members", default_members)
    base = {
        "name": "trend_family_v1",
        "description": "Do EMA and breakout entries beat random timing?",
        "plan": "plan_" + "a" * 64,
        "dataset": "dataset_" + "b" * 64,
        "role": "discovery",
        "primary_horizon": "5d",
        "correction": {"method": "benjamini_hochberg", "q": 0.1},
        "survivor": {"min_pooled_excess": 0.0},
        "members": list(members),
    }
    return BatchManifest.model_validate({**base, **over})


IDS = ["strategy_" + c * 64 for c in "123456"]


def test_batch_identity_is_canonical_and_complete():
    a = _manifest(IDS).definition()
    assert _manifest(list(reversed(IDS))).definition().batch_id == a.batch_id
    # names and prose are metadata, not identity
    assert _manifest(IDS, name="renamed", description="x").definition().batch_id == a.batch_id
    for change in (
        {"members": IDS[:-1]},
        {"plan": "plan_" + "c" * 64},
        {"dataset": "dataset_" + "d" * 64},
        {"role": "development"},
        {"primary_horizon": "10d"},
        {"correction": {"q": 0.05}},
        {"survivor": {"min_pooled_excess": 0.001}},
    ):
        assert _manifest(IDS, **change).definition().batch_id != a.batch_id, change
    for bad in (
        {"role": "final_holdout"},
        {"role": "validation"},
        {"members": [IDS[0], IDS[0]]},
        {"members": [IDS[0]]},
        {"correction": {"method": "bonferroni"}},
        {"survivor": {}},
        {"surprise": 1},
    ):
        with pytest.raises(ValidationError):
            _manifest(IDS, **bad)


def test_classification_needs_substance_and_corrected_evidence():
    definition = _manifest(IDS, survivor={"min_pooled_excess": 0.002}).definition()
    good = {
        "phase4_triage": "WEAK",
        "in_family": True,
        "excess_mean": 0.01,
        "gate_checks": {g: True for g in ("min_independent_events", "min_assets_with_events",
                                          "min_pooled_excess", "min_positive_asset_share")},
    }  # fmt: skip
    assert classify(good, 0.05, definition) == "FDR_SURVIVOR"
    # low q alone cannot pass a negative/inconsistent or tiny effect
    tiny = {**good, "excess_mean": 0.001}
    assert classify(tiny, 0.001, definition) == "FDR_SIGNIFICANT_FAILS_SCREEN"
    split = {**good, "gate_checks": {**good["gate_checks"], "min_positive_asset_share": False}}
    assert classify(split, 0.001, definition) == "FDR_SIGNIFICANT_FAILS_SCREEN"
    # a large but noisy effect cannot bypass the correction threshold
    assert classify({**good, "excess_mean": 0.2}, 0.3, definition) == "NOT_FDR_SIGNIFICANT"
    assert classify({**good, "in_family": False, "phase4_triage": "NO_EVENTS"}, None,
                    definition) == "NOT_TESTABLE"  # fmt: skip
    assert classify({**good, "phase4_triage": "ERROR"}, None, definition) == "ERROR"


# --------------------------------------------------------------------------- governed fixture

RULES = {
    # family_id: rules
    "ema_cross": [
        [("ema_10", "crosses_above", "ema_30")],
        [("ema_20", "crosses_above", "ema_50")],
        [("ema_21", "crosses_above", "ema_50")],
    ],
    "breakout": [
        [("close", "crosses_above", "donchian_high_10")],
        [("close", "crosses_above", "donchian_high_20")],
    ],
    "never": [[("close", "gt", 1e12)]],  # registered, never fires: NO_EVENTS
}


@pytest.fixture
def env(store):
    _seed(store)
    ledger = Ledger(store)
    plan = _plan(
        statistics={"min_independent_events": 5, "random_entry_samples": 300},
        periods=[
            {"role": "discovery", "start": P0, "end": P1},
            {"role": "final_holdout", "start": P1, "end": P1 + timedelta(days=200)},
        ],
    )
    plan_id = ledger.register_plan(plan)
    dataset_id = ledger.register_dataset(capture_dataset(store, _selections(plan)))
    members = {}
    for family, rules in RULES.items():
        for i, rule in enumerate(rules):
            d = _definition(rule)
            h = _hypothesis(d, name=f"{family}_{i}")
            receipt = ledger.submit(h.model_dump_json(), family_id=family, origin="test")
            assert receipt.accepted, receipt.error
            members[d.strategy_id] = family
    return SimpleNamespace(store=store, ledger=ledger, plan=plan, plan_id=plan_id,
                           dataset_id=dataset_id, members=members)  # fmt: skip


def _batch(env, members=None, **over):
    return _manifest(
        members if members is not None else list(env.members),
        **{"plan": env.plan_id, "dataset": env.dataset_id, **over},
    )


def _analysis(env, batch_id, run=0):
    return inspect_batch(env.ledger, batch_id)["analyses"][run]["payload"]


def test_full_batch_run_keeps_every_member_visible(env):
    batch_id = freeze_batch(env.ledger, _batch(env), origin="test")
    assert list_batches(env.ledger)[0]["status"] == "FROZEN"
    out = run_batch(env.ledger, batch_id, software=SW)
    assert out["status"] == "completed"
    a = _analysis(env, batch_id)
    c = a["counts"]
    assert c["preregistered"] == 6 and c["no_events"] == 1 and c["errored"] == 0
    assert c["correction_family"] == c["testable"] == 6 - 1 - c["insufficient_events"]
    by_id = {m["strategy_id"]: m for m in a["members"]}
    never = next(s for s, f in env.members.items() if f == "never")
    assert by_id[never]["batch_status"] == "NOT_TESTABLE" and by_id[never]["q"] is None
    assert by_id[never]["excluded_reason"] == "Phase 4 NO_EVENTS"
    # q-values are BH over exactly the in-family raw p-values
    family = {s: m["raw_p"] for s, m in by_id.items() if m["in_family"]}
    assert {s: by_id[s]["q"] for s in family} == pytest.approx(benjamini_hochberg(family))
    # each raw p is the stored Phase 4 primary-horizon endpoint, nothing else
    for m in by_id.values():
        record = env.ledger.inspect_experiment(m["experiment_id"])
        (result,) = record["results"]
        assert record["experiment"]["batch_id"] == batch_id
        ps = {p["endpoint"]: p["value"] for p in result["p_values"]}
        assert m["raw_p"] == ps.get("pooled:long:5d")
        assert set(ps) <= {"pooled:long:5d"}
    families = {f["family_id"]: f for f in a["families"]}
    assert families["ema_cross"]["variants"] == 3 and families["breakout"]["variants"] == 2
    assert families["never"]["testable"] == 0 and families["never"]["best_q"] is None
    assert list_batches(env.ledger)[0]["status"] == "COMPLETED"


def test_family_is_frozen_and_cannot_be_cherry_picked(env):
    members = list(env.members)
    batch_id = freeze_batch(env.ledger, _batch(env), origin="test")
    with pytest.raises(BatchError, match="identical testing family"):
        freeze_batch(env.ledger, _batch(env, name="again"), origin="test")
    with pytest.raises(BatchError, match="name"):
        freeze_batch(env.ledger, _batch(env, members=members[:3]), origin="test")
    # adding/removing members on the same inputs is a different family that overlaps
    with pytest.raises(BatchError, match="overlap"):
        freeze_batch(env.ledger, _batch(env, members=members[:-1], name="smaller"), origin="t")
    run_batch(env.ledger, batch_id, software=SW)
    a = _analysis(env, batch_id)
    weak = [m["strategy_id"] for m in a["members"] if m["batch_status"] != "FDR_SURVIVOR"]
    keep = [s for s in members if s not in weak[:1]]
    # Excluding a completed member and re-correcting as if preregistered is refused.
    with pytest.raises(BatchError, match="already preregistered"):
        freeze_batch(env.ledger, _batch(env, members=keep, name="cherry_picked"), origin="t")
    with pytest.raises(BatchError, match="already has a recorded analysis"):
        run_batch(env.ledger, batch_id, software=SW)


def test_primary_horizon_and_holdout_rules(env):
    with pytest.raises(BatchError, match="primary horizon"):
        freeze_batch(env.ledger, _batch(env, primary_horizon="10d"), origin="test")
    # A plan whose final holdout overlaps the warmup region cannot host a search batch.
    overlapping = _plan(
        name="overlap",
        statistics={"min_independent_events": 5, "random_entry_samples": 300},
        periods=[
            {"role": "final_holdout", "start": P0 - timedelta(days=60), "end": P0},
            {"role": "discovery", "start": P0, "end": P1},
        ],
    )
    plan_id = env.ledger.register_plan(overlapping)
    dataset_id = env.ledger.register_dataset(capture_dataset(env.store, _selections(overlapping)))
    with pytest.raises(BatchError, match="final_holdout"):
        freeze_batch(
            env.ledger, _batch(env, plan=plan_id, dataset=dataset_id, name="h"), origin="t"
        )
    with pytest.raises(ValidationError):
        _batch(env, role="final_holdout")


def test_monte_carlo_resolution_must_allow_a_survivor(env):
    many = ["strategy_" + f"{i:064x}" for i in range(40)]  # q/m = 0.0025 < 1/301
    with pytest.raises(BatchError, match="smallest attainable p"):
        freeze_batch(env.ledger, _batch(env, members=many, name="too_big"), origin="test")


def test_errored_members_stay_visible_and_out_of_the_family(env, monkeypatch):
    target = next(s for s, f in env.members.items() if f == "breakout")
    original = screen_mod.compile_strategy

    def flaky(definition, *args, **kwargs):
        if definition.strategy_id == target:
            raise RuntimeError("simulated compile failure")
        return original(definition, *args, **kwargs)

    monkeypatch.setattr(screen_mod, "compile_strategy", flaky)
    batch_id = freeze_batch(env.ledger, _batch(env), origin="test")
    run_batch(env.ledger, batch_id, software=SW)
    a = _analysis(env, batch_id)
    m = next(m for m in a["members"] if m["strategy_id"] == target)
    assert m["batch_status"] == "ERROR" and not m["in_family"] and m["q"] is None
    assert a["counts"]["errored"] == 1 and a["counts"]["preregistered"] == 6


def test_interrupted_run_resumes_without_losing_members(env, monkeypatch):
    target = sorted(env.members)[2]
    original = screen_mod.compile_strategy

    def crash(definition, *args, **kwargs):
        if definition.strategy_id == target:
            raise KeyboardInterrupt  # not caught by run_screen: simulates a killed process
        return original(definition, *args, **kwargs)

    monkeypatch.setattr(screen_mod, "compile_strategy", crash)
    batch_id = freeze_batch(env.ledger, _batch(env), origin="test")
    with pytest.raises(KeyboardInterrupt):
        run_batch(env.ledger, batch_id, software=SW)
    assert inspect_batch(env.ledger, batch_id)["status"] == "RUNNING"
    monkeypatch.setattr(screen_mod, "compile_strategy", original)
    other = SoftwareIdentity(label="x", python_version="3.12", source_sha256="d" * 64)
    with pytest.raises(BatchError, match="different software"):
        run_batch(env.ledger, batch_id, software=other)
    run_batch(env.ledger, batch_id, software=SW)
    a = _analysis(env, batch_id)
    m = next(m for m in a["members"] if m["strategy_id"] == target)
    assert m["batch_status"] == "ERROR" and a["counts"]["preregistered"] == 6
    assert len(inspect_batch(env.ledger, batch_id)["runs"]) == 1


def test_reruns_reproduce_exactly_and_never_rewrite_history(env):
    batch_id = freeze_batch(env.ledger, _batch(env), origin="test")
    first = run_batch(env.ledger, batch_id, software=SW)
    before = inspect_batch(env.ledger, batch_id)
    screens_before = {
        m["experiment_id"]: env.ledger.inspect_experiment(m["experiment_id"])["results"]
        for m in first["members"]
    }
    with pytest.raises(BatchError, match="rerun_of"):
        run_batch(env.ledger, batch_id, software=SW, rerun_reason="no link")
    new_sw = SoftwareIdentity(label="new", python_version="3.12", source_sha256="e" * 64)
    second = run_batch(env.ledger, batch_id, software=new_sw, rerun_of=first["run_id"],
                       rerun_reason="software upgrade check")  # fmt: skip
    assert second["attempt"] == 2 and second["rerun_of"] == first["run_id"]
    after = inspect_batch(env.ledger, batch_id)
    assert after["analyses"][0] == before["analyses"][0]
    for experiment_id, results in screens_before.items():
        assert env.ledger.inspect_experiment(experiment_id)["results"] == results

    def essence(a):
        return [(m["strategy_id"], m["raw_p"], m["q"], m["batch_status"], m["excess_mean"])
                for m in a["members"]]  # fmt: skip

    assert essence(second) == essence(first)
    assert second["counts"] == first["counts"]
    for m in second["members"]:
        record = env.ledger.get_experiment(m["experiment_id"])
        assert record.attempt == 2 and record.rerun_of is not None


def test_cli_batch_lifecycle(env, tmp_path):
    from market_signal.cli.main import app

    manifest = tmp_path / "batch.yaml"
    manifest.write_text(
        "name: cli_family\n"
        "description: smoke test family\n"
        f"plan: {env.plan_id}\n"
        f"dataset: {env.dataset_id}\n"
        "role: discovery\n"
        "primary_horizon: 5d\n"
        "correction:\n  method: benjamini_hochberg\n  q: 0.1\n"
        "survivor:\n  min_pooled_excess: 0.0\n"
        "members:\n" + "".join(f"  - {s}\n" for s in env.members)
    )
    assert load_manifest(manifest).definition().members
    dup = tmp_path / "dup.yaml"
    dup.write_text(manifest.read_text() + "name: other\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_manifest(dup)
    path = env.store.path
    env.store.close()
    runner = CliRunner()
    base = ["--db", str(path), "lab"]
    created = runner.invoke(app, [*base, "batch", "create", str(manifest)])
    assert created.exit_code == 0, created.output
    batch_id = json.loads(created.output)["batch_id"]
    listed = runner.invoke(app, [*base, "batches"])
    assert json.loads(listed.output)[0]["status"] == "FROZEN"
    ran = runner.invoke(app, [*base, "batch", "run", batch_id])
    assert ran.exit_code == 0, ran.output
    assert json.loads(ran.output)["counts"]["preregistered"] == 6
    shown = runner.invoke(app, [*base, "batch", "show", batch_id])
    assert json.loads(shown.output)["status"] == "COMPLETED"
    again = runner.invoke(app, [*base, "batch", "run", batch_id])
    assert again.exit_code == 1 and "recorded analysis" in again.output


def test_definition_round_trip():
    d = _manifest(IDS).definition()
    assert BatchDefinition.model_validate_json(d.canonical_json()).batch_id == d.batch_id
