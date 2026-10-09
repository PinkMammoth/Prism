"""Spool -> DuckDB ingest: the ONLY writer of the ``microstructure_*`` tables.

Runs as a short step of the authoritative runtime (``market microstructure ingest``, under the
runtime lock), so the live database keeps exactly one writing process at a time. Idempotent:

* a minute already stored with the same revision and content hash is a duplicate (no-op);
* a higher revision moves the stored row to ``microstructure_revisions`` and replaces it;
* the same/lower revision with DIFFERENT content is a conflict: never applied, counted, and
  the step fails (visible + alerted) instead of silently rewriting history.

On a claimed (production) database only records produced by the authoritative collector of
that same runtime are accepted; scratch/dev records are rejected, so smoke data can never
become production history. The production cutover is recorded once, from the first COMPLETE
minute so accepted, and never changed.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime

import pandas as pd

from market_signal.data.store import Store, authority_claim
from market_signal.microstructure import definitions as d
from market_signal.microstructure.spool import Spool
from market_signal.models.domain import utcnow

TS_FIELDS = {"first_recv_ms": "first_recv_at", "last_recv_ms": "last_recv_at",
             "finalized_ms": "finalized_at"}  # fmt: skip


def _ts(ms: int | None) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, UTC)


def _row(r: dict, ingested_at: datetime) -> dict:
    row = {c: r.get(c) for c in d.MINUTE_COLUMNS}
    row["minute_open"] = _ts(r["minute_open"])
    for src, dst in TS_FIELDS.items():
        row[dst] = _ts(r.get(src))
    row["ingested_at"] = ingested_at
    return row


class _Ingest:
    def __init__(self, store: Store, now: datetime):
        self.store, self.con, self.now = store, store.con, now
        self.claim = authority_claim(self.con)
        self.stats: Counter = Counter()
        self.runs: dict[str, tuple[str | None, str | None]] = {
            r[0]: (r[1], r[2]) for r in self.con.execute(
                "SELECT run_id, role, runtime_id FROM microstructure_provider_runs "
                "WHERE kind='process'").fetchall()
        }  # fmt: skip
        self.cutover = {r[0] for r in self.con.execute(
            "SELECT feature_version FROM microstructure_cutover").fetchall()}  # fmt: skip

    def accepted(self, run_id: str | None) -> bool:
        run = self.runs.get(run_id or "")
        if run is None:
            self.stats["rejected_unknown_run"] += 1
            return False
        if self.claim is not None and (run[0] != "authoritative" or run[1] != self.claim[0]):
            self.stats["rejected_not_authoritative"] += 1
            return False
        return True

    def production(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        return bool(self.claim and run and run[0] == "authoritative" and run[1] == self.claim[0])

    # ------------------------------------------------------------------ records

    def apply(self, recs: list[dict]) -> None:
        """Every record names its collector run; on a claimed database only the authoritative
        collector's records (minutes, late fills, thresholds, runs, connections) are kept."""
        minutes = []
        handlers = {"run_end": self._run_end, "conn_open": self._conn_open,
                    "conn_close": self._conn_close, "late": self._late, "lp_threshold": self._lp}  # fmt: skip
        for r in recs:
            k = r.get("kind")
            if k == "run_start":
                self.runs[r["run_id"]] = (r.get("role"), r.get("runtime_id"))
            run = r.get("session_id") if k == "minute" else r.get("run_id")
            if k not in handlers and k not in ("minute", "run_start"):
                self.stats["unknown_kind"] += 1
            elif not self.accepted(run):
                continue
            elif k == "run_start":
                self._run_start(r)
            elif k == "minute":
                minutes.append(r)
            else:
                handlers[k](r)
        if minutes:
            self._minutes(minutes)

    def _run_start(self, r: dict) -> None:
        n = self.con.execute(
            "INSERT INTO microstructure_provider_runs VALUES (?, 'process', NULL, ?, ?, ?, ?, NULL, "
            "NULL, ?) ON CONFLICT DO NOTHING RETURNING 1",
            [r["run_id"], r.get("runtime_id"), r.get("role"), r.get("git_commit"), _ts(r["at"]),
             json.dumps(r, sort_keys=True)],
        ).fetchall()  # fmt: skip
        self.stats["runs"] += len(n)

    def _run_end(self, r: dict) -> None:
        self.con.execute("UPDATE microstructure_provider_runs SET ended_at=?, end_reason=? "
                         "WHERE run_id=? AND ended_at IS NULL",
                         [_ts(r["at"]), r.get("reason"), r["run_id"]])  # fmt: skip

    def _conn_open(self, r: dict) -> None:
        run = self.runs.get(r["run_id"], (None, None))
        self.con.execute(
            "INSERT INTO microstructure_provider_runs VALUES (?, 'connection', ?, ?, ?, NULL, ?, "
            "NULL, NULL, ?) ON CONFLICT DO NOTHING",
            [r["conn_id"], r["run_id"], run[1], run[0], _ts(r["at"]), json.dumps(r, sort_keys=True)],
        )  # fmt: skip

    def _conn_close(self, r: dict) -> None:
        self.con.execute(
            "UPDATE microstructure_provider_runs SET ended_at=?, end_reason=?, payload=? "
            "WHERE run_id=? AND ended_at IS NULL",
            [_ts(r["at"]), r.get("reason"),
             json.dumps({"messages": r.get("messages"), "seconds": r.get("seconds")}), r["conn_id"]],
        )  # fmt: skip

    def _late(self, r: dict) -> None:
        n = self.con.execute(
            "INSERT INTO microstructure_late_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            [r["feature_version"], r["coin"], r["tid"], _ts(r["t"]), _ts(r["recv"]),
             _ts(r["minute_open"]), r["px"], r["sz"], r["is_buy"], r["disposition"]],
        ).fetchall()  # fmt: skip
        self.stats["late_events"] += len(n)

    def _lp(self, r: dict) -> None:
        n = self.con.execute(
            "INSERT INTO microstructure_lp_thresholds VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING RETURNING 1",
            [r["lp_version"], r["coin"], r["day"], r["status"], r.get("threshold"),
             r.get("n_prints", 0), r.get("days_used", 0), _ts(r["computed_ms"])],
        ).fetchall()  # fmt: skip
        self.stats["lp_thresholds"] += len(n)

    def _minutes(self, recs: list[dict]) -> None:
        lo = _ts(min(r["minute_open"] for r in recs))
        hi = _ts(max(r["minute_open"] for r in recs))
        have = {
            (fv, c, pd.Timestamp(m).value // 1_000_000): (rev, sha)
            for fv, c, m, rev, sha in self.con.execute(
                "SELECT feature_version, coin, minute_open, revision, content_sha FROM "
                "microstructure_minutes WHERE minute_open BETWEEN ? AND ?", [lo, hi]).fetchall()
        }  # fmt: skip
        new: dict[tuple, dict] = {}
        for r in recs:  # in spool order: a revision always follows its revision 0
            key = (r["feature_version"], r["coin"], r["minute_open"])
            cur = have.get(key)
            if cur is None:
                new[key] = r
                have[key] = (r["revision"], r["content_sha"])
            elif cur == (r["revision"], r["content_sha"]):
                self.stats["duplicates"] += 1
            elif r["revision"] > cur[0]:
                if key in new:  # its revision 0 is still pending in this batch: write it first
                    self._insert(new)
                    new = {}
                self._revise(key, r)
                have[key] = (r["revision"], r["content_sha"])
            else:
                self.stats["conflicts"] += 1
        self._insert(new)

    def _insert(self, new: dict[tuple, dict]) -> None:
        if not new:
            return
        rows = [_row(r, self.now) for r in new.values()]
        df = pd.DataFrame(rows, columns=list(d.MINUTE_COLUMNS))
        self.con.register("ms_new", df)
        try:
            cols = ", ".join(d.MINUTE_COLUMNS)
            self.con.execute(
                f"INSERT INTO microstructure_minutes ({cols}) SELECT {cols} FROM ms_new"
            )
        finally:
            self.con.unregister("ms_new")
        self.stats["minutes"] += len(rows)
        for r in new.values():
            self._maybe_cutover(r)

    def _revise(self, key: tuple, r: dict) -> None:
        fv, coin, m = key
        old = self.con.execute(
            "SELECT * FROM microstructure_minutes WHERE feature_version=? AND coin=? AND minute_open=?",
            [fv, coin, _ts(m)]).df()  # fmt: skip
        # DuckDB can return FLOAT NULLs as numpy.float32 NaNs in this mixed row.
        # Pandas' JSON encoder rejects those; missing fields must remain JSON null.
        archived = old.iloc[0]
        old_row = json.loads(archived.where(archived.notna(), None).to_json(date_format="iso"))
        self.con.execute("INSERT INTO microstructure_revisions VALUES (?, ?, ?, ?, ?, ?, ?) "
                         "ON CONFLICT DO NOTHING",
                         [fv, coin, _ts(m), int(old_row["revision"]), _ts(r["finalized_ms"]),
                          r["revision"], json.dumps(old_row, sort_keys=True)])  # fmt: skip
        row = _row(r, self.now)
        sets = [c for c in d.MINUTE_COLUMNS if c not in ("feature_version", "coin", "minute_open")]
        self.con.execute(
            f"UPDATE microstructure_minutes SET {', '.join(f'{c}=?' for c in sets)} "
            "WHERE feature_version=? AND coin=? AND minute_open=?",
            [row[c] for c in sets] + [fv, coin, _ts(m)],
        )  # fmt: skip
        self.stats["revisions"] += 1

    def _maybe_cutover(self, r: dict) -> None:
        fv = r["feature_version"]
        if fv in self.cutover or r["status"] != "COMPLETE" or not self.production(r["session_id"]):
            return
        assert self.claim is not None
        self.con.execute("INSERT INTO microstructure_cutover VALUES (?, ?, ?, ?, ?, ?, ?) "
                         "ON CONFLICT DO NOTHING",
                         [fv, self.claim[0], _ts(r["minute_open"]), r["coin"],
                          _ts(r["finalized_ms"]), r["session_id"], self.now])  # fmt: skip
        self.cutover.add(fv)
        self.stats["cutover_recorded"] += 1


def ingest(store: Store, spool: Spool, now: datetime | None = None) -> dict:
    """Ingest every complete spool line not yet ingested. Returns counts; ``status`` is
    ``failed`` on any conflict (nothing was overwritten)."""
    now = now or utcnow()
    job = _Ingest(store, now)
    offsets = dict(
        store.con.execute("SELECT path, bytes FROM microstructure_ingest_offsets").fetchall()
    )
    files = spool.files()
    for f in files:
        rel = str(f.resolve())  # absolute: two spool directories never share offsets
        off = offsets.get(rel, 0)
        if f.stat().st_size <= off:
            continue
        recs, new_off, bad = spool.read_lines(f, off)
        job.stats["invalid_lines"] += bad
        if new_off == off:
            continue
        with store.transaction():
            job.apply(recs)
            store.con.execute(
                "INSERT INTO microstructure_ingest_offsets VALUES (?, ?, ?) ON CONFLICT (path) "
                "DO UPDATE SET bytes=excluded.bytes, updated_at=excluded.updated_at",
                [rel, new_off, now],
            )
        job.stats["files_read"] += 1
    if len(files) >= 2:  # every file but the newest is closed and now fully ingested
        spool.set_ingested_watermark(f"{files[-2].parent.name}/{files[-2].name}")
    out = dict(job.stats)
    out["status"] = "failed" if job.stats["conflicts"] else "ok"
    out["production"] = job.claim is not None
    return out
