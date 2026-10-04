"""Paper auto-trader: safety boundary, prospective account, fills, funding, risk, ledger, evidence."""

# ruff: noqa: F811  (helpers take the imported ``env`` fixture's value as ``env``)

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from market_signal.paper import engine
from market_signal.paper import policy as pol
from market_signal.paper.account import RUN_STATUSES, replay
from market_signal.paper.engine import PaperError
from market_signal.research.lab import forward as fwd
from tests.test_lab_forward import SW, day, env  # noqa: F401  (env is a fixture)

SRC = Path(__file__).resolve().parents[1] / "src" / "market_signal"

# Synthetic markets have tiny samples and no Phase 9/11 records. A test-only promotion
# policy relaxes exactly those requirements; everything else equals v1.
TEST_PROMOTION = pol.AutotraderPolicy(
    version=99, min_independent_events=1, min_assets_with_events=1, min_excess=-1.0,
    max_asset_event_share=1.0, block_dominated_by_one_asset=False, block_isolated_spike=False,
    require_full_research=False, require_validation_registration=False,
)  # fmt: skip
TEST_EXIT = pol.ExitPolicy(version=99, horizon_bars=5)  # the synthetic plan's primary is 5d
RISK = pol.risk_policy(1)


@pytest.fixture
def pap(env, monkeypatch):
    monkeypatch.setitem(pol.PROMOTION_POLICIES, 99, TEST_PROMOTION)
    monkeypatch.setitem(pol.EXIT_POLICIES, 99, TEST_EXIT)
    for which in ("long", "short"):
        fwd.enroll(env.ledger, env.profile[which]["profile_id"], reason="cohort", origin="test",
                   software=SW, now=day(0, 2))  # fmt: skip
    env.names = {
        k: env.store.con.execute(
            "SELECT json_extract_string(payload,'$.subject.name') FROM lab_evidence_profiles "
            "WHERE profile_id=?", [env.profile[k]["profile_id"]]).fetchone()[0]
        for k in ("long", "short")
    }  # fmt: skip
    return env


def _create(env, at=None, risk=RISK, **kw):
    return engine.create_run(
        env.ledger, [env.names["long"], env.names["short"]], reason="test paper run",
        origin="test", software=SW, now=at or day(0, 3), promotion=TEST_PROMOTION,
        exit_=TEST_EXIT, risk=risk, **kw,
    )  # fmt: skip


def _cycle(env, run_id, k, hours=3.0, **kw):
    env.market.publish(k)
    return engine.cycle(env.ledger, run_id, software=SW, now=day(k, hours), **kw)


def _events(env, run_id, kind=None):
    return [e for e in engine.events(env.store, run_id) if kind is None or e["event_type"] == kind]


def _live(env, run_id, upto, start=1, hours=3.0):
    for k in range(start, upto + 1):
        _cycle(env, run_id, k, hours)


# --------------------------------------------------------------------------- helpers


def _until(env, run_id, kind, start=1, upto=60, hours=3.0):
    """Publish one bar a day and cycle until an event of ``kind`` appears; return (k, event)."""
    for k in range(start, upto + 1):
        _cycle(env, run_id, k, hours)
        hits = [e for e in _events(env, run_id, kind) if e["market_time"] is not None]
        if hits:
            return k, hits[0]
    raise AssertionError(f"no {kind} in the synthetic window")


def _bar(env, coin, close):
    b = env.market.bars[coin]
    return b[b["close_time"] == pd.Timestamp(close)].iloc[0]


def _publish_one(env, coin, k):
    """Publish one coin's bar k and its funding (after the others were published)."""
    from tests.test_lab_compiler import _insert_perp

    b, f = env.market.bars[coin], env.market.funding[coin]
    nb = b[b["close_time"] == pd.Timestamp(day(k))]
    nf = f[(f["time"] > pd.Timestamp(day(k - 1))) & (f["time"] <= pd.Timestamp(day(k)))]
    _insert_perp(env.store, nb, nf, coin=coin)


def _count(env, table):
    return env.store.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _digest(env, prefix) -> dict:
    out = {}
    for (t,) in env.store.con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE ? "
        "AND table_type='BASE TABLE' ORDER BY 1",
        [prefix + "%"],
    ).fetchall():
        rows = env.store.con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        out[t] = hashlib.sha256(repr(rows).encode()).hexdigest()
    return out


def _view(**over) -> dict:
    """A realistic evidence view: the live ma_trend_20_100_short after Phases 9 and 11."""
    ev = {
        "profile_id": "evidence_x", "profile_schema": "5", "tier": "EXPLORATORY",
        "historical_tier": "EXPLORATORY", "primary_horizon": "10d",
        "effect": {"excess_mean": 0.026, "net_mean": 0.013, "hit_rate": 0.56},
        "sample": {"independent_events": 39, "assets_with_events": 6},
        "assets": {"assets_with_events": 6, "positive_share": 0.67, "max_asset_event_share": 0.21,
                   "dominated_by_one_asset": False},
        "neighbourhood": {"label": "plateau", "isolated_spike": False},
        "statistics": {"raw_p": 0.11, "q": 0.91, "fdr_survivor": False},
        "full_research_status": "FULL_RESEARCH_MIXED", "validation_status": "VALIDATION_INSUFFICIENT",
        "corroboration_status": "CROSS_VENUE_MIXED",
        "forward": {"maturity": "TOO_EARLY", "excess_mean": None, "direction_vs_historical": None},
    }  # fmt: skip
    for k, v in over.items():
        ev[k] = {**ev[k], **v} if isinstance(v, dict) and isinstance(ev.get(k), dict) else v
    return ev


CTX = {"enrolled": True, "semantics_ok": True, "semantics_detail": "ok", "tracking_active": True}


def _promote(ev=None, **ctx):
    return pol.evaluate_promotion(pol.promotion_policy(1), _view() if ev is None else ev,
                                  {**CTX, **ctx})  # fmt: skip


# --------------------------------------------------------------------------- safety


FORBIDDEN_IMPORTS = ("httpx", "requests", "urllib", "http", "socket", "aiohttp", "websocket",
                     "websockets", "ccxt", "eth_account", "eth_keys", "web3", "hyperliquid",
                     "market_signal.data.http", "market_signal.data.providers",
                     "market_signal.data.registry", "market_signal.data.update",
                     "market_signal.data.updaters", "market_signal.perps.data",
                     "market_signal.portfolio", "market_signal.copilot")  # fmt: skip


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_paper_package_imports_no_network_or_order_client():
    files = sorted((SRC / "paper").glob("*.py"))
    assert {f.name for f in files} >= {"engine.py", "execution.py", "risk.py", "account.py"}
    for f in files:
        for mod in _imports(f):
            assert not any(mod == b or mod.startswith(b + ".") for b in FORBIDDEN_IMPORTS), (f, mod)
        text = f.read_text()
        for word in ("os.environ", "getenv", ".secret(", "api_key", "private_key", "/exchange"):
            assert word not in text, (f.name, word)


def test_no_order_placement_path_anywhere_in_prism():
    """Prism talks to Hyperliquid's public /info endpoint only; nothing can sign or place."""
    for f in SRC.rglob("*.py"):
        text = f.read_text()
        for word in ("/exchange", "ccxt", "eth_account", "place_order", "create_order",
                     "submit_order", "api_secret", "READY_FOR_LIVE", "APPROVED_LIVE",
                     "ENABLE_LIVE"):  # fmt: skip
            assert word not in text, (str(f), word)
    assert RUN_STATUSES == ("ACTIVE", "PAUSED", "STOPPED", "KILLED")


def test_adapter_is_structurally_paper_only():
    from market_signal.paper.execution import PaperExecutionAdapter, require_paper_adapter

    model = pol.ExecutionModel(costs=(pol.AssetCosts(symbol="BTC", fee_bps=4.5, slippage_bps=2),))
    a = PaperExecutionAdapter(model)
    assert a.mode == "paper" and a.transmits_orders is False
    assert require_paper_adapter(a) is a
    # nothing that could transmit: no client, session, key, signer or send method
    public = {n for n in dir(a) if not n.startswith("_")}
    assert public == {"mode", "transmits_orders", "model", "validate", "fill"}

    class Sneaky(PaperExecutionAdapter):
        transmits_orders = True

    class Duck:
        mode, transmits_orders = "paper", False

    for bad in (Sneaky(model), Duck(), object()):
        with pytest.raises(TypeError, match="only runs with PaperExecutionAdapter"):
            require_paper_adapter(bad)
    for model_cls in (pol.ExecutionModel, pol.RiskPolicy, pol.AutotraderPolicy):
        with pytest.raises(ValidationError, match="paper"):
            model_cls.model_validate({"mode": "live"})


def test_cli_has_no_live_mode():
    import typer.main

    from market_signal.cli.main import app

    def walk(cmd, path=()):
        yield path, cmd
        for name, sub in getattr(cmd, "commands", {}).items():
            yield from walk(sub, (*path, name))

    tree = list(walk(typer.main.get_command(app)))
    banned = (
        "live",
        "real",
        "exchange",
        "api-key",
        "apikey",
        "secret",
        "execute",
        "mainnet",
        "key",
    )
    paper_cmds = [(p, c) for p, c in tree if p[:2] == ("lab", "paper")]
    assert {p[-1] for p, _ in paper_cmds if len(p) == 3} >= {"create", "run", "status", "pause"}
    for path, cmd in paper_cmds:
        assert not any(b in path[-1] for b in banned), path
        for prm in cmd.params:
            for opt in [*getattr(prm, "opts", []), prm.name or ""]:
                assert not any(b in opt.lower() for b in banned), (path, opt)
    # app-wide: no command that could trade for real (``doctor --live`` only probes providers)
    for path, cmd in tree:
        assert not any(w in (path[-1] if path else "") for w in ("live", "order", "execute")), path
        for prm in cmd.params:
            for opt in getattr(prm, "opts", []):
                assert opt not in ("--real", "--execute", "--mainnet", "--api-key", "--secret"), (
                    path,
                    opt,
                )


def test_cycle_runs_with_the_network_disabled(pap, monkeypatch):
    import socket

    def refuse(*a, **k):
        raise AssertionError("the paper engine attempted a network connection")

    run = _create(pap)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    _live(pap, run["run_id"], 25)
    assert _events(pap, run["run_id"], "order_filled")  # it traded, offline


def test_db_refuses_non_paper_runs_and_pre_creation_events(pap):
    run = _create(pap)
    rid = run["run_id"]
    con = pap.store.con
    row = con.execute("SELECT * FROM paper_runs").fetchone()
    with pytest.raises(Exception, match=r"CHECK|Constraint"):
        con.execute("INSERT INTO paper_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ["paperrun_x", row[1], "live", *row[3:]])  # fmt: skip
    with pytest.raises(Exception, match=r"CHECK|Constraint"):
        con.execute(
            "INSERT INTO paper_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            ["paperevt_x", rid, 99, "k", "order_submitted", day(0).isoformat(),
             day(0, 3).isoformat(), day(1).isoformat(), None, "{}"],
        )  # fmt: skip


# --------------------------------------------------------------------------- policies (pure)


def test_promotion_v1_admits_the_cohort_shape_with_caveats():
    r = _promote()
    assert r["decision"] == "PAPER_ELIGIBLE" and r["blocked_by"] == []
    assert "cross-venue corroboration mixed" in r["caveats"]  # MIXED is a caveat, not fatal
    assert any("did not survive family (BH) correction: q 0.91" in c for c in r["caveats"])
    assert "validation insufficient" in r["caveats"]
    assert (
        _promote(_view(full_research_status="FULL_RESEARCH_CONSISTENT"))["decision"]
        == "PAPER_ELIGIBLE"
    )


def test_promotion_v1_is_stricter_than_the_copilot():
    cases = {
        "full_research_adequate": _view(full_research_status=None),
        "validation_registered": _view(validation_status=None),
        "corroboration_not_adverse": _view(corroboration_status="CROSS_VENUE_ADVERSE"),
        "forward_not_adverse": _view(forward={"maturity": "DEVELOPING", "excess_mean": -0.01}),
        "effect_positive": _view(effect={"excess_mean": -0.001}),
        "sample_adequate": _view(sample={"independent_events": 29}),
        "not_isolated_spike": _view(neighbourhood={"isolated_spike": True}),
        "breadth_ok": _view(assets={"dominated_by_one_asset": True}),
    }
    for rule, ev in cases.items():
        assert _promote(ev)["blocked_by"] == [rule], rule
    assert (
        "full_research_adequate"
        in _promote(_view(full_research_status="FULL_RESEARCH_INCONSISTENT"))["blocked_by"]
    )
    for vs in ("VALIDATION_ADVERSE", "VALIDATION_ERROR"):
        assert _promote(_view(validation_status=vs))["blocked_by"] == ["validation_not_adverse"]
    for tier in ("NEGATIVE", "INSUFFICIENT", "INCONCLUSIVE", "UNAVAILABLE"):
        assert "tier_eligible" in _promote(_view(tier=tier))["blocked_by"]
    assert _promote(enrolled=False)["blocked_by"] == ["strategy_enrolled"]
    assert _promote(tracking_active=False)["blocked_by"] == ["forward_tracking_active"]
    assert _promote(semantics_ok=False)["blocked_by"] == ["versions_compatible"]
    # an EARLY adverse forward sample and a high q never block (tiny samples, paper gathers evidence)
    assert (
        _promote(_view(forward={"maturity": "EARLY", "excess_mean": -0.05}))["decision"]
        == "PAPER_ELIGIBLE"
    )
    none = pol.evaluate_promotion(pol.promotion_policy(1), None, CTX)
    assert "evidence_available" in none["blocked_by"]


def test_policy_identities_are_frozen_and_separate():
    from market_signal.copilot.policy import get_policy as copilot_policy

    p = pol.promotion_policy(1)
    assert p.name == "autotrader_policy" and p.mode == "paper"
    assert p.policy_id != copilot_policy(1).policy_id and p.policy_id.startswith("appolicy_")
    assert pol.AutotraderPolicy(min_independent_events=31).policy_id != p.policy_id
    with pytest.raises(ValidationError, match="approved_for_live"):
        pol.AutotraderPolicy.model_validate({**p.model_dump(), "approved_for_live": True})
    r = pol.risk_policy(1)
    assert (r.starting_equity, r.currency, r.leverage, r.max_open_positions) == (
        10_000.0,
        "USDC",
        2.0,
        3,
    )
    assert (r.position_notional_fraction, r.max_gross_notional_fraction) == (0.20, 0.60)
    assert (r.daily_loss_halt_fraction, r.max_drawdown_kill_fraction) == (0.03, 0.15)
    assert pol.RiskPolicy(daily_loss_halt_fraction=0.05).policy_id != r.policy_id
    with pytest.raises(ValidationError, match="leverage"):
        pol.RiskPolicy(leverage=5.0)
    assert r.maintenance_rate("BTC") == 0.0125 and r.maintenance_rate("DOGE") is None
    e = pol.exit_policy(1)
    assert (e.horizon_bars, e.protective_stop, e.opposite_signal_exit) == (10, None, False)
    with pytest.raises(ValueError, match="unknown"):
        pol.risk_policy(42)


def test_maturity_counts_trades_and_days_not_quality():
    m = pol.maturity_policy(1)
    assert m.level(0, 400) == "WARMUP" and m.level(50, 10) == "WARMUP"
    assert m.level(10, 30) == "EARLY" and m.level(30, 90) == "DEVELOPING"
    assert m.level(100, 364) == "DEVELOPING" and m.level(100, 365) == "MATURE"


# --------------------------------------------------------------------------- risk engine (pure)


EXEC = pol.ExecutionModel(costs=tuple(pol.AssetCosts(symbol=s, fee_bps=4.5, slippage_bps=2)
                                      for s in ("AAVE", "BTC", "ETH", "HYPE", "LINK", "SOL")))  # fmt: skip
ELIGIBLE = {"decision": "PAPER_ELIGIBLE", "blocked_by": []}


def _snap(**over):
    return {"status": "ACTIVE", "halted": False, "equity": 10_000.0, "cash": 10_000.0,
            "positions": {}, "orders": {}, **over}  # fmt: skip


def _cand(symbol, strategy="s_long", side=1, **over):
    return {"intent_id": f"i_{strategy}_{symbol}", "strategy_id": strategy, "symbol": symbol,
            "side": side, "live": True, "promotion": ELIGIBLE, "data_ok": True, **over}  # fmt: skip


def _alloc(cands, snap=None, bar="2026-10-05T00:00:00+00:00", run="paperrun_t", policy=RISK):
    from market_signal.paper.risk import allocate

    return allocate(policy, EXEC, snap or _snap(), cands, run_id=run, bar_close=bar)


def test_sizing_is_a_fixed_bounded_fraction_of_equity():
    d = _alloc([_cand("BTC")])["i_s_long_BTC"]
    assert (
        d["decision"] == "ACCEPTED" and d["notional"] == pytest.approx(2_000.0) and d["scale"] == 1
    )
    d = _alloc([_cand("BTC")], _snap(equity=5_000.0, cash=5_000.0))["i_s_long_BTC"]
    assert d["notional"] == pytest.approx(1_000.0)


def test_simultaneous_signals_same_snapshot_lottery_not_alphabetical():
    syms = ["AAVE", "BTC", "ETH", "HYPE", "LINK", "SOL"]
    cands = [_cand(s, strategy=f"s{i % 2}", side=1) for i, s in enumerate(syms)]
    policy = pol.RiskPolicy(
        version=97, max_strategy_notional_fraction=1.0, max_gross_notional_fraction=1.0
    )
    picks: dict[str, int] = dict.fromkeys(syms, 0)
    for k in range(200):
        bar = (pd.Timestamp("2026-10-05", tz="UTC") + pd.Timedelta(days=k)).isoformat()
        a = _alloc(cands, bar=bar, policy=policy)
        b = _alloc(list(reversed(cands)), bar=bar, policy=policy)
        assert a == b  # input/iteration order never matters
        won = [c["symbol"] for c in cands if a[c["intent_id"]]["decision"] == "ACCEPTED"]
        assert (
            len(won) == 3
            and len({a[c["intent_id"]]["notional"] for c in cands if c["symbol"] in won}) == 1
        )
        lost = [c for c in cands if c["symbol"] not in won]
        assert all(a[c["intent_id"]]["reasons"] == ["max_open_positions"] for c in lost)
        for s in won:
            picks[s] += 1
    assert all(60 <= n <= 140 for n in picks.values()), picks  # ~100 each: no name advantage


def test_proportional_scaling_when_headroom_is_short():
    held = {"BTC": {"side": 1, "strategy_id": "other", "notional": 4_500.0}}
    out = _alloc([_cand("ETH"), _cand("SOL", strategy="s2")], _snap(positions=held))
    accepted = [d for d in out.values() if d["decision"] == "ACCEPTED"]
    # gross room 6000 - 4500 = 1500 for two 2000 requests -> both scaled by the same 0.375
    assert len(accepted) == 2 and all(d["scale"] == pytest.approx(0.375) for d in accepted)
    assert sum(d["notional"] for d in accepted) == pytest.approx(1_500.0)
    # too little room for two minimum positions: the last in lottery order is dropped
    held["BTC"]["notional"] = 5_300.0
    out = _alloc([_cand("ETH"), _cand("SOL", strategy="s2")], _snap(positions=held))
    ranked = sorted(out.values(), key=lambda d: d["rank"])
    assert ranked[0]["decision"] == "ACCEPTED" and ranked[0]["notional"] == pytest.approx(700.0)
    assert ranked[1]["reasons"] == ["insufficient_headroom"]


def test_exposure_limits_and_gates():
    held = {"BTC": {"side": 1, "strategy_id": "s_long", "notional": 2_000.0},
            "ETH": {"side": 1, "strategy_id": "s_long", "notional": 2_000.0}}  # fmt: skip
    out = _alloc([_cand("SOL")], _snap(positions=held, cash=8_000.0))
    assert out["i_s_long_SOL"]["reasons"] == ["max_strategy_allocation"]
    held["SOL"] = {"side": -1, "strategy_id": "s_short", "notional": 2_000.0}
    out = _alloc([_cand("LINK", strategy="s3")], _snap(positions=held))
    assert out["i_s3_LINK"]["reasons"] == ["max_open_positions"]
    # cash reserve: 25% of equity must stay free after margin + fees
    out = _alloc([_cand("BTC")], _snap(cash=3_000.0))
    assert out["i_s_long_BTC"]["notional"] == pytest.approx(500 / (0.5 + 4.5e-4) - 0, rel=1e-6)
    gates = {
        "account_paused": _alloc([_cand("BTC")], _snap(status="PAUSED")),
        "account_killed": _alloc([_cand("BTC")], _snap(status="KILLED")),
        "daily_loss_limit": _alloc([_cand("BTC")], _snap(halted=True)),
        "missed_execution_window": _alloc([_cand("BTC", live=False)]),
        "stale_or_gapped_data": _alloc([_cand("BTC", data_ok=False)]),
        "promotion_ineligible:tier_eligible": _alloc(
            [_cand("BTC", promotion={"decision": "PAPER_INELIGIBLE", "blocked_by": ["tier_eligible"]})]),
    }  # fmt: skip
    for reason, out in gates.items():
        d = out["i_s_long_BTC"]
        assert d["decision"] == "REJECTED" and d["reasons"] == [reason], reason


def test_conflicts_are_deterministic_no_hedge_no_add():
    held = {"BTC": {"side": 1, "strategy_id": "s_long", "notional": 2_000.0}}
    out = _alloc([_cand("BTC", strategy="s_long2"), _cand("BTC", strategy="s_short", side=-1)],
                 _snap(positions=held))  # fmt: skip
    assert out["i_s_long2_BTC"]["reasons"] == ["duplicate_position_no_add"]
    assert out["i_s_short_BTC"]["reasons"] == ["conflicting_position"]
    out = _alloc([_cand("ETH"), _cand("ETH", strategy="s_short", side=-1)])
    assert all(d["reasons"] == ["conflicting_signals_same_bar"] for d in out.values())
    out = _alloc([_cand("ETH"), _cand("ETH", strategy="s_long2")])
    assert sorted(d["decision"] for d in out.values()) == ["ACCEPTED", "REJECTED"]
    loser = next(d for d in out.values() if d["decision"] == "REJECTED")
    assert loser["reasons"] == ["duplicate_signal_same_bar"] and loser["rank"] == 1


# --------------------------------------------------------------------------- account (integration)


def test_creation_freezes_identity_and_refuses_ineligible_or_second_runs(pap):
    dry = _create(pap, dry_run=True)
    assert dry["dry_run"] and _count(pap, "paper_runs") == 0
    run = _create(pap)
    assert run["run_id"] == dry["run_id"] and run["mode"] == "paper"
    assert run["first_processable_bar_close"] == day(1).isoformat()
    assert [m["strategy_name"] for m in run["cohort"]] == sorted(pap.names.values())
    st = engine.status(pap.store)[0]
    assert st["status"] == "ACTIVE" and st["equity"] == 10_000.0 and st["open_positions"] == 0
    with pytest.raises(PaperError, match="still open"):
        _create(pap, at=day(0, 4))
    # v1 is strict: synthetic evidence has no full research / validation registration
    with pytest.raises(PaperError, match=r"not paper-eligible.*full_research_adequate"):
        engine.create_run(pap.ledger, [pap.names["long"]], reason="x", origin="t", software=SW,
                          now=day(0, 5), exit_=TEST_EXIT, promotion=pol.promotion_policy(1))  # fmt: skip
    with pytest.raises(PaperError, match="no active Phase 8"):
        engine.create_run(pap.ledger, ["never"], reason="x", origin="t", software=SW, now=day(0, 5))
    with pytest.raises(PaperError, match="exit policy"):
        engine.create_run(pap.ledger, [pap.names["long"]], reason="x", origin="t", software=SW,
                          now=day(0, 5), promotion=TEST_PROMOTION, exit_=pol.exit_policy(1))  # 5d plan vs 10-bar exit  # fmt: skip
    engine.set_status(pap.store, run["run_id"], "STOPPED", reason="done", now=day(0, 6))
    with pytest.raises(PaperError, match="final"):
        engine.set_status(pap.store, run["run_id"], "ACTIVE", reason="again", now=day(0, 7))
    nxt = _create(pap, at=day(0, 8), continues=run["run_id"], label="second")
    assert nxt["run_id"] != run["run_id"]
    assert engine.load_run(pap.store, nxt["run_id"]).definition.continues == run["run_id"]


def test_prospective_only_no_signal_before_creation_is_traded(pap):
    pap.market.publish(8)  # signals on bars 1..8 already exist when the account is created
    run = _create(pap, at=day(8, 5))
    rid = run["run_id"]
    _live(pap, rid, 40, start=9)
    created = pd.Timestamp(day(8, 5))
    bar_events = [e for e in _events(pap, rid) if e["market_time"] is not None]
    assert bar_events and min(pd.Timestamp(e["market_time"]) for e in bar_events) == pd.Timestamp(
        day(9)
    )
    for e in _events(pap, rid, "intent_created"):
        assert pd.Timestamp(e["payload"]["signal_bar"]) > created
    assert _events(pap, rid, "account_mark")[0]["payload"]["bar_close"] == day(9).isoformat()


def test_signal_creates_one_intent_and_entry_fills_at_next_open(pap):
    run = _create(pap)
    rid = run["run_id"]
    k, sub = _until(pap, rid, "order_submitted")
    order = sub["payload"]["order"]
    coin, signal_bar = order["symbol"], sub["payload"]["signal_bar"]
    assert signal_bar == day(k).isoformat() and order["fill_bar_close"] == day(k + 1).isoformat()
    intents = [e for e in _events(pap, rid, "intent_created") if e["payload"]["symbol"] == coin
               and e["payload"]["signal_bar"] == signal_bar]  # fmt: skip
    assert len(intents) == 1
    assert not _events(pap, rid, "position_opened")  # nothing fills at the signal close
    # make T+1's open differ from T's close (synthetic opens equal the previous close)
    b = pap.market.bars[coin]
    i = b.index[b["close_time"] == pd.Timestamp(day(k + 1))][0]
    b.loc[i, "open"] = b.loc[i, "open"] * 1.01
    b.loc[i, "high"] = max(b.loc[i, "high"], b.loc[i, "open"])
    _cycle(pap, rid, k + 1)
    opened = _events(pap, rid, "position_opened")[0]["payload"]
    filled = next(
        e
        for e in _events(pap, rid, "order_filled")
        if e["payload"]["order_id"] == order["order_id"]
    )
    side = order["side"]
    ref = float(b.loc[i, "open"])
    assert (
        opened["entry_ref"]
        == pytest.approx(ref)
        != pytest.approx(float(_bar(pap, coin, day(k))["close"]))
    )
    assert opened["entry_fill"] == pytest.approx(
        ref * (1 + side * 2.0 / 1e4)
    )  # 2 bps frozen slippage
    assert (
        opened["entry_time"] == day(k).isoformat()
        and opened["entry_bar_close"] == day(k + 1).isoformat()
    )
    mark_k = next(
        e
        for e in _events(pap, rid, "account_mark")
        if e["payload"]["bar_close"] == day(k).isoformat()
    )
    assert opened["notional"] == pytest.approx(0.2 * mark_k["payload"]["equity"])
    assert opened["entry_fee"] == pytest.approx(opened["notional"] * 4.5e-4)
    assert opened["margin"] == pytest.approx(opened["notional"] / 2)
    assert (
        filled["payload"]["fill"]["execution_model_id"]
        == engine.load_run(pap.store, rid).execution.policy_id
    )


def test_rerun_is_idempotent_no_duplicate_fills_funding_or_exits(pap):
    run = _create(pap)
    rid = run["run_id"]
    _live(pap, rid, 30)
    before = _count(pap, "paper_events")
    for hours in (3.0, 9.0, 20.0):
        out = engine.cycle(pap.ledger, rid, software=SW, now=day(30, hours))
        assert out["bars_processed"] == [] and out["events"] == {}
    assert _count(pap, "paper_events") == before
    keys = [
        (e["payload"]["position_id"], e["payload"]["bar_close"])
        for e in _events(pap, rid, "funding_accrued")
    ]
    assert len(keys) == len(set(keys))
    closed = [e["payload"]["position_id"] for e in _events(pap, rid, "position_closed")]
    assert len(closed) == len(set(closed))


def test_crash_mid_commit_leaves_nothing_and_recovers_identically(pap, monkeypatch):
    run = _create(pap)
    rid = run["run_id"]
    _live(pap, rid, 10)
    pap.market.publish(14)
    plan = engine.cycle(pap.ledger, rid, software=SW, now=day(14, 3), dry_run=True)["would_record"]
    assert plan
    before = (_count(pap, "paper_events"), _count(pap, "paper_cycles"))
    real = engine._insert

    def crash(store, run_id, created, cycle_id, new, now):
        real(store, run_id, created, cycle_id, new[:1], now)
        raise RuntimeError("power cut")

    monkeypatch.setattr(engine, "_insert", crash)
    with pytest.raises(RuntimeError, match="power cut"):
        engine.cycle(pap.ledger, rid, software=SW, now=day(14, 3))
    assert (_count(pap, "paper_events"), _count(pap, "paper_cycles")) == before
    monkeypatch.setattr(engine, "_insert", real)
    engine.cycle(pap.ledger, rid, software=SW, now=day(14, 3))
    after = _events(pap, rid)[-len(plan) :]
    assert [(e["event_key"], e["payload"]) for e in after] == [
        (e["event_key"], e["payload"]) for e in plan
    ]
    # restart: the account is rebuilt from the ledger alone
    assert replay(engine.events(pap.store, rid)).cash == engine.account(pap.store, rid).cash


def test_hand_calculated_long_and_short_trades(pap):
    run = _create(pap)
    rid = run["run_id"]
    _live(pap, rid, 60)
    trades = engine.trades(pap.store, rid)
    sides = {t["side"] for t in trades}
    assert sides == {1, -1}, "need a long and a short trade"
    funding = {}
    for e in _events(pap, rid, "funding_accrued"):
        funding[e["payload"]["position_id"]] = (
            funding.get(e["payload"]["position_id"], 0) + e["payload"]["amount"]
        )
    for t in trades:
        s, coin = t["side"], t["symbol"]
        entry_ref = float(
            _bar(pap, coin, pd.Timestamp(t["entry_time"]) + pd.Timedelta(days=1))["open"]
        )
        exit_ref = float(_bar(pap, coin, t["exit_time"])["close"])
        assert pd.Timestamp(t["exit_time"]) == pd.Timestamp(t["signal_bar"]) + pd.Timedelta(days=5)
        assert t["bars_held"] == 5 and t["reason"] == "time_exit"
        e_fill, x_fill = entry_ref * (1 + s * 2e-4), exit_ref * (1 - s * 2e-4)
        units = t["notional"] / e_fill
        fees = 4.5e-4 * units * (e_fill + x_fill)
        assert t["entry_fill"] == pytest.approx(e_fill) and t["exit_fill"] == pytest.approx(x_fill)
        assert t["fees"] == pytest.approx(fees)
        assert t["gross_pnl"] == pytest.approx(s * units * (exit_ref - entry_ref))
        assert t["funding"] == pytest.approx(funding[t["position_id"]])
        assert t["net_pnl"] == pytest.approx(
            s * units * (x_fill - e_fill) - fees - funding[t["position_id"]]
        )
        assert t["slippage_cost"] == pytest.approx(t["gross_pnl"] - s * units * (x_fill - e_fill))


def test_funding_settled_rates_charged_once_with_correct_sign(pap):
    run = _create(pap)
    rid = run["run_id"]
    _live(pap, rid, 30)
    evs = _events(pap, rid, "funding_accrued")
    assert evs
    opened = {
        e["payload"]["position_id"]: e["payload"] for e in _events(pap, rid, "position_opened")
    }
    for e in evs:
        p = e["payload"]
        pos = opened[p["position_id"]]
        f = pap.market.funding[p["symbol"]]
        bar = pd.Timestamp(p["bar_close"])
        rows = f[(f["time"] > bar - pd.Timedelta(days=1)) & (f["time"] <= bar)]
        close = float(_bar(pap, p["symbol"], bar)["close"])
        expect = sum(pos["side"] * pos["units"] * close * r for r in rows["funding_rate"])
        assert p["amount"] == pytest.approx(expect) and p["present"] == 24 and p["missing"] == 0
        # long pays positive funding, short receives it
        total_rate = rows["funding_rate"].sum()
        assert (p["amount"] > 0) == (pos["side"] * total_rate > 0)


def test_missing_funding_is_counted_never_estimated(pap):
    run = _create(pap)
    rid = run["run_id"]
    k, ev = _until(pap, rid, "position_opened")
    coin = ev["payload"]["symbol"]
    pap.market.publish(k + 1)
    pap.store.con.execute("DELETE FROM perp_funding WHERE coin=? AND time > ? AND time <= ?",
                          [coin, day(k, 5), day(k, 8)])  # three hourly settlements lost  # fmt: skip
    engine.cycle(pap.ledger, rid, software=SW, now=day(k + 1, 3))
    p = next(e["payload"] for e in _events(pap, rid, "funding_accrued")
             if e["payload"]["symbol"] == coin and e["payload"]["bar_close"] == day(k + 1).isoformat())  # fmt: skip
    assert (p["present"], p["missing"]) == (21, 3)
    assert engine.paper_summary(pap.store, rid)["data"]["funding_missing_settlements"] == 3


def test_stale_data_blocks_and_waits_never_invents(pap):
    run = _create(pap)
    rid = run["run_id"]
    k, ev = _until(pap, rid, "position_opened")
    coin = ev["payload"]["symbol"]
    others = [c for c in pap.market.bars if c != coin]
    pap.market.publish(k + 1, coins=others)  # the held coin's bar is missing
    out = engine.cycle(pap.ledger, rid, software=SW, now=day(k + 1, 3))
    assert out["bars_processed"] == [] and out["notes"][0]["state"] == "WAITING_FOR_DATA"
    out = engine.cycle(pap.ledger, rid, software=SW, now=day(k + 1, 15))  # past the window
    assert out["bars_processed"] == []
    issue = _events(pap, rid, "data_issue")
    assert len(issue) == 1 and issue[0]["payload"]["kind"] == "open_positions_unmarked"
    engine.cycle(pap.ledger, rid, software=SW, now=day(k + 1, 16))
    assert len(_events(pap, rid, "data_issue")) == 1  # recorded once
    _publish_one(pap, coin, k + 1)  # data arrives late: the bar is marked, signals are too late
    out = engine.cycle(pap.ledger, rid, software=SW, now=day(k + 1, 18))
    assert out["bars_processed"] == [day(k + 1).isoformat()]
    late = [
        e for e in _events(pap, rid, "risk_decision") if e["market_time"] == day(k + 1).isoformat()
    ]
    assert all("missed_execution_window" in e["payload"]["reasons"] for e in late)


def test_signal_seen_after_the_entry_window_is_skipped_not_filled(pap):
    run = _create(pap)
    rid = run["run_id"]
    for k in range(1, 60):
        pap.market.publish(k)
        dry = engine.cycle(pap.ledger, rid, software=SW, now=day(k, 13), dry_run=True)
        if any(e["event_type"] == "intent_created" for e in dry["would_record"]):
            break
        engine.cycle(pap.ledger, rid, software=SW, now=day(k, 3))
    engine.cycle(pap.ledger, rid, software=SW, now=day(k, 13))  # 13 h > 12 h window
    risk = [e for e in _events(pap, rid, "risk_decision") if e["market_time"] == day(k).isoformat()]
    assert risk and all(e["payload"]["reasons"][0] == "missed_execution_window" for e in risk)
    assert not [
        e for e in _events(pap, rid, "order_submitted") if e["market_time"] == day(k).isoformat()
    ]


def test_pause_blocks_entries_and_resume_never_replays(pap):
    run = _create(pap)
    rid = run["run_id"]
    engine.set_status(pap.store, rid, "PAUSED", reason="holiday", now=day(0, 4))
    _live(pap, rid, 15)
    assert not _events(pap, rid, "order_submitted")
    paused = _events(pap, rid, "risk_decision")
    assert paused and all("account_paused" in e["payload"]["reasons"] for e in paused)
    engine.set_status(pap.store, rid, "ACTIVE", reason="back", now=day(15, 4))
    _live(pap, rid, 40, start=16)
    subs = [
        e
        for e in _events(pap, rid, "order_submitted")
        if e["payload"]["order"]["purpose"] == "entry"
    ]
    assert subs and all(
        pd.Timestamp(e["payload"]["signal_bar"]) > pd.Timestamp(day(15)) for e in subs
    )
    with pytest.raises(PaperError, match="already ACTIVE"):
        engine.set_status(pap.store, rid, "ACTIVE", reason="x", now=day(40, 5))


def test_stop_manages_open_positions_to_their_exits(pap):
    run = _create(pap)
    rid = run["run_id"]
    k, ev = _until(pap, rid, "position_opened")
    engine.set_status(pap.store, rid, "STOPPED", reason="end of study", now=day(k, 4))
    _live(pap, rid, k + 8, start=k + 1)
    st = engine.account(pap.store, rid)
    assert st.status == "STOPPED" and not st.positions
    assert ev["payload"]["position_id"] in {t["position_id"] for t in st.closed}
    assert not [e for e in _events(pap, rid, "order_submitted") if e["payload"]["order"]["purpose"] == "entry"
                and pd.Timestamp(e["market_time"]) > pd.Timestamp(day(k))]  # fmt: skip
    out = engine.run_all(
        pap.ledger, software=SW, sender_factory=lambda: lambda t: None, now=day(k + 9, 3)
    )
    assert out["runs"] == []  # terminal and flat: nothing left to manage


def test_daily_loss_halt_blocks_new_entries(pap):
    tight = pol.RiskPolicy(version=96, daily_loss_halt_fraction=1e-9)
    run = _create(pap, risk=tight)
    rid = run["run_id"]
    _live(pap, rid, 45)
    halts = _events(pap, rid, "kill_switch")
    assert halts and {e["payload"]["kind"] for e in halts} == {"daily_loss_halt"}
    halted = {e["payload"]["bar_close"] for e in halts}
    for e in _events(pap, rid, "risk_decision"):
        if e["market_time"] in halted:
            assert "daily_loss_limit" in e["payload"]["reasons"]
    assert not [e for e in _events(pap, rid, "order_submitted")
                if e["market_time"] in halted and e["payload"]["order"]["purpose"] == "entry"]  # fmt: skip
    assert engine.account(pap.store, rid).status == "ACTIVE"  # a halt is per bar, not a kill


def test_drawdown_kill_stops_new_entries_but_manages_positions(pap):
    tight = pol.RiskPolicy(version=95, max_drawdown_kill_fraction=1e-9)
    run = _create(pap, risk=tight)
    rid = run["run_id"]
    _live(pap, rid, 40)
    st = engine.account(pap.store, rid)
    assert st.status == "KILLED"
    kill = next(
        e for e in _events(pap, rid, "kill_switch") if e["payload"]["kind"] == "drawdown_kill"
    )
    status = _events(pap, rid, "status_changed")[-1]["payload"]
    assert status["status"] == "KILLED" and status["actor"] == "automatic"
    later = [e for e in _events(pap, rid, "order_submitted") if e["payload"]["order"]["purpose"] == "entry"
             and pd.Timestamp(e["market_time"]) >= pd.Timestamp(kill["market_time"])]  # fmt: skip
    assert later == [] and not st.positions  # open positions ran to their exits
    with pytest.raises(PaperError, match="final"):
        engine.set_status(pap.store, rid, "ACTIVE", reason="undo", now=day(40, 5))


def test_liquidation_is_recorded_and_loses_the_isolated_margin(pap):
    run = _create(pap)
    rid = run["run_id"]
    k, ev = _until(pap, rid, "position_opened")
    p = ev["payload"]
    b = pap.market.bars[p["symbol"]]
    i = b.index[b["close_time"] == pd.Timestamp(day(k + 1))][0]
    if p["side"] > 0:
        b.loc[i, "low"] = b.loc[i, "low"] * 0.3  # a wick far through the ~-48% liquidation
    else:
        b.loc[i, "high"] = b.loc[i, "high"] * 3.0
    cash_before = engine.account(pap.store, rid).cash
    _cycle(pap, rid, k + 1)
    liq = [
        e
        for e in _events(pap, rid, "liquidation")
        if e["payload"]["position_id"] == p["position_id"]
    ]
    assert len(liq) == 1 and "not Hyperliquid liquidation parity" in liq[0]["payload"]["caveat"]
    closed = next(
        t for t in engine.account(pap.store, rid).closed if t["position_id"] == p["position_id"]
    )
    assert closed["reason"] == "liquidation" and closed["proceeds"] == 0.0
    assert closed["net_pnl"] == pytest.approx(-(p["margin"] + p["entry_fee"]))
    st = engine.account(pap.store, rid)
    assert st.cash == pytest.approx(cash_before - sum(  # only new entries moved cash besides
        e["payload"]["margin"] + e["payload"]["entry_fee"] for e in _events(pap, rid, "position_opened")
        if e["market_time"] == day(k + 1).isoformat()))  # fmt: skip


def test_repeated_engine_errors_pause_the_run(pap, monkeypatch):
    run = _create(pap)
    rid = run["run_id"]

    def boom(self):
        raise RuntimeError("feature store unreadable")

    monkeypatch.setattr(engine._Cycle, "run", boom)
    sender = []
    for h in range(3):
        out = engine.run_all(
            pap.ledger, software=SW, sender_factory=lambda: sender.append, now=day(1, 3 + h)
        )
        assert out["runs"][0]["status"] == "error"
    assert engine.account(pap.store, rid).status == "PAUSED"
    assert _events(pap, rid, "status_changed")[-1]["payload"]["actor"] == "automatic"
    assert sum("Paper engine error" in t for t in sender) == 1  # the streak start, once
    assert any("Paper run PAUSED" in t for t in sender)
    engine.run_all(pap.ledger, software=SW, sender_factory=lambda: sender.append, now=day(1, 7))
    assert len(_events(pap, rid, "status_changed")) == 1  # no second automatic pause


# --------------------------------------------------------------------------- notifications


def test_notifications_are_labelled_paper_and_never_affect_trading(pap):
    run = _create(pap)
    rid = run["run_id"]
    sent, fail = [], {"n": 1}

    def sender(text):
        if fail["n"]:
            fail["n"] -= 1
            raise RuntimeError("Telegram unreachable")
        sent.append(text)

    for k in range(1, 30):
        pap.market.publish(k)
        engine.run_all(pap.ledger, software=SW, sender_factory=lambda: sender, now=day(k, 3))
    assert sent and all(
        t.startswith("🧪 <b>PAPER · SIMULATED</b> — no real order was placed") for t in sent
    )
    for word in ("BUY", "SELL", "copilot", "WATCH"):
        assert all(word not in t for t in sent)
    opened = _events(pap, rid, "position_opened")
    closed = _events(pap, rid, "position_closed")
    states = pap.store.con.execute("SELECT subject_id, status FROM paper_notifications").fetchall()
    sent_ids = {s for s, st in states if st == "sent"}
    assert {e["event_id"] for e in opened + closed} <= sent_ids  # the failed one was retried
    assert any(st == "failed" for _, st in states)
    n = len(sent)
    engine.run_all(pap.ledger, software=SW, sender_factory=lambda: sender, now=day(29, 9))
    assert len(sent) == n  # never resent


def test_unknown_delivery_outcome_is_never_resent(pap):
    run = _create(pap)
    rid = run["run_id"]
    k, ev = _until(pap, rid, "position_opened")
    pap.store.con.execute("INSERT INTO paper_notifications VALUES (?,?,?,?,?,?,?,?,?)",
                          ["n1", rid, ev["event_id"], 1, "attempted", "telegram", "x", None, day(k, 3)])  # fmt: skip
    sent = []
    engine.notify(pap.store, lambda: sent.append, day(k, 4))
    assert not any("Paper position opened" in t and ev["payload"]["symbol"] in t for t in sent)


# --------------------------------------------------------------------------- evidence & neutrality


def test_paper_evidence_matches_the_ledger(pap):
    run = _create(pap)
    rid = run["run_id"]
    _live(pap, rid, 45)
    s = engine.paper_summary(pap.store, rid)
    closed = _events(pap, rid, "position_closed")
    assert s["stage"] == "paper_execution" and s["trades"]["closed"] == len(closed) > 0
    assert s["reconciles_with_ledger"] is True
    assert s["trades"]["net_pnl"] == pytest.approx(sum(e["payload"]["net_pnl"] for e in closed))
    fills = sum(e["payload"]["fill"]["fee"] for e in _events(pap, rid, "order_filled"))
    open_fees = sum(p["entry_fee"] for p in engine.account(pap.store, rid).positions.values())
    assert s["trades"]["fees"] == pytest.approx(fills - open_fees)
    marks = _events(pap, rid, "account_mark")
    assert s["observation"]["observed_days"] == len(marks) == 45
    assert s["account"]["max_drawdown"] == pytest.approx(
        max(m["payload"]["drawdown"] for m in marks)
    )
    assert s["maturity"]["level"] == "WARMUP"
    assert sum(g["trades"] for g in s["trades"]["by_strategy"].values()) == len(closed)
    assert "p_value" not in json.dumps(s) and "significan" not in json.dumps(s).replace(
        "no significance", ""
    )
    a = engine.record_evidence(pap.store, rid, now=day(45, 5))
    b = engine.record_evidence(pap.store, rid, now=day(45, 6))
    assert a["summary_id"] == b["summary_id"] and _count(pap, "paper_evidence") == 1


def test_phase8_copilot_and_research_records_are_untouched(pap):
    lab_before = _digest(pap, "lab_")
    copilot_before = _digest(pap, "copilot_")
    run = _create(pap)
    rid = run["run_id"]
    _live(pap, rid, 30)
    engine.record_evidence(pap.store, rid, now=day(30, 5))
    assert _digest(pap, "lab_") == lab_before  # tiers, profiles, forward rows: unchanged
    assert _digest(pap, "copilot_") == copilot_before
    # the forward tracker still observes the same bars on its own afterwards
    out = fwd.check(pap.ledger, software=SW, now=day(30, 6))
    assert out["recorded"] > 0


def test_consumers_do_not_import_each_other():
    for f in (SRC / "paper").glob("*.py"):
        assert not any(m.startswith("market_signal.copilot") for m in _imports(f)), f
    for pkg in ("copilot", "research"):
        for f in (SRC / pkg).rglob("*.py"):
            assert not any(m.startswith("market_signal.paper") for m in _imports(f)), f


def test_cli_paper_lifecycle(pap):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    path = pap.store.path
    pap.store.close()
    runner = CliRunner()
    base = ["--db", str(path), "lab", "paper"]

    def run(*args, code=0):
        res = runner.invoke(app, [*base, *args])
        assert res.exit_code == code, res.output
        out = res.output.strip()
        return json.loads(out) if code == 0 and out.startswith(("{", "[")) else res.output

    pol_out = run("policy")
    assert (
        pol_out["promotion"]["name"] == "autotrader_policy" and pol_out["risk"]["mode"] == "paper"
    )
    names = [pap.names["long"], pap.names["short"]]
    vers = ["--promotion-version", "99", "--exit-version", "99"]
    dry = run("create", *names, "--reason", "cli", "--dry-run", *vers)
    assert dry["dry_run"] is True and run("runs") == []
    run(
        "create",
        *names,
        "--reason",
        "cli",
        "--promotion-version",
        "1",
        "--exit-version",
        "99",
        code=1,
    )  # v1 promotion refuses synthetic evidence
    made = run("create", *names, "--reason", "cli", *vers)
    rid = made["run_id"]
    assert [s["run_id"] for s in run("runs")] == [rid]
    # real clock: the synthetic market ended long ago and the run was created now
    out = run("run", "--dry-run")
    assert out["dry_run"] is True and out["runs"][0]["bars_processed"] == []
    out = run("run")
    assert out["runs"][0]["status"] == "ok"
    assert run("positions", "--json") == [] and run("trades", "--json") == []
    assert [e["event_type"] for e in run("events")] == ["run_created"]
    run("pause", rid, "--reason", "x")
    run("resume", rid, "--reason", "y")
    assert run("summary")["maturity"]["level"] == "WARMUP"
    assert run("evidence")["stage"] == "paper_execution"
    run("stop", rid, "--reason", "z")
    run("resume", rid, "--reason", "again", code=1)
    run("run", "--now", "2030-01-01T00:00:00+00:00", code=1)  # --now only inspects


def test_fast_funding_readiness_equals_the_forward_trackers(env):
    from market_signal.paper.engine import _snapshot_funding_ready

    env.market.publish(5)
    for coin in ("BTC", "ETH"):
        for k in range(-3, 7):
            snap = fwd.live_snapshot(env.store, "hyperliquid", coin, day(k), 120)
            for bar in (day(k), day(k, -1), day(k, 1)):
                assert fwd.funding_ready(snap, coin, bar) == _snapshot_funding_ready(
                    snap, coin, bar
                )


def test_a_dead_cohort_asset_does_not_block_entries_elsewhere(pap):
    run = _create(pap)
    rid = run["run_id"]
    _live(pap, rid, 3)
    held = set(engine.account(pap.store, rid).positions)
    dead = next(c for c in sorted(pap.market.bars) if c not in held)
    others = [c for c in pap.market.bars if c != dead]
    for k in range(4, 35):
        pap.market.publish(k, coins=others)
        engine.cycle(pap.ledger, rid, software=SW, now=day(k, 3))
    issues = [e["payload"] for e in _events(pap, rid, "data_issue")]
    assert issues and all(dead in i["symbols"] for i in issues)
    assert any(dead in i["stale"] for i in issues)
    marks = {e["payload"]["bar_close"] for e in _events(pap, rid, "account_mark")}
    assert day(34).isoformat() in marks  # the account kept running daily
    entries = [e for e in _events(pap, rid, "order_submitted")
               if e["payload"]["order"]["purpose"] == "entry"
               and pd.Timestamp(e["payload"]["signal_bar"]) >= pd.Timestamp(day(5))]  # fmt: skip
    assert entries and all(e["payload"]["order"]["symbol"] != dead for e in entries)


def test_evidence_chain_is_point_in_time(pap):
    from market_signal.research.lab.evidence import EvidenceProfile

    base = pap.profile["long"]["profile_id"]
    row = pap.store.con.execute("SELECT payload FROM lab_evidence_profiles WHERE profile_id=?",
                                [base]).fetchone()  # fmt: skip
    p = EvidenceProfile.model_validate_json(row[0])
    full = {"status": "FULL_RESEARCH_MIXED"}
    ext = p.model_copy(update={"profile_schema": "4", "extends": base, "full_research": full,
                               "sources": (*p.sources, p.sources[0].model_copy(update={"stage": "full_research"}))})  # fmt: skip
    ext = EvidenceProfile.model_validate(ext.model_dump())
    pap.store.con.execute(
        "INSERT INTO lab_evidence_profiles (profile_id, policy_id, strategy_id, analysis_id, tier, "
        "payload, recorded_at) SELECT ?, policy_id, strategy_id, analysis_id, tier, ?, ? "
        "FROM lab_evidence_profiles WHERE profile_id=?",
        [ext.profile_id, ext.model_dump_json(), day(3), base],
    )
    assert engine.evidence_chain(pap.store, base, day(2)) == [base]
    assert engine.evidence_chain(pap.store, base, day(4)) == [base, ext.profile_id]
