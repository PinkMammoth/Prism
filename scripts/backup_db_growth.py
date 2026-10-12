"""Read-only block/row diagnostics of verified off-host recovery snapshots."""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import tempfile
from pathlib import Path

import duckdb

from market_signal.ops import raw_archives as raw


def inspect(path: Path) -> dict:
    manifest = json.loads(path.with_name(path.name + ".manifest.json").read_text())
    if raw.sha256(path) != manifest["artifact_sha256"]:
        raise ValueError("archive artifact checksum mismatch")
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "restore.duckdb"
        with gzip.open(path, "rb") as fi, db.open("wb") as fo:
            shutil.copyfileobj(fi, fo, 2**20)
        if raw.sha256(db) != manifest["sha256"]:
            raise ValueError("decompressed DB checksum mismatch")
        with duckdb.connect(str(db), read_only=True) as con:
            result = con.execute("PRAGMA database_size")
            layout = [
                dict(zip([c[0] for c in result.description], row, strict=True))
                for row in result.fetchall()
            ]
            tables = con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_catalog=current_database() AND table_schema='main' AND table_type='BASE TABLE'"
            ).fetchall()
            counts, blocks = {}, {}
            for (table,) in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                counts[table] = con.execute("SELECT count(*) FROM " + quoted).fetchone()[0]
                rows = con.execute(
                    "SELECT block_id,compression FROM pragma_storage_info(?)", [table]
                ).fetchall()
                blocks[table] = {
                    "block_ids": sorted({r[0] for r in rows if r[0] is not None and r[0] >= 0}),
                    "compression": sorted({r[1] for r in rows if r[1]}),
                }
        if raw.sha256(db) != manifest["sha256"]:
            raise ValueError("read-only diagnostics changed restored bytes")
    return {
        "snapshot_timestamp": manifest["backup_timestamp"],
        "sha256": manifest["sha256"],
        "file_size": manifest["db_file_size"],
        "schema_version": manifest["schema_version"],
        "database_size": layout,
        "table_rows": counts,
        "table_data_blocks": blocks,
        "limits": "Snapshot metadata, not active DB. Table data blocks may be shared; indexes/metadata unassigned. File-size delta is not a retained-data forecast.",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("output", type=Path)
    p.add_argument("archives", nargs="+", type=Path)
    args = p.parse_args()
    result = [inspect(path) for path in args.archives]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            [
                {k: v for k, v in r.items() if k not in {"table_rows", "table_data_blocks"}}
                for r in result
            ],
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
