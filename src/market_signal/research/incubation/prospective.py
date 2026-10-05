"""Prospective candidate incubation: the freeze, daily candidate snapshots, shadow intents.

Rules that keep it prospective (the Phase 8 forward-tracker discipline):

- **Freeze first.** ``freeze`` stores the full content-addressed definition (candidate pool,
  the three policy versions, shadow execution, costs, cadence, semantic versions) with its
  registration time. Only bars closing strictly after that time are ever evaluated.
- **Live window only.** Daily bar T is evaluated only while ``T <= now < T + 1 day``. A bar
  whose window passed unobserved is never evaluated later (a coverage gap).
- **Write once.** Every (freeze, strategy, bar) evaluation, every (evaluation, policy)
  decision, every transition, shadow intent and shadow outcome has a deterministic ID and a
  UNIQUE key; re-running is a no-op; nothing is updated or deleted. Future data can never
  change a recorded decision.
- **Decisions are causal.** The decision at T is the frozen policy's replay over outcomes
  resolved by T (``machine.replay``). In live operation CONFIRMED_PAPER is decided from the
  RECORDED shadow outcomes of the current episode only; retrospective outcomes never count
  toward graduation.
- **Shadow only.** An admitted candidate's independent signal at T becomes a shadow intent
  (direction, entry at T+1's open, fixed standardised notional, frozen costs, exit at the
  close of T+h, no stop). Its outcome is written once, when final. No order, no account, no
  exchange; the Phase 12 paper account is never read or written here.

Evidence is always reported in three parts and never blended into one statistic:
retrospective (signals before the freeze), prospective (all candidate signals after the
freeze, admitted or not) and shadow (the recorded intents' outcomes).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import uuid4

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from market_signal.models.domain import AssetClass, utcnow
from market_signal.perps.backtest import PerpCosts, side_forward_returns
from market_signal.research.incubation import evidence as ie
from market_signal.research.incubation import inputs as ip
from market_signal.research.incubation import policy as pm
from market_signal.research.incubation.machine import replay
from market_signal.research.incubation.opportunity import opportunity_rate
from market_signal.research.incubation.pool import PoolRule, balance, members, pool_id
from market_signal.research.incubation.replay import VenueReplay
from market_signal.research.lab.common import (
    LabModel,
    Name,
    PositiveInt,
    Text,
    canonical_json,
    content_id,
)
from market_signal.research.lab.compiler import COMPILER_VERSION, build_inputs
from market_signal.research.lab.datasets import SeriesSelection
from market_signal.research.lab.forward import (
    AVAILABILITY_SLACK,
    FUNDING_CONTEXT,
    _snapshot,
    funding_ready,
    in_live_window,
    newest_bar,
)
from market_signal.research.lab.ledger import Ledger, LedgerError
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.lab.spec import StrategyDefinition
from market_signal.research.lab.vocabulary import VOCABULARY_VERSION
from market_signal.research.lifecycle import estimators as es
from market_signal.research.lifecycle.machine import activation as lifecycle_activation
from market_signal.research.lifecycle.state import core_evidence
from market_signal.research.lifecycle.study import StrategyRef
from market_signal.research.structure.study.run import clean
from market_signal.research.structure.study.spec import CostRef

FREEZE_VERSION = "incubation_freeze_v1"
RUNNER_VERSION = "incubation_runner_v1"
SHADOW_RESOLVER_VERSION = "incubation_shadow_resolver_v1"
STATEMENT = ("Candidate incubation is exploratory paper admission. False candidate activations "
             "cost no capital and are useful observations. No Phase 21 state authorizes real "
             "trading.")  # fmt: skip
PROSPECTIVE_VENUE = VenueReplay(
    venue="hyperliquid", coins=("AAVE", "BTC", "ETH", "HYPE", "LINK", "SOL"),
    bars_start="2022-08-01T00:00:00Z", funding_start="2023-10-01T00:00:00Z",
    first_evaluation="2024-04-01T00:00:00Z",
)  # fmt: skip


class IncubationError(LedgerError):
    pass


def semantics() -> dict[str, str]:
    return {"methodology": pm.METHODOLOGY_VERSION, "machine": pm.MACHINE_VERSION,
            "evidence": pm.EVIDENCE_VERSION, "graduation": pm.GRADUATION_VERSION,
            "opportunity": pm.OPPORTUNITY_VERSION, "runner": RUNNER_VERSION,
            "shadow_resolver": SHADOW_RESOLVER_VERSION, "compiler": COMPILER_VERSION,
            "vocabulary": VOCABULARY_VERSION, "returns": "perps.backtest.side_forward_returns",
            "independence": "backtest.events.decluster",
            "outcome_ledger": "lifecycle.outcomes.strategy_outcomes"}  # fmt: skip


class IncubationFreeze(LabModel):
    """Everything prospective collection depends on. ``freeze_id`` hashes all of it."""

    schema_version: Literal["1"] = "1"
    freeze_version: Literal["incubation_freeze_v1"] = FREEZE_VERSION
    name: Name = "phase21_incubation_v1"
    evidence_class: Literal["EXPLORATORY"] = "EXPLORATORY"
    grants_live: Literal[False] = False
    consumers: Literal["none"] = "none"
    statement: Literal[STATEMENT] = STATEMENT  # type: ignore[valid-type]
    pool_rule: PoolRule
    pool_id: Text
    members: Annotated[tuple[StrategyRef, ...], Field(min_length=1)]
    policies: tuple[dict, ...]
    policy_ids: dict[str, str]
    venue: VenueReplay
    reference_coin: Literal["BTC"] = "BTC"
    horizon_bars: PositiveInt = 10
    costs: tuple[CostRef, ...]
    execution: pm.ShadowExecution = pm.ShadowExecution()
    cadence: pm.Cadence = pm.Cadence()
    semantics: dict[str, str]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if pool_id(self.pool_rule, self.members) != self.pool_id:
            raise ValueError("pool members do not reproduce the pool ID")
        if sorted(self.policy_ids) != sorted(pm.PROFILES):
            raise ValueError("a freeze carries exactly the three profiles")
        for raw in self.policies:
            p = (pm.ConservativeBenchmark if raw.get("profile") == "CONSERVATIVE"
                 else pm.IncubationPolicy).model_validate(raw)  # fmt: skip
            if self.policy_ids[p.profile] != p.policy_id:
                raise ValueError(f"{p.profile}: embedded policy does not reproduce its ID")
        if {(c.venue, c.coin) for c in self.costs} != {
            (self.venue.venue, c) for c in self.venue.coins
        }:
            raise ValueError("exactly one frozen cost per venue coin")
        if self.execution.horizon_bars != self.horizon_bars:
            raise ValueError("shadow exit horizon must equal the evidence horizon")
        return self

    @property
    def freeze_id(self) -> str:
        return content_id("incfreeze_", self.model_dump(mode="json"))

    def policy_objects(self) -> dict[str, pm.AnyPolicy]:
        out = {}
        for raw in self.policies:
            p = (pm.ConservativeBenchmark if raw.get("profile") == "CONSERVATIVE"
                 else pm.IncubationPolicy).model_validate(raw)  # fmt: skip
            out[p.profile] = p
        return out

    def cost(self, coin: str) -> CostRef:
        return next(c for c in self.costs if c.coin == coin)


def build_freeze(perps_cfg: dict, catalogue_dir: Path, venue: VenueReplay = PROSPECTIVE_VENUE) -> IncubationFreeze:  # fmt: skip
    from market_signal.perps.backtest import perp_costs

    rule = PoolRule()
    refs = members(rule, catalogue_dir)
    costs = tuple(CostRef(venue=venue.venue, coin=c, fee_bps=perp_costs(perps_cfg, c, venue.venue).fee_bps,
                          slippage_bps=perp_costs(perps_cfg, c, venue.venue).slippage_bps)
                  for c in venue.coins)  # fmt: skip
    return IncubationFreeze(
        pool_rule=rule, pool_id=pool_id(rule, refs), members=refs,
        policies=tuple(pm.POLICIES[p].model_dump(mode="json") for p in pm.PROFILES),
        policy_ids={p: pm.POLICIES[p].policy_id for p in pm.PROFILES}, venue=venue,
        costs=costs, semantics=semantics(),
    )  # fmt: skip


# --------------------------------------------------------------------------- storage helpers


def _ts(v) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _rows(store, sql: str, args: list | None = None) -> list[dict]:
    cur = store.con.execute(sql, args or [])
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _require(store) -> None:
    if not store.con.execute("SELECT 1 FROM information_schema.tables "
                             "WHERE table_name='incubation_freezes'").fetchone():  # fmt: skip
        raise IncubationError("incubation tables are absent; open Store writable once to migrate")


def _json(value) -> str:
    """Canonical JSON of a payload (numpy scalars and non-finite floats normalised)."""
    return canonical_json(clean(value))


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha256(canonical_json(list(parts)).encode()).hexdigest()


def freezes(store) -> list[dict]:
    _require(store)
    out = []
    for r in _rows(store, "SELECT * FROM incubation_freezes ORDER BY registered_at, freeze_id"):
        d = IncubationFreeze.model_validate_json(r["definition"])
        if d.freeze_id != r["freeze_id"]:
            raise IncubationError(f"stored freeze {r['freeze_id']} does not match its content")
        out.append({**r, "definition": d, "registered_at": _ts(r["registered_at"])})
    return out


def freeze(ledger: Ledger, defn: IncubationFreeze, *, reason: str, origin: str,
           software: SoftwareIdentity, now: datetime | None = None) -> dict:  # fmt: skip
    """Register the frozen definition. Prospective collection starts at ``registered_at``.
    Registering the same definition twice is refused (it would move its start)."""
    store = ledger.store
    _require(store)
    fid = defn.freeze_id
    if store.con.execute("SELECT 1 FROM incubation_freezes WHERE freeze_id=?", [fid]).fetchone():
        raise IncubationError(f"{fid} is already frozen; its prospective start cannot move")
    sw = ledger.register_software(software)
    at = _ts(now or utcnow())
    with store.transaction():
        store.con.execute("INSERT INTO incubation_freezes VALUES (?,?,?,?,?,?,?,?)",
                          [fid, defn.name, at.to_pydatetime(), reason, origin, sw, False,
                           canonical_json(defn.model_dump(mode="json"))])  # fmt: skip
    return {"freeze_id": fid, "registered_at": at.isoformat(), "pool_id": defn.pool_id,
            "policy_ids": defn.policy_ids, "members": len(defn.members)}  # fmt: skip


# --------------------------------------------------------------------------- daily run


def _selections(d: IncubationFreeze, coin: str, end) -> tuple[SeriesSelection, ...]:
    v = d.venue
    return (SeriesSelection(kind="perp_bars", symbol=coin, source=v.venue, timeframe="1d",
                            start=v.bars_start, end=end.to_pydatetime()),
            SeriesSelection(kind="perp_funding", symbol=coin, source=v.venue,
                            start=v.funding_start, end=end.to_pydatetime()))  # fmt: skip


def _common_bar(store, d: IncubationFreeze, now) -> pd.Timestamp | None:
    bars = [newest_bar(store, d.venue.venue, c, now) for c in d.venue.coins]
    return None if any(b is None for b in bars) else min(bars)


def _prior_decisions(store, fid: str, bar) -> dict[tuple[str, str], dict]:
    """Latest recorded decision per (profile, strategy) strictly before ``bar``."""
    rows = _rows(store, """
        SELECT e.strategy_id, d.profile, d.level, d.episode_id, e.bar_close
        FROM incubation_decisions d JOIN incubation_evaluations e USING (evaluation_id)
        WHERE e.freeze_id=? AND e.bar_close<?
        QUALIFY row_number() OVER (PARTITION BY e.strategy_id, d.profile
                                   ORDER BY e.bar_close DESC) = 1""",
                 [fid, bar.to_pydatetime()])  # fmt: skip
    return {(r["profile"], r["strategy_id"]): r for r in rows}


def _shadow(store, fid: str, profile: str, strategy_id: str, episode_id: str | None,
            asof) -> tuple[ie.Est, int, dict]:  # fmt: skip
    """Recorded shadow outcomes resolved by ``asof``: (current episode estimate, unavailable +
    missed count in the episode, summary over every episode of this candidate)."""
    rows = _rows(store, """
        SELECT i.episode_id, i.asset, i.status AS intent_status, i.signal_bar_close,
               o.status, o.net, o.exit_close
        FROM incubation_intents i LEFT JOIN incubation_outcomes o USING (intent_id)
        WHERE i.freeze_id=? AND i.profile=? AND i.strategy_id=?""",
                 [fid, profile, strategy_id])  # fmt: skip
    asof = _ts(asof)
    done = [r for r in rows if r["status"] == "resolved" and _ts(r["exit_close"]) <= asof]

    def est(rs):
        if not rs:
            return ie.estimate(np.zeros(0), np.zeros(0, dtype=np.int64), np.zeros(0),
                               np.zeros(0), ("",))  # fmt: skip
        names = tuple(sorted({r["asset"] for r in rs}))
        code = {a: i for i, a in enumerate(names)}
        net = np.array([r["net"] for r in rs], dtype=float)
        days = np.array([es.to_days(r["signal_bar_close"]) for r in rs])
        return ie.estimate(net, np.array([code[r["asset"]] for r in rs], dtype=np.int64),
                           np.floor(days / 7), net.copy(), names)  # fmt: skip

    cur = [r for r in done if r["episode_id"] == episode_id] if episode_id else []
    bad = sum(1 for r in rows if r["episode_id"] == episode_id and episode_id
              and (r["intent_status"] == "missed_entry_window" or r["status"] == "unavailable"))  # fmt: skip
    pending = sum(1 for r in rows if r["intent_status"] == "entered" and r["status"] is None)
    return est(cur), bad, {"intents": len(rows), "outcomes": len(done), "pending": pending,
                           "all_episodes": est(done).brief(), "current_episode": est(cur).brief()}  # fmt: skip


def run(ledger: Ledger, *, software: SoftwareIdentity, now: datetime | None = None,
        dry_run: bool = False) -> dict:  # fmt: skip
    """Evaluate every registered freeze at its newest completed common bar (if inside its live
    window and not yet recorded), record decisions/transitions/intents, then resolve shadow
    outcomes. Idempotent; never evaluates an older bar."""
    store = ledger.store
    _require(store)
    started = _ts(utcnow())
    now = _ts(now or started)
    sw = None if dry_run else ledger.register_software(software)
    run_id = "incrun_" + uuid4().hex
    notes, recorded = [], Counter()
    pending_rows: list[tuple[str, list]] = []
    for f in freezes(store):
        d: IncubationFreeze = f["definition"]
        fid = f["freeze_id"]
        if d.semantics != semantics():
            notes.append({"freeze_id": fid, "note": "semantics changed since the freeze; not "
                          "evaluated (freeze a new definition to continue)"})  # fmt: skip
            continue
        bar = _common_bar(store, d, now)
        note = {"freeze_id": fid, "bar_close": None if bar is None else bar.isoformat()}
        if bar is None or bar <= f["registered_at"]:
            notes.append({**note, "note": "no completed bar after the freeze yet"})
            continue
        if not in_live_window(bar, now):
            notes.append({**note, "note": "newest common bar is outside its live window; missed "
                          "bars are never backfilled"})  # fmt: skip
            continue
        have = store.con.execute("SELECT count(*) FROM incubation_evaluations WHERE freeze_id=? "
                                 "AND bar_close=?", [fid, bar.to_pydatetime()]).fetchone()[0]  # fmt: skip
        if have >= len(d.members):
            notes.append({**note, "note": "already recorded; unchanged"})
            continue
        snaps = {
            c: _snapshot(store, _selections(d, c, bar + AVAILABILITY_SLACK)) for c in d.venue.coins
        }
        late = [c for c, s in snaps.items() if not funding_ready(s, c, bar)]
        if late:
            notes.append({**note, "note": f"funding through the bar close not ingested yet for "
                          f"{late}; will retry inside the window"})  # fmt: skip
            continue
        rows = evaluate_bar(store, f, snaps, bar, now)
        pending_rows += rows
        recorded.update(t for t, _ in rows)
        notes.append({**note, "note": f"evaluated {len(d.members)} candidates"})
    outcomes = resolve(store, now)
    pending_rows += outcomes
    recorded.update(t for t, _ in outcomes)
    summary = {"runner_version": RUNNER_VERSION, "now": now.isoformat(), "recorded": dict(recorded),
               "notes": notes, "dry_run": dry_run}  # fmt: skip
    if dry_run:
        return summary
    with store.transaction():
        store.con.execute("INSERT INTO incubation_runs VALUES (?,?,?,?,?)",
                          [run_id, now.to_pydatetime(), max(now, _ts(utcnow())).to_pydatetime(),
                           sw, _json(summary)])  # fmt: skip
        for table, values in pending_rows:
            if table == "incubation_evaluations":
                values = [*values[:8], run_id, *values[8:]]
            store.con.execute(f"INSERT INTO {table} VALUES ({','.join('?' * len(values))})", values)
    return {**summary, "run_id": run_id}


def evaluate_bar(store, f: dict, snaps: dict, bar: pd.Timestamp, now: pd.Timestamp) -> list:
    """Rows to append for one freeze at one bar (computed before any write)."""
    d: IncubationFreeze = f["definition"]
    fid = f["freeze_id"]
    pols = d.policy_objects()
    lp = pols["CONSERVATIVE"].lifecycle()
    floor = pols["BALANCED"].floor.floor(d.horizon_bars)
    vi = ip.venue_inputs(snaps, d.reference_coin, lp)
    t_bar = es.to_days(bar)
    t_freeze = es.to_days(f["registered_at"])
    times = ip.daily_times(es.to_days(d.venue.first_evaluation), t_bar)
    codes = ip.regime_codes(vi.state, times)
    regime = ip.current_regime(vi.state, bar)
    costs = {c.coin: (c.fee_bps, c.slippage_bps) for c in d.costs}
    prior = _prior_decisions(store, fid, bar)
    done = {r[0] for r in store.con.execute("SELECT strategy_id FROM incubation_evaluations "
                                            "WHERE freeze_id=? AND bar_close=?",
                                            [fid, bar.to_pydatetime()]).fetchall()}  # fmt: skip
    open_intents = {(r[0], r[1], r[2]): _ts(r[3]) for r in store.con.execute(
        "SELECT profile, strategy_id, asset, max(signal_bar_close) FROM incubation_intents "
        "WHERE freeze_id=? GROUP BY ALL", [fid]).fetchall()}  # fmt: skip
    windows = {
        p: pol.window.days for p, pol in pols.items() if isinstance(pol, pm.IncubationPolicy)
    }
    out: list = []
    for ref in d.members:
        if ref.strategy_id in done:
            continue
        defn = StrategyDefinition.model_validate(ref.definition)
        df, ev = ip.strategy_ledger(defn, vi, costs, d.horizon_bars, lp)
        fired = (
            sorted(df.loc[pd.to_datetime(df["signal_time"], utc=True) == bar, "asset"])
            if len(df)
            else []
        )
        eid = _id("inceval_", fid, ref.strategy_id, bar.isoformat())
        evidence = _evidence(ev, t_bar, t_freeze, windows, regime)
        out.append(("incubation_evaluations", [eid, fid, ref.strategy_id, ref.name, ref.side,
                    d.venue.venue, bar.to_pydatetime(), now.to_pydatetime(),
                    _json({"assets": fired}), _json(evidence)]))  # fmt: skip
        for profile, pol in pols.items():
            out += _decide(store, d, fid, ref, pol, profile, ev, times, codes, floor, t_bar,
                           t_freeze, bar, now, eid, fired, regime, prior, open_intents)  # fmt: skip
    return out


def _evidence(ev, t_bar: float, t_freeze: float, windows: dict, regime: dict) -> dict:
    if ev is None:
        return {"available": False, "current_regime": regime}
    k = ev.resolved(t_bar)
    before = np.zeros(len(ev), dtype=bool)
    before[:k] = ev.t_sig[:k] < t_freeze
    after = np.zeros(len(ev), dtype=bool)
    after[:k] = ev.t_sig[:k] >= t_freeze
    return {"available": True, "eras": ie.eras(ev, t_bar, windows),
            "retrospective": ie.mask_est(ev, before).brief(),
            "prospective_candidate_outcomes": ie.mask_est(ev, after).brief(),
            "current_regime": regime}  # fmt: skip


def _decide(store, d, fid, ref, pol, profile, ev, times, codes, floor, t_bar, t_freeze, bar, now,
            eid, fired, regime, prior, open_intents) -> list:  # fmt: skip
    pid = pol.policy_id
    episode = None
    checks = None
    w = life = None
    if ev is None or not len(ev):
        base = "INSUFFICIENT"
    else:
        rep = replay(ev, times, pol, floor, strategy_key=f"{d.venue.venue}:{ref.strategy_id}",
                     regime_at=codes, prospective_from=t_freeze)  # fmt: skip
        base = rep.level_at(t_bar)
        if base == "CONFIRMED_PAPER":  # live graduation comes from recorded shadow outcomes
            base = "EXPLORATORY_PAPER"
        episode = rep.open_episode() if base in pm.ADMITTED else None
        if isinstance(pol, pm.IncubationPolicy):
            lo, k = ie.window_bounds(ev, t_bar, pol.window.days)
            w, life = ie.slice_est(ev, lo, k), ie.slice_est(ev, 0, ev.resolved(t_bar))
            _, checks = ie.admission(w, life, pol, floor)
        else:  # the benchmark explains itself with Phase 20's own evidence and checks
            core = core_evidence(ev, t_bar, pol.lifecycle())
            w, life = _phase20_est(core["recent"]), _phase20_est(core["lifetime"])
            _, checks = lifecycle_activation(core, floor, pol.lifecycle())
    level = base
    ep_id = episode["episode_id"] if episode else None
    fwd, bad, shadow = _shadow(store, fid, profile, ref.strategy_id, ep_id, bar)
    grad = None
    if level in pm.ADMITTED:
        ok_g, grad = ie.graduation(fwd, episode["admission_mean"], bad, pol, floor)
        if ok_g:
            level = "CONFIRMED_PAPER"
    ep_block = None if not episode else {
        "episode_id": ep_id, "admitted_at": es.iso(episode["start"]),
        "started_before_freeze": episode["start"] < t_freeze,
        "reactivation": episode.get("reactivation"),
        "admission_evidence": episode.get("admission_evidence")}  # fmt: skip
    expl = ie.explain(level, checks, w, life, side=ref.side, floor=floor, regime=regime,
                      episode=ep_block, prospective=shadow, policy_profile=profile)  # fmt: skip
    did = _id("incdec_", eid, pid)
    payload = {"level": level, "replay_level": base, "admission_checks": checks,
               "graduation_checks": grad, "explanation": expl, "policy_id": pid}  # fmt: skip
    rows = [("incubation_decisions", [did, eid, pid, profile, level, ep_id, _json(payload)])]
    prev = prior.get((profile, ref.strategy_id))
    prev_level = prev["level"] if prev else "UNOBSERVED"
    if prev_level != level:
        rows.append(("incubation_transitions", [
            _id("inctr_", did), did, fid, profile, ref.strategy_id, bar.to_pydatetime(), prev_level,
            level, ep_id, _json({"reasons": expl["failed_checks"] if level not in pm.ADMITTED
                                          else expl["why"], "first_observation": prev is None,
                                          "episode": ep_block})]))  # fmt: skip
    if level in pm.ADMITTED:
        ex = d.execution
        for asset in fired:
            last = open_intents.get((profile, ref.strategy_id, asset))
            if last is not None and bar - last < pd.Timedelta(days=d.horizon_bars):
                continue  # one open intent per policy, strategy and asset
            latency = (now - bar).total_seconds() / 3600
            status = "entered" if latency <= ex.max_entry_latency_hours else "missed_entry_window"
            c = d.cost(asset)
            iid = _id("incintent_", fid, profile, ref.strategy_id, asset, bar.isoformat())
            rows.append(("incubation_intents", [
                iid, did, fid, profile, ep_id, ref.strategy_id, asset, ref.side,
                bar.to_pydatetime(), now.to_pydatetime(), status, level, _json({
                    "strategy": ref.name, "policy_id": pid, "direction": ref.side,
                    "entry_rule": "open of bar T+1 (opens at the signal bar close)",
                    "exit_rule": f"close of bar T+{d.horizon_bars}", "stop": ex.stop,
                    "sizing": ex.sizing, "notional_usd": ex.notional_usd, "leverage": ex.leverage,
                    "fee_bps": c.fee_bps, "slippage_bps": c.slippage_bps,
                    "latency_hours": latency, "execution": ex.name,
                    "note": "shadow intent: analytical, no order, no account"})]))  # fmt: skip
            open_intents[(profile, ref.strategy_id, asset)] = bar
    return rows


def _phase20_est(x: dict) -> ie.Est:
    ok = bool(x.get("adequate"))
    return ie.Est(n=int(x.get("n") or 0), assets=int(x.get("assets") or 0),
                  mean=x.get("mean") if ok else None, se=x.get("se") if ok else None,
                  t=x.get("t") if ok else None, excess_mean=x.get("excess_mean"), loo_mean=None,
                  best_asset=None, best_asset_share=None, hit_rate=x.get("hit_rate"),
                  net_sum=0.0)  # fmt: skip


# --------------------------------------------------------------------------- shadow outcomes


def _shadow_outcome(store, d: IncubationFreeze, asset: str, side: str, bar: pd.Timestamp,
                    now: pd.Timestamp) -> dict | None:  # fmt: skip
    h = d.horizon_bars
    exit_close = bar + pd.Timedelta(days=h)
    if now < exit_close:
        return None
    final = now >= exit_close + pd.Timedelta(days=d.execution.outcome_wait_days)
    snap = _snapshot(store, tuple(
        SeriesSelection(kind=k, symbol=asset, source=d.venue.venue,
                        timeframe="1d" if k == "perp_bars" else None,
                        start=(bar - FUNDING_CONTEXT).to_pydatetime(),
                        end=(exit_close + AVAILABILITY_SLACK).to_pydatetime())
        for k in ("perp_bars", "perp_funding")))  # fmt: skip

    def missing(reason: str):
        if not final:
            return None
        return {"status": "unavailable", "exit_close": exit_close, "gross": None, "net": None,
                "pnl_usd": None, "payload": {"reason": reason, "input_id": snap.dataset_id}}  # fmt: skip

    try:
        frame, _, _ = build_inputs(snap, asset, "perp", AssetClass.CRYPTO, True)
    except ValueError as exc:
        return missing(f"inputs unavailable: {exc}")
    closes = pd.DatetimeIndex(frame["close_time"])
    pos = np.flatnonzero(closes == bar)
    if len(pos) != 1:
        return missing("signal bar not in stored bars")
    i = int(pos[0])
    if list(closes[i : i + h + 1]) != [bar + pd.Timedelta(days=j) for j in range(h + 1)]:
        return missing("bars T+1..T+h are missing or not contiguous")
    c = d.cost(asset)
    sign = 1 if side == "long" else -1
    r = side_forward_returns(frame, {"h": h}, sign, PerpCosts(c.fee_bps, c.slippage_bps))
    net = r["ret_h"].iloc[i]
    if not np.isfinite(net):
        return missing("funding missing or published late for a day in T+1..T+h")
    entry, exit_ = float(frame["open"].iloc[i + 1]), float(frame["close"].iloc[i + h])
    gross = sign * (exit_ / entry - 1)
    return {"status": "resolved", "exit_close": exit_close, "gross": gross, "net": float(net),
            "pnl_usd": float(net) * d.execution.notional_usd,
            "payload": {"entry_price": entry, "exit_price": exit_, "gross": gross,
                        "round_trip_cost": 2 * (c.fee_bps + c.slippage_bps) / 1e4,
                        "funding_paid": float(r["funding_h"].iloc[i]), "net": float(net),
                        "input_id": snap.dataset_id}}  # fmt: skip


def resolve(store, now: pd.Timestamp) -> list:
    """Final outcomes for entered intents whose exit bar has closed (rows; written by run)."""
    defs = {f["freeze_id"]: f["definition"] for f in freezes(store)}
    todo = _rows(store, """
        SELECT i.intent_id, i.freeze_id, i.asset, i.side, i.signal_bar_close
        FROM incubation_intents i LEFT JOIN incubation_outcomes o USING (intent_id)
        WHERE o.intent_id IS NULL AND i.status='entered'
        ORDER BY i.signal_bar_close, i.intent_id""")  # fmt: skip
    out = []
    for r in todo:
        o = _shadow_outcome(store, defs[r["freeze_id"]], r["asset"], r["side"],
                            _ts(r["signal_bar_close"]), now)  # fmt: skip
        if o is None:
            continue
        out.append(("incubation_outcomes", [
            r["intent_id"], o["status"], o["exit_close"].to_pydatetime(), o["gross"], o["net"],
            o["pnl_usd"], now.to_pydatetime(),
            _json({**o["payload"], "resolver_version": SHADOW_RESOLVER_VERSION})]))  # fmt: skip
    return out


# --------------------------------------------------------------------------- queries


LEVEL_ORDER = {lv: i for i, lv in enumerate(("CONFIRMED_PAPER", "EXPLORATORY_PAPER", "WATCH",
                                             "DORMANT", "NEUTRAL", "INSUFFICIENT", "RETIRED"))}  # fmt: skip


def _latest_bar(store, fid: str | None = None):
    sql = "SELECT freeze_id, max(bar_close) FROM incubation_evaluations"
    sql += " WHERE freeze_id=? GROUP BY ALL" if fid else " GROUP BY ALL"
    return {r[0]: _ts(r[1]) for r in store.con.execute(sql, [fid] if fid else []).fetchall()}


def candidates(store, profile: str | None = None, level: str | None = None,
               side: str | None = None) -> list[dict]:  # fmt: skip
    """The latest recorded decision of every candidate ("what looks interesting now?")."""
    _require(store)
    out = []
    for fid, bar in _latest_bar(store).items():
        rows = _rows(store, """
            SELECT e.strategy_name, e.strategy_id, e.side, e.venue, e.bar_close, e.signals,
                   d.profile, d.level, d.episode_id, d.payload
            FROM incubation_decisions d JOIN incubation_evaluations e USING (evaluation_id)
            WHERE e.freeze_id=? AND e.bar_close=?""", [fid, bar.to_pydatetime()])  # fmt: skip
        for r in rows:
            p = json.loads(r["payload"])
            out.append({"freeze_id": fid, "strategy": r["strategy_name"],
                        "strategy_id": r["strategy_id"], "side": r["side"], "venue": r["venue"],
                        "bar_close": _ts(r["bar_close"]).isoformat(), "policy": r["profile"],
                        "level": r["level"], "episode_id": r["episode_id"],
                        "signals_at_bar": json.loads(r["signals"])["assets"],
                        "explanation": p["explanation"]})  # fmt: skip
    if profile:
        out = [r for r in out if r["policy"] == profile.upper()]
    if level:
        out = [r for r in out if r["level"] == level.upper()]
    if side:
        out = [r for r in out if r["side"] == side.lower()]
    return sorted(out, key=lambda r: (LEVEL_ORDER.get(r["level"], 9), r["policy"], r["strategy"]))


def candidate(store, strategy: str) -> dict:
    """Everything recorded for one candidate: latest evidence (eras, retrospective vs
    prospective vs shadow), each policy's decision and explanation, transitions, episodes,
    shadow intents and outcomes."""
    _require(store)
    ev = _rows(store, """
        SELECT * FROM incubation_evaluations WHERE strategy_id=? OR strategy_name=?
        ORDER BY bar_close DESC LIMIT 1""", [strategy, strategy])  # fmt: skip
    if not ev:
        raise IncubationError(f"no recorded incubation evaluation for {strategy!r}")
    e = ev[0]
    sid = e["strategy_id"]
    dec = _rows(store, "SELECT profile, level, episode_id, payload FROM incubation_decisions "
                       "WHERE evaluation_id=? ORDER BY profile", [e["evaluation_id"]])  # fmt: skip
    trs = _rows(store, "SELECT profile, bar_close, from_level, to_level, episode_id, payload "
                       "FROM incubation_transitions WHERE strategy_id=? ORDER BY bar_close, profile",
                [sid])  # fmt: skip
    intents = _rows(store, """
        SELECT i.profile, i.episode_id, i.asset, i.side, i.signal_bar_close, i.status AS intent,
               i.level, o.status, o.net, o.pnl_usd, o.exit_close
        FROM incubation_intents i LEFT JOIN incubation_outcomes o USING (intent_id)
        WHERE i.strategy_id=? ORDER BY i.signal_bar_close, i.profile, i.asset""", [sid])  # fmt: skip
    return {
        "strategy": e["strategy_name"], "strategy_id": sid, "side": e["side"], "venue": e["venue"],
        "bar_close": _ts(e["bar_close"]).isoformat(), "signals_at_bar": json.loads(e["signals"]),
        "evidence": json.loads(e["evidence"]),
        "decisions": {r["profile"]: {"level": r["level"], "episode_id": r["episode_id"],
                                     **json.loads(r["payload"])} for r in dec},
        "transitions": [{**{k: v for k, v in r.items() if k != "payload"},
                         "bar_close": _ts(r["bar_close"]).isoformat(), **json.loads(r["payload"])}
                        for r in trs],
        "episodes": episodes(store, strategy_id=sid),
        "shadow": [{**r, "signal_bar_close": _ts(r["signal_bar_close"]).isoformat(),
                    "exit_close": None if r["exit_close"] is None else _ts(r["exit_close"]).isoformat()}
                   for r in intents],
        "statement": STATEMENT,
    }  # fmt: skip


def episodes(store, profile: str | None = None, strategy_id: str | None = None) -> list[dict]:
    """Prospectively recorded episodes: admission and deactivation from the transition log,
    outcomes from the shadow ledger."""
    _require(store)
    sql = (
        "SELECT t.*, e.strategy_name FROM incubation_transitions t JOIN incubation_decisions d "
        "USING (decision_id) JOIN incubation_evaluations e USING (evaluation_id) WHERE 1=1"
    )
    args: list = []
    if profile:
        sql += " AND t.profile=?"
        args.append(profile.upper())
    if strategy_id:
        sql += " AND t.strategy_id=?"
        args.append(strategy_id)
    trs = _rows(store, sql + " ORDER BY t.bar_close, t.profile, t.strategy_id", args)
    eps: dict[str, dict] = {}
    for t in trs:
        key = (t["profile"], t["strategy_id"])
        if t["to_level"] in pm.ADMITTED and t["from_level"] not in pm.ADMITTED:
            p = json.loads(t["payload"])
            ep = p.get("episode") or {}
            eps[t["episode_id"]] = {
                "episode_id": t["episode_id"], "policy": t["profile"],
                "strategy": t["strategy_name"], "strategy_id": t["strategy_id"],
                "admitted_at": ep.get("admitted_at"), "first_recorded": _ts(t["bar_close"]).isoformat(),
                "started_before_freeze": ep.get("started_before_freeze"),
                "admission_evidence": ep.get("admission_evidence"), "deactivated_at": None,
                "deactivated_to": None, "confirmed_at": None, "_key": key}  # fmt: skip
        elif t["to_level"] == "CONFIRMED_PAPER" and t["episode_id"] in eps:
            eps[t["episode_id"]]["confirmed_at"] = _ts(t["bar_close"]).isoformat()
        elif t["from_level"] in pm.ADMITTED and t["to_level"] not in pm.ADMITTED:
            for e in eps.values():
                if e["_key"] == key and e["deactivated_at"] is None:
                    e["deactivated_at"] = _ts(t["bar_close"]).isoformat()
                    e["deactivated_to"] = t["to_level"]
                    e["deactivation_reasons"] = json.loads(t["payload"]).get("reasons")
    out = []
    for e in eps.values():
        e.pop("_key")
        rs = _rows(store, """SELECT o.status, o.net, o.pnl_usd FROM incubation_intents i
                             LEFT JOIN incubation_outcomes o USING (intent_id)
                             WHERE i.episode_id=? AND i.profile=?""",
                   [e["episode_id"], e["policy"]])  # fmt: skip
        net = [r["net"] for r in rs if r["status"] == "resolved"]
        out.append({**e, "intents": len(rs), "outcomes": len(net),
                    "net_mean": float(np.mean(net)) if net else None,
                    "net_sum": float(np.sum(net)) if net else 0.0,
                    "pnl_usd": float(sum(r["pnl_usd"] or 0 for r in rs))})  # fmt: skip
    return out


def _span(store, fid: str) -> tuple[float, float, int]:
    r = store.con.execute("SELECT min(bar_close), max(bar_close), count(DISTINCT bar_close) "
                          "FROM incubation_evaluations WHERE freeze_id=?", [fid]).fetchone()  # fmt: skip
    if r[0] is None:
        return 0.0, 0.0, 0
    return es.to_days(_ts(r[0])), es.to_days(_ts(r[1])) + 1, int(r[2])


def opportunities(store) -> dict:
    """Prospective opportunity rate per policy (see ``opportunity``): candidate signals from
    the recorded evaluations, admissible signals = recorded shadow intents."""
    _require(store)
    out = {}
    for f in freezes(store):
        fid = f["freeze_id"]
        lo, hi, n_days = _span(store, fid)
        evs = _rows(store, "SELECT strategy_id, side, bar_close, signals FROM incubation_evaluations "
                           "WHERE freeze_id=?", [fid])  # fmt: skip
        sig = [(r["strategy_id"], a, r["side"], es.to_days(r["bar_close"]))
               for r in evs for a in json.loads(r["signals"])["assets"]]  # fmt: skip
        per = {}
        for profile in pm.PROFILES:
            ints = {(r[0], r[1], es.to_days(_ts(r[2]))) for r in store.con.execute(
                "SELECT strategy_id, asset, signal_bar_close FROM incubation_intents "
                "WHERE freeze_id=? AND profile=?", [fid, profile]).fetchall()}  # fmt: skip
            frame = pd.DataFrame([{"t_sig": t, "asset": a, "side": s, "admitted": (sid, a, t) in ints}
                                  for sid, a, s, t in sig],
                                 columns=["t_sig", "asset", "side", "admitted"])  # fmt: skip
            per[profile] = opportunity_rate(frame, lo, hi) if n_days else {"span_days": 0}
        out[fid] = {"evaluated_bars": n_days, "policies": per}
    return out


def compare(store) -> dict:
    """Prospective policy comparison (no winner is selected). Quantities that need the truth
    (edge captured, activation/deactivation delay vs the edge) exist only in the synthetic
    calibration and are labelled so."""
    _require(store)
    out = {}
    opp = opportunities(store)
    for f in freezes(store):
        fid = f["freeze_id"]
        lo, _, n_days = _span(store, fid)
        per = {}
        for profile in pm.PROFILES:
            levels = Counter(r[0] for r in store.con.execute("""
                SELECT d.level FROM incubation_decisions d JOIN incubation_evaluations e
                USING (evaluation_id) WHERE e.freeze_id=? AND d.profile=? AND e.bar_close=(
                SELECT max(bar_close) FROM incubation_evaluations WHERE freeze_id=?)""",
                [fid, profile, fid]).fetchall())  # fmt: skip
            res = _rows(store, """
                SELECT i.side, i.status AS intent, o.status, o.net, o.pnl_usd, o.exit_close,
                       i.signal_bar_close
                FROM incubation_intents i LEFT JOIN incubation_outcomes o USING (intent_id)
                WHERE i.freeze_id=? AND i.profile=?""", [fid, profile])  # fmt: skip
            done = sorted(
                (r for r in res if r["status"] == "resolved"), key=lambda r: r["exit_close"]
            )
            net = np.array([r["net"] for r in done], dtype=float)
            pnl = np.cumsum([r["pnl_usd"] for r in done]) if done else np.zeros(0)
            mean, se = (es.weighted_mean_se(net, np.ones(len(net)), np.floor(np.array(
                [es.to_days(r["signal_bar_close"]) for r in done]) / 7)) if len(net) else (np.nan, np.nan))  # fmt: skip
            eps = episodes(store, profile=profile)
            closed = [e for e in eps if e["deactivated_at"]]
            dur = [(_ts(e["deactivated_at"]) - _ts(e["first_recorded"])).days for e in closed]
            trs = store.con.execute("SELECT count(*) FROM incubation_transitions WHERE freeze_id=? "
                                    "AND profile=? AND from_level<>'UNOBSERVED'",
                                    [fid, profile]).fetchone()[0]  # fmt: skip
            per[profile] = {
                "levels_now": dict(levels),
                "candidates_ever_admitted": len({e["strategy_id"] for e in eps}),
                "episodes": len(eps), "episodes_closed": len(closed),
                "episode_days_median": float(np.median(dur)) if dur else None,
                "adverse_closed_episodes": sum(1 for e in closed if (e["net_mean"] or 0) < 0),
                "shadow_intents": len(res),
                "missed_entry_window": sum(r["intent"] == "missed_entry_window" for r in res),
                "shadow_outcomes": len(done),
                "unavailable": sum(r["status"] == "unavailable" for r in res),
                "pending": sum(r["intent"] == "entered" and r["status"] is None for r in res),
                "net_mean": float(mean) if len(net) else None,
                "net_se": float(se) if len(net) and np.isfinite(se) else None,
                "pnl_usd": float(pnl[-1]) if len(pnl) else 0.0,
                "max_drawdown_usd": float((np.maximum.accumulate(np.concatenate([[0.0], pnl]))[1:]
                                           - pnl).max()) if len(pnl) else 0.0,
                "by_side": {s: sum(r["side"] == s for r in res) for s in ("long", "short")},
                "transitions_per_candidate_day": (trs / (len(f["definition"].members) * n_days)
                                                  if n_days else None),
                "opportunity": opp.get(fid, {}).get("policies", {}).get(profile),
                "synthetic_only": ["edge captured", "activation delay vs the true edge start",
                                   "deactivation delay vs the true edge end"],
            }  # fmt: skip
        out[fid] = {"evaluated_bars": n_days, "first_bar": es.iso(lo) if n_days else None,
                    "policies": per, "note": "prospective record; no winner is selected"}  # fmt: skip
    return out


def status(store) -> dict:
    _require(store)
    fz = freezes(store)
    last = _rows(store, "SELECT run_id, started_at, summary FROM incubation_runs "
                        "ORDER BY started_at DESC LIMIT 1")  # fmt: skip
    latest = _latest_bar(store)
    out = []
    for f in fz:
        d: IncubationFreeze = f["definition"]
        fid = f["freeze_id"]
        lv = _rows(store, """
            SELECT d.profile, e.side, d.level, count(*) AS n
            FROM incubation_decisions d JOIN incubation_evaluations e USING (evaluation_id)
            WHERE e.freeze_id=? AND e.bar_close=? GROUP BY ALL""",
                   [fid, latest[fid].to_pydatetime()] if fid in latest else [fid, None])  # fmt: skip
        counts: dict = {}
        for r in lv:
            counts.setdefault(r["profile"], {}).setdefault(r["level"], {"long": 0, "short": 0})[
                r["side"]
            ] = r["n"]
        out.append({"freeze_id": fid, "name": d.name, "registered_at": f["registered_at"].isoformat(),
                    "pool_id": d.pool_id, "policy_ids": d.policy_ids, "members": len(d.members),
                    "balance": balance(d.members), "latest_bar": latest[fid].isoformat() if fid in latest else None,
                    "levels": counts})  # fmt: skip
    return {"statement": STATEMENT, "freezes": out,
            "last_run": ({"run_id": last[0]["run_id"], "started_at": _ts(last[0]["started_at"]).isoformat(),
                          **json.loads(last[0]["summary"])} if last else None)}  # fmt: skip


def frozen_summary(defn: IncubationFreeze) -> dict:
    """What a freeze locks (for agents and the docs)."""
    return {"freeze_id": defn.freeze_id, "pool_id": defn.pool_id, "members": len(defn.members),
            "balance": balance(defn.members), "policy_ids": defn.policy_ids,
            "venue": defn.venue.model_dump(mode="json"), "horizon_bars": defn.horizon_bars,
            "costs": [c.model_dump(mode="json") for c in defn.costs],
            "execution": defn.execution.model_dump(mode="json"),
            "cadence": defn.cadence.model_dump(mode="json"), "semantics": defn.semantics,
            "grants_live": defn.grants_live, "statement": STATEMENT}  # fmt: skip


__all__ = [
    "IncubationFreeze",
    "build_freeze",
    "candidate",
    "candidates",
    "compare",
    "episodes",
    "freeze",
    "frozen_summary",
    "opportunities",
    "run",
    "status",
]
