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
            from market_signal.universe.policy import CORE

            if coin not in CORE and mark and len(ev) >= 10:
                self.contexts[coin]["impact_bps"] = max(
                    (abs(x / mark - 1) * 10000 for x in ev[8:10] if x), default=0
                )
        elif ev[0] == "X":
            self.books.clear()  # old connection quotes must not survive a disconnect

    def feed_book(self, data, recv):
        """Satellite nearby-depth lower bound from the actual public quote snapshot."""
        from market_signal.universe.policy import CORE

        coin = data["coin"]
        if coin in CORE:
            return
        bids, asks = data["levels"]
        if not bids or not asks:
            self.books.pop(coin, None)
            return
        bid, ask = float(bids[0]["px"]), float(asks[0]["px"])
        depth = min(
            sum(float(x["px"]) * float(x["sz"]) for x in bids if float(x["px"]) >= bid * 0.999),
            sum(float(x["px"]) * float(x["sz"]) for x in asks if float(x["px"]) <= ask * 1.001),
        )
        self.books[coin] = dict(
            asset=coin,
            at=timestamp(int(data["time"])),
            received_at=timestamp(recv),
            bid=bid,
            ask=ask,
            depth_usd=depth,
            session_id=self.session_id,
            kind="live_book",
        )

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
