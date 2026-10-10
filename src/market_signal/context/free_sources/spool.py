"""Phase 26A fsynced receipt primitives reused on a separate information-only spool."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from market_signal.context.gateway.spool import Spool as GatewaySpool
from market_signal.context.gateway.spool import durable_create
from market_signal.research.lab.common import canonical_json, content_id


def default_root():
    from market_signal.config import get_settings

    return Path(
        os.environ.get("PRISM_FREE_SOURCES_DIR")
        or get_settings().paths.db.parent / "free_event_sources"
    )


def replace_durable(path, obj):
    tmp = path.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("x") as f:
            f.write(canonical_json(obj))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        tmp.unlink(missing_ok=True)


class Spool(GatewaySpool):
    def state(self, pid):
        path = self.root / f"state-{pid}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def save_state(self, pid, state):
        replace_durable(self.root / f"state-{pid}.json", state)

    def accept_batch(self, source, observations, received, stats):
        rid = content_id("ctxgw_", {"provider": source["id"], "observations": observations})
        with self.lock():
            rec = self.read("receipts", rid)
            if rec is None:
                self.check_capacity()
                rec = dict(
                    receipt_id=rid,
                    gateway_received_at=received.isoformat(),
                    source=source,
                    observations=observations,
                    stats=stats,
                    observations_sha256=content_id("", observations),
                )
                durable_create(self.root / "receipts" / f"{rid}.json", rec)
            # Re-fsync an existing receipt too: recovery may follow link() before directory fsync.
            path = self.root / "receipts" / f"{rid}.json"
            with path.open("rb") as f:
                os.fsync(f.fileno())
            fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self.seal(rid)
        return rec

    def pending(self):
        # Completion is published after DB commit and outbox. O(number of pending), not
        # repeated parsing of the whole lifetime receipt history every five seconds.
        done = {p.name for p in (self.root / "done").glob("*.json")}
        return sorted(
            (
                json.loads(p.read_text())
                for p in (self.root / "receipts").glob("*.json")
                if p.name not in done
            ),
            key=lambda r: (r["gateway_received_at"], r["receipt_id"]),
        )
