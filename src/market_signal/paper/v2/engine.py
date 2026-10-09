"""Deterministic prospective paper state machine with no executable exchange adapter.

All writes are append-only paper_v2_* rows. Registration accepts only the released
bootstrap. No public operation accepts a trade, discretionary size, or risk override.
"""

from __future__ import annotations

import json
import math
import resource
import time
from collections import defaultdict
from dataclasses import asdict
from uuid import uuid4

import pandas as pd

from market_signal.models.domain import utcnow
from market_signal.research.lab.common import canonical_json, content_id

from . import spec
from .data import Observation, Quote, Sources, clean, ts


def rows(store, sql, args=()) -> list[dict]:
    cur = store.con.execute(sql, args)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def register(store, *, now=None) -> dict:
    at = ts(now or utcnow())
    uid = spec.universe_id()
    with store.transaction():
        store.con.execute(
            "INSERT INTO paper_v2_universes VALUES (?,?,?) ON CONFLICT DO NOTHING",
            [uid, at.to_pydatetime(), canonical_json(spec.definition())],
        )
        for kind, p in (
            ("admission", spec.AdmissionPolicy()),
            ("risk", spec.RiskPolicy()),
            ("execution", spec.ExecutionPolicy()),
        ):
            store.con.execute(
                "INSERT INTO paper_v2_policies VALUES (?,?,?) ON CONFLICT DO NOTHING",
                [p.policy_id, kind, canonical_json(p.model_dump(mode="json"))],
            )
    return {"universe_id": uid, "hypotheses": len(spec.bootstrap()), "mode": "paper"}


def create(store, *, now=None) -> dict:
    at = ts(now or utcnow())
    uid = spec.universe_id()
    row = store.con.execute(
        "SELECT definition,registered_at FROM paper_v2_universes WHERE universe_id=?", [uid]
    ).fetchone()
    if not row or json.loads(row[0]) != json.loads(canonical_json(spec.definition())):
        raise ValueError("register the released frozen v2 universe first")
    if at < ts(row[1]):
        raise ValueError("activation precedes registration")
    # Exactly one active account; new runs require the old one to be terminal and flat.
    for r in runs(store):
        st = state(store, r["run_id"])
        if st["status"] == "ACTIVE" or st["open"] or st["pending"]:
            raise ValueError("an active or draining v2 run already exists")
    from market_signal.paper.retirement import v1_work_needed

    if v1_work_needed(store):
        raise ValueError("retire/drain v1 before creating v2")
    d = {
        "universe_id": uid,
        "activated_at": at.isoformat(),
        "mode": "paper",
        "nonce": uuid4().hex,
        "version": spec.VERSION,
        "admission_policy_id": spec.AdmissionPolicy().policy_id,
        "risk_policy_id": spec.RiskPolicy().policy_id,
        "execution_policy_id": spec.ExecutionPolicy().policy_id,
    }
    rid = content_id("paperrun_v2_", d)
    store.con.execute(
        "INSERT INTO paper_v2_runs VALUES (?,?,?,?,?,?,?,?)",
        [
            rid,
            uid,
            at.to_pydatetime(),
            "paper",
            d["admission_policy_id"],
            d["risk_policy_id"],
            d["execution_policy_id"],
            canonical_json(d),
        ],
    )
    return {"run_id": rid, **d, "starting_equity": spec.RiskPolicy().starting_equity}


def runs(store) -> list[dict]:
    return rows(store, "SELECT * FROM paper_v2_runs ORDER BY activated_at")


def current(store) -> str:
    r = runs(store)
    if not r:
        raise ValueError("no v2 run; register then create prospectively after deployment")
    return r[-1]["run_id"]


def load(store, rid) -> dict:
    r = rows(store, "SELECT * FROM paper_v2_runs WHERE run_id=?", [rid])
    if not r:
        raise ValueError("unknown v2 run")
    r = r[0]
    d = json.loads(
        store.con.execute(
            "SELECT definition FROM paper_v2_universes WHERE universe_id=?", [r["universe_id"]]
        ).fetchone()[0]
    )
    # A code change cannot silently alter a registered policy or hypothesis definition.
    if canonical_json(d) != canonical_json(spec.definition()):
        raise ValueError("unsupported frozen universe: deploy its engine or register a new version")
    for kind, p in (
        ("admission", spec.AdmissionPolicy()),
        ("risk", spec.RiskPolicy()),
        ("execution", spec.ExecutionPolicy()),
    ):
        row = store.con.execute(
            "SELECT definition FROM paper_v2_policies WHERE policy_id=?", [r[f"{kind}_policy_id"]]
        ).fetchone()
        if not row or canonical_json(json.loads(row[0])) != canonical_json(
            p.model_dump(mode="json")
        ):
            raise ValueError("policy identity/content mismatch")
    return r


def state(store, rid) -> dict:
    risk = spec.RiskPolicy()
    st = {
        "status": "ACTIVE",
        "cash": risk.starting_equity,
        "equity": risk.starting_equity,
        "peak": risk.starting_equity,
        "open": {},
        "pending": {},
        "closed": [],
        "marks": [],
        "hypotheses": {},
        "seq": 0,
    }
    for e in rows(
        store,
        "SELECT seq,event_type,payload FROM paper_v2_events WHERE run_id=? ORDER BY seq",
        [rid],
    ):
        p = json.loads(e["payload"])
        st["seq"] = e["seq"]
        kind = e["event_type"]
        tid = p.get("trade_id")
        if kind == "pending":
            st["pending"][tid] = p
        elif kind == "opened":
            st["pending"].pop(tid)
            st["open"][tid] = p
            st["cash"] -= p["entry_fee"]
        elif kind == "closed":
            st["open"].pop(tid)
            st["closed"].append(p)
            st["cash"] += p["net_pnl"] + p["entry_fee"]
        elif kind == "expired":
            st["pending"].pop(tid, None)
        elif kind == "mark":
            st["marks"].append(p)
            st["equity"], st["peak"] = p["equity"], p["peak"]
        elif kind == "run_state":
            st["status"] = p["state"]
            st["kill_at"] = p.get("at")
        elif kind == "hypothesis_state":
            st["hypotheses"][p["hypothesis_id"]] = p
        elif kind == "support":
            pos = st["open"].get(tid) or st["pending"].get(tid)
            if pos:
                pos["supporting_hypothesis_ids"] = sorted(
                    set(pos["supporting_hypothesis_ids"]) | set(p["ids"])
                )
                pos["support_count"] = p["support_count"]
    return st


def event(store, rid, kind, key, payload, now) -> None:
    if store.con.execute(
        "SELECT 1 FROM paper_v2_events WHERE run_id=? AND event_key=?", [rid, key]
    ).fetchone():
        return
    seq = store.con.execute(
        "SELECT coalesce(max(seq),0)+1 FROM paper_v2_events WHERE run_id=?", [rid]
    ).fetchone()[0]
    store.con.execute(
        "INSERT INTO paper_v2_events VALUES (?,?,?,?,?,?,?)",
        [
            content_id("paperevent_v2_", {"run_id": rid, "key": key}),
            rid,
            seq,
            key,
            kind,
            ts(now).to_pydatetime(),
            canonical_json(clean(payload)),
        ],
    )


def kill(store, rid, *, reason: str, now=None) -> dict:
    if not reason.strip():
        raise ValueError("kill needs a reason")
    load(store, rid)
    at = ts(now or utcnow())
    event(
        store,
        rid,
        "run_state",
        "human_kill",
        {"state": "KILLED", "reason": reason, "at": at.isoformat()},
        at,
    )
    return {
        "run_id": rid,
        "state": "KILLED",
        "note": "pending cancelled and positions close on next executable quote",
    }


def _sign(side):
    return 1 if side == "long" else -1


def fill_price(price, side, asset, *, entry):
    slip = spec.ExecutionPolicy().slippage_bps[asset] / 10000
    return price * (1 + _sign(side) * slip * (1 if entry else -1))


def size(equity, price, asset) -> tuple[float, float]:
    # Decimal floor protects against binary-float rounding above the frozen target.
    from decimal import ROUND_DOWN, Decimal

    step = Decimal(10) ** -spec.RiskPolicy().size_decimals[asset]
    units = (
        Decimal(str(equity * spec.RiskPolicy().notional_fraction)) / Decimal(str(price))
    ).quantize(step, rounding=ROUND_DOWN)
    return float(units), float(units) * price


def _valid_quotes(quotes, now):
    return sorted(
        [
            q
            for q in quotes
            if q.complete
            and q.price > 0
            and math.isfinite(q.price)
            and ts(q.available_at) <= ts(now)
            and ts(q.at) <= ts(now)
            and ts(q.available_at) >= ts(q.at)
        ],
        key=lambda q: (ts(q.at), q.kind != "minute_mid"),
    )


def path_metrics(side, entry, path) -> dict:
    sign = _sign(side)
    if not path:
        return {
            "mfe": None,
            "mae": None,
            "favourable_first": "UNKNOWN",
            "time_to_mfe_minutes": None,
            "time_to_mae_minutes": None,
        }
    highs = [sign * ((q.high if sign > 0 else q.low) / entry - 1) for q in path]
    lows = [sign * ((q.low if sign > 0 else q.high) / entry - 1) for q in path]
    imax, imin = (
        int(max(range(len(highs)), key=highs.__getitem__)),
        int(min(range(len(lows)), key=lows.__getitem__)),
    )
    fav = next((i for i, v in enumerate(highs) if v >= 0.005), None)
    adv = next((i for i, v in enumerate(lows) if v <= -0.005), None)
    first = (
        "NEITHER"
        if fav is None and adv is None
        else "FAVOURABLE"
        if adv is None
        else "ADVERSE"
        if fav is None
        else "AMBIGUOUS"
        if fav == adv
        else "FAVOURABLE"
        if fav < adv
        else "ADVERSE"
    )
    start = ts(path[0].at)
    return {
        "mfe": max(0.0, max(highs)),
        "mae": min(0.0, min(lows)),
        "favourable_first": first,
        "time_to_mfe_minutes": (ts(path[imax].at) - start).total_seconds() / 60,
        "time_to_mae_minutes": (ts(path[imin].at) - start).total_seconds() / 60,
    }


def price_path(quotes, asset, entry_at):
    """Minute priority per instant, with later coarse fallback and no pre-entry wicks."""
    selected = {}
    for q in quotes:
        at = ts(q.at)
        if q.asset != asset or at <= ts(entry_at) or q.kind == "bar_open":
            continue
        if q.kind == "bar_close" and at - ts(entry_at) < pd.Timedelta(minutes=15):
            q = Quote(q.asset, q.at, q.available_at, q.price, q.price, q.price, q.kind)
        selected.setdefault(at, q)  # valid quotes are sorted with minute priority
    return list(selected.values())


def funding_cost(asset, side, notional, start, end, rates) -> tuple[float, list[str]]:
    hours = pd.date_range(
        ts(start).floor("h") + pd.Timedelta(hours=1), ts(end).floor("h"), freq="h"
    )
    found = {ts(r.at): r.rate for r in rates if r.asset == asset}
    missing = [h.isoformat() for h in hours if h not in found]
    return _sign(side) * notional * sum(found.get(h, 0) for h in hours), missing


def manage(store, rid, quotes, rates, now) -> None:
    risk, ex = spec.RiskPolicy(), spec.ExecutionPolicy()
    st = state(store, rid)
    for tid, p in st["pending"].items():
        if st["status"] != "ACTIVE":
            event(
                store,
                rid,
                "expired",
                "expired:" + tid,
                {"trade_id": tid, "reason": "KILL_SWITCH"},
                now,
            )
            continue
        eligible = [
            q
            for q in quotes
            if q.asset == p["asset"]
            and ts(q.at) > ts(p["decision_at"])
            and q.kind in ("minute_mid", "bar_open")
        ]
        q = eligible[0] if eligible else None
        if q is None or ts(q.at) - ts(p["decision_at"]) > pd.Timedelta(
            minutes=risk.max_entry_wait_minutes
        ):
            if ts(now) - ts(p["decision_at"]) > pd.Timedelta(
                minutes=risk.max_entry_wait_minutes + 15
            ):
                event(
                    store,
                    rid,
                    "expired",
                    "expired:" + tid,
                    {"trade_id": tid, "reason": "EXECUTION_EXPIRED"},
                    now,
                )
            continue
        px = fill_price(q.price, p["side"], p["asset"], entry=True)
        units, ntl = size(p["equity_at_decision"], px, p["asset"])
        # Recheck account constraints at fill: pending intents do not bypass risk.
        latest = state(store, rid)
        gross = sum(x["notional"] for x in latest["open"].values())
        reason = (
            "MIN_NOTIONAL"
            if ntl < risk.min_notional
            else "EXPOSURE_CAP"
            if gross + ntl > latest["equity"] * risk.gross_fraction + 1e-9
            else None
        )
        if reason:
            event(store, rid, "expired", "expired:" + tid, {"trade_id": tid, "reason": reason}, now)
            continue
        opened = {
            **p,
            "entry_at": q.at,
            "entry_ref": q.price,
            "entry_fill": px,
            "entry_available_at": q.available_at,
            "entry_quote_kind": q.kind,
            "units": units,
            "notional": ntl,
            "margin": ntl / risk.leverage,
            "entry_fee": ntl * ex.fee_bps / 10000,
            "exit_due_at": (ts(q.at) + pd.Timedelta(minutes=p["horizon_minutes"])).isoformat(),
        }
        event(store, rid, "opened", "opened:" + tid, opened, now)
    st = state(store, rid)
    floating, gross, net, funding_pending = 0.0, 0.0, 0.0, []
    positions = {}
    for tid, p in st["open"].items():
        path = price_path(quotes, p["asset"], p["entry_at"])
        if path:
            path.insert(
                0,
                Quote(
                    p["asset"],
                    p["entry_at"],
                    p["entry_available_at"],
                    p["entry_ref"],
                    p["entry_ref"],
                    p["entry_ref"],
                ),
            )
        if not path:
            floating += 0
            gross += p["notional"]
            net += _sign(p["side"]) * p["notional"]
            positions[tid] = {"price": p["entry_ref"], "stale": True, "unrealised": 0}
            continue
        forced = st["status"] != "ACTIVE"
        stop_at = ts(st["kill_at"]) if forced else ts(p["exit_due_at"])
        # Isolated margin-loss guard is frozen, symmetric and includes observed path extremes.
        liquidation = next(
            (
                q
                for q in path
                if _sign(p["side"])
                * p["units"]
                * ((q.low if p["side"] == "long" else q.high) - p["entry_fill"])
                <= -p["margin"] * (1 - 1 / spec.RiskPolicy().max_leverage[p["asset"]])
            ),
            None,
        )
        exiting = next((q for q in path if ts(q.at) >= stop_at), None)
        if liquidation and (exiting is None or ts(liquidation.at) <= ts(exiting.at)):
            exiting = liquidation
        last = exiting or path[-1]
        fund, missing = funding_cost(
            p["asset"], p["side"], p["notional"], p["entry_at"], last.at, rates
        )
        if missing:
            funding_pending.extend(f"{p['asset']}:{h}" for h in missing)
        if exiting and not missing:
            exit_px = fill_price(last.price, p["side"], p["asset"], entry=False)
            fee = p["units"] * exit_px * ex.fee_bps / 10000
            pnl = _sign(p["side"]) * p["units"] * (exit_px - p["entry_fill"])
            liq = liquidation is last
            net_pnl = -p["margin"] - p["entry_fee"] if liq else pnl - p["entry_fee"] - fee - fund
            closed = {
                **p,
                "exit_at": last.at,
                "exit_ref": last.price,
                "exit_fill": exit_px,
                "exit_fee": 0 if liq else fee,
                "fees": p["entry_fee"] + (0 if liq else fee),
                "funding": fund,
                "gross_pnl": pnl,
                "net_pnl": net_pnl,
                "slippage_cost": p["units"]
                * (abs(p["entry_fill"] - p["entry_ref"]) + abs(exit_px - last.price)),
                "trade_return": net_pnl / p["notional"],
                "account_impact": net_pnl / p["equity_at_decision"],
                "exit_reason": "LIQUIDATION"
                if liq
                else "KILL_SWITCH"
                if forced
                else "FIXED_HORIZON",
                "exit_delay_minutes": max(
                    0, (ts(last.at) - ts(p["exit_due_at"])).total_seconds() / 60
                ),
                **path_metrics(
                    p["side"], p["entry_ref"], [q for q in path if ts(q.at) <= ts(last.at)]
                ),
            }
            event(store, rid, "closed", "closed:" + tid, closed, now)
        else:
            unrealised = _sign(p["side"]) * p["units"] * (last.price - p["entry_fill"]) - fund
            floating += unrealised
            gross += p["units"] * last.price
            net += _sign(p["side"]) * p["units"] * last.price
            positions[tid] = {
                "price": last.price,
                "at": last.at,
                "unrealised": unrealised,
                "stale": ts(now) - ts(last.at) > pd.Timedelta(minutes=30),
            }
    st = state(store, rid)
    equity = st["cash"] + floating
    peak = max(st["peak"], equity)
    event(
        store,
        rid,
        "mark",
        "mark:" + ts(now).isoformat(),
        {
            "at": ts(now).isoformat(),
            "equity": equity,
            "peak": peak,
            "drawdown": 1 - equity / peak,
            "gross_notional": gross,
            "net_notional": net,
            "long_notional": (gross + net) / 2,
            "short_notional": (gross - net) / 2,
            "directional_concentration": abs(net) / gross if gross else 0,
            "positions": positions,
            "missing_funding": sorted(set(funding_pending)),
            "provisional": bool(funding_pending),
        },
        now,
    )
    if equity <= peak * (1 - risk.catastrophe_drawdown) and st["status"] == "ACTIVE":
        event(
            store,
            rid,
            "run_state",
            "catastrophe",
            {"state": "KILLED", "reason": "CATASTROPHE_GUARD", "at": ts(now).isoformat()},
            now,
        )


def gate(o: Observation, h, now, activated_at) -> str | None:
    if h is None:
        return "HYPOTHESIS_NOT_REGISTERED"
    if o.intended_side is not None and o.intended_side != h.side:
        return "HYPOTHESIS_NOT_REGISTERED"
    if (
        not o.causal
        or ts(o.available_at) > ts(now)
        or ts(o.available_at) < ts(o.signal_at)
        or ts(o.signal_at) <= ts(activated_at)
    ):
        return "INVALID_TIMING"
    if not o.healthy or ts(now) - ts(o.signal_at) > pd.Timedelta(
        minutes=spec.AdmissionPolicy().max_signal_age_minutes
    ):
        return "DATA_STALE"
    if not o.context_available:
        return "CONTEXT_UNAVAILABLE"
    if not o.warm:
        return "FEATURE_WARMUP"
    if not o.fired:
        return "NO_SIGNAL"
    return None


def observation_payload(store, o):
    payload = clean(asdict(o))
    context = payload.get("context") or {}
    if context:
        sid = context.get("snapshot_id") or content_id("paperctx_v2_", context)
        encoded = canonical_json(context)
        row = store.con.execute(
            "SELECT payload FROM paper_v2_context_snapshots WHERE snapshot_id=?", [sid]
        ).fetchone()
        if row and canonical_json(json.loads(row[0])) != encoded:
            raise ValueError("context snapshot identity changed")
        store.con.execute(
            "INSERT INTO paper_v2_context_snapshots VALUES (?,?) ON CONFLICT DO NOTHING",
            [sid, encoded],
        )
        payload["context"] = {
            "snapshot_id": sid,
            **{k: context[k] for k in ("market", "macro", "activity") if k in context},
        }
    return payload


def admit(store, rid, observations, quotes, rates, now, activated_at) -> dict:
    hypotheses = {h.hypothesis_id: h for h in spec.bootstrap()}
    initial = state(store, rid)
    candidates = []
    for o in observations:
        # Cutover: no historical rows, even rejected signals, enter the new account.
        if ts(o.signal_at) <= ts(activated_at):
            continue
        oid = content_id(
            "paperopp_v2_",
            {
                "run_id": rid,
                "hypothesis_id": o.hypothesis_id,
                "asset": o.asset,
                "signal_at": o.signal_at,
            },
        )
        if store.con.execute(
            "SELECT 1 FROM paper_v2_opportunities WHERE opportunity_id=?", [oid]
        ).fetchone():
            continue
        h = hypotheses.get(o.hypothesis_id)
        if h is None and o.intended_side not in ("long", "short"):
            raise ValueError("unregistered observation must declare intended side")
        reason = gate(o, h, now, activated_at)
        if reason is None:
            lifecycle = initial["hypotheses"].get(h.hypothesis_id, {}).get("state")
            if lifecycle and lifecycle != "ENABLED_EXPLORATORY":
                reason = "HYPOTHESIS_DISABLED"
            elif initial["status"] != "ACTIVE":
                reason = (
                    "CATASTROPHE_GUARD"
                    if initial["equity"] <= initial["peak"] * 0.25
                    else "KILL_SWITCH"
                )
        candidates.append({"o": o, "h": h, "oid": oid, "reason": reason})
    groups = defaultdict(list)
    for c in candidates:
        if c["reason"] is None:
            groups[c["o"].asset].append(c)
    # Freeze deterministic conflict cancellation before resource allocation, with no side score.
    for group in groups.values():
        if len({c["h"].side for c in group}) > 1:
            for c in group:
                c["reason"] = "CONFLICT"
                c["conflict"] = [
                    {
                        "hypothesis_id": x["h"].hypothesis_id,
                        "side": x["h"].side,
                        "maturity": x["o"].maturity,
                    }
                    for x in group
                ]
    # Hash order is side-neutral; does not optimise strength from paper profitability.
    candidates.sort(
        key=lambda c: content_id(
            "priority_",
            {"asset": c["o"].asset, "at": c["o"].signal_at, "hypothesis": c["o"].hypothesis_id},
        )
    )
    totals = defaultdict(int)
    for c in candidates:
        o, h, reason, oid = c["o"], c["h"], c["reason"], c["oid"]
        st, tid, admission = state(store, rid), None, "REJECTED"
        support_ids = []
        risk = spec.RiskPolicy()
        if reason is None:
            last = store.con.execute(
                "SELECT max(evaluated_at) FROM paper_v2_opportunities WHERE run_id=? "
                "AND hypothesis_id=? AND asset=? AND admission IN ('ADMITTED','SUPPORTED')",
                [rid, h.hypothesis_id, o.asset],
            ).fetchone()[0]
            if last is not None and ts(now) - ts(last) < pd.Timedelta(minutes=h.cooldown_minutes):
                reason = "COOLDOWN"
        if reason is None:
            positions = list(st["open"].values()) + list(st["pending"].values())
            existing = next((p for p in positions if p["asset"] == o.asset), None)
            if existing:
                if existing["side"] != h.side:
                    reason = "CONFLICT"
                    c["conflict"] = [
                        {"existing_trade_id": existing["trade_id"], "side": existing["side"]}
                    ]
                elif ts(now) - ts(existing["decision_at"]) <= pd.Timedelta(
                    minutes=spec.AdmissionPolicy().episode_minutes
                ):
                    tid, admission = existing["trade_id"], "SUPPORTED"
                    support_ids = sorted(
                        set(existing["supporting_hypothesis_ids"]) | {h.hypothesis_id}
                    )
                    independent = len({hypotheses[i].family for i in support_ids})
                    event(
                        store,
                        rid,
                        "support",
                        "support:" + oid,
                        {"trade_id": tid, "ids": support_ids, "support_count": independent},
                        now,
                    )
                else:
                    reason = "DUPLICATE_EPISODE"
            elif len(positions) >= risk.max_positions:
                reason = "MAX_POSITIONS"
            elif (
                sum(p.get("notional", p.get("target_notional", 0)) for p in positions)
                + st["equity"] * risk.notional_fraction
                > st["equity"] * risk.gross_fraction + 1e-8
            ):
                reason = "EXPOSURE_CAP"
            else:
                reference = next((q for q in reversed(quotes) if q.asset == o.asset), None)
                latest_rate = next(
                    (r for r in reversed(rates) if r.asset == o.asset and ts(r.at) <= ts(now)), None
                )
                if reference is None or ts(now) - ts(reference.at) > pd.Timedelta(minutes=30):
                    reason = "DATA_STALE"
                elif latest_rate is None or ts(now) - ts(latest_rate.at) > pd.Timedelta(hours=2):
                    reason = "FUNDING_UNAVAILABLE"
                else:
                    _, ntl = size(
                        st["equity"],
                        fill_price(reference.price, h.side, o.asset, entry=True),
                        o.asset,
                    )
                    if ntl < risk.min_notional:
                        reason = "MIN_NOTIONAL"
                    else:
                        tid = content_id("papertrade_v2_", {"run_id": rid, "opportunity_id": oid})
                        admission, support_ids = "ADMITTED", [h.hypothesis_id]
                        event(
                            store,
                            rid,
                            "pending",
                            "pending:" + tid,
                            {
                                "trade_id": tid,
                                "primary_hypothesis_id": h.hypothesis_id,
                                "supporting_hypothesis_ids": support_ids,
                                "support_count": 1,
                                "family": h.family,
                                "asset": o.asset,
                                "side": h.side,
                                "signal_at": o.signal_at,
                                "available_at": o.available_at,
                                "decision_at": ts(now).isoformat(),
                                "horizon_minutes": h.horizon_minutes,
                                "equity_at_decision": st["equity"],
                                "target_notional": st["equity"] * risk.notional_fraction,
                                "leverage": risk.leverage,
                                "evidence": observation_payload(store, o),
                            },
                            now,
                        )
        if reason == "NO_SIGNAL":
            admission = "NO_SIGNAL"
        if reason not in spec.REJECTIONS and reason is not None:
            raise ValueError("unrecognised rejection reason")
        payload = {
            **observation_payload(store, o),
            "hypothesis": h.model_dump(mode="json") if h else None,
            "admission": admission,
            "rejection_reason": reason,
            "trade_id": tid,
            "supporting_hypothesis_ids": support_ids,
            "conflict": c.get("conflict"),
            "context_snapshot_id": (o.context or {}).get("snapshot_id"),
            "microstructure_feature_version": h.feature_version
            if h and h.source_phase == 24
            else None,
            "frozen_horizon_minutes": h.horizon_minutes if h else None,
            "evaluated_at": ts(now).isoformat(),
            "side": o.intended_side or h.side,
        }
        store.con.execute(
            "INSERT INTO paper_v2_opportunities VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                oid,
                rid,
                o.hypothesis_id,
                o.asset,
                o.intended_side or h.side,
                ts(o.signal_at).to_pydatetime(),
                ts(o.available_at).to_pydatetime(),
                ts(now).to_pydatetime(),
                ts(activated_at).to_pydatetime(),
                o.fired,
                admission,
                reason,
                tid,
                canonical_json(payload),
            ],
        )
        totals[admission] += 1
    return dict(totals)


def outcomes(store, rid, quotes, rates, now):
    for o in rows(
        store,
        "SELECT o.* FROM paper_v2_opportunities o LEFT JOIN paper_v2_outcomes f "
        "USING(opportunity_id) WHERE o.run_id=? AND o.fired AND f.opportunity_id IS NULL",
        [rid],
    ):
        p = json.loads(o["payload"])
        h = p["frozen_horizon_minutes"]
        if h is None or not p["causal"] or ts(o["available_at"]) < ts(o["signal_at"]):
            continue
        causal_start = max(ts(o["evaluated_at"]), ts(o["available_at"]))
        path = [q for q in quotes if q.asset == o["asset"] and ts(q.at) > causal_start]
        entries = [q for q in path if q.kind in ("minute_mid", "bar_open")]
        if not entries:
            continue
        en = entries[0]
        if ts(en.at) - ts(o["evaluated_at"]) > pd.Timedelta(minutes=20):
            continue
        due = ts(en.at) + pd.Timedelta(minutes=h)
        ends = [q for q in path if ts(q.at) >= due and q.kind != "bar_open"]
        if not ends:
            continue
        end = ends[0]
        fund, missing = funding_cost(o["asset"], o["side"], 1, en.at, end.at, rates)
        if missing:
            continue
        ef = fill_price(en.price, o["side"], o["asset"], entry=True)
        xf = fill_price(end.price, o["side"], o["asset"], entry=False)
        fee = spec.ExecutionPolicy().fee_bps / 10000 * (1 + xf / ef)
        gross = _sign(o["side"]) * (xf / ef - 1)
        result = {
            "hypothetical_only": True,
            "entry_at": en.at,
            "exit_at": end.at,
            "gross_return": _sign(o["side"]) * (end.price / en.price - 1),
            "net_return": gross - fee - fund,
            "funding": fund,
            "exit_delay_minutes": (ts(end.at) - due).total_seconds() / 60,
            **path_metrics(
                o["side"],
                en.price,
                [Quote(en.asset, en.at, en.available_at, en.price, en.price, en.price)]
                + [q for q in price_path(path, o["asset"], en.at) if ts(q.at) <= ts(end.at)],
            ),
        }
        store.con.execute(
            "INSERT INTO paper_v2_outcomes VALUES (?,?,?)",
            [o["opportunity_id"], ts(now).to_pydatetime(), canonical_json(clean(result))],
        )


def degrade(store, rid, now):
    st = state(store, rid)
    for h in spec.bootstrap():
        if h.hypothesis_id in st["hypotheses"]:
            continue
        closed = [p for p in st["closed"] if p["primary_hypothesis_id"] == h.hypothesis_id]
        rule = spec.AdmissionPolicy()
        recent = closed[-rule.degradation_recent_trades :]
        reason = None
        if (
            len(closed) >= rule.min_closed_for_degradation
            and sum(p["trade_return"] for p in recent) / len(recent)
            <= rule.degradation_mean_net_return
            and sum(p["net_pnl"] < 0 for p in recent) >= 40
        ):
            reason = "100+ trades, last 50 mean net <= -0.5% and >=40 losses"
        last = store.con.execute(
            "SELECT max(signal_at) FROM paper_v2_opportunities WHERE run_id=? "
            "AND hypothesis_id=? AND fired",
            [rid, h.hypothesis_id],
        ).fetchone()[0]
        activation = load(store, rid)["activated_at"]
        if ts(now) - ts(last or activation) >= pd.Timedelta(days=rule.dormant_days):
            reason = "no fired signal for 30 days: dormant dependency/hypothesis review required"
        if reason:
            event(
                store,
                rid,
                "hypothesis_state",
                "degrade:" + h.hypothesis_id,
                {"hypothesis_id": h.hypothesis_id, "state": "DEGRADED", "reason": reason},
                now,
            )


def evaluate(store, rid, *, now=None, sources=None) -> dict:
    """Injectable read-only sources enable mechanical tests. CLI has no injection/clock flag."""
    at = ts(now or utcnow())
    run = load(store, rid)
    if at < ts(run["activated_at"]):
        raise ValueError("evaluation precedes activation")
    evaluation_id = content_id("papereval_v2_", {"run_id": rid, "at": at.isoformat()})
    prior = store.con.execute(
        "SELECT payload FROM paper_v2_evaluations WHERE evaluation_id=?", [evaluation_id]
    ).fetchone()
    if prior:
        return json.loads(prior[0])
    start, cpu = time.perf_counter(), time.process_time()
    source = sources or Sources(store, at)
    source.run_id = rid
    st = state(store, rid)
    if st["status"] != "ACTIVE" and not st["open"] and not st["pending"]:
        return {"state": st["status"], "evaluated": False}
    active_times = [
        p.get("entry_at", p["decision_at"])
        for p in list(st["open"].values()) + list(st["pending"].values())
    ]
    since = min(
        [ts(x) for x in active_times] + [max(ts(run["activated_at"]), at - pd.Timedelta(days=2))]
    )
    try:
        quotes = _valid_quotes(source.quotes(since), at)
        rates = [
            r
            for r in source.funding(since - pd.Timedelta(hours=2))
            if ts(r.available_at) <= at and ts(r.at) <= at
        ]
        observations = (
            source.observations(spec.bootstrap(), run["activated_at"])
            if st["status"] == "ACTIVE"
            else []
        )
        with store.transaction():
            manage(store, rid, quotes, rates, at)
            result = admit(store, rid, observations, quotes, rates, at, run["activated_at"])
            outcomes(store, rid, quotes, rates, at)
            degrade(store, rid, at)
            result.update(
                run_id=rid,
                evaluated_at=at.isoformat(),
                status="ok",
                dependencies=getattr(source, "dependencies", {}),
                wall_seconds=time.perf_counter() - start,
                cpu_seconds=time.process_time() - cpu,
                peak_rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
            )
            store.con.execute(
                "INSERT INTO paper_v2_evaluations VALUES (?,?,?,?,?)",
                [evaluation_id, rid, at.to_pydatetime(), "ok", canonical_json(clean(result))],
            )
        return result
    except Exception as exc:
        result = {
            "run_id": rid,
            "evaluated_at": at.isoformat(),
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "wall_seconds": time.perf_counter() - start,
            "cpu_seconds": time.process_time() - cpu,
        }
        store.con.execute(
            "INSERT INTO paper_v2_evaluations VALUES (?,?,?,?,?)",
            [evaluation_id, rid, at.to_pydatetime(), "error", canonical_json(result)],
        )
        raise
