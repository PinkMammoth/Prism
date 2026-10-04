"""Prospective forward tracking: enrollment, no-backfill checks, write-once outcomes, evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from typer.testing import CliRunner

from market_signal.research.lab import forward as fwd
from market_signal.research.lab.batch import freeze_batch, run_batch
from market_signal.research.lab.datasets import capture_dataset
from market_signal.research.lab.evidence import (
    EvidencePolicy,
    ExploratoryRules,
    build_profiles,
    gather,
    load_profiles,
    record_profiles,
)
from market_signal.research.lab.forward import ForwardConflict, ForwardError
from market_signal.research.lab.ledger import Ledger
from market_signal.research.lab.provenance import SoftwareIdentity
from tests.test_lab_batch import _manifest
from tests.test_lab_compiler import _bars, _definition, _funding, _insert_perp
from tests.test_lab_evidence import FORBIDDEN, _keys
from tests.test_lab_screen import COINS, SW, N, _hypothesis, _plan, _selections

EXTRA = 60  # "future" bars, published one day at a time
HIST_END = datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=N)  # last historical close
SW2 = SoftwareIdentity(label="test2", python_version="3.12", source_sha256="b" * 64)
LONG = [("close", "crosses_above", "sma_10")]
SHORTS = [[("close", "crosses_below", f"sma_{n}")] for n in (10, 20, 5, 30)]
NEVER = [("close", "gt", 1e12)]
# Every adequate member becomes EXPLORATORY under this test-only policy; tiers are not the
# subject here, enrollment eligibility is.
LENIENT = EvidencePolicy(
    version=99,
    exploratory=ExploratoryRules(
        min_independent_events=1, min_assets_with_events=1, min_effect=-1.0,
        min_positive_asset_share=0.0, max_asset_event_share=1.0, max_raw_p=1.0,
        min_neighbour_support=0.0, strong_effect=-1.0,
    ),
)  # fmt: skip


def day(k: int, hours: float = 0) -> datetime:
    """Close of forward bar k (k = 1 is the first bar after the history)."""
    return HIST_END + timedelta(days=k, hours=hours)


class Market:
    """Full synthetic series per coin; the DB only holds what has been 'published'."""

    def __init__(self, store):
        self.store = store
        self.bars = {c: _bars(N + EXTRA, seed=i) for i, c in enumerate(COINS)}
        self.funding = {c: _funding(self.bars[c], seed=10 + i) for i, c in enumerate(COINS)}
        self.published = 0
        for c in COINS:
            b, f = self.bars[c], self.funding[c]
            _insert_perp(self.store, b.iloc[:N], f[f["time"] <= HIST_END], coin=c)

    def publish(self, k: int, coins=COINS, skip_funding_day: dict | None = None) -> None:
        """Publish forward bars up to k (and their funding)."""
        for c in coins:
            b, f = self.bars[c], self.funding[c]
            lo, hi = HIST_END + timedelta(days=self.published), day(k)
            nb = b[(b["close_time"] > lo) & (b["close_time"] <= hi)]
            nf = f[(f["time"] > lo) & (f["time"] <= hi)]
            if skip_funding_day and c in skip_funding_day:
                d = skip_funding_day[c]
                nf = nf[~((nf["time"] > day(d - 1)) & (nf["time"] <= day(d)))]
            _insert_perp(self.store, nb, nf, coin=c)
        self.published = max(self.published, k)


@pytest.fixture
def env(store):
    market = Market(store)
    ledger = Ledger(store)
    plan = _plan(statistics={"min_independent_events": 5, "random_entry_samples": 300})
    plan_id = ledger.register_plan(plan)
    dataset_id = ledger.register_dataset(capture_dataset(store, _selections(plan)))
    ids = {}
    rules = [("long", LONG, "long"), ("never", NEVER, "long")]
    rules += [(f"short{i}", r, "short") for i, r in enumerate(SHORTS)]
    for name, rule, side in rules:
        d = _definition(rule, side=side)
        receipt = ledger.submit(_hypothesis(d, name=name).model_dump_json(), family_id=name,
                                origin="test")  # fmt: skip
        assert receipt.accepted, receipt.error
        ids[name] = d.strategy_id
    manifest = _manifest(list(ids.values()), plan=plan_id, dataset=dataset_id)
    batch_id = freeze_batch(ledger, manifest, origin="test")
    run_batch(ledger, batch_id, software=SW)
    records, analysis = gather(ledger, batch_id)
    record_profiles(ledger, build_profiles(records, analysis, LENIENT, builder_software_id="sw"),
                    LENIENT)  # fmt: skip
    profiles = {p["subject"]["strategy_id"]: p for p in load_profiles(ledger)}
    # the synthetic data decides which short rule is not asset-dominated; take the first
    shorts = [k for k in ids if k.startswith("short")]
    ids["short"] = next(ids[k] for k in shorts if profiles[ids[k]]["tier"] == "EXPLORATORY")
    return SimpleNamespace(
        store=store, ledger=ledger, market=market, plan=plan, plan_id=plan_id, ids=ids,
        batch_id=batch_id, analysis=analysis,
        profile={k: profiles[v] for k, v in ids.items()},
    )  # fmt: skip


def _enroll(env, which="long", at=None, **kw):
    return fwd.enroll(env.ledger, env.profile[which]["profile_id"], reason="test cohort",
                      origin="test", software=SW, now=at or day(0, 2), **kw)  # fmt: skip


def _tid(env):
    return fwd.trackings(env.ledger)[0]["tracking_id"]


def _evals(env, tracking_id=None):
    sql = "SELECT * FROM lab_forward_evaluations"
    args = []
    if tracking_id:
        sql += " WHERE tracking_id=?"
        args = [tracking_id]
    return fwd._rows(env.ledger, sql + " ORDER BY bar_close, symbol", args)


def _live(env, upto: int, start: int = 1, hours: float = 3, tid=None):
    """PC always on: publish each bar and check a few hours after it closes."""
    for k in range(start, upto + 1):
        env.market.publish(k)
        fwd.check(env.ledger, software=SW, now=day(k, hours))


def _oracle(env, which: str, coin: str) -> pd.Series:
    """Signals on the FULL synthetic series (SMA rules do not depend on the window start)."""
    from market_signal.research.lab.compiler import Snapshot, compile_strategy
    from market_signal.research.lab.datasets import snapshot_rows

    env.market.publish(EXTRA)
    sel = fwd._selections("hyperliquid", coin, day(-200), day(EXTRA, 1))
    cap = capture_dataset(env.store, sel)
    blobs = dict(cap.blobs)
    snap = Snapshot(cap.manifest.dataset_id, cap.manifest,
                    tuple(snapshot_rows(s, blobs[s.sha256]) for s in cap.manifest.series))  # fmt: skip
    c = compile_strategy(env.ledger.get_strategy(env.ids[which]), snap, coin)
    return c.signal[c.signal.index > HIST_END]


# --------------------------------------------------------------------------- enrollment


def test_enrollment_freezes_the_profile_and_allows_exploratory(env):
    p = env.profile["long"]
    assert p["tier"] == "EXPLORATORY"
    out = _enroll(env)
    d = out["definition"]
    assert d["enrollment_profile_id"] == p["profile_id"] and d["enrollment_tier"] == "EXPLORATORY"
    assert d["plan_id"] == env.plan_id and d["batch_id"] == env.batch_id
    assert [h["label"] for h in d["horizons"]] == ["1d", "5d", "10d"]
    assert d["semantics"] == fwd.semantics() and d["lookback_days"] == env.plan.warmup_days
    assert {c["symbol"] for c in d["costs"]} == set(COINS)
    assert out["first_evaluable_bar_close"] == day(1).isoformat()
    # later evidence about the same strategy does not rewrite what was known at enrollment
    other = EvidencePolicy(version=98, exploratory=ExploratoryRules(min_independent_events=10_000))
    records, analysis = gather(env.ledger, env.batch_id)
    record_profiles(env.ledger, build_profiles(records, analysis, other, builder_software_id="x"),
                    other)  # fmt: skip
    newer = [q for q in load_profiles(env.ledger, strategy_id=env.ids["long"])
             if q["profile_id"] != p["profile_id"]]  # fmt: skip
    assert newer and newer[-1]["tier"] == "INSUFFICIENT"
    t = fwd.show(env.ledger, out["tracking_id"], now=day(0, 3))
    assert t["enrollment_profile_id"] == p["profile_id"] and t["enrollment_tier"] == "EXPLORATORY"
    # evidence collection only: no action/consumer keys anywhere in what enrollment records
    keys = set(_keys(json.loads(json.dumps(t, default=str))))
    assert not [k for k in keys if any(f in k.lower() for f in FORBIDDEN if f != "eligib")]


def test_enrollment_rules(env):
    assert env.profile["never"]["tier"] == "INSUFFICIENT"
    with pytest.raises(ForwardError, match="not trackable"):
        _enroll(env, "never")
    with pytest.raises(ForwardError, match="reason"):
        fwd.enroll(env.ledger, env.profile["long"]["profile_id"], reason=" ", origin="t",
                   software=SW)  # fmt: skip
    dry = _enroll(env, dry_run=True)
    assert not fwd.trackings(env.ledger)
    first = _enroll(env)
    assert first["tracking_id"] == dry["tracking_id"]
    with pytest.raises(ForwardError, match="already enrolled"):
        _enroll(env)
    # a changed horizon set or label is a new tracking definition, never a rewrite
    sub = _enroll(env, horizons=("5d",))
    labelled = _enroll(env, label="second_cohort")
    ids = {first["tracking_id"], sub["tracking_id"], labelled["tracking_id"]}
    assert len(ids) == 3 and labelled["other_open_trackings_of_strategy"]
    with pytest.raises(ForwardError, match="horizons"):
        _enroll(env, horizons=("3d",))


# --------------------------------------------------------------------------- prospective checks


def test_no_signal_before_bar_completes(env):
    tid = _enroll(env)["tracking_id"]
    env.market.publish(1)  # bar 1 stored early (e.g. a glitch): not complete until day(1)
    out = fwd.check(env.ledger, software=SW, now=day(1, -1))
    assert out["recorded"] == 0 and not _evals(env)
    assert any("before enrollment" in n["note"] for n in out["notes"])
    # the database refuses an evaluation stamped before its bar closed
    with pytest.raises(duckdb.ConstraintException):
        env.store.con.execute(
            "INSERT INTO lab_forward_evaluations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ["e", tid, "BTC", day(1), day(1, -1), "r", "no_signal", True, False, False,
             None, None, "i", "s", "{}"],
        )  # fmt: skip
    assert fwd.check(env.ledger, software=SW, now=day(1, 1))["recorded"] == len(COINS)


def test_no_backfill_after_downtime_and_gaps_are_explicit(env):
    tid = _enroll(env)["tracking_id"]
    _live(env, 2)  # days 1-2 observed
    env.market.publish(6)  # PC off: bars 3-6 arrive while nothing checks
    late = fwd.check(env.ledger, software=SW, now=day(6, 30))  # restart 30h after bar 6 closed
    assert late["recorded"] == 0
    assert all("outside its evaluation window" in n["note"] for n in late["notes"])
    env.market.publish(7)
    assert fwd.check(env.ledger, software=SW, now=day(7, 8))["recorded"] == len(COINS)
    bars = sorted({r["bar_close"] for r in _evals(env)})
    assert [pd.Timestamp(b) for b in bars] == [pd.Timestamp(day(k)) for k in (1, 2, 7)]
    cov = fwd.coverage(env.ledger, tid, now=day(7, 9))
    btc = cov["by_symbol"]["BTC"]
    assert btc["evaluated"] == 3
    gap_days = [g["bar_close"] for g in btc["gaps"]]
    assert gap_days == [day(k).isoformat() for k in (3, 4, 5, 6)]
    reasons = {g["bar_close"]: g["reason"] for g in btc["gaps"]}
    # the restart ran 30h after bar 6 closed: outside bar 6's window, so no check ran in it
    assert set(reasons.values()) == {"no check ran inside the window"}
    assert cov["expected_evaluations"] == 7 * len(COINS) and cov["gaps"] == 4 * len(COINS)
    # evaluated-without-signal is a row; never-evaluated is not
    statuses = {r["status"] for r in _evals(env)}
    assert statuses <= {"signal", "no_signal", "ineligible"}
    assert len(_evals(env)) == 3 * len(COINS)


def test_repeated_check_is_idempotent(env):
    _enroll(env)
    env.market.publish(1)
    first = fwd.check(env.ledger, software=SW, now=day(1, 2))
    again = fwd.check(env.ledger, software=SW2, now=day(1, 9))
    assert first["recorded"] == len(COINS) and again["recorded"] == 0
    assert all("already recorded" in n["note"] for n in again["notes"])
    assert len(_evals(env)) == len(COINS)


def test_changed_answer_for_a_recorded_bar_is_a_conflict(env):
    _enroll(env)
    env.market.publish(1)
    fwd.check(env.ledger, software=SW, now=day(1, 2))
    before = _evals(env)
    # a provider revision makes bar 1 unusable: recomputation disagrees with the record
    env.store.con.execute(
        "UPDATE perp_bars SET close='NaN' WHERE coin='BTC' AND close_time=?", [day(1)]
    )
    with pytest.raises(ForwardConflict, match="nothing was overwritten"):
        fwd.check(env.ledger, software=SW, now=day(1, 5))
    assert _evals(env) == before
    runs = fwd._rows(env.ledger, "SELECT summary FROM lab_forward_runs ORDER BY started_at", [])
    assert json.loads(runs[-1]["summary"])["conflicts"][0]["symbol"] == "BTC"


def test_funding_not_ingested_waits_inside_the_window(env):
    _enroll(env)
    env.market.publish(1, skip_funding_day={"BTC": 1})
    out = fwd.check(env.ledger, software=SW, now=day(1, 2))
    assert {r["symbol"] for r in _evals(env)} == set(COINS) - {"BTC"}
    assert any("funding through the bar close" in n["note"] for n in out["notes"])
    gaps = fwd.coverage(env.ledger, _tid(env), now=day(2, 1))["by_symbol"]["BTC"]["gaps"]
    assert gaps[0]["reason"].startswith("check ran but did not evaluate: funding")


def test_signals_match_the_phase3_compiler_and_record_provenance(env):
    tid = _enroll(env)["tracking_id"]
    _live(env, 25)
    oracle = {c: _oracle(env, "long", c) for c in COINS}
    for r in _evals(env, tid):
        bar = pd.Timestamp(r["bar_close"])
        assert r["fired"] == bool(oracle[r["symbol"]].loc[bar]), (r["symbol"], bar)
        p = json.loads(r["payload"])
        assert p["semantics"] == fwd.semantics() and p["software_id"] == SW.software_id
        assert p["enrollment_profile_id"] == env.profile["long"]["profile_id"]
        assert p["signal_bar"]["close"] == bar.isoformat()
        assert p["data_cutoff"]["bars_close_time_lte"] == bar.isoformat()
        assert p["compile"]["last_close"] == bar.isoformat()
        manifest = json.loads(env.store.con.execute(
            "SELECT manifest FROM lab_forward_inputs WHERE input_id=?", [r["input_id"]]
        ).fetchone()[0])  # fmt: skip
        bars = next(s for s in manifest["series"] if s["selection"]["kind"] == "perp_bars")
        assert pd.Timestamp(bars["last_observed"]) == bar  # nothing after T was read
    assert sum(r["fired"] for r in _evals(env, tid)) > 0


def test_semantics_change_stops_evaluation_until_reenrolled(env, monkeypatch):
    _enroll(env)
    env.market.publish(1)
    monkeypatch.setattr(fwd, "EVALUATOR_VERSION", "lab_forward_evaluator_v2")
    out = fwd.check(env.ledger, software=SW, now=day(1, 2))
    assert out["recorded"] == 0 and "semantics changed" in out["notes"][0]["note"]


def test_recorded_evaluations_are_isolated_from_live_table_changes(env):
    _enroll(env)
    env.market.publish(1)
    fwd.check(env.ledger, software=SW, now=day(1, 2))
    before = _evals(env)
    manifests = env.store.con.execute("SELECT * FROM lab_forward_inputs").fetchall()
    env.store.con.execute("UPDATE perp_bars SET close = close * 2, open = open * 2")
    env.store.con.execute("UPDATE perp_funding SET funding_rate = 0.01")
    assert _evals(env) == before
    assert env.store.con.execute("SELECT * FROM lab_forward_inputs").fetchall() == manifests


def test_pause_and_stop(env):
    tid = _enroll(env)["tracking_id"]
    _live(env, 1)
    fwd.set_status(env.ledger, tid, "paused", reason="trip", now=day(1, 5))
    _live(env, 3, start=2)
    assert len(_evals(env)) == len(COINS)
    fwd.set_status(env.ledger, tid, "active", reason="back", now=day(3, 5))
    _live(env, 4, start=4)
    cov = fwd.coverage(env.ledger, tid, now=day(4, 6))
    # bars 2 and 3 closed while paused: neither evaluated nor counted as gaps
    assert cov["gaps"] == 0 and cov["evaluated"] == 2 * len(COINS)
    assert cov["paused_or_stopped"] == 2 * len(COINS)
    fwd.set_status(env.ledger, tid, "stopped", reason="done", now=day(4, 7))
    with pytest.raises(ForwardError, match="final"):
        fwd.set_status(env.ledger, tid, "active", reason="again", now=day(4, 8))


# --------------------------------------------------------------------------- outcomes


def _hand(env, coin, bar_k, h, side, fee=4.5, slip=2.0):
    """Hand calculation: entry at bar T+1 open, exit T+h close, hourly funding summed."""
    b = env.market.bars[coin].set_index("close_time")
    f = env.market.funding[coin]
    closes = [pd.Timestamp(day(bar_k + j)) for j in range(1, h + 1)]
    entry = b.loc[closes[0], "open"]
    exit_ = b.loc[closes[-1], "close"]
    paid = 0.0
    for c in closes:
        fd = f[(f["time"] > c - pd.Timedelta(days=1)) & (f["time"] <= c)]["funding_rate"].sum()
        paid += fd * b.loc[c, "close"]
    sign = 1 if side == "long" else -1
    gross = sign * (exit_ / entry - 1)
    net = gross - 2 * (fee + slip) / 1e4 - sign * paid / entry
    return entry, gross, net


@pytest.mark.parametrize("side", ["long", "short"])
def test_hand_calculated_outcomes_enter_at_next_open(env, side):
    tid = _enroll(env, side)["tracking_id"]
    _live(env, 40)
    fwd.resolve(env.ledger, software=SW, now=day(40, 4))
    sig = [r for r in _evals(env, tid) if r["fired"]]
    assert sig
    checked = 0
    for r in sig:
        k = (pd.Timestamp(r["bar_close"]) - pd.Timestamp(HIST_END)).days
        entry = fwd._rows(env.ledger, "SELECT * FROM lab_forward_entries WHERE evaluation_id=?",
                          [r["evaluation_id"]])  # fmt: skip
        if k + 1 > 40:
            continue
        assert entry[0]["status"] == "entered"
        assert pd.Timestamp(entry[0]["entry_bar_open"]) == pd.Timestamp(r["bar_close"])
        assert entry[0]["entry_price"] == env.market.bars[r["symbol"]].iloc[N + k]["open"]
        for o in fwd._rows(env.ledger, "SELECT * FROM lab_forward_outcomes WHERE evaluation_id=?",
                           [r["evaluation_id"]]):  # fmt: skip
            h = {"1d": 1, "5d": 5, "10d": 10}[o["horizon"]]
            e, gross, net = _hand(env, r["symbol"], k, h, side)
            p = json.loads(o["payload"])
            assert o["status"] == "resolved" and p["entry_price"] == e
            assert o["gross"] == pytest.approx(gross, rel=1e-12)
            assert o["net"] == pytest.approx(net, rel=1e-10)
            assert pd.Timestamp(o["exit_bar_close"]) == pd.Timestamp(day(k + h))
            checked += 1
    assert checked > 0
    # baseline outcomes exist for evaluated no-signal bars too
    n_base = env.store.con.execute(
        "SELECT count(*) FROM lab_forward_outcomes o JOIN lab_forward_evaluations e "
        "USING (evaluation_id) WHERE NOT e.fired AND e.tracking_id=?", [tid]).fetchone()[0]  # fmt: skip
    assert n_base > 0


def test_pending_until_exit_data_exists_then_immutable(env):
    tid = _enroll(env)["tracking_id"]
    _live(env, 1)
    first = fwd.resolve(env.ledger, software=SW, now=day(1, 4))
    assert first["resolved"] == 0 and first["pending"] == 3 * len(COINS)  # 1d/5d/10d
    env.market.publish(2)
    second = fwd.resolve(env.ledger, software=SW, now=day(2, 1))
    assert second["resolved"] == len(COINS) and second["pending"] == 2 * len(COINS)
    rows = fwd._rows(env.ledger, "SELECT * FROM lab_forward_outcomes ORDER BY evaluation_id", [])
    # a later price revision never rewrites a resolved outcome
    env.store.con.execute("UPDATE perp_bars SET close = close * 3")
    env.market.publish(11)
    fwd.resolve(env.ledger, software=SW2, now=day(11, 1))
    after = fwd._rows(env.ledger, "SELECT * FROM lab_forward_outcomes WHERE horizon='1d' "
                      "ORDER BY evaluation_id", [])  # fmt: skip
    assert after == [r for r in rows if r["horizon"] == "1d"]
    assert fwd.resolve(env.ledger, software=SW, now=day(11, 2))["resolved"] == 0
    assert all(r["tracking_id"] == tid for r in _evals(env))


def test_missing_funding_is_unavailable_never_zero(env):
    _enroll(env)
    env.market.publish(1)
    fwd.check(env.ledger, software=SW, now=day(1, 2))
    env.market.publish(6, skip_funding_day={"BTC": 3})
    early = fwd.resolve(env.ledger, software=SW, now=day(6, 1))
    btc = lambda: fwd._rows(  # noqa: E731
        env.ledger, "SELECT o.* FROM lab_forward_outcomes o JOIN lab_forward_evaluations e "
        "USING (evaluation_id) WHERE e.symbol='BTC' ORDER BY horizon", [])  # fmt: skip
    assert [r["horizon"] for r in btc()] == ["1d"]  # 5d waits for funding (may arrive late)
    assert early["pending"] > 0
    fwd.resolve(env.ledger, software=SW, now=day(6 + fwd.OUTCOME_WAIT_DAYS, 1))
    five = next(r for r in btc() if r["horizon"] == "5d")
    assert five["status"] == "unavailable" and five["net"] is None and five["gross"] is None
    assert "funding" in json.loads(five["payload"])["reason"]


def test_missing_next_bar_makes_entry_unavailable(env):
    tid = _enroll(env)["tracking_id"]
    _live(env, 25)
    sig = next(r for r in _evals(env, tid) if r["fired"])
    k = (pd.Timestamp(sig["bar_close"]) - pd.Timestamp(HIST_END)).days
    env.store.con.execute("DELETE FROM perp_bars WHERE coin=? AND close_time=?",
                          [sig["symbol"], day(k + 1)])  # fmt: skip
    fwd.resolve(env.ledger, software=SW, now=day(k + 1, 5))
    assert not fwd._rows(env.ledger, "SELECT * FROM lab_forward_entries WHERE evaluation_id=?",
                         [sig["evaluation_id"]])  # fmt: skip
    fwd.resolve(env.ledger, software=SW, now=day(k + 2 + fwd.OUTCOME_WAIT_DAYS))
    entry = fwd._rows(env.ledger, "SELECT * FROM lab_forward_entries WHERE evaluation_id=?",
                      [sig["evaluation_id"]])  # fmt: skip
    assert entry[0]["status"] == "unavailable" and entry[0]["entry_price"] is None


# --------------------------------------------------------------------------- evidence


def test_paper_forward_extends_without_touching_history(env):
    tid = _enroll(env)["tracking_id"]
    _live(env, 30)
    fwd.resolve(env.ledger, software=SW, now=day(30, 5))
    before = env.store.con.execute(
        "SELECT profile_id, payload FROM lab_evidence_profiles ORDER BY profile_id").fetchall()  # fmt: skip
    out = fwd.record_forward_evidence(env.ledger, tid, software=SW, as_of=day(30, 6))
    again = fwd.record_forward_evidence(env.ledger, tid, software=SW2, as_of=day(30, 6))
    assert out == {**again, "new_profiles": 1} and again["new_profiles"] == 0
    after = env.store.con.execute(
        "SELECT profile_id, payload FROM lab_evidence_profiles ORDER BY profile_id").fetchall()  # fmt: skip
    assert set(before) < set(after) and len(after) == len(before) + 1
    new = next(json.loads(p) for i, p in after if i == out["profile_id"])
    base = env.profile["long"]
    assert new["extends"] == base["profile_id"] and new["profile_schema"] == "3"
    assert new["tier"] == base["tier"] == out["tier"]
    assert [s["stage"] for s in new["sources"]] == ["fast_screen", "batch_fdr", "paper_forward"]
    assert new["sources"][-1]["records"]["summary_id"] == out["summary_id"]
    assert new["statistics"] == base["statistics"] and new["horizons"] == base["horizons"]
    fwd_block = new["forward"]
    assert fwd_block["maturity"]["level"] in ("TOO_EARLY", "EARLY")
    assert fwd_block["observation"]["evaluated"] == 30 * len(COINS)
    primary = next(h for h in fwd_block["horizons"] if h["primary"])
    assert primary["historical"]["excess_mean"] == next(
        r["excess_mean"] for r in base["horizons"]["rows"] if r["primary"])  # fmt: skip
    assert "p_value" not in json.dumps(fwd_block) and "raw_p" not in json.dumps(fwd_block)
    keys = set(_keys(new))
    assert not [k for k in keys if any(f in k.lower() for f in FORBIDDEN)]
    # summaries are append-only snapshots: a later as_of is a new summary and profile
    later = fwd.record_forward_evidence(env.ledger, tid, software=SW, as_of=day(30, 7))
    assert later["profile_id"] != out["profile_id"] or later["summary_id"] == out["summary_id"]


def test_forward_summary_distinguishes_no_signal_from_gaps(env):
    tid = _enroll(env)["tracking_id"]
    _live(env, 3)
    env.market.publish(5)
    s = fwd.forward_summary(env.ledger, tid, as_of=day(6, 1))  # bars 4-5 missed, 6 open
    o = s["observation"]
    assert o["evaluated"] == 3 * len(COINS) and o["gap_evaluations"] == 2 * len(COINS)
    assert o["evaluated_signal"] + o["evaluated_no_signal"] + o[
        "evaluated_inputs_incomplete"
    ] == 3 * len(COINS)
    assert s["maturity"]["level"] == "TOO_EARLY"


def test_maturity_is_counts_not_quality():
    m = fwd.MaturityPolicy()
    assert m.level(0, 0) == m.level(9, 400) == "TOO_EARLY"
    assert m.level(10, 1) == "EARLY" and m.level(30, 1) == "DEVELOPING"
    assert m.level(150, 100) == "DEVELOPING" and m.level(100, 180) == "MATURE"


def test_candidate_rule_ignores_effect_size(env, monkeypatch):
    def p(name, family, side, label, agree, testable, excess, tier="EXPLORATORY"):
        return {"profile_id": f"evidence_{name}", "policy_id": "pol", "tier": tier,
                "subject": {"name": name, "family": family, "ledger_family": family,
                            "side": side, "strategy_id": f"strategy_{name}"},
                "neighbourhood": {"label": label, "same_direction": agree,
                                  "testable_neighbours": testable,
                                  "isolated_spike": label == "isolated"},
                "effect": {"excess_mean": excess}}  # fmt: skip

    rows = [
        p("best_spike", "trend", "long", "isolated", 0, 2, 0.09),
        p("edge", "trend", "long", "plateau", 2, 3, 0.05),
        p("centre", "trend", "long", "plateau", 4, 4, 0.01),
        p("b_tie", "trend", "short", "plateau", 2, 2, 0.02),
        p("a_tie", "trend", "short", "plateau", 2, 2, 0.001),
        p("mixed_only", "fade", "long", "mixed", 1, 2, 0.003),
        p("alone", "other", "long", "no_testable_neighbours", 0, 0, 0.2),
        p("neg", "trend", "long", "plateau", 4, 4, -0.01, tier="NEGATIVE"),
    ]
    import market_signal.research.lab.evidence as ev

    monkeypatch.setattr(ev, "load_profiles", lambda ledger, **kw: rows)
    out = {(g["family"], g["side"]): g for g in fwd.candidates(env.ledger, "a", "pol")["groups"]}
    pick = {k: (g["suggested"] or {}).get("name") for k, g in out.items()}
    assert pick == {("trend", "long"): "centre", ("trend", "short"): "a_tie",
                    ("fade", "long"): "mixed_only", ("other", "long"): None}  # fmt: skip


# --------------------------------------------------------------------------- CLI


def test_cli_forward_lifecycle(env):
    from market_signal.cli.main import app

    profile_id = env.profile["long"]["profile_id"]
    path = env.store.path
    env.store.close()
    runner = CliRunner()
    base = ["--db", str(path), "lab", "forward"]

    def run(*args, code=0):
        res = runner.invoke(app, [*base, *args])
        assert res.exit_code == code, res.output
        return (
            json.loads(res.output)
            if code == 0 and res.output.strip().startswith(("{", "["))
            else res.output
        )

    dry = run("enroll", profile_id, "--reason", "cli", "--dry-run")
    assert dry["dry_run"] is True
    assert run("list") == []
    enrolled = run("enroll", profile_id, "--reason", "cli", "--horizon", "5d")
    tid = enrolled["tracking_id"]
    assert [t["tracking_id"] for t in run("list")] == [tid]
    run("enroll", env.profile["never"]["profile_id"], "--reason", "x", code=1)
    # the synthetic market ended long ago: a real-clock check finds nothing in its window
    checked = run("check", "--dry-run")
    assert checked["recorded"] == 0
    assert run("check")["recorded"] == 0
    assert run("resolve")["resolved"] == 0
    shown = run("show", tid)
    assert shown["tracking_id"] == tid and shown["status"] == "active"
    run("pause", tid, "--reason", "trip")
    run("resume", tid, "--reason", "back")
    candidates = run("candidates", env.batch_id, "--policy-version", "2")
    assert candidates["rule"] == fwd.SELECTION_RULE
    ev = run("evidence", tid)
    assert ev["tier"] == "EXPLORATORY" and ev["maturity"] == "TOO_EARLY"
    run("stop", tid, "--reason", "done")
    run("resume", tid, "--reason", "again", code=1)
    out = runner.invoke(app, [*base, "run", "--no-update"])
    assert out.exit_code == 0, out.output
