"""Export Railway backups locally, restore-test, then acknowledge preservation.

Default exports latest; --all inventories and preserves every historical backup.
No deletion by default. Preview with --retain; delete only with --apply-retention.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from market_signal.ops import backups as b


def remote(service, code, *args):
    out = subprocess.run(
        ["railway", "ssh", "--service", service, "--", "/app/.venv/bin/python", "-c", code, *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    ).stdout
    return json.loads(next(line for line in out.splitlines() if line.startswith("{")))


# Bootstrap lifecycle module without installing/deploying or opening the live DB.
# Source transmitted as an argument, never a shell-interpolated script.
def program(action):
    source = Path(b.__file__).read_text()
    return (
        "import sys,json; ns={'__name__':'backup_lifecycle'}; exec("
        + repr(source)
        + ",ns); "
        + action
    )


def stream_download(service, path, destination):
    """Read-only compressed SSH stream; no temporary production volume copy."""
    code = """
import base64,zlib,sys
z=zlib.compressobj(6,zlib.DEFLATED,31)
with open(sys.argv[1], 'rb') as f:
    while chunk:=f.read(2**20):
        data=z.compress(chunk)
        for start in range(0,len(data),16384):
            print('PRISM_CHUNK '+base64.b64encode(data[start:start+16384]).decode(),flush=True)
print('PRISM_CHUNK '+base64.b64encode(z.flush()).decode(),flush=True)
print('PRISM_DONE',flush=True)
"""
    compressed = destination.with_suffix(".gz")
    process = subprocess.Popen(
        ["railway", "ssh", "--service", service, "--", "/app/.venv/bin/python", "-c", code, path],
        stdout=subprocess.PIPE,
        text=True,
    )
    done = False
    try:
        with compressed.open("wb") as out:
            for line in process.stdout:
                if line.startswith("PRISM_CHUNK "):
                    out.write(base64.b64decode(line.split(" ", 1)[1].strip(), validate=True))
                elif line.strip() == "PRISM_DONE":
                    done = True
        if process.wait() != 0 or not done:
            raise ValueError("incomplete Railway download")
        with gzip.open(compressed, "rb") as fi, destination.open("wb") as fo:
            shutil.copyfileobj(fi, fo, 2**20)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("destination", type=Path)
    p.add_argument("--all", action="store_true")
    p.add_argument("--service", default="prism-runtime")
    p.add_argument("--no-compression", action="store_true")
    p.add_argument("--no-acknowledge", action="store_true")
    p.add_argument(
        "--retain", action="store_true", help="Preview eligible old backup cleanup after export"
    )
    p.add_argument(
        "--apply-retention",
        action="store_true",
        help="Apply reviewed retention after successful export",
    )
    args = p.parse_args()
    if os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative":
        raise ValueError("run the archive helper on the off-host destination machine")
    args.destination = b.safe_location(args.destination)
    root, db = "/data/backups", "/data/prism.duckdb"
    listing = remote(
        args.service,
        program(
            "print(json.dumps({'rows':ns['inventory'](ns['Path'](sys.argv[1]),ns['Path'](sys.argv[2]))}))"
        ),
        root,
        db,
    )["rows"]
    selected = listing if args.all else listing[-1:]
    if not selected:
        raise ValueError("no existing backups")
    args.destination.mkdir(parents=True, exist_ok=True)
    for row in selected:
        path = row["path"]
        manifest = remote(
            args.service,
            program(
                "print(json.dumps(ns['register'](ns['Path'](sys.argv[1]),ns['Path'](sys.argv[3]),ns['Path'](sys.argv[2]),origin='Railway '+ns['os'].environ.get('RAILWAY_ENVIRONMENT_NAME','unknown'))))"
            ),
            root,
            db,
            path,
        )
        suffix = "" if args.no_compression else ".gz"
        dest = args.destination / (Path(path).name + suffix)
        if dest.exists():
            receipt = json.loads(b.sidecar(dest).read_text())
            from market_signal.ops import runtime as rt

            if (
                rt._sha256(dest) != receipt["artifact_sha256"]
                or receipt["sha256"] != manifest["sha256"]
            ):
                raise ValueError("existing archive mismatch")
        else:
            with tempfile.TemporaryDirectory(dir=args.destination) as d:
                downloaded = Path(d) / "download.duckdb"
                stream_download(args.service, path, downloaded)
                receipt = b.export_backup(
                    downloaded, dest, manifest, compress=not args.no_compression
                )
        if not args.no_acknowledge:
            remote(
                args.service,
                program(
                    "ns['acknowledge'](ns['Path'](sys.argv[1]),ns['Path'](sys.argv[3]),ns['Path'](sys.argv[2]),json.loads(sys.argv[4])); print(json.dumps({'acknowledged':sys.argv[3]}))"
                ),
                root,
                db,
                path,
                json.dumps(receipt),
            )
        print(
            f"Verified off-host archive: {dest}; logical SHA256 {receipt['sha256']}; restore passed",
            flush=True,
        )

    if args.retain or args.apply_retention:
        result = remote(
            args.service,
            program(
                "from market_signal.ops import runtime as rt; "
                "root=ns['Path'](sys.argv[1]); db=ns['Path'](sys.argv[2]); "
                "apply=sys.argv[3]=='yes'; "
                "lock=rt.runtime_lock(db,'backup-retention',600); lock.__enter__(); "
                "result=ns['retain'](root,db,**ns['policy'](root),apply=apply); "
                "lock.__exit__(None,None,None); print(json.dumps(result))"
            ),
            root,
            db,
            "yes" if args.apply_retention else "no",
        )
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
