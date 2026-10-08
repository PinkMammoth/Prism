"""Collector-owned files on the runtime volume (never the DuckDB database).

    <root>/                       default: <database dir>/microstructure  (/data/microstructure)
      collector.lock              exclusive flock held for the collector's lifetime
      status.json                 live health, atomically replaced every few seconds
      spool/YYYYMMDD/HH.jsonl     append-only records (minutes, revisions, late fills, runs,
                                  connections, large-print thresholds); one JSON per line,
                                  fsync'd; the ingest job is their only reader
      raw/YYYYMMDD/HH.jsonl.gz    short-retention journal of normalized events (one gzip
                                  member per receipt minute; no addresses) for replay/debug
      lp/YYYY-MM-DD.json          cache of a completed day's fine print histogram
      .ingested                   watermark written by the ingest job (spool pruning guard)

A crash can only leave a partial last line (skipped by readers, truncated by ``repair``) or
a truncated last gzip member (read up to the damage).
"""

from __future__ import annotations

import contextlib
import fcntl
import gzip
import json
import os
import shutil
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_signal.microstructure.engine import LargePrints, day_str


class CollectorBusy(RuntimeError):
    """Another collector holds the lock for this directory."""


def default_root(db: Path | None = None) -> Path:
    """PRISM_MICRO_DIR, else ``microstructure/`` next to the database (/data on Railway)."""
    env = os.environ.get("PRISM_MICRO_DIR", "").strip()
    if env:
        return Path(env)
    if db is None:
        from market_signal.config import get_settings

        db = get_settings().paths.db
    return Path(db).parent / "microstructure"


def _hour_parts(ms: int) -> tuple[str, str]:
    t = datetime.fromtimestamp(ms / 1000, UTC)
    return t.strftime("%Y%m%d"), t.strftime("%H")


class Spool:
    def __init__(self, root: Path | str, *, fsync: bool = True):
        self.root = Path(root)
        self.fsync = fsync
        self.errors = 0
        self._fd: tuple[str, int] | None = None
        self._lock_fd: int | None = None

    # ------------------------------------------------------------------ lock

    def lock(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.root / "collector.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise CollectorBusy(f"another collector is running on {self.root}") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def unlock(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    # ------------------------------------------------------------------ spool

    def spool_dir(self) -> Path:
        return self.root / "spool"

    def _path(self, ms: int) -> Path:
        day, hh = _hour_parts(ms)
        return self.spool_dir() / day / f"{hh}.jsonl"

    def append(self, rec: dict, now_ms: int) -> None:
        """Append one record as one line and fsync it (durable before it counts)."""
        line = (json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n").encode()
        path = self._path(now_ms)
        try:
            if self._fd is None or self._fd[0] != str(path):
                if self._fd is not None:
                    os.close(self._fd[1])
                path.parent.mkdir(parents=True, exist_ok=True)
                self._fd = (str(path), os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644))
            os.write(self._fd[1], line)
            if self.fsync:
                os.fsync(self._fd[1])
        except OSError:
            self.errors += 1
            raise

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd[1])
            self._fd = None

    def files(self) -> list[Path]:
        return sorted(self.spool_dir().glob("*/*.jsonl"))

    def repair(self) -> int:
        """Truncate a partial trailing line left by a crash (only the newest file can have
        one). Returns the bytes removed."""
        files = self.files()
        if not files:
            return 0
        f = files[-1]
        data = f.read_bytes()
        if not data or data.endswith(b"\n"):
            return 0
        keep = data.rfind(b"\n") + 1
        with f.open("r+b") as fh:
            fh.truncate(keep)
        return len(data) - keep

    @staticmethod
    def read_lines(path: Path, offset: int = 0) -> tuple[list[dict], int, int]:
        """Complete lines from ``offset``: (records, new_offset, invalid_lines)."""
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        end = data.rfind(b"\n") + 1
        recs, bad = [], 0
        for line in data[:end].splitlines():
            if not line.strip():
                continue
            try:
                recs.append(json.loads(line))
            except ValueError:
                bad += 1
        return recs, offset + end, bad

    def iter_records(self, since_day: str | None = None) -> Iterator[dict]:
        for f in self.files():
            if since_day and f.parent.name < since_day.replace("-", ""):
                continue
            yield from self.read_lines(f)[0]

    def last_finalized(self, coins: tuple[str, ...], feature_version: str) -> dict[str, int | None]:
        """Newest finalized minute per coin, scanning the spool newest file first."""
        out: dict[str, int | None] = dict.fromkeys(coins)
        for f in reversed(self.files()):
            for r in self.read_lines(f)[0]:
                if r.get("kind") == "minute" and r.get("feature_version") == feature_version:
                    c = r["coin"]
                    if c in out and (out[c] is None or r["minute_open"] > out[c]):
                        out[c] = r["minute_open"]
            if all(v is not None for v in out.values()):
                break
        return out

    # ------------------------------------------------------------------ large prints

    def load_large_prints(self, now_ms: int, days: int = 8) -> LargePrints:
        """Per-day fine print histograms for the last ``days`` UTC days: cached completed
        days, else rebuilt from the spool (revision 0 of each minute, once)."""
        today = day_str(now_ms)
        wanted = [day_str(now_ms - i * 86_400_000) for i in range(days, -1, -1)]
        out: dict[str, dict] = {}
        rebuild = []
        for day in wanted:
            cache = self.root / "lp" / f"{day}.json"
            if day != today and cache.is_file():
                with contextlib.suppress(ValueError, OSError):
                    out[day] = json.loads(cache.read_text())
                    continue
            rebuild.append(day)
        if rebuild:
            seen = set()
            for r in self.iter_records(since_day=min(rebuild)):
                if r.get("kind") != "minute" or r.get("revision") != 0:
                    continue
                day = day_str(r["minute_open"])
                key = (r["coin"], r["minute_open"])
                if day not in rebuild or key in seen:
                    continue
                seen.add(key)
                c = out.setdefault(day, {}).setdefault(r["coin"], {"bins": {}, "minutes": 0})
                c["minutes"] += int(r.get("status") != "GAP")
                for k, n in (r.get("fh") or {}).items():
                    c["bins"][k] = c["bins"].get(k, 0) + n
            for day in rebuild:
                if day != today and day in out:
                    self.save_day(day, out[day])
        return LargePrints(out)

    def save_day(self, day: str, hist: dict) -> None:
        p = self.root / "lp" / f"{day}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(hist, sort_keys=True))
        os.replace(tmp, p)

    # ------------------------------------------------------------------ raw archive

    def archive(self, events: list[tuple], now_ms: int) -> int:
        """Append one gzip member (one JSON event per line) to the hour's raw file."""
        if not events:
            return 0
        day, hh = _hour_parts(now_ms)
        path = self.root / "raw" / day / f"{hh}.jsonl.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events).encode()
        blob = gzip.compress(body, compresslevel=6)
        with path.open("ab") as fh:
            fh.write(blob)
            fh.flush()
            if self.fsync:
                os.fsync(fh.fileno())
        return len(blob)

    @staticmethod
    def read_raw(path: Path) -> list[tuple]:
        """Every event of a raw hour file, tolerating a truncated last member."""
        data = path.read_bytes()
        out: list[tuple] = []
        while data:
            d = zlib.decompressobj(zlib.MAX_WBITS | 16)
            try:
                body = d.decompress(data)
            except zlib.error:
                break
            if not d.eof:  # truncated member: keep what is complete
                body = body[: body.rfind(b"\n") + 1]
                data = b""
            else:
                data = d.unused_data
            out += [tuple(json.loads(x)) for x in body.splitlines() if x.strip()]
        return out

    # ------------------------------------------------------------------ status

    def write_status(self, obj: dict) -> None:
        p = self.root / "status.json"
        tmp = self.root / ".status.json.tmp"
        tmp.write_text(json.dumps(obj, sort_keys=True, default=str))
        os.replace(tmp, p)

    def read_status(self) -> dict | None:
        try:
            return json.loads((self.root / "status.json").read_text())
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------------ retention

    def ingested_watermark(self) -> str | None:
        try:
            return (self.root / ".ingested").read_text().strip() or None
        except OSError:
            return None

    def set_ingested_watermark(self, rel: str) -> None:
        tmp = self.root / ".ingested.tmp"
        tmp.write_text(rel)
        os.replace(tmp, self.root / ".ingested")

    def prune(self, now_ms: int, *, spool_days: int, raw_days: int, lp_days: int = 10) -> list[str]:
        """Delete whole day directories past retention. Spool days are deleted only once the
        ingest watermark is past them, so an ingest outage never loses unread records."""
        removed = []
        now = datetime.fromtimestamp(now_ms / 1000, UTC)
        mark = self.ingested_watermark() or ""
        for sub, keep in (("spool", spool_days), ("raw", raw_days)):
            cutoff = (now - timedelta(days=keep)).strftime("%Y%m%d")
            for p in sorted((self.root / sub).glob("*")):
                if not p.is_dir() or p.name >= cutoff:
                    continue
                if sub == "spool" and not mark[:8] > p.name:
                    continue  # not yet ingested past this day
                shutil.rmtree(p)
                removed.append(f"{sub}/{p.name}")
        cutoff_lp = (now - timedelta(days=lp_days)).strftime("%Y-%m-%d")
        for p in sorted((self.root / "lp").glob("*.json")):
            if p.stem < cutoff_lp:
                p.unlink()
                removed.append(f"lp/{p.name}")
        return removed

    def usage(self) -> dict:
        def size(p: Path) -> int:
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0

        return {"spool_bytes": size(self.root / "spool"), "raw_bytes": size(self.root / "raw")}
