"""Versioned causal context packages; frozen context_snapshot_v1 is untouched."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from market_signal.context import ledger
from market_signal.context.macro import macro_state
from market_signal.context.taxonomy import Confidence, materiality, severity, taxonomy
from market_signal.intelligence.clocks import ClockBasis, utc
from market_signal.intelligence.definitions import DEFINITION, PACKAGE_VERSION
from market_signal.intelligence.positioning import positioning_summary
from market_signal.intelligence.repository import ReadOperation, decode_json, normalise
from market_signal.research.discovery.primitives import Frame
from market_signal.research.lab.common import canonical_json, content_id, strict_json

BAR_VALUES = ("open", "high", "low", "close", "volume", "trades")


def load_bars(operation, asset, *, start, source="hyperliquid", timeframe="1h"):
    """Reconstruct versions independently for EACH record's selected access basis.

    Later inaccessible revisions fall back to retained old values. An inaccessible
    intermediate revision blocks descendants: a replica cannot skip dependencies.
    """
    t = operation.cutoff
    where = "source=? AND coin=? AND timeframe=? AND close_time>=? AND close_time<=?"
    params = [source, asset, timeframe, utc(start), t]
    rows = operation.rows("perp_intraday_bars", where=where, params=params, order="open_time")
    revisions = operation.rows(
        "perp_intraday_revisions", where=where, params=params, order="open_time,revision"
    )
    by_open = {}
    for r in revisions:
        by_open.setdefault(r["open_time"], []).append(r)
    selected = []
    for row in rows:
        versions = []
        for r in by_open.get(row["open_time"], []):
            versions.append(
                {
                    **{
                        k: row[k]
                        for k in (
                            "source",
                            "coin",
                            "timeframe",
                            "open_time",
                            "close_time",
                            "derivation",
                            "first_observed_at",
                            "observed_live",
                        )
                    },
                    **{k: r["old_" + k] for k in BAR_VALUES},
                    "revision": r["revision"] - 1,
                    "updated_at": r["old_observed_at"],
                    "ingest_run_id": r["old_run_id"],
                }
            )
        versions.append(
            {
                k: row[k]
                for k in (
                    *BAR_VALUES,
                    "source",
                    "coin",
                    "timeframe",
                    "open_time",
                    "close_time",
                    "derivation",
                    "first_observed_at",
                    "observed_live",
                    "revision",
                    "updated_at",
                    "ingest_run_id",
                )
            }
        )
        picked = None
        for expected, version in enumerate(versions):
            if version["revision"] != expected:
                raise ValueError("bar revision history is incomplete")
            key = [source, asset, timeframe, str(utc(row["open_time"])), version["revision"]]
            eid = operation.include(
                "perp_intraday_bars",
                key,
                version,
                version["updated_at"],
                market_time=version["close_time"],
            )
            if eid is None:
                break
            picked = {**version, "evidence_id": eid}
        if picked:
            selected.append(picked)
    columns = [*BAR_VALUES, "open_time", "close_time", "revision", "evidence_id"]
    result = pd.DataFrame(selected) if selected else pd.DataFrame(columns=columns)
    for k in ("open_time", "close_time"):
        result[k] = pd.to_datetime(result[k], utc=True)
    if result["open_time"].duplicated().any():
        raise ValueError("duplicate source bars")
    return result


def _event_states(operation, asset):
    t = operation.cutoff
    rows = operation.rows(
        "context_events", where="first_seen_at<=?", params=[t], order="first_seen_at,event_id"
    )
    updates = operation.rows(
        "context_event_updates", where="observed_at<=?", params=[t], order="event_id,seq"
    )
    links = operation.rows(
        "context_asset_links",
        where="asset=? AND linked_at<=?",
        params=[asset, t],
        order="event_id,link_type",
    )
    by_event = {}
    for row in updates:
        by_event.setdefault(row["event_id"], []).append(row)
    source_ids, result = set(), []
    for row in rows:
        eid = operation.include("context_events", row["event_id"], row, row["first_seen_at"])
        if not eid:
            continue
        state = ledger._base_state(row)
        report_ids = [eid]
        access_history = [operation.evidence[eid]["clocks"]]
        source_ids.add(row["source_id"])
        if operation.basis != ClockBasis.ORIGIN_OBSERVED:
            state["attr_seen"] = {
                k: operation.evidence[eid]["selected_available_at"] for k in state["attr_seen"]
            }
        for seq, update in enumerate(by_event.get(row["event_id"], []), 1):
            if update["seq"] != seq:
                raise ValueError("event update history is incomplete")
            uid = operation.include(
                "context_event_updates", update["update_id"], update, update["observed_at"]
            )
            if not uid:
                break
            state = ledger._apply(state, SimpleNamespace(**update))
            if operation.basis != ClockBasis.ORIGIN_OBSERVED:
                changes = decode_json(update["changes"])
                for k in changes.get("attributes", {}):
                    state["attr_seen"][k] = operation.evidence[uid]["selected_available_at"]
            report_ids.append(uid)
            access_history.append(operation.evidence[uid]["clocks"])
            source_ids.add(update["source_id"])
        own_links, link_refs = [], []
        for link in links:
            if link["event_id"] != row["event_id"]:
                continue
            lid = operation.include(
                "context_asset_links",
                [row["event_id"], asset, link["link_type"]],
                link,
                link["linked_at"],
            )
            if lid:
                own_links.append(link["link_type"])
                link_refs.append(lid)
        state["evidence_ids"] = report_ids + link_refs
        state["access_history"] = access_history
        state["asset_link"] = next(
            (x for x in ("direct", "ecosystem", "market_wide") if x in own_links), None
        )
        result.append(state)
    for sid in sorted(source_ids):
        for row in operation.rows("context_sources", where="source_id=?", params=[sid]):
            operation.include("context_sources", sid, row, row["recorded_at"])
    return result


def _event_view(state, operation):
    link = state["asset_link"]
    m = materiality(
        state["subcategory"],
        Confidence(state["confidence"]),
        link=link,
        scope=state["scope"],
        loss_usd=(state["attributes"] or {}).get("loss_usd_estimate"),
    )
    start, end = ledger.relevance_window(state)
    return {
        **state,
        "active": ledger.is_active(state, operation.cutoff),
        "materiality": m,
        "severity": severity(m["score"]),
        "relevance": {"start": start.isoformat(), "end": end.isoformat()},
    }


def _activity(bars, t):
    out = {
        "version": "intelligence_hourly_activity_v1",
        "state": "unknown",
        "evidence_ids": [],
        "range_expansion": None,
        "volume_spike": None,
        "rv_state": None,
    }
    if len(bars) < 50 or t - bars["close_time"].iloc[-1] > pd.Timedelta(hours=3):
        return out
    segment = np.cumsum((bars["open_time"].diff() != pd.Timedelta(hours=1)).to_numpy())
    f = Frame(
        *(bars[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume")), segment
    )
    tra, volume, rvp = f.tr_atr[-1], f.rel_volume(20)[-1], f.rv_pct(24, 720)[-1]
    rvstate = None if not np.isfinite(rvp) else ("low", "normal", "high")[min(int(rvp * 3), 2)]
    expansion = None if not np.isfinite(tra) else bool(tra >= 2.5)
    spike = None if not np.isfinite(volume) else bool(volume >= 3)
    known = any(x is not None for x in (rvstate, expansion, spike))
    out.update(
        state="elevated"
        if bool(expansion) or bool(spike) or rvstate == "high"
        else "quiet"
        if rvstate == "low"
        else "normal"
        if known
        else "unknown",
        range_expansion=expansion,
        volume_spike=spike,
        rv_state=rvstate,
        tr_atr=tra,
        rel_volume=volume,
        rv_pct_30d=rvp,
        evidence_ids=bars["evidence_id"].tolist(),
        bar_close=bars["close_time"].iloc[-1],
    )
    return out


def _market(operation, cohort, asset_bars):
    t, changes, refs = operation.cutoff, {}, {}
    for coin in cohort:
        bars = asset_bars.get(coin)
        if bars is None:
            bars = load_bars(operation, coin, start=t - pd.Timedelta(hours=30))
        recent = bars[bars["close_time"] >= t - pd.Timedelta(hours=30)]
        value = None
        if len(recent) and t - recent["close_time"].iloc[-1] <= pd.Timedelta(hours=3):
            last = recent.iloc[-1]
            reference_time = last["close_time"] - pd.Timedelta(hours=24)
            base = recent[recent["close_time"] == reference_time]
            if len(base) and base["close"].iloc[-1] > 0:
                value = (last["close"] / base["close"].iloc[-1] - 1) * 100
                refs[coin] = [base["evidence_id"].iloc[-1], last["evidence_id"]]
        changes[coin] = value
    macro = {}
    for sid in ("DGS2", "DGS10", "VIXCLS", "DTWEXBGS", "DEXJPUS", "SP500", "DCOILWTICO"):
        values = []
        for row in operation.rows(
            "macro_observations",
            where="series_id=? AND obs_date>=? AND obs_date<=?",
            params=[sid, (t - pd.Timedelta(days=60)).date(), t.date()],
            order="obs_date,realtime_start,source",
        ):
            key = [sid, row["source"], str(row["obs_date"]), str(row["realtime_start"])]
            observed = operation.observed_ingestion(row)
            eid = operation.include(
                "macro_observations", key, row, observed, market_time=row["available_at"]
            )
            if eid and row["value"] is not None:
                values.append((row, eid))
        # Each observation date contributes once, using its latest ACCESSIBLE vintage.
        by_date = {row["obs_date"]: (row, eid) for row, eid in values}
        chosen = [by_date[k] for k in sorted(by_date, reverse=True)][:6]
        macro[sid] = (
            None
            if not chosen
            else {
                "value": chosen[0][0]["value"],
                "obs_date": chosen[0][0]["obs_date"],
                "change_5obs": chosen[0][0]["value"] - chosen[-1][0]["value"]
                if len(chosen) == 6
                else None,
                "evidence_ids": [eid for _, eid in chosen],
                "pit_method": chosen[0][0]["pit_method"],
                "source": chosen[0][0]["source"],
            }
        )
    known = [v for v in changes.values() if v is not None]
    return {
        "cohort": list(cohort),
        "returns_24h_pct": changes,
        "return_evidence_ids": refs,
        "breadth_known_assets": len(known),
        "breadth_up_share": sum(v > 0 for v in known) / len(known) if len(known) >= 3 else None,
        "macro_known": macro,
        "semantics": "descriptive market state, not expected returns",
    }


def _implementation_manifest():
    root = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((root / "intelligence").glob("*.py")),
        *(
            root / "context" / f"{name}.py"
            for name in ("ledger", "taxonomy", "positioning", "macro")
        ),
        root / "research/discovery/primitives.py",
        root / "research/lab/common.py",
    ]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def context_package(
    repository, asset, cutoff, *, basis, horizon="daily", cohort=None, funding_cadence_hours=None
):
    """Pure read. Access basis is mandatory; no 'latest', account or write defaults."""
    if horizon not in DEFINITION["horizons"]:
        raise ValueError("horizon must be fast, daily or weekly")
    asset = asset.upper()
    cohort = tuple(sorted(set(cohort or ("BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE"))))
    if not asset or not cohort or len(cohort) > DEFINITION["max_assets"]:
        raise ValueError("invalid asset/cohort budget")
    operation = ReadOperation(repository, cutoff, basis)
    states = _event_states(operation, asset)
    views = []
    for state in states:
        if state["asset_link"] is None:
            continue
        view = _event_view(state, operation)
        last = utc(state["last_updated_at"])
        upcoming = (
            state["scheduled"]
            and state["event_time"]
            and operation.cutoff
            < utc(state["event_time"])
            <= operation.cutoff + pd.Timedelta(days=45)
        )
        if (
            view["active"]
            or upcoming
            or last >= operation.cutoff - pd.Timedelta(days=DEFINITION["horizons"][horizon])
        ):
            views.append(view)
    bars = load_bars(operation, asset, start=operation.cutoff - pd.Timedelta(days=40))
    positioning = positioning_summary(operation, asset, funding_cadence_hours=funding_cadence_hours)
    definition = {
        "read": DEFINITION,
        "taxonomy": taxonomy(),
        "implementation_sha256": _implementation_manifest(),
        "selection": {
            "horizon": horizon,
            "cohort": list(cohort),
            "funding_cadence_hours": positioning["funding_cadence_hours"],
        },
    }
    body = {
        "version": PACKAGE_VERSION,
        "asset": asset,
        "as_of": operation.cutoff.isoformat(),
        "clock_basis": operation.basis.value,
        "origin_id": repository.origin_id,
        "consumer_id": repository.consumer_id,
        "definition": definition,
        "definition_id": content_id("intelligencedef_", definition),
        "events": sorted(views, key=lambda v: (-v["materiality"]["score"], v["event_id"])),
        "macro": macro_state(states, operation.cutoff),
        "positioning": positioning,
        "activity": _activity(bars, operation.cutoff),
        "market": _market(operation, cohort, {asset: bars}),
        "semantics": "causal descriptive read; no recommendation, risk, sizing or execution authority",
    }
    body["evidence"] = operation.manifest()
    body["freshness"] = {
        "warnings": sorted(operation.warnings),
        "access_rule": "every cited input accessible by as_of under selected clock basis",
    }
    body = normalise(body)
    if len(canonical_json(body).encode()) > DEFINITION["max_package_bytes"]:
        raise ValueError("intelligence package byte budget exceeded")
    return {"package_id": content_id("intpackage_", body), **body}


def validate_package(package):
    """Validate a retained package independently of current source values/definitions."""
    if isinstance(package, str):
        package = strict_json(package)
    if not isinstance(package, dict) or package.get("version") != PACKAGE_VERSION:
        raise ValueError("unsupported read package")
    body = {k: v for k, v in package.items() if k != "package_id"}
    if len(canonical_json(body).encode()) > DEFINITION["max_package_bytes"]:
        raise ValueError("intelligence package byte budget exceeded")
    if package.get("package_id") != content_id("intpackage_", body):
        raise ValueError("package content identity mismatch")
    if package["definition_id"] != content_id("intelligencedef_", package["definition"]):
        raise ValueError("definition content identity mismatch")
    from market_signal.intelligence.clocks import AccessClock
    from market_signal.intelligence.repository import evidence_id

    cutoff, basis = utc(package["as_of"]), ClockBasis(package["clock_basis"])
    for eid, evidence in package["evidence"].items():
        if (
            eid != evidence_id(evidence["table"], evidence["key"], evidence["record"])
            or eid != evidence["evidence_id"]
        ):
            raise ValueError("evidence content identity mismatch")
        clock = AccessClock(**evidence["clocks"])
        if clock.origin_id != package["origin_id"] or evidence["selected_basis"] != basis.value:
            raise ValueError("evidence origin/basis mismatch")
        available = clock.time(basis, package["consumer_id"])
        if (
            available is None
            or available > cutoff
            or available != utc(evidence["selected_available_at"])
        ):
            raise ValueError("evidence inaccessible at package cutoff")
        if any(
            ref not in package["evidence"] for ref in evidence.get("provenance_evidence_ids", [])
        ):
            raise ValueError("unresolved source provenance")

    def check_references(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "evidence_ids" and any(ref not in package["evidence"] for ref in child):
                    raise ValueError("unresolved evidence reference")
                if key == "return_evidence_ids" and any(
                    ref not in package["evidence"] for refs in child.values() for ref in refs
                ):
                    raise ValueError("unresolved return evidence reference")
                check_references(child)
        elif isinstance(value, list):
            for child in value:
                check_references(child)

    check_references(package)
    return json.loads(canonical_json(package))


def resolve_evidence(package, evidence_id):
    verified = validate_package(package)
    if evidence_id not in verified["evidence"]:
        raise KeyError(evidence_id)
    return verified["evidence"][evidence_id]
