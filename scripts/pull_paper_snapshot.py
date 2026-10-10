"""Retrieve an existing Railway backup; remote commands are reads only.

Run with `uv run python scripts/pull_paper_snapshot.py`. Never creates a production backup,
checkpoints production, or connects to its live DuckDB. Only a local verified copy is replaced.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

BACKUP = re.compile(
    r"/data/backups/(?:daily|manual|weekly)/prism-(\d{8}T\d{6}Z)(?:-[\w-]+)?\.duckdb\Z"
)


def command(args, *, capture=True):
    return (
        subprocess.run(args, check=True, text=True, capture_output=capture, timeout=1800).stdout
        or ""
    )


# The remote program opens an existing immutable backup, never the production live DB.
# Keep an explicit table/filter allowlist. No remote files or export artifacts are written.
REQUIRED_PAPER_TABLES = (
    "paper_v2_universes",
    "paper_v2_policies",
    "paper_v2_runs",
    "paper_v2_evaluations",
    "paper_v2_events",
    "paper_v2_opportunities",
    "paper_nimble_policies",
    "paper_nimble_runs",
    "paper_nimble_events",
    "paper_nimble_accounts",
    "paper_nimble_positions",
    "paper_nimble_opportunities",
    "paper_nimble_theses",
    "paper_nimble_research",
)
OPTIONAL_PAPER_TABLES = ("paper_event_evidence", "paper_event_pending")
PAPER_TABLES = (*REQUIRED_PAPER_TABLES, *OPTIONAL_PAPER_TABLES)
REMOTE_EXPORT = """
import base64, gzip, hashlib, json, sys
import duckdb
con = duckdb.connect(sys.argv[1], read_only=True)
con.execute("BEGIN TRANSACTION")
out = {}
for table in sys.argv[2:]:
    columns = con.execute("SELECT column_name,data_type FROM information_schema.columns WHERE table_name=? ORDER BY ordinal_position", [table]).fetchall()
    if not columns:
        continue
    where = " WHERE fired" if table.endswith("_opportunities") else " WHERE event_type != 'position_mark'" if table == 'paper_nimble_events' else ''
    values = con.execute('SELECT * FROM ' + table + where).fetchall()
    out[table] = {"columns": columns, "rows": values}
raw = json.dumps(out, default=str, separators=(',', ':')).encode()
con.close()
print('PRISM_PAPER_EXPORT ' + hashlib.sha256(raw).hexdigest() + ' ' + base64.b64encode(gzip.compress(raw)).decode())
"""


def export_paper_backup(remote, destination, service):
    output = command(
        [
            "railway",
            "ssh",
            "--service",
            service,
            "--",
            "/app/.venv/bin/python",
            "-c",
            REMOTE_EXPORT,
            remote,
            *PAPER_TABLES,
        ]
    )
    line = next(
        (line for line in output.splitlines() if line.startswith("PRISM_PAPER_EXPORT ")), None
    )
    if line is None:
        raise ValueError("Paper snapshot export unavailable; previous snapshot retained")
    _, expected, encoded = line.split(" ", 2)
    raw = gzip.decompress(base64.b64decode(encoded, validate=True))
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Paper export checksum mismatch; previous snapshot retained")
    exported = json.loads(raw)
    if not set(REQUIRED_PAPER_TABLES) <= set(exported) or not set(exported) <= set(PAPER_TABLES):
        raise ValueError("Paper export table set differs from the reporting allowlist")
    with duckdb.connect(str(destination)) as con:
        con.execute("BEGIN TRANSACTION")
        for table in PAPER_TABLES:
            if table not in exported:
                continue
            columns = exported[table]["columns"]
            # Types and names are validated before building local-only snapshot tables.
            if not columns or any(
                not re.fullmatch(r"[a-z_]+", name)
                or kind
                not in {
                    "VARCHAR",
                    "INTEGER",
                    "BIGINT",
                    "BOOLEAN",
                    "JSON",
                    "TIMESTAMP WITH TIME ZONE",
                    "TIMESTAMP",
                    "DOUBLE",
                }
                for name, kind in columns
            ):
                raise ValueError("Unsupported paper export schema")
            con.execute(
                "CREATE TABLE "
                + table
                + "("
                + ",".join('"' + name + '" ' + kind for name, kind in columns)
                + ")"
            )
            if exported[table]["rows"]:
                con.executemany(
                    "INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in columns) + ")",
                    exported[table]["rows"],
                )
        con.execute("COMMIT")
    return expected


def pull(
    destination: Path, *, service="prism-runtime", volume="prism-runtime-volume", paper_only=True
):
    destination = destination.resolve()
    execution_db = Path(os.environ.get("PRISM_DB_PATH") or "data/prism.duckdb").resolve()
    if os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative" or destination in {
        execution_db,
        Path("/data/prism.duckdb"),
    }:
        raise ValueError(
            "Snapshot retrieval refuses an authoritative runtime or configured execution database destination"
        )
    listing = command(
        [
            "railway",
            "ssh",
            "--service",
            service,
            "--",
            "find",
            "/data/backups",
            "-type",
            "f",
            "-name",
            "prism-*.duckdb",
        ]
    )
    candidates = [
        (match[1], line.strip())
        for line in listing.splitlines()
        if (match := BACKUP.fullmatch(line.strip()))
    ]
    if not candidates:
        raise ValueError(
            "No existing production backups found; no backup will be created by this tool."
        )
    stamp, remote = max(candidates)
    sha_output = command(["railway", "ssh", "--service", service, "--", "sha256sum", remote])
    match = re.search(r"\b([a-f0-9]{64})\s+" + re.escape(remote), sha_output)
    if not match:
        raise ValueError("Remote checksum unavailable")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paper-snapshot-", dir=destination.parent) as directory:
        temp = Path(directory) / "snapshot.duckdb"
        if paper_only:
            export_digest = export_paper_backup(remote, temp, service)
        else:
            export_digest = None
            command(
                [
                    "railway",
                    "volume",
                    "files",
                    "--volume",
                    volume,
                    "download",
                    remote.removeprefix("/data"),
                    str(temp),
                    "--json",
                ],
                capture=False,
            )
        with temp.open("rb") as downloaded:
            sha = hashlib.file_digest(downloaded, "sha256").hexdigest()
        if not paper_only and sha != match[1]:
            raise ValueError("Snapshot checksum mismatch; previous local snapshot retained")
        with duckdb.connect(str(temp), read_only=True) as con:
            tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            if not {"paper_v2_runs", "paper_nimble_runs"} <= tables:
                raise ValueError(
                    "Backup predates paper dashboard schema; previous snapshot retained"
                )
            con.execute("SELECT count(*) FROM paper_v2_runs").fetchone()
            con.execute("SELECT count(*) FROM paper_nimble_runs").fetchone()
        os.replace(temp, destination)
        # Remote filename is the production backup creation timestamp, not local mtime.
        manifest = {
            "source": "Railway existing verified backup",
            "scope": "paper observability tables; fired opportunities; optional event evidence and pending decisions; no per-position mark events"
            if paper_only
            else "full database backup",
            "event_evidence_available": "paper_event_evidence" in tables,
            "production_backup_sha256": match[1],
            "export_sha256": export_digest,
            "production_path": remote,
            "snapshot_at": datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=UTC)
            .isoformat(),
            "downloaded_at": datetime.now(UTC).isoformat(),
            "sha256": sha,
            "bytes": destination.stat().st_size,
            "local_mtime_ns": destination.stat().st_mtime_ns,
        }
        sidecar = destination.with_suffix(".snapshot.json")
        manifest_temp = Path(directory) / "snapshot.json"
        manifest_temp.write_text(json.dumps(manifest, indent=2) + "\n")
        os.replace(manifest_temp, sidecar)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination", type=Path, default=Path("data/remote/paper-dashboard.duckdb")
    )
    parser.add_argument(
        "--full-backup",
        action="store_true",
        help="Download the entire DuckDB instead of the compact paper observability snapshot",
    )
    args = parser.parse_args()
    result = pull(
        args.destination,
        service=os.environ.get("PRISM_RAILWAY_SERVICE", "prism-runtime"),
        volume=os.environ.get("PRISM_RAILWAY_VOLUME", "prism-runtime-volume"),
        paper_only=not args.full_backup,
    )
    print(
        f"Verified local snapshot: {args.destination}\nSnapshot: {result['snapshot_at']}\nDownloaded: {result['downloaded_at']}\nSHA256: {result['sha256']}"
    )


if __name__ == "__main__":
    main()
