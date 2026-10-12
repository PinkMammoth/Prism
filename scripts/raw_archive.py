"""Operator workflow: list -> export/read-verify -> dry-run -> explicit apply.

All artifacts/manifests live off-host. Railway transfer creates no intermediate files.
Microstructure raw/spool/calibration copies are export-only in v1.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from market_signal.ops import backups
from market_signal.ops import raw_archives as raw

ROOTS = {
    "provider-raw": "/data/raw",
    "microstructure-raw": "/data/microstructure/raw",
    "microstructure-spool": "/data/microstructure/spool",
    "large-print-calibration": "/data/microstructure/lp",
}


def bootstrap(action: str) -> str:
    return (
        "import sys,json; ns={'__name__':'raw_archive_ops'}; exec("
        + repr(Path(raw.__file__).read_text())
        + ",ns); "
        + action
    )


def command(args, code, *parameters):
    return [
        "railway",
        "ssh",
        "--project",
        args.project,
        "--environment",
        "production",
        "--service",
        args.service,
        "--",
        "/app/.venv/bin/python",
        "-c",
        code,
        *parameters,
    ]


def remote(args, action, *parameters, stdin: str | None = None):
    result = subprocess.run(
        command(args, bootstrap(action), *parameters),
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
        input=stdin,
    )
    if result.returncode:
        raise RuntimeError(
            "Railway archive action failed: " + result.stderr[-4000:] + result.stdout[-1000:]
        )
    return json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))


LIST_ACTION = """
from pathlib import Path
from market_signal.ops import runtime as rt
db=Path('/data/prism.duckdb')
context=ns['recovery_context'](db,sys.argv[3])
root=Path(sys.argv[1]); kind=sys.argv[2]; rows=[]; errors=[]
pattern='*/*/*' if kind=='provider-raw' else ('*' if kind=='large-print-calibration' else '*/*')
for path in sorted(root.glob(pattern)):
    if not path.is_file(): continue
    try: rows.append(ns['make_manifest'](root,path,kind,context))
    except Exception as e: errors.append({'source_path':str(path),'reason':str(e)})
print(json.dumps({'manifest_version':1,'source_revision':rt.revision(rt._root()),
 'archive_type':kind,'boundary':{k:v for k,v in context.items() if k not in ['references','spool_offsets']},
 'rows':rows,'errors':errors},default=str))
"""

STREAM_ACTION = """
import tarfile,base64
from pathlib import Path
rows=json.loads(sys.argv[1]); root=Path(sys.argv[2]); kind=sys.argv[3]
class Writer:
    def __init__(self): self.pending=bytearray()
    def write(self,data):
        self.pending.extend(data)
        if len(self.pending)>=2**20: self.flush()
        return len(data)
    def flush(self):
        if self.pending:
            print('PRISM_CHUNK '+base64.b64encode(self.pending).decode(),flush=True)
            self.pending.clear()
w=Writer()
with tarfile.open(fileobj=w,mode='w|') as tar:
    for i,row in enumerate(rows):
        path=ns['safe_source'](root,Path(row['source_path']),kind)
        if ns['identity'](path)!=row['source_identity'] or ns['sha256'](path)!=row['sha256']:
            raise ValueError('source changed before transfer')
        entry=tarfile.TarInfo(str(i)); entry.size=row['source_size']
        with path.open('rb') as f: tar.addfile(entry,f)
        if ns['identity'](path)!=row['source_identity']: raise ValueError('source changed during transfer')
w.flush()
print('PRISM_DONE',flush=True)
"""

APPLY_ACTION = """
from pathlib import Path
from market_signal.ops import runtime as rt
db=Path('/data/prism.duckdb'); receipts=json.loads(sys.stdin.read()); review=sys.argv[1]; apply=sys.argv[2]=='yes'
with rt.runtime_lock(db,'verified-provider-offload',600):
    rt.require_authoritative(db)
    context=ns['recovery_context'](db,review)
    # Validate the entire small batch before the first mutation; apply revalidates too.
    for receipt in receipts:
        ns['delete_verified'](Path('/data/raw'),Path(receipt['source_path']),receipt,context,
                              Path('/data/backups/raw-archive-deletions.jsonl'),apply=False)
    results=[ns['delete_verified'](Path('/data/raw'),Path(r['source_path']),r,context,
              Path('/data/backups/raw-archive-deletions.jsonl'),apply=apply) for r in receipts]
print(json.dumps({'dry_run':not apply,'results':results},default=str))
"""


def destination(base: Path, row: dict) -> Path:
    root = Path(ROOTS[row["archive_type"]])
    rel = Path(row["source_path"]).relative_to(root)
    return backups.safe_location(base / row["archive_type"] / rel)


def verified_receipt(base: Path, row: dict) -> dict:
    path = destination(base, row)
    receipt = json.loads(backups.safe_location(backups.sidecar(path)).read_text())
    if receipt.get("source_path") != row["source_path"] or raw.sha256(path) != row["sha256"]:
        raise ValueError("off-host artifact/source mismatch")
    if not receipt.get("off_host_verified") or receipt.get("artifact_sha256") != row["sha256"]:
        raise ValueError("verified export required")
    if raw.read_test(path, row["archive_type"]) != receipt["restore_read_test"]:
        raise ValueError("off-host restored read differs")
    return receipt


def export_batch(args, rows: list[dict], base: Path):
    request = [
        {k: r[k] for k in ("source_path", "source_identity", "sha256", "source_size")} for r in rows
    ]
    with tempfile.TemporaryDirectory(dir=base) as d:
        archive = Path(d) / "transfer.tar"
        process = subprocess.Popen(
            command(
                args,
                bootstrap(STREAM_ACTION),
                json.dumps(request),
                ROOTS[rows[0]["archive_type"]],
                rows[0]["archive_type"],
            ),
            stdout=subprocess.PIPE,
            text=True,
        )
        done = False
        try:
            with archive.open("wb") as f:
                for line in process.stdout:
                    if line.startswith("PRISM_CHUNK "):
                        f.write(base64.b64decode(line.split(" ", 1)[1].strip(), validate=True))
                    elif line.strip() == "PRISM_DONE":
                        done = True
            if process.wait() or not done:
                raise ValueError("incomplete Railway archive transfer")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        seen = set()
        with tarfile.open(archive, "r:") as tar:
            for member in tar:
                if not member.isfile() or not member.name.isdecimal():
                    raise ValueError("unexpected tar entry")
                index = int(member.name)
                if index in seen or index >= len(rows) or member.size != rows[index]["source_size"]:
                    raise ValueError("tar entry mismatch")
                seen.add(index)
                row = rows[index]
                directory = Path(d) / str(index)
                directory.mkdir()
                source = directory / Path(row["source_path"]).name
                with tar.extractfile(member) as fi, source.open("wb") as fo:
                    shutil.copyfileobj(fi, fo, 2**20)
                raw.export_file(source, destination(base, row), row)
        if len(seen) != len(rows):
            raise ValueError("missing transferred source files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["list", "export", "apply", "read"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--service", default="prism-runtime")
    parser.add_argument("--type", choices=ROOTS, default="provider-raw")
    parser.add_argument("--destination", type=Path, default=Path("data/backup-archive"))
    parser.add_argument("--dependency-review", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()
    if os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative" or os.environ.get(
        "RAILWAY_ENVIRONMENT_ID"
    ):
        raise ValueError("run this archive helper on the off-host destination machine")
    if not 1 <= args.batch_size <= 32:
        parser.error("batch size must be 1–32")
    args.manifest = backups.safe_location(args.manifest)
    args.destination = backups.safe_location(args.destination)
    if args.action == "read":
        print(json.dumps(raw.read_test(args.manifest, args.type), indent=2))
        return
    if args.action == "list":
        result = remote(args, LIST_ACTION, ROOTS[args.type], args.type, args.dependency_review)
        result["created_at"] = datetime.now(UTC).isoformat()
        result["dependency_review"] = args.dependency_review
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        backups.write_json(args.manifest, result)
        selected = [r for r in result["rows"] if r["eligibility"]["export_eligible"]]
        print(
            json.dumps(
                {
                    "manifest": str(args.manifest),
                    "files": len(result["rows"]),
                    "eligible": len(selected),
                    "eligible_bytes": sum(r["source_size"] for r in selected),
                    "errors": len(result["errors"]),
                }
            )
        )
        return
    manifest = json.loads(args.manifest.read_text())
    rows = [r for r in manifest["rows"] if r["eligibility"]["export_eligible"]]
    args.destination.mkdir(parents=True, exist_ok=True)
    if args.action == "export":
        missing = []
        for row in rows:
            if destination(args.destination, row).exists():
                verified_receipt(args.destination, row)
            else:
                missing.append(row)
        for i in range(0, len(missing), args.batch_size):
            export_batch(args, missing[i : i + args.batch_size], args.destination)
            print(
                json.dumps(
                    {
                        "exported_files": min(i + args.batch_size, len(missing)),
                        "remaining_files": max(0, len(missing) - i - args.batch_size),
                    }
                ),
                flush=True,
            )
        print(json.dumps({"verified_files": len(rows), "deletion_performed": False}))
    else:
        if manifest["archive_type"] != "provider-raw":
            raise ValueError("microstructure working-set deletion protected in v1")
        # Verify every local artifact before any Railway deletion in this invocation.
        receipts = [verified_receipt(args.destination, r) for r in rows]
        if len(receipts) > 10000 or len(json.dumps(receipts).encode()) > 64 * 2**20:
            raise ValueError("operator batch exceeds bounded verification limit")
        # Stream receipts over SSH stdin. One boundary verification and one writer
        # lock; validate every file before mutation, then revalidate at unlink.
        result = remote(
            args,
            APPLY_ACTION,
            manifest["dependency_review"],
            "yes" if args.apply else "no",
            stdin=json.dumps(receipts),
        )
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
