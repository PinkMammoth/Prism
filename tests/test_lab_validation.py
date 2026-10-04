"""Phase 9: full research and independent validation of frozen Lab strategies."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.backtest.events import run_event_study
from market_signal.backtest.robustness import make_folds, walk_forward
from market_signal.perps.backtest import PerpCosts, perp_asset_events
from market_signal.research.lab import validation as val
from market_signal.research.lab.adapter import FullResearchPolicy, frozen_walk_forward
from market_signal.research.lab.batch import freeze_batch, run_batch
from market_signal.research.lab.compiler import Snapshot, compile_strategy
from market_signal.research.lab.datasets import SeriesSelection, capture_dataset
from market_signal.research.lab.evidence import POLICIES as EV_POLICIES
from market_signal.research.lab.evidence import (
    EvidenceProfile,
    build_profiles,
    gather,
    load_profiles,
    record_profiles,
)
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.policy import ScreenPlan
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.screen import screen
from market_signal.research.lab.spec import Hypothesis
from market_signal.research.lab.validation import ResearchError
from tests.test_lab_batch import _manifest
from tests.test_lab_compiler import _definition, _funding, _insert_perp
from tests.test_lab_evidence import FORBIDDEN, _keys

START = datetime(2023, 1, 1, tzinfo=UTC)
WARM = 60
P0, P1 = START + timedelta(days=60), START + timedelta(days=620)  # discovery
V0, V1 = P1, START + timedelta(days=860)  # reserved validation
H1 = START + timedelta(days=920)  # final holdout [V1, H1)
N = 920
COINS = ("BTC", "ETH", "SOL")
SW = SoftwareIdentity(label="test", python_version="3.12", source_sha256="a" * 64)
SW2 = SoftwareIdentity(label="test2", python_version="3.12", source_sha256="c" * 64)
THRESHOLDS = (0.03, 0.05, 0.07)  # one-parameter toy family: every variant fires on jumps


def _toy_bars(seed: int, validation_drift: float, holdout_drift: float = 0.015) -> pd.DataFrame:
    """Calm random walk; every 12 days a +8% jump followed by a 5-day drift, and six days
    later a -8% jump with no drift (so random entry does not inherit the jump effect).

    The drift after an up-jump is +1.5%/day (momentum) before V0, ``validation_drift``
    inside the validation period and ``holdout_drift`` in the final holdout, so the toy's
    expected outcome is known by construction.
    """
    rng = np.random.default_rng(seed)
    ts = pd.date_range(START, periods=N, freq="1D", tz="UTC")
    r = rng.normal(0, 0.004, N)
    for j in range(5 + seed, N - 6, 12):
        r[j] = 0.08
        when = ts[j] + timedelta(days=1)  # the signal bar's close
        drift = 0.015 if when < V0 else validation_drift if when < V1 else holdout_drift
        r[j + 1 : j + 6] += drift
        r[j + 6] = -0.08
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.r_[100.0, close[:-1]]
    spread = np.abs(rng.normal(0, 0.002, N)) * close
    df = pd.DataFrame({"ts": ts, "open": open_, "high": np.maximum(open_, close) + spread,
                       "low": np.minimum(open_, close) - spread, "close": close,
                       "volume": rng.uniform(1e3, 2e3, N)})  # fmt: skip
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    return df


def _plan(**over) -> ScreenPlan:
    base = {
        "name": "toy_discovery",
        "version": 1,
        "market": "perp",
        "source": "hyperliquid",
        "warmup_days": WARM,
        "periods": [{"role": "discovery", "start": P0, "end": P1}],
        "costs": [{"symbol": s, "fee_bps": 4.5, "slippage_bps": 2.0} for s in COINS],
        "horizons": [{"label": "1d", "bars": 1}, {"label": "5d", "bars": 5}],
        "primary_horizon": "5d",
        "return_model": "perp_notional_v1",
        "funding": {},
        "statistics": {"min_independent_events": 30, "random_entry_samples": 300},
        "gates": {"min_assets_with_events": 2},
    }
    return ScreenPlan.model_validate({**base, **over})


def _validation_plan(source: ScreenPlan, *, start=V0, end=V1, holdout=True, **over):
    periods = [*source.model_dump(mode="python")["periods"],
               {"role": "validation", "start": start, "end": end}]  # fmt: skip
    if holdout:
        periods.append({"role": "final_holdout", "start": V1, "end": H1})
    data = {**source.model_dump(mode="python"), "name": "toy_validation", "version": 1,
            "periods": periods, **over}  # fmt: skip
    return ScreenPlan.model_validate(data)


def _hypothesis(t: float, name: str) -> Hypothesis:
    lineage = {"generator": "lab_family_grid_v1", "family": "toy_jump", "version": 1,
               "family_id": "toyfam", "market": "perp", "side": "long", "params": {"t": t}}  # fmt: skip
    return Hypothesis.model_validate({
        "name": name, "hypothesis": "jumps continue", "source": json.dumps(lineage),
        "created_at": START, "definition": _definition([("ret_1", "gt", t)]).model_dump(mode="json"),
    })  # fmt: skip


def _selections(plan: ScreenPlan, role="discovery"):
    p = plan.period(role)
    return tuple(
        SeriesSelection(kind=k, symbol=c, source="hyperliquid",
                        timeframe="1d" if k == "perp_bars" else None,
                        start=plan.data_start(role), end=p.end)
        for c in COINS for k in ("perp_bars", "perp_funding")
    )  # fmt: skip


def _build(store, validation_drift: float):
    for i, c in enumerate(COINS):
        bars = _toy_bars(i, validation_drift)
        _insert_perp(store, bars, _funding(bars, seed=10 + i, every_hours=8), coin=c)
    ledger = Ledger(store)
    plan = _plan()
    plan_id = ledger.register_plan(plan)
    vplan = _validation_plan(plan)
    vplan_id = ledger.register_plan(vplan)
    dataset_id = ledger.register_dataset(capture_dataset(store, _selections(plan)))
    ids = {}
    for t in THRESHOLDS:
        h = _hypothesis(t, f"toy_jump_t{int(t * 100)}_long")
        receipt = ledger.submit(h.model_dump_json(), family_id="toy_jump", origin="test")
        assert receipt.accepted, receipt.error
        ids[t] = h.strategy_id
    # an economic floor no member reaches: no FDR survivors, so profiles stay EXPLORATORY
    manifest = _manifest(list(ids.values()), plan=plan_id, dataset=dataset_id,
                         survivor={"min_pooled_excess": 5.0})  # fmt: skip
    batch_id = freeze_batch(ledger, manifest, origin="test")
    run_batch(ledger, batch_id, software=SW)
    records, analysis = gather(ledger, batch_id)
    policy = EV_POLICIES[2]
    record_profiles(ledger, build_profiles(records, analysis, policy, builder_software_id="sw"),
                    policy)  # fmt: skip
    profiles = {p["subject"]["strategy_id"]: p for p in load_profiles(ledger)}
    return SimpleNamespace(store=store, ledger=ledger, plan=plan, plan_id=plan_id, vplan=vplan,
                           vplan_id=vplan_id, dataset_id=dataset_id, ids=ids, batch_id=batch_id,
                           profiles=profiles, target=ids[0.05],
                           profile_id=profiles[ids[0.05]]["profile_id"])  # fmt: skip


@pytest.fixture
def toy(store):
    return _build(store, validation_drift=0.015)  # the effect persists


@pytest.fixture
def toy_adverse(store):
    return _build(store, validation_drift=-0.015)  # the effect reverses on unseen data


def _register(env, **kw):
    kw.setdefault("now", P1 + timedelta(days=1))
    return val.register(env.ledger, kw.pop("profile_id", env.profile_id),
                        kw.pop("plan_id", env.vplan_id), reason="phase 9 test", origin="test",
                        software=SW, **kw)  # fmt: skip


def _rid(env, **kw):
    return _register(env, **kw)["registration_id"]


# --------------------------------------------------------------------------- registration


def test_registration_freezes_strategy_profile_period_and_versions(toy):
    p = toy.profiles[toy.target]
    assert p["tier"] == "EXPLORATORY" and p["statistics"]["fdr_survivor"] is False
    dry = _register(toy, dry_run=True)
    assert not val.list_registrations(toy.ledger)
    out = _register(toy)
    assert out["registration_id"] == dry["registration_id"]
    d = out["definition"]
    assert d["strategy_id"] == toy.target and d["source_profile_id"] == toy.profile_id
    assert d["historical_tier"] == "EXPLORATORY" and d["batch_id"] == toy.batch_id
    assert d["validation_period"]["role"] == "validation"
    assert d["source_plan_id"] == toy.plan_id and d["validation_plan_id"] == toy.vplan_id
    assert d["semantics"] == val.semantics()
    assert set(d["neighbour_strategy_ids"]) == {toy.ids[0.03], toy.ids[0.07]}
    assert out["independence"]["label"] == "untouched_on_record"
    with pytest.raises(ResearchError, match="already registered"):
        _register(toy)
    assert _register(toy, label="second")["registration_id"] != out["registration_id"]
    with pytest.raises(ResearchError, match="reason"):
        val.register(toy.ledger, toy.profile_id, toy.vplan_id, reason=" ", origin="t", software=SW)


def test_only_explicit_exploratory_or_research_supported_profiles(toy):
    from market_signal.research.lab.evidence import EvidencePolicy, ExploratoryRules

    strict = EvidencePolicy(version=97, exploratory=ExploratoryRules(min_independent_events=9999))
    records, analysis = gather(toy.ledger, toy.batch_id)
    record_profiles(toy.ledger, build_profiles(records, analysis, strict, builder_software_id="x"),
                    strict)  # fmt: skip
    insufficient = next(p for p in load_profiles(toy.ledger, strategy_id=toy.target)
                        if p["tier"] == "INSUFFICIENT")  # fmt: skip
    with pytest.raises(ResearchError, match="not eligible"):
        _register(toy, profile_id=insufficient["profile_id"])


def test_validation_needs_a_reserved_untouched_period(toy):
    # the source plan reserves no validation period: validation never borrows other data
    with pytest.raises(ResearchError, match="reserves no `validation` period"):
        _register(toy, plan_id=toy.plan_id)
    # a "validation" period inside the discovery data region (warmup included) is refused
    early = _validation_plan(toy.plan, start=START, end=P0 - timedelta(days=1), holdout=False,
                             name="toy_early")  # fmt: skip
    with pytest.raises(ResearchError, match="overlaps the source data region"):
        _register(toy, plan_id=toy.ledger.register_plan(early))
    # changing methodology through the "validation" plan (e.g. horizons, costs) is refused
    tweaked = _validation_plan(toy.plan, name="toy_tweak", primary_horizon="1d")
    with pytest.raises(ResearchError, match="differs from the source plan"):
        _register(toy, plan_id=toy.ledger.register_plan(tweaked))
    # the validation data region may not reach into a final holdout
    clash = _validation_plan(toy.plan, holdout=False, name="toy_clash")
    clash = ScreenPlan.model_validate({**clash.model_dump(mode="python"), "periods": [
        *clash.model_dump(mode="python")["periods"][:1],
        {"role": "validation", "start": V0 + timedelta(days=100), "end": V1},
        {"role": "final_holdout", "start": V0, "end": V0 + timedelta(days=90)}]})  # fmt: skip
    with pytest.raises(ResearchError, match="final holdout"):
        _register(toy, plan_id=toy.ledger.register_plan(clash))


# --------------------------------------------------------------------------- full research


def test_full_research_reuses_prism_engine_and_reproduces_discovery(toy):
    rid = _rid(toy)
    out = val.run_full_research(toy.ledger, rid, software=SW)
    assert out["status"] == "FULL_RESEARCH_CONSISTENT", out
    fr = val.show(toy.ledger, rid)["full_research"]
    payload = val._latest_result(toy.ledger, rid, "full_research")["payload"]
    assert payload["parity"]["target"]["exact"] is True
    assert all(p["exact"] for p in payload["parity"]["neighbours"].values())
    assert payload["steps"]["portfolio_simulation"]["status"] == "unsupported"
    assert payload["steps"]["walk_forward"]["status"] == "adapted"
    assert payload["independence"]["label"] == "not_independent"
    # independent recomputation with Prism's own perp event-study entry point
    snap = Snapshot.from_ledger(toy.ledger, toy.dataset_id)
    definition = toy.ledger.get_strategy(toy.target)
    aevs = []
    for c in COINS:
        comp = compile_strategy(definition, snap, c)
        frame = comp.inputs.reset_index(drop=True)
        ct = frame["close_time"]
        window = (ct >= P0) & (ct < P1)
        aevs += perp_asset_events(
            c, frame, {"1d": 1, "5d": 5}, PerpCosts(4.5, 2.0),
            long_signal=pd.Series(comp.signal.to_numpy() & window.to_numpy()),
            eligible=pd.Series(comp.eligible.to_numpy() & window.to_numpy()),
        )  # fmt: skip
    study = run_event_study(aevs, "5d", 300, 12345, 30)
    row = study.summary.set_index("horizon").loc["5d"]
    es = fr["event_study"]
    assert es["independent_events"] == row["n_independent"]
    assert es["excess_mean"] == pytest.approx(row["excess_mean_indep"], rel=1e-12)
    assert es["raw_p_random_entry"] == pytest.approx(row["p_value_random_entry"])


def test_walk_forward_is_the_frozen_strategy_on_chronological_blocks(toy):
    snap = Snapshot.from_ledger(toy.ledger, toy.dataset_id)
    result = screen(toy.ledger.get_strategy(toy.target), toy.plan, snap, "discovery", COINS)
    policy = FullResearchPolicy()
    wf = frozen_walk_forward(result, toy.plan, "discovery", policy)
    assert wf["parameter_reselection"] is False and len(wf["folds"]) == 3
    assert [f["start"] for f in wf["folds"]] == sorted(f["start"] for f in wf["folds"])
    assert wf["label"] == "consistent"
    # identical to calling Prism's walk_forward directly with the frozen strategy only
    from market_signal.backtest.events import baseline_bars

    folds = make_folds(pd.Timestamp(P0), pd.Timestamp(P1), 0.0, 0.5)
    table, _ = walk_forward(lambda _p: result.events, baseline_bars(list(result.asset_events)),
                            [{}], {}, folds, "5d", 5, pd.Timedelta(days=10), 1)  # fmt: skip
    assert [f["independent_events"] for f in wf["folds"]] == table["test_n"].tolist()
    assert table["chosen"].str.startswith("default").all()
    # a 1-point grid means no fold can pick anything else, however the data look
    assert sum(f["independent_events"] for f in wf["folds"]) <= len(
        result.events[(result.events["horizon"] == "5d")]
    )


def test_sensitivity_describes_neighbours_and_never_selects_a_winner(toy, monkeypatch):
    rid = _rid(toy)
    val.run_full_research(toy.ledger, rid, software=SW)
    sens = val._latest_result(toy.ledger, rid, "full_research")["payload"]["sensitivity"]
    assert sens["winner_selection"] is False
    assert [r["target"] for r in sens["rows"]] == [True, False, False]
    assert sens["rows"][0]["strategy_id"] == toy.target  # always the frozen target
    assert sens["prism_plateau_verdict"]["verdict"] == "PLATEAU"
    assert sens["phase7_neighbourhood"]["label"] == "plateau" and sens["knife_edge"] is False
    keys = set(_keys(sens))
    assert not {"best", "selected", "chosen", "winner"} & keys


def test_full_research_reruns_are_explicit_and_keep_earlier_results(toy):
    rid = _rid(toy)
    first = val.run_full_research(toy.ledger, rid, software=SW)
    with pytest.raises(ResearchError, match="rerun"):
        val.run_full_research(toy.ledger, rid, software=SW2)
    second = val.run_full_research(toy.ledger, rid, software=SW2, rerun_of=first["run_id"],
                                   rerun_reason="new software")  # fmt: skip
    runs = val._runs(toy.ledger, rid, "full_research")
    assert [r["result_id"] for r in runs] == [first["result_id"], second["result_id"]]
    a = val._result(toy.ledger, first["result_id"])["payload"]
    b = val._result(toy.ledger, second["result_id"])["payload"]
    assert (
        a["event_study"] == b["event_study"]
        and a["provenance"]["software_id"] != b["provenance"]["software_id"]
    )


# --------------------------------------------------------------------------- validation


def test_incomplete_period_is_insufficient_without_exposure(toy):
    rid = _rid(toy)
    pre = val.preview_validation(toy.ledger, rid, now=V0 + timedelta(days=30))
    assert pre["would_consume_validation_data"] is False
    assert pre["readiness"]["complete"] is False
    assert pre["projection"]["projected_independent_events_at_completion"] > 0
    out = val.run_validation(toy.ledger, rid, software=SW, now=V0 + timedelta(days=30))
    assert out["status"] == "VALIDATION_INSUFFICIENT" and out["exposure"] is False
    assert "incomplete" in out["reasons"][0]
    assert not toy.ledger.exposures(start=V0, end=V1, strategy_id=toy.target)
    assert not toy.ledger.store.con.execute(
        "SELECT 1 FROM lab_experiments WHERE role='validation'"
    ).fetchone()
    # a later complete run is a plain new attempt: nothing was looked at before
    done = val.run_validation(toy.ledger, rid, software=SW)
    assert done["attempt"] == 2 and done["status"] == "VALIDATION_SUPPORTIVE"


def test_capacity_gate_reports_insufficient_before_any_outcome(toy):
    short = _validation_plan(toy.plan, end=V0 + timedelta(days=20), holdout=False,
                             name="toy_short")  # fmt: skip
    rid = _rid(toy, plan_id=toy.ledger.register_plan(short))
    out = val.run_validation(toy.ledger, rid, software=SW)
    assert out["status"] == "VALIDATION_INSUFFICIENT" and out["exposure"] is False
    assert "capacity" in " ".join(out["reasons"])


def test_supportive_validation_records_exposure_and_comparison(toy):
    rid = _rid(toy)
    pre = val.preview_validation(toy.ledger, rid)
    assert pre["would_consume_validation_data"] is True
    assert pre["independence_now"]["label"] == "untouched_on_record"
    out = val.run_validation(toy.ledger, rid, software=SW)
    assert out["status"] == "VALIDATION_SUPPORTIVE" and out["exposure"] is True
    payload = val._latest_result(toy.ledger, rid, "validation")["payload"]
    cmp = payload["comparison"]
    assert cmp["same_direction"] is True and cmp["validation"]["independent_events"] >= 30
    assert cmp["assets_same_direction"] == 3 and payload["first_look"] is True
    exp = toy.ledger.get_experiment(out["experiment_id"])
    assert exp.role == "validation" and exp.strategy_id == toy.target
    assert (exp.period_start, exp.period_end) == (V0, V1)
    # exposure is permanent and visible for the strategy and its neighbours
    seen = toy.ledger.exposures(start=V0, end=V1, family_id="toy_jump")
    assert {r["strategy_id"] for r in seen} == set(toy.ids.values())
    assert {r["kind"] for r in seen} == {"evaluation_started"}
    nb = payload["neighbours"]
    assert nb["neighbourhood"]["label"] == "plateau"
    assert nb["depends_on_single_parameterisation"] is False


def test_validation_reads_only_its_window_and_never_the_final_holdout(toy):
    rid = _rid(toy)
    out = val.run_validation(toy.ledger, rid, software=SW)
    exp = toy.ledger.get_experiment(out["experiment_id"])
    manifest = toy.ledger.get_dataset(exp.dataset_id)
    for s in manifest.series:
        assert s.selection.start == V0 - timedelta(days=WARM) and s.selection.end == V1
        assert s.last_observed is None or s.last_observed < V1
    result = toy.ledger.inspect_experiment(exp.experiment_id)["results"][0]["metrics"]
    assert result["provenance"]["window_start"] == V0.isoformat(timespec="microseconds")
    # every scored event lies inside the validation window
    snap = Snapshot.from_ledger(toy.ledger, exp.dataset_id)
    events = screen(toy.ledger.get_strategy(toy.target), toy.vplan, snap, "validation",
                    COINS).events  # fmt: skip
    assert (events["signal_time"] >= V0).all() and (events["signal_time"] < V1).all()


def test_future_and_holdout_rows_cannot_change_validation(store):
    env = _build(store, validation_drift=0.015)
    rid = _rid(env)
    first = val.run_validation(env.ledger, rid, software=SW)
    m1 = env.ledger.inspect_experiment(first["experiment_id"])["results"][0]["metrics"]
    # wreck the final holdout and everything after it, plus discovery rows before warmup
    env.store.con.execute("UPDATE perp_bars SET close = close * 10, open = open * 0.1 "
                          "WHERE close_time >= ? OR close_time < ?",
                          [V1, V0 - timedelta(days=WARM)])  # fmt: skip
    rid2 = _rid(env, label="after_edit")
    second = val.run_validation(env.ledger, rid2, software=SW)
    m2 = env.ledger.inspect_experiment(second["experiment_id"])["results"][0]["metrics"]
    assert m1["aggregate"] == m2["aggregate"] and m1["per_asset"] == m2["per_asset"]
    # the same retained dataset was captured: identical content, so the look was reused
    assert first["experiment_id"] == second["experiment_id"]


def test_adverse_validation_is_recorded_and_caps_the_tier(toy_adverse):
    env = toy_adverse
    rid = _rid(env)
    assert val.run_full_research(env.ledger, rid, software=SW)["status"] == (
        "FULL_RESEARCH_CONSISTENT"
    )  # discovery looked fine
    out = val.run_validation(env.ledger, rid, software=SW)
    assert out["status"] == "VALIDATION_ADVERSE"
    cmp = val._latest_result(env.ledger, rid, "validation")["payload"]["comparison"]
    assert cmp["same_direction"] is False and cmp["validation"]["excess_mean"] < 0
    ext = val.extend_profile(env.ledger, rid, software=SW)
    assert ext["historical_tier"] == "EXPLORATORY" and ext["tier"] == "INCONCLUSIVE"


def test_too_few_validation_events_is_insufficient_not_a_rejection(store):
    env = _build(store, validation_drift=0.015)
    # a real validation window with enough bars but too few events for 30 (3 coins x ~8)
    short = _validation_plan(env.plan, end=V0 + timedelta(days=100), holdout=False,
                             name="toy_hundred")  # fmt: skip
    rid = _rid(env, plan_id=env.ledger.register_plan(short))
    out = val.run_validation(env.ledger, rid, software=SW)
    assert out["exposure"] is True  # the look happened and is recorded...
    payload = val._latest_result(env.ledger, rid, "validation")["payload"]
    assert out["status"] == "VALIDATION_INSUFFICIENT"  # ...but too few events to judge
    assert payload["sample"]["checks"]["min_independent_events"] is False
    assert payload["comparison"]["validation"]["excess_mean"] > 0  # still reported


def test_validation_reruns_never_restore_independence(toy):
    rid = _rid(toy)
    first = val.run_validation(toy.ledger, rid, software=SW)
    with pytest.raises(ResearchError, match="already evaluated"):
        val.run_validation(toy.ledger, rid, software=SW)
    again = val.run_validation(toy.ledger, rid, software=SW, rerun_of=first["run_id"],
                               rerun_reason="reproduce")  # fmt: skip
    p1 = val._result(toy.ledger, first["result_id"])["payload"]
    p2 = val._result(toy.ledger, again["result_id"])["payload"]
    assert p1["first_look"] is True and p2["first_look"] is False
    assert p2["prior_own_runs"] == [first["run_id"]]
    assert p1["comparison"] == p2["comparison"]
    assert toy.ledger.get_experiment(again["experiment_id"]).rerun_of == first["experiment_id"]
    # a fresh registration of the same strategy now sees the earlier look
    later = _register(toy, label="later", now=datetime.now(UTC))
    assert later["independence"]["label"] == "compromised"
    # and a family member registered afterwards is family/strategy exposed too
    nb_profile = toy.profiles[toy.ids[0.03]]["profile_id"]
    nb = _register(toy, profile_id=nb_profile, now=datetime.now(UTC))
    assert nb["independence"]["label"] == "compromised"  # its own validation look happened


def test_frozen_strategy_cannot_change_under_validation(toy, monkeypatch):
    rid = _rid(toy)
    reg, _ = val.get_registration(toy.ledger, rid)
    # the registration is content-addressed: any edit is a different identity
    edited = reg.model_copy(update={"params": {"t": 0.04}})
    assert edited.registration_id != rid
    # a semantic change (e.g. a new compiler/screen version) stops the run instead of mixing
    monkeypatch.setattr(val, "semantics", lambda: {**reg.semantics, "screen_version": "v999"})
    with pytest.raises(ResearchError, match="semantic versions differ"):
        val.run_validation(toy.ledger, rid, software=SW)
    monkeypatch.undo()
    out = val.run_validation(toy.ledger, rid, software=SW)
    exp = toy.ledger.get_experiment(out["experiment_id"])
    assert exp.strategy_id == reg.strategy_id
    assert (
        toy.ledger.get_strategy(exp.strategy_id).canonical_json()
        == toy.ledger.get_strategy(toy.target).canonical_json()
    )


def test_forward_records_never_enter_validation_metrics(toy):
    from market_signal.research.lab import forward as fwd

    rid = _rid(toy)
    fwd.enroll(toy.ledger, toy.profile_id, reason="t", origin="t", software=SW,
               now=V0 + timedelta(days=2))  # fmt: skip
    for k in range(3, 40):
        fwd.check(toy.ledger, software=SW, now=V0 + timedelta(days=k, hours=3))
    fwd.resolve(toy.ledger, software=SW, now=V0 + timedelta(days=60))
    n_fwd = toy.ledger.store.con.execute("SELECT count(*) FROM lab_forward_evaluations").fetchone()
    assert n_fwd[0] > 0
    out = val.run_validation(toy.ledger, rid, software=SW)
    payload = val._latest_result(toy.ledger, rid, "validation")["payload"]
    indep = payload["independence"]
    assert indep["label"] == "untouched_on_record"  # registered before any forward record
    assert indep["concurrent_prospective_tracking"] is True
    # the stored validation metrics equal a pure screen on the validation snapshot alone
    exp = toy.ledger.get_experiment(out["experiment_id"])
    snap = Snapshot.from_ledger(toy.ledger, exp.dataset_id)
    pure = screen(toy.ledger.get_strategy(toy.target), toy.vplan, snap, "validation", COINS)
    stored = toy.ledger.inspect_experiment(exp.experiment_id)["results"][0]["metrics"]
    assert json.loads(json.dumps(pure.metrics["aggregate"])) == stored["aggregate"]
    shown = val.show(toy.ledger, rid)
    assert shown["forward"]["trackings"] and "never merged" in shown["forward"]["note"]
    summary = shown["forward"]["trackings"][0]["summary"]
    assert "error" not in summary and summary["observation"]["evaluated"] == n_fwd[0]
    assert summary["primary_horizon"]["horizon"] == "5d"
    # forward outcomes recorded BEFORE a registration compromise its independence
    late = _register(toy, label="late", now=V0 + timedelta(days=70))
    assert late["independence"]["forward_observations"]["outcomes_before_registration"] > 0


# --------------------------------------------------------------------------- evidence


def test_extension_promotes_only_with_supportive_independent_validation(toy):
    rid = _rid(toy)
    base_row = toy.ledger.store.con.execute(
        "SELECT payload FROM lab_evidence_profiles WHERE profile_id=?", [toy.profile_id]
    ).fetchone()[0]
    val.run_full_research(toy.ledger, rid, software=SW)
    only_fr = val.extend_profile(toy.ledger, rid, software=SW)
    assert only_fr["tier"] == "EXPLORATORY" and only_fr["validation"] is None  # FR alone: no change
    val.run_validation(toy.ledger, rid, software=SW)
    ext = val.extend_profile(toy.ledger, rid, software=SW)
    assert ext["tier"] == "RESEARCH_SUPPORTED" and ext["tier_changed"] is True
    assert ext["extends"] == toy.profile_id and ext["profile_id"] != only_fr["profile_id"]
    stored = {p["profile_id"]: p for p in load_profiles(toy.ledger, strategy_id=toy.target)}
    p = stored[ext["profile_id"]]
    stages = [s["stage"] for s in p["sources"]]
    assert stages == ["fast_screen", "batch_fdr", "full_research", "validation"]
    assert p["profile_schema"] == "4" and p["validation"]["status"] == "VALIDATION_SUPPORTIVE"
    assert p["effect"] == stored[toy.profile_id]["effect"]  # historical fields copied, not mixed
    # the historical profile is byte-for-byte untouched
    assert (
        toy.ledger.store.con.execute(
            "SELECT payload FROM lab_evidence_profiles WHERE profile_id=?", [toy.profile_id]
        ).fetchone()[0]
        == base_row
    )
    # idempotent: the same inputs give the same profile, no new row
    assert val.extend_profile(toy.ledger, rid, software=SW2)["new_profiles"] == 0
    keys = set(_keys(json.loads(json.dumps(p, default=str))))
    assert not [k for k in keys if any(f in k.lower() for f in FORBIDDEN if f != "eligib")]


def test_validated_stays_unreachable(toy):
    rid = _rid(toy)
    val.run_full_research(toy.ledger, rid, software=SW)
    val.run_validation(toy.ledger, rid, software=SW)
    ext = val.extend_profile(toy.ledger, rid, software=SW)
    p = next(q for q in load_profiles(toy.ledger) if q["profile_id"] == ext["profile_id"])
    data = {k: v for k, v in p.items() if k != "profile_id"}
    EvidenceProfile.model_validate(data)  # the stored extension itself is valid
    with pytest.raises(ValidationError, match="VALIDATED is reserved"):
        EvidenceProfile.model_validate({**data, "tier": "VALIDATED"})
    # a RESEARCH_SUPPORTED profile with supportive validation stays RESEARCH_SUPPORTED
    tier, _, _ = val._extended_tier(
        "RESEARCH_SUPPORTED", {"status": "FULL_RESEARCH_CONSISTENT"},
        {"status": "VALIDATION_SUPPORTIVE",
         "payload": {"independence": {"label": "untouched_on_record"}, "first_look": True}},
    )  # fmt: skip
    assert tier == "RESEARCH_SUPPORTED"
    assert val.ExtensionPolicy().validated_reachable is False
    with pytest.raises(ValidationError):
        val.ExtensionPolicy(validated_reachable=True)


def test_compromised_validation_cannot_promote():
    full = {"status": "FULL_RESEARCH_CONSISTENT"}
    for indep, first in (("compromised", True), ("family_exposed", True),
                         ("untouched_on_record", False)):  # fmt: skip
        v = {"status": "VALIDATION_SUPPORTIVE",
             "payload": {"independence": {"label": indep}, "first_look": first}}  # fmt: skip
        assert val._extended_tier("EXPLORATORY", full, v)[0] == "EXPLORATORY"
    v = {"status": "VALIDATION_SUPPORTIVE",
         "payload": {"independence": {"label": "untouched_on_record"}, "first_look": True}}  # fmt: skip
    assert val._extended_tier("EXPLORATORY", {"status": "FULL_RESEARCH_MIXED"}, v)[0] == (
        "EXPLORATORY"
    )
    assert val._extended_tier("EXPLORATORY", full, v)[0] == "RESEARCH_SUPPORTED"


def test_forward_enrollment_refuses_extended_profiles(toy):
    from market_signal.research.lab import forward as fwd

    rid = _rid(toy)
    val.run_full_research(toy.ledger, rid, software=SW)
    ext = val.extend_profile(toy.ledger, rid, software=SW)
    with pytest.raises(fwd.ForwardError, match="historical profile"):
        fwd.enroll(toy.ledger, ext["profile_id"], reason="t", origin="t", software=SW)


def test_errors_are_recorded_not_hidden(toy, monkeypatch):
    rid = _rid(toy)

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(val, "full_research", boom)
    out = val.run_full_research(toy.ledger, rid, software=SW)
    assert out["status"] == "FULL_RESEARCH_ERROR" and "synthetic failure" in out["error"]["message"]
    assert val._latest_result(toy.ledger, rid, "full_research")["status"] == "FULL_RESEARCH_ERROR"


# --------------------------------------------------------------------------- CLI


def test_cli_research_and_validation_lifecycle(toy):
    from market_signal.cli.main import app

    path = toy.store.path
    toy.store.close()
    runner = CliRunner()

    def run(*args, code=0):
        res = runner.invoke(app, ["--db", str(path), "lab", *args])
        assert res.exit_code == code, res.output
        return json.loads(res.output) if code == 0 else res.output

    reserved = run("research", "reserve-plan", toy.plan_id, "--name", "toy_cli_validation",
                   "--validation-start", V0.date().isoformat(),
                   "--validation-end", V1.date().isoformat(),
                   "--final-holdout-start", V1.date().isoformat(),
                   "--final-holdout-end", H1.date().isoformat())  # fmt: skip
    plan_id = reserved["plan_id"]
    assert plan_id.startswith("plan_") and plan_id != toy.vplan_id  # new frozen name
    dry = run("research", "register", toy.profile_id, "--validation-plan", plan_id,
              "--reason", "cli", "--dry-run")  # fmt: skip
    assert run("research", "list") == []
    rid = run("research", "register", toy.profile_id, "--validation-plan", plan_id,
              "--reason", "cli")["registration_id"]  # fmt: skip
    assert rid == dry["registration_id"]
    pre = run("validation", "preview", rid)
    assert pre["would_consume_validation_data"] is True and pre["assets"] == list(COINS)
    assert run("research", "run", rid)["status"] == "FULL_RESEARCH_CONSISTENT"
    assert run("validation", "run", rid)["status"] == "VALIDATION_SUPPORTIVE"
    run("validation", "run", rid, code=1)  # a second look needs an explicit rerun
    ext = run("research", "evidence", rid)
    assert ext["tier"] == "RESEARCH_SUPPORTED"
    shown = run("research", "show", rid)
    assert shown["validation"]["status"] == "VALIDATION_SUPPORTIVE"
    assert shown["extended_profiles"][0]["profile_id"] == ext["profile_id"]
    listed = run("research", "list")
    assert listed[0]["latest"] == {"full_research": "FULL_RESEARCH_CONSISTENT",
                                   "validation": "VALIDATION_SUPPORTIVE"}  # fmt: skip


# --------------------------------------------------------------------------- ledger clock


def test_ledger_orders_through_small_backward_clock_steps(toy, monkeypatch):
    """Regression: WSL2 steps the wall clock back ~0.6 s while re-syncing (observed during
    Phase 9), which made start/record_result raise "clock precedes ..." intermittently."""
    from market_signal.research.lab import ledger as ledger_mod

    ledger = toy.ledger
    sub = ledger.store.con.execute(
        "SELECT submission_id FROM lab_submissions WHERE hypothesis_id IS NOT NULL LIMIT 1"
    ).fetchone()[0]
    real = ledger_mod.utcnow
    exp = ledger.preregister(sub, toy.vplan_id, _validation_dataset(toy), role="validation",
                             assets=COINS, software=SW, origin="clock_test")  # fmt: skip
    step = timedelta(milliseconds=600)
    monkeypatch.setattr(ledger_mod, "utcnow", lambda: exp.created_at - step)
    started = ledger.start(exp.experiment_id, software=SW)
    assert started == exp.created_at  # recorded as equal, never earlier
    monkeypatch.setattr(ledger_mod, "utcnow", lambda: started - step)
    ledger.record_result(exp.experiment_id, status="insufficient_data", metrics={})
    row = ledger.inspect_experiment(exp.experiment_id)
    assert row["results"][0]["completed_at"] >= row["start"]["started_at"]
    # a large regression is still an impossible ordering and is refused
    ds2 = _validation_dataset(toy, days=101)  # registers its own plan first
    exp2 = ledger.preregister(
        sub, toy.vplan_id, ds2, role="validation", assets=COINS, software=SW, origin="clock"
    )
    monkeypatch.setattr(ledger_mod, "utcnow", lambda: exp2.created_at - timedelta(minutes=5))
    with pytest.raises(Exception, match="clock precedes preregistration"):
        ledger.start(exp2.experiment_id, software=SW)
    monkeypatch.setattr(ledger_mod, "utcnow", real)


def _validation_dataset(env, days: int | None = None) -> str:
    plan = env.vplan
    end = V1 if days is None else V0 + timedelta(days=days)
    if days is not None:
        plan = _validation_plan(env.plan, end=end, holdout=False, name=f"clock_{days}")
        env.vplan_id = env.ledger.register_plan(plan)
    return env.ledger.register_dataset(capture_dataset(env.store, _selections(plan, "validation")))
