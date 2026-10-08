"""Governance of the prospective Phase 24B study (migration 24, append-only).

1. ``register``: freezes ``MicroStudyDefinition`` (``study_id`` = its content hash). Nothing
   is evaluated. A name or definition can be registered once.
2. ``checkpoint``: evaluates the frozen study at a grid instant ``as_of`` on the data Prism
   held then (``known_at=as_of``). The checkpoint row (the exposure record) is COMMITTED
   before evaluation; one terminal result follows (COMPLETED + digest, or FAILED + error).

Sequential-evaluation rules (no optional stopping):

* ``as_of`` is never chosen freely. ``daily`` checkpoints sit at 00:00 UTC, ``weekly`` at
  Monday 00:00 UTC; the first is the first grid instant after registration and each later
  one is exactly the next grid instant (a missed checkpoint is caught up in order, never
  skipped). ``due`` lists what is owed.
* ``as_of`` must be at least ``SETTLE`` before the run (the 15-minute ingest has caught up).
* Daily checkpoints are descriptive (compact payload); weekly checkpoints are the governed
  evidence (full payload). Every checkpoint is kept: a hypothesis that crossed a threshold
  and later fell back keeps both records, and ``history`` reports the whole path.
* A reproduction (``reproduces`` + reason) re-evaluates an earlier checkpoint's ``as_of``.

No consumer reads these tables, and no field can mark the study validated or live.
"""

from __future__ import annotations

import json
import traceback
from uuid import uuid4

import pandas as pd

from market_signal.data.store import Store
from market_signal.models.domain import utcnow
from market_signal.research.lab.common import canonical_json
from market_signal.research.lab.ledger import Ledger, LedgerError, ordered_now
from market_signal.research.lab.provenance import SoftwareIdentity
from market_signal.research.microdir import spec as sp

SETTLE = pd.Timedelta(hours=1)
CADENCES = ("daily", "weekly")


class MicroStudyError(LedgerError):
    pass


def _require(store: Store) -> None:
    if not store.con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='lab_prospective_studies'"
    ).fetchone():
        raise MicroStudyError("prospective-study tables are absent (schema < 24); open the "
                              "database writable once to migrate")  # fmt: skip


def _rows(store: Store, sql: str, args: list) -> list[dict]:
    cur = store.con.execute(sql, args)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def register(store: Store, defn: sp.MicroStudyDefinition, *, software: SoftwareIdentity,
             origin: str, reason: str) -> dict:  # fmt: skip
    _require(store)
    if not reason.strip() or not origin.strip():
        raise MicroStudyError("registration needs a reason and an origin")
    if store.con.execute("SELECT 1 FROM lab_prospective_studies WHERE name=? OR study_id=?",
                         [defn.name, defn.study_id]).fetchone():  # fmt: skip
        raise MicroStudyError(f"study {defn.name!r} is already frozen; a change needs a new name")
    Ledger(store).register_software(software)
    at = utcnow()
    with store.transaction():
        store.con.execute(
            "INSERT INTO lab_prospective_studies VALUES (?,?,?,?,?,?,?,?,?,?)",
            [defn.study_id, defn.name, defn.study_version, defn.evidence_class,
             defn.availability_mode, at, reason, origin, software.software_id,
             defn.canonical_json()])  # fmt: skip
    return {"study_id": defn.study_id, "name": defn.name, "registered_at": at.isoformat()}


def get_study(store: Store, study_id: str | None = None) -> tuple[sp.MicroStudyDefinition, dict]:
    _require(store)
    rows = (_rows(store, "SELECT * FROM lab_prospective_studies WHERE study_id=?", [study_id])
            if study_id else
            _rows(store, "SELECT * FROM lab_prospective_studies WHERE name=?", [sp.STUDY_NAME]))  # fmt: skip
    if not rows:
        raise MicroStudyError("the Phase 24B study is not registered in this database")
    defn = sp.MicroStudyDefinition.model_validate_json(rows[0]["definition"])
    if defn.study_id != rows[0]["study_id"]:
        raise MicroStudyError("stored definition does not match its study ID")
    return defn, rows[0]


def grid_after(t: pd.Timestamp, cadence: str) -> pd.Timestamp:
    """First grid instant strictly after ``t``."""
    t = _ts(t)
    d = t.floor("D") + pd.Timedelta(days=1)
    if cadence == "daily":
        return d
    return d + pd.Timedelta(days=(7 - d.weekday()) % 7)


def on_grid(t: pd.Timestamp, cadence: str) -> bool:
    t = _ts(t)
    return t == t.floor("D") and (cadence == "daily" or t.weekday() == 0)


def checkpoints(store: Store, study_id: str) -> list[dict]:
    rows = _rows(store, "SELECT c.*, r.result_id, r.completed_at, r.status, r.result_digest "
                        "FROM lab_prospective_checkpoints c LEFT JOIN lab_prospective_results r "
                        "USING (checkpoint_id) WHERE c.study_id=? ORDER BY c.seq", [study_id])  # fmt: skip
    for r in rows:
        r["as_of"] = _ts(r["as_of"])
    return rows


def due(store: Store, study_id: str, cadence: str, now=None) -> list[pd.Timestamp]:
    """Grid instants owed (in order) for ``cadence`` up to ``now - SETTLE``."""
    _, row = get_study(store, study_id)
    now = _ts(now or utcnow())
    done = [c["as_of"] for c in checkpoints(store, study_id)
            if c["cadence"] == cadence and c["reproduces"] is None]  # fmt: skip
    nxt = grid_after(max(done) if done else _ts(row["registered_at"]), cadence)
    out = []
    while nxt <= now - SETTLE:
        out.append(nxt)
        nxt = grid_after(nxt, cadence)
    return out


def compact(payload: dict) -> dict:
    """Daily (descriptive) payload: per-member headline numbers, maturity, coverage."""
    mem = {}
    for k, v in (payload.get("members") or {}).items():
        st = v.get("stats") or {}
        net = st.get("net") or {}
        mem[k] = {"episodes": st.get("episodes"), "evaluable": st.get("evaluable"),
                  "net_mean": net.get("mean"), "net_t": net.get("t"),
                  "gross_mean": (st.get("gross") or {}).get("mean"),
                  "per_day": (st.get("frequency") or {}).get("independent_per_day"),
                  "maturity": v.get("maturity"), "verdict": (v.get("verdict") or {}).get("verdict")}  # fmt: skip
    return {"study": payload.get("study"), "as_of": payload.get("as_of"), "compact": True,
            "definition_digest": payload.get("definition_digest"),
            "maturity": payload.get("maturity"), "data": payload.get("data"),
            "members": mem, "summary": payload.get("summary")}  # fmt: skip


def checkpoint(store: Store, study_id: str, cadence: str, as_of, *, software: SoftwareIdentity,
               evaluate, now=None, reproduces: str | None = None,
               reason: str | None = None) -> dict:  # fmt: skip
    """Commit a checkpoint row, THEN evaluate (``evaluate(defn, as_of) -> (payload, meta)``),
    then record one result."""
    from market_signal.research.structure.study.run import digest

    if cadence not in CADENCES:
        raise MicroStudyError(f"cadence must be one of {CADENCES}")
    defn, row = get_study(store, study_id)
    as_of = _ts(as_of)
    now = _ts(now or utcnow())
    prior = checkpoints(store, study_id)
    if reproduces is not None:
        src = next((c for c in prior if c["checkpoint_id"] == reproduces), None)
        if src is None:
            raise MicroStudyError("reproduces must be an earlier checkpoint of this study")
        if not reason or not reason.strip():
            raise MicroStudyError("a reproduction needs a reason")
        if src["as_of"] != as_of or src["cadence"] != cadence:
            raise MicroStudyError("a reproduction re-evaluates the same as_of and cadence")
    else:
        if reason:
            raise MicroStudyError("a reason is only recorded with a reproduction")
        if not on_grid(as_of, cadence):
            raise MicroStudyError(f"{cadence} checkpoints sit on the "
                                  f"{'00:00 UTC' if cadence == 'daily' else 'Monday 00:00 UTC'} grid")  # fmt: skip
        owed = due(store, study_id, cadence, now)
        if not owed or owed[0] != as_of:
            want = owed[0].isoformat() if owed else "none yet (settling)"
            raise MicroStudyError(f"the next {cadence} checkpoint is {want}; checkpoints are "
                                  "evaluated in order on the fixed grid (no skipping)")  # fmt: skip
    if as_of > now - SETTLE:
        raise MicroStudyError("as_of is too recent (ingest may not have caught up)")
    Ledger(store).register_software(software)
    cid = "mcheck_" + uuid4().hex
    with store.transaction():
        started = ordered_now(_ts(row["registered_at"]).to_pydatetime())
        store.con.execute(
            "INSERT INTO lab_prospective_checkpoints VALUES (?,?,?,?,?,?,?,?,?)",
            [cid, study_id, len(prior) + 1, cadence, as_of.to_pydatetime(), started,
             software.software_id, reproduces, reason])  # fmt: skip
    try:
        payload, meta = evaluate(defn, as_of)
        if cadence == "daily":
            payload = compact(payload)
        status, dig = "COMPLETED", digest(payload)
    except Exception as exc:  # recorded, never swallowed silently
        payload = {"error": {"kind": type(exc).__name__, "message": str(exc),
                             "traceback": traceback.format_exc()[-8000:]}}  # fmt: skip
        meta, status, dig = {}, "FAILED", None
    rid = "mresult_" + uuid4().hex
    with store.transaction():
        done = ordered_now(started)
        if done < started:
            raise MicroStudyError("completion clock precedes start")
        store.con.execute("INSERT INTO lab_prospective_results VALUES (?,?,?,?,?,?,?)",
                          [rid, cid, done, status, dig, canonical_json(payload),
                           canonical_json(meta)])  # fmt: skip
    return {"checkpoint_id": cid, "result_id": rid, "as_of": as_of.isoformat(),
            "cadence": cadence, "status": status, "result_digest": dig, "payload": payload}  # fmt: skip


def result(store: Store, checkpoint_id: str) -> dict:
    rows = _rows(store, "SELECT * FROM lab_prospective_results WHERE checkpoint_id=?",
                 [checkpoint_id])  # fmt: skip
    if not rows:
        raise MicroStudyError(f"no result for checkpoint {checkpoint_id}")
    r = rows[0]
    return {"status": r["status"], "result_digest": r["result_digest"],
            "payload": json.loads(r["payload"]), "meta": json.loads(r["meta"])}  # fmt: skip


def latest(store: Store, study_id: str, cadence: str | None = None) -> dict | None:
    cs = [c for c in checkpoints(store, study_id) if c["status"] == "COMPLETED"
          and c["reproduces"] is None and (cadence is None or c["cadence"] == cadence)]  # fmt: skip
    if not cs:
        return None
    c = max(cs, key=lambda x: (x["as_of"], x["cadence"] == "weekly", x["seq"]))
    return {"checkpoint": c, **result(store, c["checkpoint_id"])}


RANK = {v: i for i, v in enumerate(sp.VERDICTS)}


def history(store: Store, study_id: str) -> dict:
    """Every checkpoint (FAILED included) and, per member, its verdict path: the full path,
    the best verdict ever reached, the current one and whether it fell back."""
    rows = []
    paths: dict[str, list] = {}
    for c in checkpoints(store, study_id):
        entry = {"checkpoint_id": c["checkpoint_id"], "seq": c["seq"], "cadence": c["cadence"],
                 "as_of": c["as_of"].isoformat(), "status": c["status"],
                 "reproduces": c["reproduces"], "result_digest": c["result_digest"]}  # fmt: skip
        if c["status"] == "COMPLETED":
            p = result(store, c["checkpoint_id"])["payload"]
            entry["maturity"] = (p.get("maturity") or {}).get("study_level")
            entry["verdicts"] = (p.get("summary") or {}).get("verdicts")
            for k, v in (p.get("members") or {}).items():
                vd = (
                    v["verdict"]
                    if isinstance(v.get("verdict"), str)
                    else (v.get("verdict") or {}).get("verdict")
                )
                net = (
                    v.get("net_t")
                    if "net_t" in v
                    else ((v.get("stats") or {}).get("net") or {}).get("t")
                )
                if c["reproduces"] is None:
                    paths.setdefault(k, []).append({"as_of": entry["as_of"], "cadence": c["cadence"],
                                                    "verdict": vd, "net_t": net,
                                                    "maturity": v.get("maturity")})  # fmt: skip
        rows.append(entry)
    members = {}
    for k, p in paths.items():
        weekly = [x for x in p if x["cadence"] == "weekly"] or p
        best = max(weekly, key=lambda x: RANK.get(x["verdict"], -1))
        cur = weekly[-1]
        members[k] = {"path": p, "best": best, "current": cur,
                      "fell_back": RANK.get(cur["verdict"], -1) < RANK.get(best["verdict"], -1)}  # fmt: skip
    return {"study_id": study_id, "checkpoints": rows, "members": members}
