"""Immutable receipt files + append-only audit/attempts on the persistent volume. No DB."""

from __future__ import annotations

import fcntl
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_signal.research.lab.common import canonical_json, content_id, strict_json


def now() -> datetime:
    return datetime.now(UTC)


def default_root() -> Path:
    from market_signal.config import get_settings

    return Path(
        os.environ.get("PRISM_CONTEXT_GATEWAY_DIR")
        or get_settings().paths.db.parent / "context_gateway"
    )


def durable_create(path: Path, obj: dict) -> None:
    """Publish an entire immutable record. Both file content AND directory entry fsync."""
    tmp = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x", encoding="utf-8") as f:
            os.chmod(tmp, 0o600)
            f.write(canonical_json(obj) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)  # never replace a receipt, completion or timing seal
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


class Spool:
    def __init__(self, root: Path, *, rate: int | None = None, max_bytes: int | None = None):
        rate = (
            rate if rate is not None else int(os.environ.get("PRISM_CONTEXT_RATE_PER_MINUTE", "10"))
        )
        max_bytes = (
            max_bytes
            if max_bytes is not None
            else int(os.environ.get("PRISM_CONTEXT_SPOOL_MAX_BYTES", str(128 * 1024 * 1024)))
        )
        if not 1 <= rate <= 60 or max_bytes < 1:
            raise ValueError("invalid gateway rate or spool bounds")
        self.root, self.rate, self.max_bytes = root, rate, max_bytes
        root.mkdir(parents=True, exist_ok=True)
        for name in ("receipts", "seals", "done", "attempts"):
            (root / name).mkdir(exist_ok=True)
        for directory in (root, root.parent):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    @contextmanager
    def lock(self):
        with (self.root / "spool.lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def read(self, kind: str, rid: str) -> dict | None:
        # Receipt IDs are opaque hashes, never caller-provided paths.
        if (
            len(rid) != 70
            or not rid.startswith("ctxgw_")
            or any(c not in "0123456789abcdef" for c in rid[6:])
        ):
            raise ValueError("invalid receipt ID")
        p = self.root / kind / f"{rid}.json"
        return strict_json(p.read_text()) if p.exists() else None

    def records(self) -> list[dict]:
        return [strict_json(p.read_text()) for p in sorted((self.root / "receipts").glob("*.json"))]

    def check_capacity(self):
        if (
            shutil.disk_usage(self.root).free < 16 * 1024 * 1024
            or sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file()) >= self.max_bytes
        ):
            raise OSError("gateway spool capacity exhausted")

    def audit(self, kind: str, *, identity: str | None = None, receipt_id: str | None = None):
        # Only predefined reason codes and identities. No tokens, bodies or validation echoes.
        # Reject traffic must not bypass the spool bound and fill the runtime volume.
        self.check_capacity()
        rec = {
            "at": now().isoformat(),
            "kind": kind,
            "identity": identity,
            "receipt_id": receipt_id,
        }
        path = self.root / "audit.jsonl"
        # A crash can leave a partial final diagnostic line. Discard only that uncommitted
        # tail before appending; accepted receipts and completion records are immutable.
        if path.exists():
            data = path.read_bytes()
            if data and not data.endswith(b"\n"):
                with path.open("r+b") as f:
                    f.truncate(data.rfind(b"\n") + 1)
                    os.fsync(f.fileno())
        with path.open("a") as f:
            f.write(canonical_json(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def audits(self) -> list[dict]:
        path = self.root / "audit.jsonl"
        if not path.exists():
            return []
        return [
            strict_json(line)
            for line in path.read_text().splitlines(keepends=True)
            if line.endswith("\n")
        ]

    def seal(self, rid: str) -> dict:
        seal = self.read("seals", rid)
        if seal is None:
            received = datetime.fromisoformat(self.read("receipts", rid)["gateway_received_at"])
            seal = {"receipt_id": rid, "spool_fsynced_at": max(now(), received).isoformat()}
            durable_create(self.root / "seals" / f"{rid}.json", seal)
        else:
            # A previous process may have died after linking the seal but before its
            # directory fsync. A retry must durably preserve the original timing too.
            path = self.root / "seals" / f"{rid}.json"
            with path.open("rb") as f:
                os.fsync(f.fileno())
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return seal

    def accept(
        self,
        raw: dict,
        sub,
        observation,
        identity: str,
        received: datetime,
        entity_map_version: str | None = None,
    ) -> dict:
        rid = content_id("ctxgw_", {"identity": identity, "submission_id": sub.submission_id})
        ph = content_id("", raw)
        with self.lock():
            recent = [
                a
                for a in self.audits()
                if datetime.fromisoformat(a["at"]) > now() - timedelta(minutes=1)
                and a["identity"] == identity
                and a["kind"] in ("accepted", "duplicate")
            ]
            if len(recent) >= self.rate:
                raise GatewayError(429, "rate_limited")
            existing = self.read("receipts", rid)
            if existing:
                if existing["payload_sha256"] != ph:
                    raise GatewayError(409, "submission_id_conflict")
                # Re-fsync even after a process died between link and directory fsync.
                path = self.root / "receipts" / f"{rid}.json"
                with path.open("rb") as f:
                    os.fsync(f.fileno())
                fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                rec, duplicate = existing, True
            else:
                if abs((received - sub.sent_at).total_seconds()) > 300:
                    raise GatewayError(409, "stale_submission")
                try:
                    self.check_capacity()
                except OSError:
                    raise GatewayError(503, "spool_full") from None
                rec = {
                    "receipt_id": rid,
                    "payload_sha256": ph,
                    "gateway_received_at": received.isoformat(),
                    "auth_identity": identity,
                    "provider": "chatgpt_work_v1",
                    "schema_version": "context_import_v1",
                    "payload": raw,
                    "observation": observation.model_dump(mode="json"),
                    "entity_map_version": entity_map_version,
                }
                durable_create(self.root / "receipts" / f"{rid}.json", rec)
                duplicate = False
            timing = self.seal(rid)
            try:
                self.audit(
                    "duplicate" if duplicate else "accepted", identity=identity, receipt_id=rid
                )
            except OSError:
                # The receipt and seal are already durable. Diagnostic capacity must
                # neither invalidate that acknowledgement nor prevent identical retries.
                self.last_failure_at = now().isoformat()
            done = self.read("done", rid)
            return {
                "status": "DUPLICATE" if duplicate else "ACCEPTED",
                "receipt_id": rid,
                "gateway_received_at": rec["gateway_received_at"],
                **timing,
                "logical_event_id": done.get("logical_event_id") if done else None,
            }

    def pending(self) -> list[dict]:
        return sorted(
            [r for r in self.records() if not self.read("done", r["receipt_id"])],
            key=lambda r: (r["gateway_received_at"], r["receipt_id"]),
        )

    def complete(self, rid: str, result: dict):
        with self.lock():
            if self.read("done", rid) is None:
                durable_create(self.root / "done" / f"{rid}.json", result)

    def attempt(self, rid: str, code: str):
        self.check_capacity()
        durable_create(
            self.root / "attempts" / f"{rid}_{uuid.uuid4().hex}.json",
            {"receipt_id": rid, "at": now().isoformat(), "error": code},
        )


class GatewayError(ValueError):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)
