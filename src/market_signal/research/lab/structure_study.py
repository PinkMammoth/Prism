"""Phase 17 governance adapter: preregistered structural falsification studies.

> Historical intraday availability is reconstructed under an explicit latency assumption
> rather than observed in real time. Phase 17 results are EXPLORATORY.

A structural study is not a Lab ``StrategyDefinition`` (daily, one condition tree, one
side): it is a frozen family of chain hypotheses evaluated on retained intraday snapshots.
It therefore gets its own registration/run/result tables (migration 19), following the
Phase 9/11 pattern, and reuses the Lab's machinery for everything else:

- datasets: ``capture_dataset`` + ``Ledger.register_dataset`` (strong, content-hashed,
  retained snapshots of ``perp_intraday_bars`` and ``perp_funding``); evaluation reads
  ONLY the retained rows, verified against their hashes (``Ledger.read_dataset``);
- software identity: ``SoftwareIdentity`` in ``lab_software``;
- clock ordering: ``ledger.ordered_now`` (the WSL clock-step guard);
- costs: Prism's perp cost machinery (``perps.backtest.perp_costs``), frozen per venue/coin;
- statistics: the Lab's BH implementation and Prism's declustering/plateau rules (see
  ``research/structure/study``).

Lifecycle (append-only; no update/delete API):

1. ``register``: captures and registers the datasets, freezes the definition
   (``study_id`` = its content hash). Nothing is evaluated. A name or definition can be
   registered once.
2. ``run``: verifies the retained datasets, COMMITS a run row (the exposure record) and
   only then evaluates. A second run needs ``rerun_of`` and a reason (e.g. the
   reproducibility rerun on a scratch copy); results are never replaced.
3. one terminal result per run: COMPLETED (payload + digest) or FAILED (error).

No consumer reads these tables, and no field can mark a study validated.

Phase 18 (relative strength / BTC dislocation) studies use the same tables and lifecycle:
the definition type is chosen by its ``study_version`` (``RelativeStudyDefinition`` for
``relative_strength_v1``), and each kind brings its own manifest, dataset selections and
evaluation. Nothing else differs.
"""

from __future__ import annotations

import json
import traceback
from uuid import uuid4

from market_signal.models.domain import Timeframe, utcnow
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.datasets import SeriesSelection, capture_dataset
from market_signal.research.lab.ledger import Ledger, LedgerError, ordered_now
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.structure.study.spec import (
    CostRef,
    DatasetRef,
    StudyDefinition,
    semantics,
)

TIMEFRAMES = ("4h", "1h", "15m")


class StudyError(LedgerError):
    pass


def _require(ledger: Ledger) -> None:
    if not ledger.store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='lab_structure_studies'"
    ).fetchone():
        raise StudyError("structure-study tables are absent; open Store writable once to migrate")


def _rows(ledger: Ledger, sql: str, args: list) -> list[dict]:
    cur = ledger.store.con.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


RELATIVE_VERSION = "relative_strength_v1"


def _is_relative(obj) -> bool:
    """True for a Phase 18 manifest or definition (they carry their own selections)."""
    from market_signal.research.relative.study import spec as rs

    return isinstance(obj, rs.StudyManifest | rs.RelativeStudyDefinition)


def _venue_coins(manifest) -> list[tuple[str, str]]:
    if _is_relative(manifest):
        return manifest.venue_coins()
    return [(v.venue, c) for v in manifest.venues for c in manifest.coins]


def _definition_class(definition_json: str):
    if json.loads(definition_json).get("study_version") == RELATIVE_VERSION:
        from market_signal.research.relative.study.spec import RelativeStudyDefinition

        return RelativeStudyDefinition
    return StudyDefinition


def selections(manifest, venue: str, coin: str) -> tuple[SeriesSelection, ...]:
    """The exact retained series of one (venue, coin). Phase 17: three bar timeframes and
    funding; Phase 18: the manifest's own per-timeframe windows and funding."""
    if _is_relative(manifest):
        return manifest.selections(venue, coin)
    v = next(x for x in manifest.venues if x.venue == venue)
    bars = tuple(
        SeriesSelection(kind="perp_intraday_bars", symbol=coin, source=venue,
                        timeframe=Timeframe(tf), start=v.start(tf), end=v.end)
        for tf in TIMEFRAMES
    )  # fmt: skip
    return (*bars, SeriesSelection(kind="perp_funding", symbol=coin, source=venue,
                                   start=v.start_funding, end=v.end))  # fmt: skip


def frozen_costs(manifest, perps_cfg: dict) -> tuple[CostRef, ...]:
    """Per-side fee and slippage from Prism's perp cost machinery, frozen at registration."""
    from market_signal.perps.backtest import perp_costs

    out = []
    for venue, coin in _venue_coins(manifest):
        c = perp_costs(perps_cfg, coin, venue)
        out.append(CostRef(venue=venue, coin=coin, fee_bps=c.fee_bps,
                           slippage_bps=c.slippage_bps))  # fmt: skip
    return tuple(out)


def verify_datasets(ledger: Ledger, defn) -> None:
    """Every referenced dataset is strong, retained and selects exactly the manifest's
    windows for its venue/coin (nothing more, nothing less)."""
    for ref in defn.datasets:
        ds = ledger.get_dataset(ref.dataset_id)
        if ds.strength != "content_sha256":
            raise StudyError(f"{ref.dataset_id} is not a retained content-hashed snapshot")
        want = sorted(canonical_json(s.model_dump(mode="python"))
                      for s in selections(defn.manifest, ref.venue, ref.coin))  # fmt: skip
        got = sorted(canonical_json(s.selection.model_dump(mode="python")) for s in ds.series)
        if want != got:
            raise StudyError(
                f"{ref.dataset_id} does not select the frozen {ref.venue}/{ref.coin} windows"
            )


def register(ledger: Ledger, manifest, *, perps_cfg: dict, software: SoftwareIdentity,
             origin: str, reason: str, max_rows: int = 250_000) -> StudyDefinition:  # fmt: skip
    """Capture + register the datasets, then freeze the study. Evaluates nothing."""
    _require(ledger)
    if not reason.strip() or not origin.strip():
        raise StudyError("registration needs a reason and an origin")
    if ledger.store.con.execute(
        "SELECT 1 FROM lab_structure_studies WHERE name=?", [manifest.name]
    ).fetchone():
        raise StudyError(f"study name {manifest.name!r} is already frozen; declare a new name")
    refs = []
    for venue, coin in _venue_coins(manifest):
        cap = capture_dataset(ledger.store, selections(manifest, venue, coin), max_rows=max_rows)
        refs.append(DatasetRef(venue=venue, coin=coin, dataset_id=ledger.register_dataset(cap)))
    if _is_relative(manifest):
        from market_signal.research.relative.study import spec as rs

        defn = rs.RelativeStudyDefinition(manifest=manifest, families=rs.families_spec(),
                                          costs=frozen_costs(manifest, perps_cfg),
                                          datasets=tuple(refs), semantics=rs.semantics())  # fmt: skip
    else:
        defn = StudyDefinition(manifest=manifest, costs=frozen_costs(manifest, perps_cfg),
                               datasets=tuple(refs), semantics=semantics())  # fmt: skip
    verify_datasets(ledger, defn)
    ledger.register_software(software)
    with ledger.store.transaction():
        if ledger.store.con.execute(
            "SELECT 1 FROM lab_structure_studies WHERE study_id=?", [defn.study_id]
        ).fetchone():
            raise StudyError("an identical study definition is already frozen")
        ledger.store.con.execute(
            "INSERT INTO lab_structure_studies VALUES (?,?,?,?,?,?,?,?,?,?)",
            [defn.study_id, manifest.name, defn.evidence_class, defn.availability_mode,
             manifest.assumed_latency_s, utcnow(), reason, origin, software.software_id,
             defn.canonical_json()],
        )  # fmt: skip
        for r in refs:
            ledger.store.con.execute(
                "INSERT INTO lab_structure_study_datasets VALUES (?,?,?,?)",
                [defn.study_id, r.venue, r.coin, r.dataset_id],
            )
    return defn


def get_study(ledger: Ledger, study_id: str) -> tuple:
    _require(ledger)
    rows = _rows(ledger, "SELECT * FROM lab_structure_studies WHERE study_id=?", [study_id])
    if not rows:
        raise StudyError(f"unknown study {study_id}")
    defn = _definition_class(rows[0]["definition"]).model_validate_json(rows[0]["definition"])
    if defn.study_id != study_id:
        raise StudyError("stored definition does not match its study ID")
    return defn, rows[0]


def run(ledger: Ledger, study_id: str, *, software: SoftwareIdentity, rerun_of: str | None = None,
        rerun_reason: str | None = None, evaluate=None) -> dict:  # fmt: skip
    """Commit a run row, THEN evaluate from the retained snapshots, then record one result."""
    from market_signal.research.structure.study.data import load_coin
    from market_signal.research.structure.study.run import digest

    defn, row = get_study(ledger, study_id)
    if evaluate is None:
        if _is_relative(defn):
            from market_signal.research.relative.study.run import evaluate
        else:
            from market_signal.research.structure.study.run import evaluate
    runs = _rows(
        ledger, "SELECT * FROM lab_structure_runs WHERE study_id=? ORDER BY attempt", [study_id]
    )
    if runs and not rerun_of:
        raise StudyError(
            f"study already run ({runs[-1]['run_id']}); an explicit rerun link and reason are required"
        )
    if rerun_of:
        if not rerun_reason or not rerun_reason.strip():
            raise StudyError("a rerun needs a reason")
        if rerun_of not in {r["run_id"] for r in runs}:
            raise StudyError("rerun_of must be an earlier run of this study")
    elif rerun_reason:
        raise StudyError("rerun_reason requires rerun_of")
    verify_datasets(ledger, defn)
    ledger.register_software(software)
    run_id = "srun_" + uuid4().hex
    with ledger.store.transaction():
        started = ordered_now(row["registered_at"])
        if started < row["registered_at"]:
            raise StudyError("clock precedes registration")
        ledger.store.con.execute(
            "INSERT INTO lab_structure_runs VALUES (?,?,?,?,?,?,?)",
            [
                run_id,
                study_id,
                len(runs) + 1,
                software.software_id,
                started,
                rerun_of,
                rerun_reason,
            ],
        )
    latency = defn.manifest.assumed_latency_s

    def load(venue: str, coin: str):
        return load_coin(
            ledger, defn.dataset(venue, coin), venue=venue, coin=coin, latency_s=latency
        )

    try:
        payload, meta = evaluate(defn, load)
        status, dig = "COMPLETED", digest(payload)
    except Exception as exc:  # recorded, never swallowed silently
        payload = {"error": {"kind": type(exc).__name__, "message": str(exc),
                             "traceback": traceback.format_exc()[-8000:]}}  # fmt: skip
        meta, status, dig = {}, "FAILED", None
    result_id = "sresult_" + uuid4().hex
    with ledger.store.transaction():
        done = ordered_now(started)
        if done < started:
            raise StudyError("completion clock precedes start")
        ledger.store.con.execute(
            "INSERT INTO lab_structure_results VALUES (?,?,?,?,?,?,?,?)",
            [result_id, run_id, done, status, defn.evidence_class, dig, canonical_json(payload),
             canonical_json(meta)],
        )  # fmt: skip
    return {"run_id": run_id, "result_id": result_id, "status": status, "result_digest": dig,
            "meta": meta, "payload": payload}  # fmt: skip


def inspect(ledger: Ledger, study_id: str) -> dict:
    defn, row = get_study(ledger, study_id)
    runs = _rows(ledger, "SELECT r.*, s.result_id, s.completed_at, s.status, s.result_digest, s.meta "
                         "FROM lab_structure_runs r LEFT JOIN lab_structure_results s USING (run_id) "
                         "WHERE r.study_id=? ORDER BY r.attempt", [study_id])  # fmt: skip
    for r in runs:
        r["meta"] = json.loads(r["meta"]) if r.get("meta") else None
    return {"study_id": study_id, "name": row["name"], "study_version": defn.study_version,
            "registered_at": row["registered_at"],
            "evidence_class": row["evidence_class"], "reason": row["reason"],
            "datasets": [d.model_dump() for d in defn.datasets], "runs": runs}  # fmt: skip


def result_payload(ledger: Ledger, run_id: str) -> dict:
    rows = _rows(ledger, "SELECT payload FROM lab_structure_results WHERE run_id=?", [run_id])
    if not rows:
        raise StudyError(f"no result for run {run_id}")
    return json.loads(rows[0]["payload"])


def list_studies(ledger: Ledger) -> list[dict]:
    _require(ledger)
    return _rows(ledger, "SELECT study_id, name, evidence_class, registered_at FROM "
                         "lab_structure_studies ORDER BY registered_at", [])  # fmt: skip
