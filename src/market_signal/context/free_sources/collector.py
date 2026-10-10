"""Public deterministic polling. No database, execution engine, LLM, or paid credentials."""

from __future__ import annotations

import hashlib
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from market_signal.context.free_sources.parsers import parse
from market_signal.context.free_sources.registry import active, load
from market_signal.context.free_sources.spool import Spool, default_root, replace_durable
from market_signal.context.gateway.spool import now
from market_signal.research.lab.common import content_id

MAX_BODY = 2 * 1024 * 1024


def retry_delay(headers, received):
    delay = 60.0
    try:
        value = headers.get("retry-after", "")
        delay = max(
            delay,
            float(value)
            if value.isdigit()
            else (parsedate_to_datetime(value) - received).total_seconds(),
        )
    except (ValueError, TypeError):
        pass
    try:
        if headers.get("x-ratelimit-remaining") == "0":
            delay = max(delay, float(headers["x-ratelimit-reset"]) - received.timestamp())
    except (ValueError, KeyError):
        pass
    return delay


class Collector:
    def __init__(self, spool, cfg=None, *, stage=None, transport=None, clock=now, jitter=None):
        self.spool, self.cfg, self.clock = spool, cfg or load(), clock
        self.stage = stage
        self.sources = active(self.cfg, stage)
        self.next_due = {}
        self.last_heartbeat = 0.0
        self.jitter = jitter or (lambda: random.uniform(0, 0.1))
        self.client = httpx.Client(
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=True,
            transport=transport,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            headers={"User-Agent": self.cfg["user_agent"], "Accept-Encoding": "gzip"},
        )

    def close(self):
        self.client.close()

    def poll(self, source, *, force=False):
        pid = source["id"]
        st = self.spool.state(pid)
        started = self.clock()
        if not force and st.get("next_poll") and started < datetime.fromisoformat(st["next_poll"]):
            return st
        cpu, wall = time.thread_time(), time.monotonic()
        headers = {}
        # Daily unconditional refresh defends against broken/stale ETags.
        unconditional = (
            not st.get("last_unconditional")
            or (started - datetime.fromisoformat(st["last_unconditional"])).total_seconds() >= 86400
        )
        if not unconditional:
            if st.get("etag"):
                headers["If-None-Match"] = st["etag"]
            if st.get("last_modified"):
                headers["If-Modified-Since"] = st["last_modified"]
        if source["url"].startswith("https://api.github.com/"):
            headers.update(
                Accept="application/vnd.github+json", **{"X-GitHub-Api-Version": "2022-11-28"}
            )
            # Explicit opt-in existing read token only; never discover credentials elsewhere.
            token = os.environ.get("PRISM_FREE_GITHUB_TOKEN")
            if token:
                headers["Authorization"] = "Bearer " + token
        st.update(last_poll=started.isoformat(), http_status=None, error=None)
        downloaded = wire_bytes = accepted = duplicates = 0
        stats = {"detected": 0, "rejected": {}}
        try:
            with self.client.stream("GET", source["url"], headers=headers) as response:
                received = max(self.clock(), started)
                st["http_status"] = response.status_code
                st["rate_limit"] = {
                    k: v
                    for k, v in response.headers.items()
                    if k.startswith("x-ratelimit") or k == "retry-after"
                }
                if response.status_code in (403, 429) and (
                    response.status_code == 429
                    or response.headers.get("x-ratelimit-remaining") == "0"
                    or "retry-after" in response.headers
                ):
                    st["rate_limited_until"] = (
                        received + timedelta(seconds=retry_delay(response.headers, received))
                    ).isoformat()
                if response.status_code != 304:
                    response.raise_for_status()
                body = bytearray()
                if response.status_code != 304:
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if time.monotonic() - wall > 20:
                            raise TimeoutError("bounded total fetch time exceeded")
                        if len(body) > MAX_BODY:
                            raise ValueError("bounded source payload exceeded")
                    received = max(self.clock(), started)
                    downloaded, wire_bytes = len(body), response.num_bytes_downloaded
                    bh = hashlib.sha256(body).hexdigest()
                    if bh != st.get("body_hash"):
                        observations, stats = parse(source, bytes(body), received)
                        known = st.get("known", [])
                        fresh = [o for o in observations if content_id("", o) not in known]
                        duplicates = len(observations) - len(fresh)
                        accepted = len(fresh)
                        # Do not advance validators, hashes, or known IDs until fsync succeeds.
                        if fresh:
                            self.spool.accept_batch(source, fresh, received, stats)
                        known = list(
                            dict.fromkeys([*known, *[content_id("", o) for o in observations]])
                        )[-1000:]
                        st.update(
                            body_hash=bh, last_content_change=received.isoformat(), known=known
                        )
                    st.update(
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                    )
                    if unconditional:
                        st["last_unconditional"] = received.isoformat()
                elif not st.get("body_hash"):
                    raise ValueError("304 without durable prior content")
                st.update(
                    last_successful_poll=received.isoformat(),
                    consecutive_failures=0,
                    rate_limited_until=None,
                )
        except Exception as exc:
            st.update(
                error=type(exc).__name__, consecutive_failures=st.get("consecutive_failures", 0) + 1
            )
        finished = max(self.clock(), started)
        delay = source["cadence_seconds"] * (1 + self.jitter())
        if st["consecutive_failures"]:
            delay = max(
                delay,
                min(3600, source["cadence_seconds"] * 2 ** min(st["consecutive_failures"], 8)),
            )
        next_at = finished + timedelta(seconds=delay)
        if st.get("rate_limited_until"):
            next_at = max(next_at, datetime.fromisoformat(st["rate_limited_until"]))
        st["next_poll"] = next_at.isoformat()
        bucket = finished.replace(minute=0, second=0, microsecond=0).isoformat()
        hours = {
            k: v
            for k, v in st.get("hours", {}).items()
            if datetime.fromisoformat(k) >= finished - timedelta(hours=24)
        }
        h = hours.setdefault(
            bucket,
            dict(
                requests=0,
                decoded_bytes=0,
                wire_bytes=0,
                cpu_seconds=0,
                detected=0,
                accepted=0,
                duplicates=0,
                rejected=0,
            ),
        )
        for k, v in dict(
            requests=1,
            decoded_bytes=downloaded,
            wire_bytes=wire_bytes,
            cpu_seconds=time.thread_time() - cpu,
            detected=stats["detected"],
            accepted=accepted,
            duplicates=duplicates,
            rejected=sum(stats["rejected"].values()),
        ).items():
            h[k] += v
        totals = st.get("totals", {})
        for k, v in dict(
            requests=1,
            decoded_bytes=downloaded,
            wire_bytes=wire_bytes,
            cpu_seconds=time.thread_time() - cpu,
            detected=stats["detected"],
            accepted=accepted,
            duplicates=duplicates,
            rejected=sum(stats["rejected"].values()),
        ).items():
            totals[k] = totals.get(k, 0) + v
        st.update(
            hours=hours, totals=totals, last_stats=stats, poll_wall_seconds=time.monotonic() - wall
        )
        self.spool.save_state(pid, st)
        return st

    def tick(self, *, force=False):
        # A durable stage marker allows bounded expansion without restarting paper execution.
        if self.stage is None:
            self.sources = active(self.cfg)
        # Four bounded workers: a slow/broken source does not delay another source's receipt.
        with ThreadPoolExecutor(max_workers=4) as pool:
            due = [
                s
                for s in self.sources
                if force
                or self.clock() >= self.next_due.get(s["id"], datetime.min.replace(tzinfo=UTC))
            ]
            futures = {s["id"]: pool.submit(self.poll, s, force=force) for s in due}
            out = {}
            for pid, f in futures.items():
                try:
                    out[pid] = f.result()
                    if out[pid].get("next_poll"):
                        self.next_due[pid] = datetime.fromisoformat(out[pid]["next_poll"])
                except Exception as exc:
                    out[pid] = {"error": type(exc).__name__}
                    cadence = next(s["cadence_seconds"] for s in self.sources if s["id"] == pid)
                    self.next_due[pid] = self.clock() + timedelta(seconds=min(3600, cadence * 2))
        if futures or time.monotonic() - self.last_heartbeat >= 30:
            replace_durable(
                self.spool.root / "collector.json",
                {
                    "heartbeat_at": self.clock().isoformat(),
                    "active": [s["id"] for s in self.sources],
                },
            )
            self.last_heartbeat = time.monotonic()
        return out


def run():
    import fcntl

    from market_signal.context.free_sources.registry import verify_cost_report

    verify_cost_report(load())
    spool = Spool(default_root())
    # Refuse overlapping collectors sharing one source state/conditional cache.
    with (spool.root / "collector.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        c = Collector(spool)
        try:
            while True:
                c.tick()
                time.sleep(1)
        finally:
            c.close()


if __name__ == "__main__":
    run()
