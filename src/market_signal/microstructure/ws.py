"""The persistent Hyperliquid WebSocket runner (public market data only; no credentials).

One connection carries 4 subscriptions per coin (trades, l2Book fast, l2Book, activeAssetCtx).
Receipt time is taken immediately on ``recv``. A watchdog declares the connection dead when
no message arrives for ``STALE_SOCKET_S``, when a coin's fast book is silent for
``STALE_BOOK_S`` (a silently missing subscription), or when a subscription is never
acknowledged; the runner then reconnects with capped exponential backoff and resubscribes.
Finalization keeps ticking while disconnected, so outage minutes are recorded as GAP/PARTIAL
from coverage evidence, never zero-filled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import resource
import signal
import socket
import time
import uuid
from collections import deque

from market_signal.microstructure import definitions as d
from market_signal.microstructure.engine import Engine, day_str
from market_signal.microstructure.live import LatestBooks
from market_signal.microstructure.spool import Spool

log = logging.getLogger("prism.microstructure")

STALE_SOCKET_S = 10.0
STALE_BOOK_S = 10.0
ACK_TIMEOUT_S = 15.0
PING_EVERY_S = 20.0
TICK_S = 0.25
STATUS_EVERY_S = 5.0
BACKOFF_MAX_S = 60.0
STABLE_AFTER_S = 60.0  # a connection that lasted this long resets the backoff
DISK_RAW_MIN_FREE = 0.15  # raw archive pauses below this free fraction (aggregates continue)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _ntl(levels: list[dict]) -> float:
    return float(sum(float(x["px"]) * float(x["sz"]) for x in levels))


def parse(msg: dict, recv: int) -> list[tuple]:
    """One Hyperliquid WS message -> normalized engine events (empty if not market data).

    ``side`` is the venue's taker side ("B" buy / "A" sell). Book messages carry no flag for
    fast vs full depth: a side with more than 5 levels can only be the 20-level feed; a
    message with at most 5 levels per side is the fast feed (and also a complete 20-level
    view when both sides are thinner than 5 levels).
    """
    ch = msg.get("channel")
    data = msg.get("data")
    out: list[tuple] = []
    if ch == "trades":
        for x in data or []:
            side = x.get("side")
            if side not in ("A", "B"):
                out.append(("?", recv, "bad_side"))
                continue
            h = x.get("hash") or ""
            hkey = h[:18] if h and h != d.ZERO_HASH else None
            out.append(("T", recv, x["coin"], int(x["time"]), float(x["px"]), float(x["sz"]),
                        side == "B", int(x["tid"]), hkey))  # fmt: skip
    elif ch == "l2Book":
        bids, asks = data["levels"]
        coin, t = data["coin"], int(data["time"])
        if not bids or not asks:
            return [("?", recv, "empty_book")]
        top = (float(bids[0]["px"]), float(asks[0]["px"]))
        if len(bids) > 5 or len(asks) > 5:
            out.append(("D", recv, coin, t, _ntl(bids[:20]), _ntl(asks[:20])))
        else:
            out.append(("F", recv, coin, t, *top, _ntl(bids[:5]), _ntl(asks[:5])))
            if len(bids) < 5 and len(asks) < 5:
                out.append(("D", recv, coin, t, _ntl(bids), _ntl(asks)))
    elif ch == "activeAssetCtx":
        c = data["ctx"]
        imp = c.get("impactPxs") or [None, None]

        def f(v):
            return None if v is None else float(v)

        out.append(("C", recv, data["coin"], f(c.get("openInterest")), f(c.get("markPx")),
                    f(c.get("oraclePx")), f(c.get("funding")), f(imp[0]), f(imp[1])))  # fmt: skip
    elif ch == "subscriptionResponse":
        s = (data or {}).get("subscription") or {}
        feed = {"trades": "trades", "activeAssetCtx": "ctx"}.get(s.get("type"))
        if s.get("type") == "l2Book":
            feed = "book5" if s.get("fast") else "book20"
        if feed and s.get("coin"):
            out.append(("A", recv, s["coin"], feed))
    elif ch == "error":
        out.append(("?", recv, "error:" + str(data)[:120]))
    return out


def subscriptions(coins: tuple[str, ...]) -> list[dict]:
    subs = []
    for c in coins:
        subs += [{"type": "trades", "coin": c}, {"type": "l2Book", "coin": c, "fast": True},
                 {"type": "l2Book", "coin": c}, {"type": "activeAssetCtx", "coin": c}]  # fmt: skip
    return subs


class Collector:
    def __init__(self, spool: Spool, coins: tuple[str, ...] = d.COINS, *, url: str = d.WS_URL,
                 role: str = "", runtime_id: str = "", git_commit: str | None = None,
                 raw: bool = True, duration_s: float | None = None,
                 spool_days: int = 8, raw_days: int = 1):  # fmt: skip
        self.spool = spool
        self.coins = tuple(coins)
        self.url = url
        self.role, self.runtime_id, self.git_commit = role, runtime_id, git_commit
        self.raw = raw
        self.duration_s = duration_s
        self.spool_days, self.raw_days = spool_days, raw_days
        self.run_id = f"msrun_{uuid.uuid4().hex[:16]}"
        self.latest_books = LatestBooks(spool.root, self.run_id, role, runtime_id)
        self.stop = asyncio.Event()
        self.ws = None
        self.reconnects = 0
        self.backoff = 1.0
        self.last_error: str | None = None
        self.disconnected_since: int | None = None
        self.raw_paused = False
        self.raw_bytes = 0
        self.msg_times: deque = deque()
        self.msg_bytes: deque = deque()
        self.flush_ms: deque = deque(maxlen=500)
        self.started_ms = now_ms()
        self.engine: Engine | None = None
        self.conn_started = 0
        self.conn_msgs = 0
        self.close_reason: str | None = None
        self.pending: deque = deque()
        self.dropped_records = 0

    # ------------------------------------------------------------------ records

    def _emit(self, rec: dict) -> None:
        """Durably append; on a write failure keep the record (in order) and retry on the
        next emit. Pending records and write errors are reported in status (health fails)."""
        self.pending.append(rec)
        t0 = time.perf_counter()
        try:
            while self.pending:
                self.spool.append(self.pending[0], now_ms())
                self.pending.popleft()
        except OSError as exc:
            self.last_error = f"spool write failed: {exc}"
            log.error(self.last_error)
            if len(self.pending) > 100_000:  # bounded memory: oldest dropped, counted
                self.pending.popleft()
                self.dropped_records += 1
        self.flush_ms.append((time.perf_counter() - t0) * 1000)

    def _record_run(self, kind: str, **extra) -> None:
        self._emit({"kind": kind, "run_id": self.run_id, "at": now_ms(), "role": self.role,
                    "runtime_id": self.runtime_id, "git_commit": self.git_commit,
                    "feature_version": d.FEATURE_VERSION, "definition": d.definition_digest(),
                    "coins": list(self.coins), "url": self.url, "pid": os.getpid(),
                    "host": socket.gethostname(), **extra})  # fmt: skip

    # ------------------------------------------------------------------ main

    async def run(self) -> int:
        self.spool.lock()
        try:
            repaired = self.spool.repair()
            start = now_ms()
            last = self.spool.last_finalized(self.coins, d.FEATURE_VERSION)
            lp = self.spool.load_large_prints(start)
            self.engine = Engine(self.coins, self._emit, run_id=self.run_id, lp=lp)
            self._record_run("run_start", started_ms=start, repaired_bytes=repaired,
                             previous_last_minute={k: v for k, v in last.items()})  # fmt: skip
            self.engine.start(start, last)
            self.spool.prune(start, spool_days=self.spool_days, raw_days=self.raw_days)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, self.stop.set)
            if self.duration_s:
                loop.call_later(self.duration_s, self.stop.set)
            ticker = asyncio.create_task(self._ticker())
            try:
                await self._connections()
            finally:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker
                self.engine.tick(now_ms())
                self._flush_raw()
                self._record_run("run_end", reason="stopped", counters=dict(self.engine.counters))
                self._write_status()
                self.spool.close()
            return 0
        finally:
            self.spool.unlock()

    async def _connections(self) -> None:
        from websockets.asyncio.client import connect

        assert self.engine is not None
        while not self.stop.is_set():
            conn_id = f"msconn_{uuid.uuid4().hex[:12]}"
            self.close_reason = None
            try:
                async with connect(self.url, ping_interval=None, max_size=2**23,
                                   open_timeout=15, close_timeout=3) as ws:  # fmt: skip
                    self.ws = ws
                    self.conn_started, self.conn_msgs = now_ms(), 0
                    self.engine.feed(("O", now_ms(), conn_id))
                    self._emit({"kind": "conn_open", "conn_id": conn_id, "run_id": self.run_id,
                                "at": now_ms(), "reconnects": self.reconnects})  # fmt: skip
                    self.disconnected_since = None
                    for s in subscriptions(self.coins):
                        await ws.send(json.dumps({"method": "subscribe", "subscription": s}))
                    await self._receive(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors of any kind: reconnect
                self.close_reason = self.close_reason or f"{type(exc).__name__}: {str(exc)[:160]}"
            finally:
                self.ws = None
            reason = self.close_reason or ("stopped" if self.stop.is_set() else "closed by server")
            t = now_ms()
            if self.conn_started:
                self.engine.feed(("X", t, reason))
                self.latest_books.feed(("X", t, reason))
                self._emit({"kind": "conn_close", "conn_id": conn_id, "run_id": self.run_id,
                            "at": t, "reason": reason, "messages": self.conn_msgs,
                            "seconds": round((t - self.conn_started) / 1000, 1)})  # fmt: skip
            self.last_error = None if self.stop.is_set() else reason
            if self.stop.is_set():
                break
            self.disconnected_since = self.disconnected_since or t
            lasted = (t - self.conn_started) / 1000 if self.conn_started else 0
            self.conn_started = 0
            self.backoff = 1.0 if lasted >= STABLE_AFTER_S else min(self.backoff * 2, BACKOFF_MAX_S)
            self.reconnects += 1
            log.warning("disconnected (%s); reconnect #%d in %.1fs", reason, self.reconnects,
                        self.backoff)  # fmt: skip
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), self.backoff * random.uniform(0.8, 1.2))

    async def _receive(self, ws) -> None:
        assert self.engine is not None
        stop = asyncio.create_task(self.stop.wait())
        try:
            while not self.stop.is_set():
                recv_task = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait({recv_task, stop}, timeout=STALE_SOCKET_S,
                                             return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
                if recv_task not in done:
                    recv_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await recv_task
                    if not self.stop.is_set():
                        self.close_reason = f"stale: no message for {STALE_SOCKET_S:.0f}s"
                    return
                raw = recv_task.result()
                t = now_ms()
                self.conn_msgs += 1
                self.msg_times.append(t)
                self.msg_bytes.append(len(raw))
                try:
                    msg = json.loads(raw)
                    evs = parse(msg, t)
                except (ValueError, KeyError, TypeError, IndexError):
                    self.engine.counters["invalid"] += 1
                    continue
                for ev in evs:
                    if ev[0] == "?":
                        self.engine.counters["invalid"] += 1
                    else:
                        self.engine.feed(ev)
                        self.latest_books.feed(ev)
        finally:
            stop.cancel()

    # ------------------------------------------------------------------ periodic

    async def _ticker(self) -> None:
        assert self.engine is not None
        last_status = last_ping = last_disk = last_books = 0.0
        raw_minute = now_ms() // d.MINUTE_MS
        day = day_str(now_ms())
        while True:
            await asyncio.sleep(TICK_S)
            t = now_ms()
            self.engine.tick(t)
            if t // d.MINUTE_MS != raw_minute:
                raw_minute = t // d.MINUTE_MS
                self._flush_raw()
            if day_str(t) != day:  # cache the completed day's print histogram
                if day in self.engine.lp.days:
                    self.spool.save_day(day, self.engine.lp.days[day])
                day = day_str(t)
                self.spool.prune(t, spool_days=self.spool_days, raw_days=self.raw_days)
            mono = time.monotonic()
            if self.ws is not None and mono - last_ping >= PING_EVERY_S:
                last_ping = mono
                with contextlib.suppress(Exception):
                    await self.ws.send(json.dumps({"method": "ping"}))
            if self.ws is not None:
                await self._watchdog(t)
            if mono - last_disk >= 60:
                last_disk = mono
                self._check_disk()
            if mono - last_books >= 2:
                last_books = mono
                try:
                    self.latest_books.publish(t)
                except (OSError, ValueError) as exc:
                    self.last_error = f"book cache write failed: {exc}"
            if mono - last_status >= STATUS_EVERY_S:
                last_status = mono
                self._write_status()

    async def _watchdog(self, t: int) -> None:
        assert self.engine is not None
        if not self.conn_started or (t - self.conn_started) / 1000 < ACK_TIMEOUT_S:
            return
        reason = None
        for c, st in self.engine.coin.items():
            missing = set(d.FEEDS) - st.acked
            if missing:
                reason = f"subscription not acknowledged: {c} {sorted(missing)}"
                break
            age = (t - st.last.get("book5", 0)) / 1000
            if age > STALE_BOOK_S:
                reason = f"stale book: {c} silent for {age:.0f}s"
                break
        if reason and self.ws is not None:
            self.close_reason = reason
            with contextlib.suppress(Exception):
                await self.ws.close()

    def _flush_raw(self) -> None:
        assert self.engine is not None
        events, self.engine.journal = self.engine.journal, []
        if not self.raw or self.raw_paused or not events:
            return
        try:
            self.raw_bytes += self.spool.archive(events, now_ms())
        except OSError as exc:
            self.last_error = f"raw archive failed: {exc}"

    def _check_disk(self) -> None:
        import shutil

        u = shutil.disk_usage(self.spool.root)
        paused = u.free / u.total < DISK_RAW_MIN_FREE
        if paused != self.raw_paused:
            log.warning("raw archive %s (free %.1f%%)", "PAUSED" if paused else "resumed",
                        100 * u.free / u.total)  # fmt: skip
        self.raw_paused = paused

    def status(self) -> dict:
        assert self.engine is not None
        t = now_ms()
        while self.msg_times and self.msg_times[0] < t - 60_000:
            self.msg_times.popleft()
            self.msg_bytes.popleft()
        ru = resource.getrusage(resource.RUSAGE_SELF)
        fl = sorted(self.flush_ms)
        return {"written_ms": t, "run_id": self.run_id, "pid": os.getpid(), "role": self.role,
                "runtime_id": self.runtime_id, "git_commit": self.git_commit,
                "feature_version": d.FEATURE_VERSION, "definition": d.definition_digest(),
                "started_ms": self.started_ms, "url": self.url, "coins": list(self.coins),
                "reconnects": self.reconnects, "backoff_s": self.backoff,
                "last_error": self.last_error, "disconnected_since_ms": self.disconnected_since,
                "spool_errors": self.spool.errors, "pending_records": len(self.pending),
                "dropped_records": self.dropped_records, "raw": self.raw, "raw_paused": self.raw_paused,
                "raw_bytes_written": self.raw_bytes,
                "msgs_per_s_60s": round(len(self.msg_times) / 60, 2),
                "bytes_per_s_60s": round(sum(self.msg_bytes) / 60, 1),
                "flush_ms": {"p50": fl[len(fl) // 2] if fl else None,
                             "max": fl[-1] if fl else None},
                "cpu_s": round(ru.ru_utime + ru.ru_stime, 2), "max_rss_mb": round(ru.ru_maxrss / 1024, 1),
                **self.engine.health(t)}  # fmt: skip

    def _write_status(self) -> None:
        try:
            self.spool.write_status(self.status())
        except OSError as exc:
            self.last_error = f"status write failed: {exc}"
