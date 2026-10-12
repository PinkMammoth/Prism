"""Host validation for a Prism runtime (docs/RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md, "Validation").

Engineering measurements only: no database authority, no production contact, no orders.
Run the same commands on the candidate VPS and on the current host (Railway via `railway ssh`
or a local machine) and compare the JSON with `compare`.

  python scripts/vps_validate.py host                       OS/CPU/RAM/swap/disk/time sync
  python scripts/vps_validate.py cpu --seconds 600          sustained CPU, steal, throttling
  python scripts/vps_validate.py disk --dir /data/validation   seq/random IO + fsync latency
  python scripts/vps_validate.py duckdb --backup B --dir D  backup copy/checkpoint/verify/restore
  python scripts/vps_validate.py network --seconds 900      Hyperliquid WS + HTTP providers
  python scripts/vps_validate.py compare A.json B.json      side-by-side table

Every command prints one JSON object (and writes it with --out).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import multiprocessing as mp
import os
import platform
import shutil
import socket
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

CORE = ("BTC", "ETH", "SOL", "HYPE", "LINK", "AAVE")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def _summary(xs: list[float]) -> dict:
    return {
        "n": len(xs),
        "p50": _pct(xs, 0.5),
        "p95": _pct(xs, 0.95),
        "p99": _pct(xs, 0.99),
        "max": max(xs) if xs else None,
        "mean": statistics.fmean(xs) if xs else None,
    }


def _cmd(*args: str) -> str | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _cpu_times() -> list[int]:
    with open("/proc/stat") as f:
        return [int(x) for x in f.readline().split()[1:]]


# ---------------------------------------------------------------------------- host


def host(args) -> dict:
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0]) * 1024
    data = Path(args.data)
    du = shutil.disk_usage(data if data.exists() else "/")
    cg_cpu = Path("/sys/fs/cgroup/cpu.max")
    cg_mem = Path("/sys/fs/cgroup/memory.max")
    return {
        "kind": "host",
        "at": _now(),
        "hostname": socket.gethostname(),
        "os": _cmd("sh", "-c", ". /etc/os-release && echo $PRETTY_NAME"),
        "kernel": platform.release(),
        "virtualization": _cmd("systemd-detect-virt"),
        "cpu_model": next(
            (
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")
            ),
            None,
        ),
        "cpus": os.cpu_count(),
        "cgroup_cpu_max": cg_cpu.read_text().strip() if cg_cpu.exists() else None,
        "cgroup_memory_max": cg_mem.read_text().strip() if cg_mem.exists() else None,
        "mem_total": mem.get("MemTotal"),
        "mem_available": mem.get("MemAvailable"),
        "swap_total": mem.get("SwapTotal"),
        "swap_free": mem.get("SwapFree"),
        "data_path": str(data),
        "disk_total": du.total,
        "disk_free": du.free,
        "time_sync": _cmd("timedatectl", "show", "-p", "NTPSynchronized", "--value"),
        "python": sys.version.split()[0],
    }


# ---------------------------------------------------------------------------- cpu


def _burn(seconds: float, q) -> None:
    """Fixed work units (SHA-256 of 64 KiB) per wall second, for ``seconds``."""
    blob = os.urandom(65536)
    end = time.monotonic() + seconds
    out = []
    while time.monotonic() < end:
        t0, n = time.monotonic(), 0
        while time.monotonic() - t0 < 1.0:
            hashlib.sha256(blob).digest()
            n += 1
        out.append(n)
    q.put(out)


def cpu(args) -> dict:
    single_q: mp.Queue = mp.Queue()
    _burn(10, single_q)
    single = single_q.get()
    n = os.cpu_count() or 1
    q: mp.Queue = mp.Queue()
    t0 = _cpu_times()
    procs = [mp.Process(target=_burn, args=(args.seconds, q)) for _ in range(n)]
    for p in procs:
        p.start()
    series = [q.get() for _ in procs]
    for p in procs:
        p.join()
    t1 = _cpu_times()
    delta = [b - a for a, b in zip(t0, t1, strict=False)]
    total = sum(delta) or 1
    steal = delta[7] / total if len(delta) > 7 else None
    per_second = [sum(s[i] for s in series if i < len(s)) for i in range(min(map(len, series)))]
    first, last = (
        per_second[: max(1, len(per_second) // 10)],
        per_second[-max(1, len(per_second) // 10) :],
    )
    return {
        "kind": "cpu",
        "at": _now(),
        "cpus": n,
        "seconds": args.seconds,
        "single_thread_units_per_s": statistics.fmean(single),
        "all_core_units_per_s": _summary(per_second),
        "all_core_cv": statistics.pstdev(per_second) / statistics.fmean(per_second),
        "scaling_vs_single": statistics.fmean(per_second) / statistics.fmean(single),
        # sustained throttling: last 10% vs first 10% of the run
        "last_vs_first": statistics.fmean(last) / statistics.fmean(first),
        "min_second_vs_median": min(per_second) / statistics.median(per_second),
        "steal_fraction": steal,
    }


# ---------------------------------------------------------------------------- disk


def disk(args) -> dict:
    d = Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "prism-disk-test.bin"
    size = args.mb * 2**20
    block = os.urandom(2**20)
    t0 = time.monotonic()
    with f.open("wb") as fh:
        for _ in range(args.mb):
            fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())
    seq_write = size / (time.monotonic() - t0)
    t0 = time.monotonic()
    with f.open("rb") as fh:
        while fh.read(8 * 2**20):
            pass
    seq_read = size / (time.monotonic() - t0)  # page cache may serve this; noted below
    fd = os.open(f, os.O_RDWR)
    lat, rlat = [], []
    small = os.urandom(4096)
    blocks = size // 4096
    try:
        for i in range(args.ops):
            off = (int.from_bytes(os.urandom(4), "little") % blocks) * 4096
            t = time.perf_counter()
            os.pwrite(fd, small, off)
            os.fsync(fd)
            lat.append((time.perf_counter() - t) * 1000)
            off = ((i * 7919) % blocks) * 4096
            t = time.perf_counter()
            os.pread(fd, 4096, off)
            rlat.append((time.perf_counter() - t) * 1000)
    finally:
        os.close(fd)
        f.unlink(missing_ok=True)
    out = {
        "kind": "disk",
        "at": _now(),
        "dir": str(d),
        "seq_write_fsync_MBps": seq_write / 2**20,
        "seq_read_MBps_maybe_cached": seq_read / 2**20,
        "rand4k_write_fsync_ms": _summary(lat),
        "rand4k_read_ms_maybe_cached": _summary(rlat),
    }
    if shutil.which("fio"):  # optional cross-check with direct IO
        res = _cmd("fio", "--name=prism", f"--directory={d}", "--rw=randrw", "--bs=4k",
                   "--size=512M", "--runtime=60", "--time_based", "--direct=1",
                   "--ioengine=libaio", "--iodepth=16", "--fsync=32",
                   "--output-format=json")  # fmt: skip
        if res:
            j = json.loads(res)["jobs"][0]
            out["fio_randrw_4k"] = {
                "read_iops": j["read"]["iops"],
                "write_iops": j["write"]["iops"],
                "write_clat_p99_us": j["write"]["clat_ns"]["percentile"].get("99.000000", 0) / 1000,
            }
        for p in d.glob("prism.*"):
            p.unlink(missing_ok=True)
    return out


# ---------------------------------------------------------------------------- duckdb


def _timed(fn):
    t0 = time.monotonic()
    r = fn()
    return r, round(time.monotonic() - t0, 3)


def duckdb_ops(args) -> dict:
    """Realistic Prism operations on a disposable copy of a real backup (scratch role)."""
    os.environ["PRISM_RUNTIME_ROLE"] = "scratch"  # never authoritative; copies only
    import duckdb

    from market_signal.ops import runtime as rt

    src, d = Path(args.backup), Path(args.dir)
    d.mkdir(parents=True, exist_ok=True)
    copy = d / "validation-copy.duckdb"
    copy.unlink(missing_ok=True)
    out = {"kind": "duckdb", "at": _now(), "backup_bytes": src.stat().st_size}

    def copy_fsync():
        rt._copy_atomic(src, copy)

    _, out["backup_copy_fsync_s"] = _timed(copy_fsync)
    _, out["verify_backup_s"] = _timed(lambda: rt.verify_backup(copy))

    def checkpoint():
        con = duckdb.connect(str(copy))
        try:
            n = con.execute("SELECT count(*) FROM microstructure_minutes").fetchone()[0]
            con.execute("CREATE TABLE _v AS SELECT * FROM microstructure_minutes LIMIT 50000")
            con.execute("DROP TABLE _v")
            con.execute("CHECKPOINT")
            return n
        finally:
            con.close()

    _, out["write_and_checkpoint_s"] = _timed(checkpoint)

    def scans():
        con = duckdb.connect(str(copy), read_only=True)
        try:
            tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
            return sum(con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables)
        finally:
            con.close()

    out["rows_total"], out["full_count_scan_s"] = _timed(scans)
    restored, out["restore_test_s"] = _timed(lambda: rt.restore_test(copy, d))
    out["restored_ok"] = restored["restored_ok"]

    def gzip_archive():
        import gzip

        with copy.open("rb") as fi, gzip.open(d / "archive.duckdb.gz", "wb", compresslevel=6) as fo:
            shutil.copyfileobj(fi, fo, 8 * 2**20)
        return (d / "archive.duckdb.gz").stat().st_size

    out["archive_gz_bytes"], out["archive_gzip_s"] = _timed(gzip_archive)
    for p in (copy, d / "archive.duckdb.gz"):
        p.unlink(missing_ok=True)
    return out


# ---------------------------------------------------------------------------- network


async def _ws(seconds: float) -> dict:
    import websockets

    from market_signal.microstructure.ws import STALE_BOOK_S, subscriptions

    url = "wss://api.hyperliquid.xyz/ws"
    stats = {"connects": 0, "disconnects": 0, "messages": 0, "errors": []}
    gaps: dict[str, list[float]] = {}
    last: dict[str, float] = {}
    rtt: list[float] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            async with websockets.connect(url, ping_interval=None, max_size=2**22) as ws:
                stats["connects"] += 1
                # exactly the production collector's subscriptions
                for sub in subscriptions(CORE):
                    await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                next_ping, ping_at = time.monotonic() + 30, None
                while time.monotonic() < end:
                    if time.monotonic() >= next_ping:
                        ping_at = time.perf_counter()
                        await ws.send(json.dumps({"method": "ping"}))
                        next_ping = time.monotonic() + 30
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    ch = msg.get("channel")
                    if ch == "pong" and ping_at:
                        rtt.append((time.perf_counter() - ping_at) * 1000)
                        continue
                    data = msg.get("data")
                    coin = (data or {}).get("coin") if isinstance(data, dict) else (
                        data[0].get("coin") if isinstance(data, list) and data else None)  # fmt: skip
                    if not coin:
                        continue
                    key, t = f"{ch}:{coin}", time.monotonic()
                    if key in last:
                        gaps.setdefault(key, []).append(t - last[key])
                    last[key] = t
                    stats["messages"] += 1
        except Exception as exc:  # recorded, then reconnect like the collector does
            stats["disconnects"] += 1
            stats["errors"].append(f"{_now()} {type(exc).__name__}")
            await asyncio.sleep(2)
    book_gaps = [g for k, v in gaps.items() if k.startswith("l2Book") for g in v]
    return {
        **stats,
        "seconds": seconds,
        "messages_per_s": stats["messages"] / seconds,
        "l2book_gap_s": _summary(book_gaps),
        "stale_book_threshold_s": STALE_BOOK_S,
        "l2book_gaps_over_stale_threshold": sum(g > STALE_BOOK_S for g in book_gaps),
        "ping_rtt_ms": _summary(rtt),
        "errors": stats["errors"][-20:],
    }


def _http(name: str, method: str, url: str, body=None, n: int = 20) -> dict:
    import httpx

    lat, errors = [], []
    with httpx.Client(timeout=10) as c:
        for _ in range(n):
            t = time.perf_counter()
            try:
                r = c.request(method, url, json=body)
                r.raise_for_status()
                lat.append((time.perf_counter() - t) * 1000)
            except Exception as exc:
                errors.append(type(exc).__name__)
            time.sleep(0.5)
    return {"name": name, "url": url, "latency_ms": _summary(lat), "errors": errors}


def network(args) -> dict:
    jwks = os.environ.get("PRISM_CONTEXT_OAUTH_JWKS") or args.jwks
    probes = [
        ("hyperliquid_info", "POST", "https://api.hyperliquid.xyz/info", {"type": "meta"}),
        ("binance_fapi", "GET", "https://fapi.binance.com/fapi/v1/time", None),
        ("coinbase_status", "GET", "https://status.coinbase.com/api/v2/status.json", None),
        ("kraken_status", "GET", "https://status.kraken.com/api/v2/status.json", None),
        ("fred", "GET", "https://fred.stlouisfed.org/", None),
        ("sec", "GET", "https://www.sec.gov/news/pressreleases.rss", None),
        ("defillama", "GET", "https://api.llama.fi/protocols", None),
    ]
    if jwks:
        probes.append(("auth0_jwks", "GET", jwks, None))
    if args.gateway:
        probes.append(("work_gateway", "GET", args.gateway.rstrip("/") + "/health", None))
    out = {"kind": "network", "at": _now(), "http": [_http(*p) for p in probes]}
    if shutil.which("ping"):
        res = _cmd("ping", "-c", "50", "-i", "0.2", "api.hyperliquid.xyz")
        out["icmp_hyperliquid"] = (res or "").splitlines()[-2:]
    out["hyperliquid_ws"] = asyncio.run(_ws(args.seconds))
    return out


# ---------------------------------------------------------------------------- compare


def _flat(d, prefix=""):
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            yield from _flat(v, key + ".")
        elif isinstance(v, int | float) and not isinstance(v, bool):
            yield key, v


def compare(args) -> dict:
    a, b = (json.loads(Path(p).read_text()) for p in (args.a, args.b))
    fa, fb = dict(_flat(a)), dict(_flat(b))
    rows = {k: {"a": fa.get(k), "b": fb.get(k)} for k in sorted(fa.keys() | fb.keys())}
    for k, r in rows.items():
        if r["a"] and r["b"]:
            r["b_over_a"] = round(r["b"] / r["a"], 3)
        print(f"{k:60} {r['a']!s:>18} {r['b']!s:>18} {r.get('b_over_a', '')!s:>8}")
    return {"kind": "compare", "a": args.a, "b": args.b, "rows": rows}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--out", type=Path)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("host")
    s.add_argument("--data", default="/data")
    s = sub.add_parser("cpu")
    s.add_argument("--seconds", type=int, default=600)
    s = sub.add_parser("disk")
    s.add_argument("--dir", default="/data/validation")
    s.add_argument("--mb", type=int, default=1024)
    s.add_argument("--ops", type=int, default=2000)
    s = sub.add_parser("duckdb")
    s.add_argument("--backup", required=True)
    s.add_argument("--dir", default="/data/validation")
    s = sub.add_parser("network")
    s.add_argument("--seconds", type=int, default=900)
    s.add_argument("--jwks")
    s.add_argument("--gateway")
    s = sub.add_parser("compare")
    s.add_argument("a")
    s.add_argument("b")
    args = p.parse_args()
    fn = {"host": host, "cpu": cpu, "disk": disk, "duckdb": duckdb_ops, "network": network,
          "compare": compare}[args.cmd]  # fmt: skip
    out = fn(args)
    text = json.dumps(out, indent=2, default=str)
    if args.out:
        args.out.write_text(text + "\n")
    if args.cmd != "compare":
        print(text)


if __name__ == "__main__":
    main()
