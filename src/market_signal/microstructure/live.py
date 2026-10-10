"""Atomic latest public book cache. Data only; never opens DuckDB or decides a trade."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def timestamp(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


class LatestBooks:
    def __init__(self, root: Path, session_id: str, role: str, runtime_id: str):
        self.path = root / "latest_books.json"
        self.session_id, self.role, self.runtime_id = session_id, role, runtime_id
        self.books, self.contexts = {}, {}

    def feed(self, ev):
        if ev[0] == "F":
            _, recv, coin, at, bid, ask, *_ = ev
            self.books[coin] = dict(
                asset=coin,
                at=timestamp(at),
                received_at=timestamp(recv),
                bid=bid,
                ask=ask,
                session_id=self.session_id,
                kind="live_book",
            )
        elif ev[0] == "C":
            _, recv, coin, oi, mark, oracle, funding, *_ = ev
            self.contexts[coin] = dict(
                observed_at=timestamp(recv),
                open_interest=oi,
                funding_rate=funding,
                mark=mark,
                oracle=oracle,
            )
        elif ev[0] == "X":
            self.books.clear()  # old connection quotes must not survive a disconnect

    def publish(self, now_ms):
        payload = {
            "role": self.role,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "published_at": timestamp(now_ms),
            "books": self.books,
            "contexts": self.contexts,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with temp.open("w") as f:
            json.dump(payload, f, sort_keys=True, allow_nan=False)
        os.replace(temp, self.path)
