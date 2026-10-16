"""Pull, verify, restore-test and acknowledge a Lab recovery backup over SSH.

Only the backup file and its manifest travel off-host. Acknowledgement writes the
verified export receipt; pruning stays with the existing governed retention path.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from market_signal.ops import backups as b
from market_signal.ops import runtime as rt


def remote(host, program, *args, payload=None):
    command = "sudo -u prism /opt/prism/current/.venv/bin/python -c " + shlex.quote(program)
    command += " " + " ".join(shlex.quote(str(arg)) for arg in args)
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", host, command],
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def pull(host: str, destination: Path, *, source: str | None = None) -> dict:
    destination = b.safe_location(destination)
    destination.mkdir(parents=True, exist_ok=True)
    rows = remote(
        host,
        "from pathlib import Path; import json; "
        "from market_signal.ops.backups import inventory; "
        "print(json.dumps(inventory(Path('/data/backups'), Path('/data/prism.duckdb'))))",
    )
    selected = [r for r in rows if source is None or r["path"] == source]
    if not selected:
        raise ValueError("no matching Lab recovery backup")
    row = selected[-1]
    path = Path(row["path"])
    if path.parent.parent != Path("/data/backups") or not b.NAME.fullmatch(path.name):
        raise ValueError("unexpected Lab backup path")
    manifest = row["manifest"]
    if not manifest.get("sha256") or not manifest.get("verification_result"):
        raise ValueError("source backup is not registered and verified")
    archive = destination / (path.name + ".gz")
    with tempfile.TemporaryDirectory(prefix=".lab-export-", dir=destination) as tmp:
        copy = Path(tmp) / path.name
        subprocess.run(
            [
                "rsync",
                "--partial",
                "--times",
                "--rsync-path=sudo -u prism rsync",
                f"{host}:{path}",
                str(copy),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if rt._sha256(copy) != manifest["sha256"]:
            raise ValueError("download checksum mismatch")
        restore = rt.restore_test(copy, Path(tmp))
        if not restore["restored_ok"]:
            raise ValueError("scratch restore failed")
        if archive.exists():
            restored = Path(tmp) / "existing-archive.duckdb"
            with gzip.open(archive, "rb") as fi, restored.open("wb") as fo:
                shutil.copyfileobj(fi, fo, 2**20)
            verified = b.inspect_backup(restored)
            if verified["sha256"] != manifest["sha256"]:
                raise ValueError("existing archive checksum mismatch")
            receipt = {
                **manifest,
                "restore_test": verified,
                "export_download_timestamp": b.now(),
                "archive_path": str(archive.resolve()),
                "artifact_sha256": rt._sha256(archive),
                "artifact_bytes": archive.stat().st_size,
                "compression": "gzip",
                "off_host_verified": True,
            }
        else:
            receipt = b.export_backup(copy, archive, manifest)
        if receipt["restore_test"]["table_fingerprints"] != manifest["table_fingerprints"]:
            raise ValueError("source and restored table fingerprints differ")
        receipt["scratch_restore_test"] = restore
        b.write_json(b.sidecar(archive), receipt)
    acknowledged = remote(
        host,
        "from pathlib import Path; import json,sys; "
        "from market_signal.ops.backups import acknowledge; "
        "acknowledge(Path('/data/backups'),Path(sys.argv[1]),Path('/data/prism.duckdb'),json.load(sys.stdin)); "
        "print(json.dumps({'acknowledged':sys.argv[1]}))",
        path,
        payload=json.dumps(receipt, default=str),
    )
    return {
        "source": str(path),
        "archive": str(archive.resolve()),
        "sha256": manifest["sha256"],
        "restored_ok": True,
        "table_fingerprints_match": True,
        **acknowledged,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination", type=Path, nargs="?", default=Path("data/backup-archive/vps")
    )
    parser.add_argument("--source", help="Specific registered source; default newest")
    args = parser.parse_args()
    host = os.environ.get("PRISM_VPS_HOST")
    if not host:
        parser.error("set PRISM_VPS_HOST=user@host")
    print(json.dumps(pull(host, args.destination, source=args.source), indent=2, default=str))


if __name__ == "__main__":
    main()
