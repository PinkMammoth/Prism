"""Capture a WAL-free Railway DB directly off-host under the existing runtime lock.

No production checkpoint, backup file, pruning or deployment. Refuses a nonempty WAL.
An independent read-only DuckDB connection fences writers throughout the byte stream.
Only publish an archive after checksum matching and all-table decompression/restore proof.
Run the local --prove-source rehearsal before using this new path on Railway.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from market_signal.ops import backups as b


def payload(db: str, revision: str | None = None) -> str:
    return """
import base64,hashlib,json,signal,time,zlib
from datetime import UTC,datetime
from pathlib import Path
import duckdb
from market_signal.ops import runtime as rt
signal.alarm(180)
db=Path(DB_PATH)
if not db.is_file() or any(p.is_symlink() for p in (db,*db.parents)):
    raise ValueError('unsafe source')
with rt.runtime_lock(db,'offhost-read-only-recovery-capture',45):
    with duckdb.connect(str(db),read_only=True) as con:
        wal=db.with_name(db.name+'.wal')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('nonempty WAL: refuse physical stream')
        authority=rt.authority_claim(con)
        if not authority or authority[0]!='railway-prism-runtime':
            raise ValueError('source authority mismatch')
        started=datetime.now(UTC).isoformat()
        stat=db.stat()
        expected=rt._sha256(db)
        schema=con.execute('SELECT max(version) FROM schema_version').fetchone()[0]
        cycles=con.execute('SELECT max(finished_at) FROM runtime_cycles').fetchone()[0]
        sequences=con.execute('SELECT run_id,max(seq) FROM paper_nimble_events GROUP BY run_id').fetchall()
        z=zlib.compressobj(6,zlib.DEFLATED,31); h=hashlib.sha256(); size=0
        with db.open('rb') as f:
            while chunk:=f.read(2**20):
                h.update(chunk); size+=len(chunk); data=z.compress(chunk)
                for offset in range(0,len(data),16384):
                    print('PRISM_CHUNK '+base64.b64encode(data[offset:offset+16384]).decode(),flush=True)
        print('PRISM_CHUNK '+base64.b64encode(z.flush()).decode(),flush=True)
        after=db.stat()
        if (stat.st_ino,stat.st_size,stat.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
            raise ValueError('source changed during capture')
        if h.hexdigest()!=expected or rt._sha256(db)!=expected or size!=stat.st_size:
            raise ValueError('source checksum changed')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('WAL appeared during capture')
        manifest={'sha256':expected,'db_file_size':size,'schema_version':schema,
            'source_revision':REVISION_VALUE,'origin':'Railway production read-only live capture',
            'capture_started_at':started,'capture_finished_at':datetime.now(UTC).isoformat(),
            'authority':authority,'last_finished_cycle':cycles,'nimble_ledger_frontier':sequences,
            'source_path':str(db),'wal_bytes':0,'production_temporary_bytes':0,
            'frontier_basis':'physical byte identity under runtime flock and DuckDB read-only fence'}
        print('PRISM_MANIFEST '+json.dumps(manifest,default=str),flush=True)
signal.alarm(0)
""".replace("DB_PATH", repr(db)).replace("REVISION_VALUE", repr(revision))


def capture(command: list[str], destination: Path) -> dict:
    destination = b.safe_location(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or b.sidecar(destination).exists():
        raise ValueError("destination already exists")
    with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
        compressed, restored = Path(temp) / "stream.gz", Path(temp) / "source.duckdb"
        manifest = None
        process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
        try:
            with compressed.open("wb") as output:
                for line in process.stdout:
                    if line.startswith("PRISM_CHUNK "):
                        output.write(base64.b64decode(line.split(" ", 1)[1].strip(), validate=True))
                    elif line.startswith("PRISM_MANIFEST "):
                        if manifest is not None:
                            raise ValueError("duplicate completion manifest")
                        manifest = json.loads(line.split(" ", 1)[1])
            if process.wait() or manifest is None:
                raise ValueError("capture incomplete; no recovery artifact published")
            with gzip.open(compressed, "rb") as fi, restored.open("wb") as fo:
                shutil.copyfileobj(fi, fo, 2**20)
            # export_backup checks logical SHA, restores a second copy, scans every table,
            # checks restored SHA, fsyncs and atomically publishes with a receipt.
            return b.export_backup(restored, destination, manifest, compress=True)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--prove-source", type=Path, help="Local immutable schema-30 proof before Railway"
    )
    args = parser.parse_args()
    if os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative" or os.environ.get(
        "RAILWAY_ENVIRONMENT_ID"
    ):
        raise ValueError("run on the off-host destination machine, never the production runtime")
    if args.prove_source:
        command = [sys.executable, "-c", payload(str(args.prove_source.resolve()), "local-proof")]
    else:
        code = "from pathlib import Path\n" + payload("/data/prism.duckdb").replace(
            "'source_revision':None", "'source_revision':Path('/app/REVISION').read_text().strip()"
        )
        command = [
            "railway",
            "ssh",
            "--service",
            "prism-runtime",
            "--",
            "/app/.venv/bin/python",
            "-c",
            code,
        ]
    receipt = capture(command, args.destination)
    print(
        json.dumps(
            {
                k: receipt[k]
                for k in ("archive_path", "sha256", "artifact_bytes", "off_host_verified")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
