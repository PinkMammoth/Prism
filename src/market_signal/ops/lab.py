"""Autotrader lab identity and its minimum causal seed (docs/RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md).

Infrastructure only. Nothing here registers a policy, creates a paper run, changes a
hypothesis or touches the source database (opened read-only, checksummed before and after).

- ``LAB_EXPERIMENT`` names the VPS laboratory's new prospective frontier. Railway's paper
  runs are predecessors whose evidence stays where it is; the lab never continues them.
- ``SEED_POLICY`` lists, for every table, whether a fresh lab database receives a copy of
  history up to an explicit frontier, and why. Execution history, runtime/host state,
  receipts and research authority are never seeded; microstructure is never seeded, so the
  lab's microstructure warmup starts at its own first authoritative minute (no backdating).
"""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from market_signal.data.store import MIGRATIONS, Store, new_id
from market_signal.models.domain import utcnow

# Railway runs the lab descends from (Phase 27/30 reports). Their evidence is preserved
# unchanged on Railway; the lab starts new accounts at its own activation.
PREDECESSOR_RUNS = {
    "nimble_baseline": "paperrun_nimble_a8aef8be36c02700688053a1e57d2f908573fe8898860450b00b889d4f98f54c",
    "event_immediate": "paperrun_nimble_a5fafb2cf008686ad99d2e1e039d428cd95349423d2e31778409810be6cce3d3",
    "event_confirmed": "paperrun_nimble_981fac864a79b04053e1ca081731a692db95b5e69cec9f29e74618e6e84e0528",
}  # fmt: skip
EXPERIMENT_ID = "paper_nimble_dynamic_lab_v1"
SEED_POLICY_ID = "lab_seed_v1"


def experiment() -> dict[str, Any]:
    """The frozen lab identity. Policy ids are computed from the released code, so a changed
    definition shows up here as a different identity, never silently."""
    from market_signal.ops.profiles import PROFILES
    from market_signal.paper.nimble import event_spec, spec
    from market_signal.paper.v2 import spec as baseline

    lab = PROFILES["autotrader_lab"]
    d = {
        "experiment_id": EXPERIMENT_ID,
        "profile": lab.identity,
        "profile_digest": lab.digest(),
        "runtime_id": lab.runtime_ids["full"],
        "base_policies": {
            "nimble": spec.policy_id(),
            "event": event_spec.policy_id(),
            "risk": spec.risk_id(),
            "entry_universe": baseline.universe_id(),
            "event_universes": {v: event_spec.universe_id(v) for v in ("immediate", "confirmed")},
        },
        "predecessor_runs": dict(PREDECESSOR_RUNS),
        "continuation": "none: new paper accounts at lab activation; predecessors stay on Railway",
        "seed_policy": SEED_POLICY_ID,
        "dynamic_universe": "hyperliquid_dynamic_universe_v1: gated until VPS validation",
        # Accounts are independent run objects, each with its own frozen risk policy id;
        # alternative sizing (fixed fraction, volatility/risk-reward aware) or a thesis cohort
        # is a new policy id + run object, never a change to these.
        "sizing": "per-run frozen risk policy; parallel policies are parallel runs",
        "activation": "not activated (requires explicit approval after VPS validation)",
    }
    d["digest"] = hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()
    return d


@dataclass(frozen=True)
class Rule:
    kind: str  # all | upto | child | exclude
    column: str | None = None
    parent: tuple[str, str] | None = None  # (table, key) for child rows of seeded parents
    reason: str = ""


# Included inputs carry their complete history up to the frontier: lookbacks reach 120 days
# (funding percentiles) and 60 days (scheduled events), and the whole input set is tens of MB,
# so a guessed window would risk a silently different context for no real saving. Minimality
# is by category: what the lab trades on is seeded; authority, execution and host state never.
SEED_POLICY: dict[str, Rule] = {
    # definitions / reference
    "assets": Rule("all", reason="asset reference"),
    "context_sources": Rule("all", reason="context source registry"),
    "paper_nimble_policies": Rule("upto", "registered_at", reason="frozen released definitions"),
    # descriptive Phase 24B science labels read by Nimble observations (never decisions);
    # frozen at the frontier because the study's authority remains on Railway
    "lab_software": Rule("all", reason="software provenance referenced by 24B labels"),
    "lab_prospective_studies": Rule("upto", "registered_at", reason="24B labels (descriptive)"),
    "lab_prospective_checkpoints": Rule("upto", "as_of", reason="24B labels (descriptive)"),
    "lab_prospective_results": Rule("upto", "completed_at", reason="24B labels (descriptive)"),
    # causal trading-input history (warmup), complete up to the frontier
    "perp_intraday_bars": Rule("upto", "open_time", reason="Phase 22 15m/1h warmup"),
    "perp_funding": Rule("upto", "time", reason="funding continuity"),
    "perp_oi_history": Rule("upto", "observed_at", reason="positioning context"),
    "perp_snapshots": Rule("upto", "snapshot_at", reason="positioning context"),
    "perp_bars": Rule("upto", "ts", reason="daily context"),
    "context_hl_oi_hourly": Rule("upto", "captured_at", reason="Phase 23 OI warmup"),
    "context_ls_ratios": Rule("upto", "observed_at", reason="positioning crowding"),
    "macro_observations": Rule("upto", "available_at", reason="context snapshot macro"),
    # event lifecycle (scheduled events, ongoing episodes), so nothing old is seen as new
    "context_events": Rule("upto", "first_seen_at", reason="ongoing event episodes"),
    "context_event_updates": Rule(
        "child", parent=("context_events", "event_id"), reason="updates of seeded events"
    ),
    "context_asset_links": Rule(
        "child", parent=("context_events", "event_id"), reason="links of seeded events"
    ),
}
_EXCLUDE_PREFIXES = {
    "paper_": "execution/paper history: the lab starts a new frontier",
    "runtime_": "host runtime state/authority: never copied between runtimes",
    "microstructure_": "lab collects its own; warmup starts at its first authoritative minute",
    "copilot_": "research/copilot authority stays on Railway",
    "incubation_": "research authority stays on Railway",
    "lab_": "research authority stays on Railway",
    "context_gateway_": "Railway ingress receipts",
    "context_work_": "Railway ingress receipts",
    "context_free_": "host collector receipts",
    "perp_intraday_": "derived coverage/revisions are rebuilt by the lab",
    "universe_": "Dynamic Universe ranking is prospective: the lab accumulates its own "
    "(the frozen policy self-registers); never backfilled from another runtime",
}
_EXCLUDE_OTHER = "not a lab trading input"


def rule(table: str) -> Rule:
    if table in SEED_POLICY:
        return SEED_POLICY[table]
    if table == "schema_version":
        return Rule("exclude", reason="created by migrations")
    for prefix, reason in _EXCLUDE_PREFIXES.items():
        if table.startswith(prefix):
            return Rule("exclude", reason=reason)
    return Rule("exclude", reason=_EXCLUDE_OTHER)


def policy_digest() -> str:
    blob = json.dumps(
        {t: r.__dict__ for t, r in sorted(SEED_POLICY.items())}, sort_keys=True, default=str
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SeedRefused(RuntimeError):
    pass


def _where(table: str, r: Rule, frontier: datetime) -> tuple[str, list]:
    if r.kind == "all":
        return "", []
    if r.kind == "upto":
        return f"WHERE {r.column} <= ?", [frontier]
    if r.kind == "child":
        ptable, key = r.parent
        pw, pp = _where(ptable, SEED_POLICY[ptable], frontier)
        return f"WHERE {key} IN (SELECT {key} FROM src.{ptable} {pw})", pp
    raise ValueError(r.kind)


def build_seed(source: Path, target: Path, frontier: datetime, *, live_db: Path | None = None,
               now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """Create a NEW, unclaimed lab database at ``target`` from a verified snapshot ``source``.

    Refuses: a missing source, the live database itself, an existing target, a frontier in the
    future, or a source schema different from this code's. The source is attached read-only
    and its checksum is verified unchanged afterwards.
    """
    now = now or utcnow()
    source, target = Path(source), Path(target)
    if frontier.tzinfo is None:
        raise SeedRefused("frontier must be timezone-aware (UTC)")
    if frontier > now:
        raise SeedRefused("frontier is in the future; seeds never contain unknown data")
    if not source.is_file():
        raise SeedRefused(f"{source} does not exist")
    if live_db is not None and source.resolve() == Path(live_db).resolve():
        raise SeedRefused("seed from a verified snapshot/backup, never the live database")
    if target.exists():
        raise SeedRefused(f"{target} exists; a seed always creates a new database")
    sha_before = _sha256(source)
    store = Store(target)  # fresh: every migration, no authority claim
    ok = False
    try:
        con = store.con
        con.execute(f"ATTACH '{source}' AS src (READ_ONLY)")
        src_version = con.execute("SELECT max(version) FROM src.schema_version").fetchone()[0]
        if src_version != len(MIGRATIONS):
            raise SeedRefused(f"source schema {src_version} != code schema {len(MIGRATIONS)}")
        tables = sorted(
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_catalog='src' "
                "AND table_schema='main'"
            ).fetchall()
        )
        out: dict[str, Any] = {}
        # parents before children (foreign keys): SEED_POLICY order, then the exclusions
        order = [t for t in SEED_POLICY if t in tables] + [
            t for t in tables if t not in SEED_POLICY
        ]
        with store.transaction():
            for t in order:
                r = rule(t)
                total = con.execute(f"SELECT count(*) FROM src.{t}").fetchone()[0]
                if r.kind == "exclude":
                    out[t] = {"rule": "exclude", "reason": r.reason, "source_rows": total}
                    continue
                where, params = _where(t, r, frontier)
                con.execute(f"INSERT INTO main.{t} SELECT * FROM src.{t} {where}", params)
                n = con.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
                digest = con.execute(
                    f"SELECT md5(string_agg(md5(CAST(x AS VARCHAR)), '' ORDER BY "
                    f"md5(CAST(x AS VARCHAR)))) FROM main.{t} x"
                ).fetchone()[0]
                out[t] = {"rule": r.kind, "column": r.column,                           "reason": r.reason, "source_rows": total, "seeded_rows": n,
                          "digest": digest}  # fmt: skip
        con.execute("DETACH src")
        manifest = {
            "seed_id": new_id("labseed_"),
            "seed_policy": SEED_POLICY_ID,
            "seed_policy_digest": policy_digest(),
            "experiment": experiment(),
            "frontier": frontier.isoformat(),
            "created_at": now.isoformat(),
            "host": socket.gethostname(),
            "source": {"path": str(source), "sha256": sha_before, "schema_version": src_version},
            "target": str(target),
            "authority": "unclaimed: claim with the lab runtime id only at an approved cutover",
            "tables": out,
        }
        con.execute(
            "INSERT INTO runtime_events VALUES (?, 'lab_seeded', 'lab-seed', ?, NULL, ?)",
            [new_id("rtev_"), now, json.dumps(manifest, sort_keys=True, default=str)],
        )
        con.execute("CHECKPOINT")
        ok = True
    finally:
        store.close()
        if not ok:  # never leave a half-built lab database behind
            for p in (target, target.with_name(target.name + ".wal")):
                p.unlink(missing_ok=True)
    if _sha256(source) != sha_before:
        raise SeedRefused("source changed while seeding (it must be an immutable snapshot)")
    manifest["target_sha256"] = _sha256(target)
    return manifest


def record_activation(store: Store, *, run_ids: list[str], seed_manifest: dict[str, Any],
                      profile_record: dict[str, Any], revision: str | None,
                      now: datetime | None = None) -> str:  # fmt: skip
    """Append the lab activation provenance (experiment, seed frontier, runs, profile, runtime,
    host, revision). Called by a later, separately approved activation; never by this phase."""
    payload = {
        "experiment": experiment(),
        "run_ids": sorted(run_ids),
        "seed_id": seed_manifest["seed_id"],
        "seed_frontier": seed_manifest["frontier"],
        "seed_source_sha256": seed_manifest["source"]["sha256"],
        "profile": profile_record,
        "host": socket.gethostname(),
        "activated_at": (now or utcnow()).isoformat(),
    }
    eid = new_id("rtev_")
    store.con.execute(
        "INSERT INTO runtime_events VALUES (?, 'lab_experiment_activated', ?, ?, ?, ?)",
        [eid, profile_record.get("runtime_id") or "unset", now or utcnow(), revision,
         json.dumps(payload, sort_keys=True, default=str)],
    )  # fmt: skip
    return eid
