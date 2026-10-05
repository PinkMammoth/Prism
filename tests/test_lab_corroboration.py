"""Phase 11: cross-venue historical corroboration (historically exposed, never independent)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from market_signal.research.lab import corroboration as xv
from market_signal.research.lab import validation as val
from market_signal.research.lab.adapter import evaluate
from market_signal.research.lab.batch import freeze_batch, run_batch
from market_signal.research.lab.compiler import Snapshot, compile_strategy
from market_signal.research.lab.corroboration import CorroborationError
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
from market_signal.research.lab.screen import ScreenWorkspace
from market_signal.research.lab.spec import Hypothesis
from tests.test_lab_batch import _manifest
from tests.test_lab_compiler import _definition, _funding
from tests.test_lab_evidence import FORBIDDEN, _keys

START = datetime(2022, 1, 1, tzinfo=UTC)
N = 1100
WARM = 60
B0, B1 = START + timedelta(days=WARM), START + timedelta(days=700)  # Binance corroboration
P0, P1 = B1, START + timedelta(days=1000)  # Hyperliquid discovery (starts where B ends)
V1 = P1 + timedelta(days=60)  # reserved Hyperliquid validation
HL_COINS = ("BTC", "ETH", "SOL", "HYPE")
BN_COINS = ("BTC", "ETH", "SOL")  # no Binance HYPE history
SOL_LISTED = 150  # Binance SOL history starts later than BTC/ETH
SW = SoftwareIdentity(label="test", python_version="3.12", source_sha256="a" * 64)
SW2 = SoftwareIdentity(label="test2", python_version="3.12", source_sha256="c" * 64)
THRESHOLDS = (0.03, 0.05, 0.07)
PERPS_CFG = {
    "costs": {"taker_fee_bps": 4.5, "slippage_bps": {"BTC": 2, "ETH": 2, "SOL": 4, "default": 8}},
    "venues": {"binance": {"enabled": True, "taker_fee_bps": 5.0}},
}


def _bars(seed: int, drift: float, start_day: int = 0) -> pd.DataFrame:
    """Calm walk; every 12 days a +8% jump then 5 days of ``drift``, then a -8% jump."""
    rng = np.random.default_rng(seed)
    n = N - start_day
    ts = pd.date_range(START + timedelta(days=start_day), periods=n, freq="1D", tz="UTC")
    r = rng.normal(0, 0.004, n)
    for j in range(5 + seed % 7, n - 6, 12):
        r[j] = 0.08
        r[j + 1 : j + 6] += drift
        r[j + 6] = -0.08
    close = 100 * np.exp(np.cumsum(r))
    open_ = np.r_[100.0, close[:-1]]
    spread = np.abs(rng.normal(0, 0.002, n)) * close
    df = pd.DataFrame({"ts": ts, "open": open_, "high": np.maximum(open_, close) + spread,
                       "low": np.minimum(open_, close) - spread, "close": close,
                       "volume": rng.uniform(1e3, 2e3, n)})  # fmt: skip
    df["close_time"] = df["ts"] + pd.Timedelta(days=1)
    return df


def _insert(store, bars: pd.DataFrame, funding: pd.DataFrame, coin: str, source: str) -> None:
    b = bars.assign(coin=coin, timeframe="1d", source=source, ingest_run_id="fixture")
    b["ingested_at"] = START
    store.con.register("_b", b)
    store.con.execute(
        "INSERT INTO perp_bars SELECT coin, timeframe, source, ts, open, high, low, close, "
        "volume, close_time, ingested_at, ingest_run_id FROM _b"
    )
    store.con.unregister("_b")
    f = funding.assign(coin=coin, source=source, premium=None, ingest_run_id="fixture")
    f["pit_method"] = "market_close"
    store.con.register("_f", f)
    store.con.execute(
        "INSERT INTO perp_funding SELECT coin, source, time, funding_rate, "
        "CAST(premium AS DOUBLE), available_at, pit_method, ingest_run_id FROM _f"
    )
    store.con.unregister("_f")


def _plan() -> ScreenPlan:
    return ScreenPlan.model_validate({
        "name": "toy_hl_discovery", "version": 1, "market": "perp", "source": "hyperliquid",
        "warmup_days": WARM, "periods": [{"role": "discovery", "start": P0, "end": P1}],
        "costs": [{"symbol": s, "fee_bps": 4.5, "slippage_bps": 2.0} for s in HL_COINS],
        "horizons": [{"label": "1d", "bars": 1}, {"label": "5d", "bars": 5}],
        "primary_horizon": "5d", "return_model": "perp_notional_v1", "funding": {},
        "statistics": {"min_independent_events": 30, "random_entry_samples": 300},
        "gates": {"min_assets_with_events": 2},
    })  # fmt: skip


def _hypothesis(t: float) -> Hypothesis:
    lineage = {"generator": "lab_family_grid_v1", "family": "toy_jump", "version": 1,
               "family_id": "toyfam", "market": "perp", "side": "long", "params": {"t": t}}  # fmt: skip
    return Hypothesis.model_validate({
        "name": f"toy_jump_t{int(t * 100)}_long", "hypothesis": "jumps continue",
        "source": json.dumps(lineage), "created_at": START,
        "definition": _definition([("ret_1", "gt", t)]).model_dump(mode="json"),
    })  # fmt: skip


def _build(store, binance_drift: float):
    for i, c in enumerate(HL_COINS):
        hl = _bars(i, 0.015)
        _insert(store, hl, _funding(hl, seed=10 + i, every_hours=1), c, "hyperliquid")
    for i, c in enumerate(BN_COINS):
        bn = _bars(20 + i, binance_drift, SOL_LISTED if c == "SOL" else 0)
        _insert(store, bn, _funding(bn, seed=30 + i, every_hours=8), c, "binance")
    ledger = Ledger(store)
    plan = _plan()
    plan_id = ledger.register_plan(plan)
    vplan = ScreenPlan.model_validate({
        **plan.model_dump(mode="python"), "name": "toy_hl_validation",
        "periods": [*plan.model_dump(mode="python")["periods"],
                    {"role": "validation", "start": P1, "end": V1}],
    })  # fmt: skip
    vplan_id = ledger.register_plan(vplan)
    selections = tuple(
        SeriesSelection(kind=k, symbol=c, source="hyperliquid",
                        timeframe="1d" if k == "perp_bars" else None,
                        start=plan.data_start("discovery"), end=P1)
        for c in HL_COINS for k in ("perp_bars", "perp_funding")
    )  # fmt: skip
    dataset_id = ledger.register_dataset(capture_dataset(store, selections))
    ids = {}
    for t in THRESHOLDS:
        h = _hypothesis(t)
        receipt = ledger.submit(h.model_dump_json(), family_id="toy_jump", origin="test")
        assert receipt.accepted, receipt.error
        ids[t] = h.strategy_id
    manifest = _manifest(list(ids.values()), plan=plan_id, dataset=dataset_id,
                         survivor={"min_pooled_excess": 5.0})  # fmt: skip
    batch_id = freeze_batch(ledger, manifest, origin="test")
    run_batch(ledger, batch_id, software=SW)
    records, analysis = gather(ledger, batch_id)
    policy = EV_POLICIES[2]
    record_profiles(ledger, build_profiles(records, analysis, policy, builder_software_id="sw"),
                    policy)  # fmt: skip
    profiles = {p["subject"]["strategy_id"]: p for p in load_profiles(ledger)}
    target = ids[0.05]
    hist_id = profiles[target]["profile_id"]
    rid = val.register(ledger, hist_id, vplan_id, reason="t", origin="test", software=SW,
                       now=P1 + timedelta(days=1))["registration_id"]  # fmt: skip
    val.run_full_research(ledger, rid, software=SW)
    ext = val.extend_profile(ledger, rid, software=SW)  # schema 4 (full research only)
    return SimpleNamespace(store=store, ledger=ledger, plan=plan, plan_id=plan_id, ids=ids,
                           target=target, hist_id=hist_id, profile_id=ext["profile_id"],
                           research_id=rid)  # fmt: skip


@pytest.fixture
def toy(store):
    return _build(store, binance_drift=0.015)  # the effect also holds on the earlier venue


@pytest.fixture
def toy_adverse(store):
    return _build(store, binance_drift=-0.015)  # reversed on the earlier venue


def _register(env, **kw):
    kw.setdefault("start", B0)
    kw.setdefault("end", B1)
    return xv.register(env.ledger, kw.pop("profile_id", env.profile_id), venue="binance",
                       perps_cfg=PERPS_CFG, reason="phase 11 test", origin="test", software=SW,
                       **kw)  # fmt: skip


def _rid(env, **kw):
    return _register(env, **kw)["registration_id"]


def _payloads(store, table, key):
    return dict(store.con.execute(f"SELECT {key}, payload FROM {table}").fetchall())


# --------------------------------------------------------------------------- terminology


def test_corroboration_is_never_independent_validation(toy):
    out = _register(toy)
    assert out["independent"] is False and out["exposure"]["independent"] is False
    assert out["definition"]["independent"] is False
    assert out["definition"]["period"]["role"] == "corroboration"
    assert "not independent validation" in out["exposure"]["statement"]
    with pytest.raises(ValidationError):
        xv.CorroborationRegistration.model_validate({**out["definition"], "independent": True})
    with pytest.raises(ValidationError):
        xv.CorroborationPolicy(satisfies_independent_validation=True)
    for status in xv.STATUSES:
        assert "VALID" not in status and status.startswith("CROSS_VENUE_")
    # the DB refuses an independent registration even outside the API
    with pytest.raises(Exception, match="CHECK"):
        toy.store.con.execute("UPDATE lab_corroboration_registrations SET independent = true")
    # a corroboration plan (another venue, no validation role) is refused as a validation plan
    with pytest.raises(val.ResearchError, match="differs from the source plan"):
        val.register(toy.ledger, toy.hist_id, out["plan_id"], reason="t", origin="t",
                     software=SW)  # fmt: skip
    run = xv.run(toy.ledger, out["registration_id"], software=SW)
    res = xv._result(toy.ledger, run["result_id"])["payload"]
    assert res["independent"] is False
    assert res["independence"]["satisfies_independent_validation"] is False


# --------------------------------------------------------------------------- registration


def test_registration_freezes_strategy_period_assets_and_costs(toy):
    dry = _register(toy, dry_run=True)
    assert not xv.list_registrations(toy.ledger)  # dry run writes nothing
    out = _register(toy)
    assert out["registration_id"] == dry["registration_id"]
    d = out["definition"]
    assert d["strategy_id"] == toy.target and d["base_profile_id"] == toy.profile_id
    assert d["source_profile_id"] == toy.hist_id and d["source_venue"] == "hyperliquid"
    assert set(d["neighbour_strategy_ids"]) == {toy.ids[0.03], toy.ids[0.07]}
    assert d["semantics"] == xv.semantics()
    assert out["strategy_definition"] == json.loads(
        toy.ledger.get_strategy(toy.target).canonical_json()
    )
    plan = toy.ledger.get_plan(out["plan_id"])
    assert plan.source == "binance" and plan.periods[0].role == "corroboration"
    # methodology identical to the source plan; only venue, costs and period differ
    xv.check_plan_compatibility(toy.plan, plan)
    assert plan.horizons == toy.plan.horizons and plan.funding == toy.plan.funding
    assert {c.symbol: (c.fee_bps, c.slippage_bps) for c in plan.costs} == {
        "BTC": (5.0, 2.0),
        "ETH": (5.0, 2.0),
        "SOL": (5.0, 4.0),
    }
    with pytest.raises(CorroborationError, match="already registered"):
        _register(toy)


def test_plan_compatibility_refuses_methodology_changes(toy):
    costs = xv.venue_costs(PERPS_CFG, "binance", BN_COINS)
    plan = xv.corroboration_plan(toy.plan, "binance", B0, B1, costs)
    changed = ScreenPlan.model_validate({**plan.model_dump(mode="python"), "primary_horizon": "1d"})
    with pytest.raises(CorroborationError, match="primary_horizon"):
        xv.check_plan_compatibility(toy.plan, changed)
    same_venue = ScreenPlan.model_validate({**plan.model_dump(mode="python"),
                                            "source": "hyperliquid"})  # fmt: skip
    with pytest.raises(CorroborationError, match="different venue"):
        xv.check_plan_compatibility(toy.plan, same_venue)
    with pytest.raises(CorroborationError, match="not supported"):
        xv.venue_costs(PERPS_CFG, "okx", BN_COINS)


def test_period_must_be_earlier_than_the_source_discovery(toy):
    with pytest.raises(CorroborationError, match="must end by"):
        _register(toy, end=B1 + timedelta(days=1))
    with pytest.raises(ValidationError):
        xv.CorroborationRegistration.model_validate({
            **_register(toy, dry_run=True)["definition"],
            "period": {"role": "corroboration", "start": B0, "end": P0 + timedelta(days=5)},
        })  # fmt: skip


# --------------------------------------------------------------------------- assets


def test_missing_listing_history_is_excluded_or_reported_never_fabricated(toy):
    out = _register(toy, dry_run=True)
    assert out["assets"]["included"] == ["BTC", "ETH", "SOL"]
    assert out["assets"]["excluded"] == [
        {"symbol": "HYPE", "reason": "no stored binance perp bars and funding in the period"}
    ]
    cov = out["coverage"]
    assert cov["HYPE"]["bars"] == 0 and cov["HYPE"]["usable"] is False
    assert cov["SOL"]["first_bar_close"].startswith(
        (START + timedelta(days=SOL_LISTED + 1)).date().isoformat()
    )
    assert cov["BTC"]["first_bar_close"] < cov["SOL"]["first_bar_close"]
    assert cov["BTC"]["modal_settlements_per_day"] == 3  # Binance 8-hourly settlements
    assert all(c["bar_gaps"] == 0 for c in cov.values())
    # too few venue assets: breadth cannot be assessed, so registration is refused
    toy.store.con.execute("DELETE FROM perp_bars WHERE source='binance' AND coin IN ('ETH','SOL')")
    with pytest.raises(CorroborationError, match="breadth cannot be assessed"):
        _register(toy, dry_run=True)


# --------------------------------------------------------------------------- exposure


def test_historical_exposure_is_recorded_honestly(toy):
    toy.store.con.execute(
        "INSERT INTO research_runs (run_id, name, kind, created_at, config, config_hash, "
        "data_fingerprint, code_version, git_commit, summary, report_path) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?)",
        ["prr_legacy", "perp_trend_ls@binance", "perp_experiment", START + timedelta(days=1),
         json.dumps({"strategy": "trend_ls", "venue": "binance",
                     "period": [str(B0.date()), str((B0 + timedelta(days=300)).date())]}),
         "h", "[]", "v", "c", json.dumps({"verdict": {"verdict": "REJECT"}}), "p"],
    )  # fmt: skip
    toy.store.con.execute(
        "INSERT INTO research_runs (run_id, name, kind, created_at, config, config_hash, "
        "data_fingerprint, code_version, git_commit, summary, report_path) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?)",
        ["prr_hl", "perp_trend_ls", "perp_experiment", START, json.dumps({"strategy": "x"}),
         "h", "[]", "v", "c", "{}", "p"],
    )  # fmt: skip
    rid = _rid(toy)
    _, row = xv.get_registration(toy.ledger, rid)
    exp = row["exposure"]
    assert exp["independent"] is False
    assert [r["run_id"] for r in exp["legacy_research_runs"]] == ["prr_legacy"]  # venue only
    legacy = exp["legacy_research_runs"][0]
    assert legacy["verdict"] == "REJECT" and legacy["strategy"] == "trend_ls"
    # legacy covered [B0, B0+300d) of the [B0, B1) period
    assert exp["legacy_share_of_period"] == pytest.approx(300 / (B1 - B0).days)
    assert "not recorded" in exp["unrecorded"]
    assert exp["lab_exposures_before_registration"] == {"strategy": [], "other": []}
    # the governed look is itself recorded as exposure of the venue window
    xv.run(toy.ledger, rid, software=SW)
    looks = toy.ledger.exposures(start=B0, end=B1, strategy_id=toy.target)
    assert [x["role"] for x in looks] == ["corroboration"]
    assert looks[0]["kind"] == "evaluation_started"
    # a later registration of the same strategy sees that look in its recorded exposure
    later = _register(toy, label="second_look")["exposure"]["lab_exposures_before_registration"]
    assert [x["experiment_id"] for x in later["strategy"]] == [looks[0]["experiment_id"]]


# --------------------------------------------------------------------------- runs


def test_run_uses_only_venue_rows_and_reproduces_the_lab_screen(toy):
    rid = _rid(toy)
    out = xv.run(toy.ledger, rid, software=SW)
    assert out["status"] == "CROSS_VENUE_CORROBORATIVE", out
    payload = xv._result(toy.ledger, out["result_id"])["payload"]
    exp = toy.ledger.get_experiment(out["experiment_id"])
    assert exp.role == "corroboration" and exp.origin == f"lab_corroboration:{rid}"
    manifest = toy.ledger.get_dataset(exp.dataset_id)
    assert {s.selection.source for s in manifest.series} == {"binance"}
    assert {s.selection.symbol for s in manifest.series} == set(BN_COINS)
    assert payload["corroboration_experiment"]["parity"]["exact"] is True
    # neighbours are evaluated as their own governed experiments (descriptive only)
    nb = payload["sensitivity"]
    assert {r["strategy_id"] for r in nb["experiments"]} == {toy.ids[0.03], toy.ids[0.07]}
    assert nb["winner_selection"] is False
    assert payload["regimes"]["label"] == "broadly_persistent"
    assert [b["block"] for b in payload["regimes"]["blocks"]] == ["early", "middle", "late"]
    assert payload["walk_forward"]["parameter_reselection"] is False
    # Hyperliquid rows in the same window would change nothing: they are never read
    toy.store.con.execute(
        "UPDATE perp_bars SET close = close * 3 WHERE source='hyperliquid' AND close_time < ?",
        [B1],
    )
    snap = Snapshot.from_ledger(toy.ledger, exp.dataset_id)
    plan = toy.ledger.get_plan(exp.plan_id)
    again = evaluate(toy.ledger.get_strategy(toy.target), plan, snap, "corroboration", BN_COINS)
    assert again.metrics["aggregate"] == toy.ledger.inspect_experiment(exp.experiment_id)[
        "results"][0]["metrics"]["aggregate"]  # fmt: skip


def test_t_plus_one_entry_and_venue_costs_and_funding(toy):
    rid = _rid(toy)
    out = xv.run(toy.ledger, rid, software=SW)
    exp = toy.ledger.get_experiment(out["experiment_id"])
    snap = Snapshot.from_ledger(toy.ledger, exp.dataset_id)
    plan = toy.ledger.get_plan(exp.plan_id)
    definition = toy.ledger.get_strategy(toy.target)
    ws = ScreenWorkspace(snap)
    res = evaluate(definition, plan, snap, "corroboration", BN_COINS, ws)
    compiled = compile_strategy(definition, snap, "BTC", asset_class=None)
    f = compiled.inputs.reset_index(drop=True)
    ev = res.events[(res.events["symbol"] == "BTC:long") & (res.events["horizon"] == "5d")
                    & res.events["independent"]].iloc[0]  # fmt: skip
    t, h = int(ev["bar"]), 5
    assert f["close_time"].iloc[t] >= B0  # signal inside the corroboration period
    entry, exit_ = f["open"].iloc[t + 1], f["close"].iloc[t + h]  # T+1 open, T+h close
    assert ev["gross"] == pytest.approx(exit_ / entry - 1)
    funding = (f["funding_day"].iloc[t + 1 : t + h + 1] * f["close"].iloc[t + 1 : t + h + 1]).sum()
    cost = 2 * (5.0 + 2.0) / 1e4  # Binance fee + BTC slippage, both sides
    assert ev["ret"] == pytest.approx(exit_ / entry - 1 - cost - funding / entry)
    # Binance funding is the sum of its three 8-hourly settlements in (open, close]
    day = f.iloc[t + 1]
    rates = toy.store.con.execute(
        "SELECT sum(funding_rate), count(*) FROM perp_funding WHERE source='binance' AND "
        "coin='BTC' AND time > ? AND time <= ?",
        [day["ts"].to_pydatetime(), (day["ts"] + pd.Timedelta(days=1)).to_pydatetime()],
    ).fetchone()
    assert rates[1] == 3 and day["funding_day"] == pytest.approx(rates[0])
    # the same events under the source venue's costs differ by exactly the fee difference
    hl_costs = ScreenPlan.model_validate({**plan.model_dump(mode="python"), "costs": [
        {"symbol": s, "fee_bps": 4.5, "slippage_bps": c.slippage_bps}
        for s, c in ((c.symbol, c) for c in plan.costs)]})  # fmt: skip
    other = evaluate(definition, hl_costs, snap, "corroboration", BN_COINS)
    a = res.events.set_index(["symbol", "horizon", "bar"])["ret"]
    b = other.events.set_index(["symbol", "horizon", "bar"])["ret"]
    assert np.allclose(b - a, 2 * 0.5 / 1e4)


def test_adverse_venue_history_is_reported_not_hidden(toy_adverse):
    out = xv.run(toy_adverse.ledger, _rid(toy_adverse), software=SW)
    assert out["status"] == "CROSS_VENUE_ADVERSE"
    p = xv._result(toy_adverse.ledger, out["result_id"])["payload"]
    assert p["comparison"]["same_sign"] is False and p["regimes"]["label"] == "adverse"


def test_runs_are_looked_at_once_and_reruns_are_explicit(toy):
    rid = _rid(toy)
    first = xv.run(toy.ledger, rid, software=SW)
    with pytest.raises(CorroborationError, match="already run"):
        xv.run(toy.ledger, rid, software=SW)
    second = xv.run(toy.ledger, rid, software=SW, rerun_of=first["run_id"],
                    rerun_reason="reproduce")  # fmt: skip
    p = xv._result(toy.ledger, second["result_id"])["payload"]
    assert p["first_look"] is False and p["prior_runs"] == [first["run_id"]]
    assert xv._result(toy.ledger, first["result_id"])["status"] == first["status"]
    assert toy.ledger.get_experiment(second["experiment_id"]).rerun_of == first["experiment_id"]


def test_frozen_semantics_and_errors_are_recorded(toy, monkeypatch):
    rid = _rid(toy)
    monkeypatch.setattr(xv, "semantics", lambda: {"changed": "yes"})
    with pytest.raises(CorroborationError, match="semantic versions differ"):
        xv.run(toy.ledger, rid, software=SW)
    monkeypatch.undo()

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(xv, "regime_blocks", boom)
    out = xv.run(toy.ledger, rid, software=SW)
    assert out["status"] == "CROSS_VENUE_ERROR" and "synthetic failure" in out["error"]["message"]


def test_too_few_events_is_insufficient_not_adverse(toy):
    out = xv.run(toy.ledger, _rid(toy, start=B1 - timedelta(days=40)), software=SW)
    assert out["status"] == "CROSS_VENUE_INSUFFICIENT"


# --------------------------------------------------------------------------- no pooling


def test_venues_are_compared_side_by_side_never_pooled(toy):
    out = xv.run(toy.ledger, _rid(toy), software=SW)
    p = xv._result(toy.ledger, out["result_id"])["payload"]
    c = p["comparison"]
    hist = next(q for q in load_profiles(toy.ledger) if q["profile_id"] == toy.hist_id)
    assert c["pooled"] is False
    assert c["source_venue_discovery"]["independent_events"] == hist["sample"]["independent_events"]
    assert c["source_venue_discovery"]["excess_mean"] == hist["effect"]["excess_mean"]
    assert c["corroboration_venue"]["independent_events"] == p["event_study"]["independent_events"]
    assert c["source_venue_discovery"]["full_research_status"].startswith("FULL_RESEARCH_")
    assert c["asset_composition"]["source_only"] == ["HYPE"]
    assert c["asset_composition"]["common"] == ["BTC", "ETH", "SOL"]
    assert c["same_sign"] is True and c["both_expected_direction"] is True
    keys = set(_keys(c))
    assert not [k for k in keys if "combined" in k or "pooled_p" in k]


# --------------------------------------------------------------------------- evidence


def _all_profiles(store):
    return _payloads(store, "lab_evidence_profiles", "profile_id")


def test_evidence_extension_appends_without_touching_existing_profiles(toy):
    before = _all_profiles(toy.store)
    rid = _rid(toy)
    xv.run(toy.ledger, rid, software=SW)
    ext = xv.extend_profile(toy.ledger, rid, software=SW)
    after = _all_profiles(toy.store)
    assert {k: after[k] for k in before} == before  # byte-identical
    assert set(after) - set(before) == {ext["profile_id"]}
    p = json.loads(after[ext["profile_id"]])
    base = json.loads(before[toy.profile_id])
    assert p["profile_schema"] == "5" and p["extends"] == toy.profile_id
    assert [s["stage"] for s in p["sources"]] == [
        "fast_screen",
        "batch_fdr",
        "full_research",
        "cross_venue_corroboration",
    ]
    assert p["tier"] == base["tier"] and ext["tier_changed"] is False
    for k in ("effect", "sample", "assets", "statistics", "full_research"):
        assert p[k] == base[k]  # copied, not mixed with the venue's numbers
    block = p["corroboration"]
    assert block["independent"] is False and block["venue"] == "binance"
    assert block["status"] == "CROSS_VENUE_CORROBORATIVE"
    assert p["components"]["cross_venue_corroboration"] == "cross_venue_corroborative"
    assert xv.extend_profile(toy.ledger, rid, software=SW2)["new_profiles"] == 0  # idempotent
    keys = set(_keys(block))
    assert not [k for k in keys if any(f in k.lower() for f in FORBIDDEN)]
    for word in ("approved", "executable", "paper_ready", "live_ready"):
        assert word not in json.dumps(p)


def test_corroboration_cannot_create_validated_or_research_supported(toy):
    rid = _rid(toy)
    xv.run(toy.ledger, rid, software=SW)
    ext = xv.extend_profile(toy.ledger, rid, software=SW)
    p = next(q for q in load_profiles(toy.ledger) if q["profile_id"] == ext["profile_id"])
    data = {k: v for k, v in p.items() if k != "profile_id"}
    EvidenceProfile.model_validate(data)
    assert ext["tier"] == "EXPLORATORY"  # a corroborative result left the tier unchanged
    with pytest.raises(ValidationError, match="VALIDATED"):
        EvidenceProfile.model_validate({**data, "tier": "VALIDATED"})
    with pytest.raises(ValidationError, match="never independent"):
        EvidenceProfile.model_validate(
            {**data, "corroboration": {**data["corroboration"], "independent": True}}
        )
    with pytest.raises(ValidationError, match="schema 5"):
        EvidenceProfile.model_validate({**data, "profile_schema": "4"})
    assert xv.CorroborationPolicy().tier_effect == "none"
    with pytest.raises(ValidationError):
        xv.CorroborationPolicy(tier_effect="promote")
    # the extended profile cannot seed Phase 8 tracking or Phase 9 registration
    from market_signal.research.lab import forward as fwd

    with pytest.raises(fwd.ForwardError, match="historical profile"):
        fwd.enroll(toy.ledger, ext["profile_id"], reason="t", origin="t", software=SW)
    with pytest.raises(val.ResearchError, match="historical"):
        val.register(toy.ledger, ext["profile_id"], "plan_x", reason="t", origin="t", software=SW)


# --------------------------------------------------------------------------- co-pilot


def test_copilot_policy_and_evidence_view_are_untouched(toy):
    from market_signal.copilot import engine
    from market_signal.copilot import policy as pol

    v1 = pol.get_policy(1).policy_id
    d = SimpleNamespace(strategy_id=toy.target, baseline_profile_id=toy.hist_id)
    before = engine.latest_extension(toy.store, d)
    assert before == toy.profile_id  # the Phase 9 schema-4 profile
    tables = [r[0] for r in toy.store.con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'copilot_%'"
    ).fetchall()]  # fmt: skip

    def digest():
        h = hashlib.sha256()
        for t in sorted(tables):
            h.update(repr(toy.store.con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall())
                     .encode())  # fmt: skip
        return h.hexdigest()

    snapshot = digest()
    rid = _rid(toy)
    xv.run(toy.ledger, rid, software=SW)
    xv.extend_profile(toy.ledger, rid, software=SW)
    assert engine.latest_extension(toy.store, d) == before  # schema 5 is invisible to it
    assert pol.get_policy(1).policy_id == v1
    assert digest() == snapshot


def test_batch_report_is_unchanged_by_corroboration_profiles(toy):
    from market_signal.research.lab.evidence import batch_report

    _, analysis = gather(toy.ledger, toy.ledger.store.con.execute(
        "SELECT batch_id FROM lab_batches").fetchone()[0])  # fmt: skip

    def report():
        ps = [
            p
            for p in load_profiles(toy.ledger, analysis_id=analysis["analysis_id"])
            if p.get("profile_schema") != "5"
        ]  # the CLI's filter
        return batch_report(ps)

    before = report()
    rid = _rid(toy)
    xv.run(toy.ledger, rid, software=SW)
    xv.extend_profile(toy.ledger, rid, software=SW)
    assert report() == before


# --------------------------------------------------------------------------- CLI


def test_cli_corroboration_lifecycle(toy, monkeypatch):
    from market_signal.cli.main import app

    monkeypatch.setattr("market_signal.perps.data.perp_config", lambda settings: PERPS_CFG)
    path = toy.store.path
    toy.store.close()
    runner = CliRunner()

    def call(*args, code=0):
        res = runner.invoke(app, ["--db", str(path), "lab", "corroboration", *args])
        assert res.exit_code == code, res.output
        return json.loads(res.output) if code == 0 else res.output

    dates = ("--start", B0.date().isoformat(), "--end", B1.date().isoformat())
    dry = call("register", toy.profile_id, *dates, "--dry-run")
    assert dry["dry_run"] is True and dry["exposure"]["independent"] is False
    assert dry["assets"]["included"] == list(BN_COINS)
    assert "costs_per_side_bps" in dry["venue_semantics"]
    assert call("list") == []
    call("register", toy.profile_id, *dates, code=1)  # --reason is required
    rid = call("register", toy.profile_id, *dates, "--reason", "cli")["registration_id"]
    assert rid == dry["registration_id"]
    assert call("run", rid)["status"] == "CROSS_VENUE_CORROBORATIVE"
    call("run", rid, code=1)  # a second look needs an explicit rerun
    ext = call("evidence", rid)
    assert ext["tier"] == "EXPLORATORY" and ext["tier_changed"] is False
    shown = call("show", rid)
    assert shown["independent"] is False and shown["latest"]["status"].startswith("CROSS_VENUE")
    assert shown["extended_profiles"][0]["profile_id"] == ext["profile_id"]
    assert call("list")[0]["latest"] == "CROSS_VENUE_CORROBORATIVE"
