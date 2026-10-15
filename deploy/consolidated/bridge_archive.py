"""Temporary Railway->OVH archive bridge. Receiver is a forced SSH command, never a shell.

The sender removes only an immutable backup whose exact compressed/logical hashes and
all-table restore were acknowledged by the pinned receiver. Never touches live DB/raw.
Archive files have bounded sizes and disk floor; this bridge never expires evidence.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zlib
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

NAME = re.compile(r"prism-\d{8}T\d{6}Z(?:-[\w-]+)?\.duckdb\Z")
MAX_COMPRESSED, MAX_LOGICAL = 1024**3, 16 * 1024**3
DISK_FLOOR = 20 * 1024**3


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(2**20), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    if path.is_symlink():
        raise ValueError("symlink metadata")
    temp = path.with_suffix(path.suffix + ".part")
    with temp.open("x") as f:
        json.dump(value, f, indent=2, default=str)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


def receive(stream, root, *, disk_floor=DISK_FLOOR):
    import duckdb

    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("symlink archive root")
    root.mkdir(parents=True, exist_ok=True)
    line = stream.readline(128 * 1024 + 1)
    if len(line) > 128 * 1024:
        raise ValueError("oversized header")
    header = json.loads(line)
    if not NAME.fullmatch(header["name"]) or not re.fullmatch("[0-9a-f]{64}", header["sha256"]):
        raise ValueError("invalid artifact identity")
    size = header["source_size"]
    if not isinstance(size, int) or not 0 < size <= MAX_LOGICAL:
        raise ValueError("invalid source size")
    if shutil.disk_usage(root).free < disk_floor + size * 2 + MAX_COMPRESSED:
        raise ValueError("archive safety floor")
    dest = root / (header["name"] + "." + header["sha256"][:12] + ".gz")
    sidecar = dest.with_suffix(dest.suffix + ".manifest.json")
    if dest.is_symlink() or sidecar.is_symlink():
        raise ValueError("unsafe existing artifact")
    with tempfile.TemporaryDirectory(dir=root, prefix="incoming-") as temp:
        compressed, restored = Path(temp) / "archive.gz", Path(temp) / "restore.duckdb"
        total = 0
        with compressed.open("xb") as f:
            while chunk := stream.read(2**20):
                total += len(chunk)
                if total > MAX_COMPRESSED:
                    raise ValueError("compressed size limit")
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        logical = 0
        with gzip.open(compressed, "rb") as fi, restored.open("xb") as fo:
            while chunk := fi.read(2**20):
                logical += len(chunk)
                if logical > size:
                    raise ValueError("decompression size limit")
                fo.write(chunk)
            fo.flush()
            os.fsync(fo.fileno())
        if logical != size or sha(restored) != header["sha256"]:
            raise ValueError("logical checksum/size mismatch")
        tables = {}
        with duckdb.connect(
            str(restored),
            read_only=True,
            config={
                "threads": "1",
                "memory_limit": "256MB",
                "temp_directory": str(Path(temp) / "spill"),
            },
        ) as con:
            version = con.execute("SELECT max(version) FROM schema_version").fetchone()[0]
            authority = con.execute(
                "SELECT runtime_id,recorded_at FROM runtime_events WHERE event_type='authority_claimed' ORDER BY recorded_at DESC,event_id DESC LIMIT 1"
            ).fetchone()
            if (
                not authority
                or authority[0] != "railway-prism-runtime"
                or version != header["schema_version"]
            ):
                raise ValueError("backup authority/schema mismatch")
            for (table,) in con.execute("SHOW TABLES").fetchall():
                quoted = '"' + table.replace('"', '""') + '"'
                columns = [r[1] for r in con.execute(f"PRAGMA table_info({quoted})").fetchall()]
                args = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
                n, digest = con.execute(
                    f"SELECT count(*),bit_xor(hash({args})) FROM {quoted}"
                ).fetchone()
                tables[table] = {"rows": n, "value_hash": digest}
            frontier = con.execute("SELECT max(finished_at) FROM runtime_cycles").fetchone()[0]
        expected = header.get("source_manifest", {}).get("tables_read")
        if not expected or {t: v["rows"] for t, v in tables.items()} != expected:
            raise ValueError("source/restore table inventory mismatch")
        artifact_hash = sha(compressed)
        receipt = {
            "off_host_verified": True,
            "source": header,
            "sha256": header["sha256"],
            "artifact_sha256": artifact_hash,
            "artifact_bytes": total,
            "archive_path": str(dest),
            "authority": list(authority),
            "last_finished_cycle": frontier,
            "schema_version": version,
            "restore_test": {"sha256": header["sha256"], "all_tables": tables},
            "received_verified_at": datetime.now(UTC).isoformat(),
        }
        if dest.exists():
            if sha(dest) != artifact_hash or not sidecar.exists():
                raise ValueError("existing archive conflict; preserve for review")
        else:
            os.replace(compressed, dest)
            sync_dir(root)
            write_json(sidecar, receipt)
        return receipt


def send(config, *, apply=False, root=Path("/data/backups"), db=Path("/data/prism.duckdb")):
    from market_signal.ops import backups as b
    from market_signal.ops import runtime as rt

    results = []
    with rt.runtime_lock(db, "railway-bridge-archive", 240):
        rt.require_authoritative(db)
        for row in b.inventory(root, db):
            path = b.safe_path(root, Path(row["path"]), db)
            manifest = row["manifest"]
            if not manifest.get("verification_result") or sha(path) != manifest.get("sha256"):
                raise ValueError("unverified source backup")
            st = path.stat()
            identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            header = {
                "name": path.name,
                "sha256": manifest["sha256"],
                "source_size": st.st_size,
                "schema_version": manifest["schema_version"],
                "source_manifest": manifest,
            }
            command = [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UserKnownHostsFile=" + config["known_hosts"],
                "-i",
                config["identity_file"],
                config["target"],
            ]
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            h, compressed_hash = hashlib.sha256(), hashlib.sha256()
            try:
                process.stdin.write((json.dumps(header) + "\n").encode())
                z = zlib.compressobj(6, zlib.DEFLATED, 31)
                with path.open("rb") as f:
                    while chunk := f.read(2**20):
                        h.update(chunk)
                        data = z.compress(chunk)
                        compressed_hash.update(data)
                        process.stdin.write(data)
                data = z.flush()
                compressed_hash.update(data)
                process.stdin.write(data)
                process.stdin.close()
                process.stdin = None
                output, error = process.communicate(timeout=240)
                if process.returncode:
                    raise RuntimeError(
                        "archive receiver failed: " + error.decode(errors="replace")[-1000:]
                    )
                receipt = json.loads(output)
                after = path.stat()
                if (
                    identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or h.hexdigest() != manifest["sha256"]
                ):
                    raise ValueError("source changed during export")
                if not (
                    receipt.get("off_host_verified") is True
                    and receipt.get("sha256") == manifest["sha256"]
                    and receipt.get("artifact_sha256") == compressed_hash.hexdigest()
                    and receipt.get("restore_test", {}).get("sha256") == manifest["sha256"]
                ):
                    raise ValueError("invalid remote restore receipt")
                record = {
                    "source_path": str(path),
                    "logical_bytes": st.st_size,
                    "allocated_bytes": st.st_blocks * 512,
                    "receipt": receipt,
                    "at": datetime.now(UTC).isoformat(),
                    "applied": apply,
                }
                receipt_path = root / (path.name + ".bridge-receipt.json")
                write_json(receipt_path, record)
                if apply:
                    audit = root / "bridge-archive.jsonl"
                    if audit.is_symlink():
                        raise ValueError("unsafe audit")
                    with audit.open("a") as log:
                        log.write(
                            json.dumps({**record, "action": "delete_intent"}, default=str) + "\n"
                        )
                        log.flush()
                        os.fsync(log.fileno())
                        # Recheck after durable receipt and immediately before unlink.
                        if (
                            path.stat().st_mtime_ns != st.st_mtime_ns
                            or sha(path) != manifest["sha256"]
                        ):
                            raise ValueError("source changed before deletion")
                        path.unlink()
                        sync_dir(path.parent)
                        log.write(json.dumps({**record, "action": "deleted"}, default=str) + "\n")
                        log.flush()
                        os.fsync(log.fileno())
                results.append(record)
            except BrokenPipeError as exc:
                with suppress(OSError):
                    process.stdin.close()
                process.stdin = None
                _, error = process.communicate(timeout=15)
                raise RuntimeError(
                    "archive receiver rejected stream: " + error.decode(errors="replace")[-1500:]
                ) from exc
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["receive", "send"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    signal.alarm(300)
    if args.mode == "receive":
        result = receive(sys.stdin.buffer, args.root)
    else:
        result = send(json.loads(args.config.read_text()), apply=args.apply)
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
