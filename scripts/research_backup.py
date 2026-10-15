"""Explicit sealed-candidate backup/recovery CLI. No live writer or Research authority.

Off-host commands inherit RESTIC_REPOSITORY / RESTIC_PASSWORD_FILE / RCLONE_CONFIG from a
protected environment file; nothing secret is accepted on the command line or printed.
"""

import argparse
import json
from pathlib import Path

from market_signal.ops import research_namespace as ns


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("local")
    local.add_argument("--db", type=Path, required=True)
    local.add_argument("--root", type=Path, required=True)
    local.add_argument("--source", type=Path, required=True)
    local.add_argument("--keep-verified", action="store_true")
    remote = commands.add_parser("offhost")
    remote.add_argument("--config", type=Path, required=True)
    remote.add_argument("--generation", type=Path, required=True)
    remote.add_argument("--scratch", type=Path, required=True)
    remote.add_argument("--keep-restore", action="store_true")
    recover = commands.add_parser("offhost-restore", help="recover a snapshot to a fresh path")
    recover.add_argument("--snapshot", default="latest")
    recover.add_argument("--scratch", type=Path, required=True)
    recover.add_argument("--keep", action="store_true")
    commands.add_parser("offhost-snapshots")
    nightly = commands.add_parser("scheduled")
    nightly.add_argument("--config", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "local":
        result = ns.backup_sealed(
            args.db,
            args.root,
            json.loads(args.source.read_text()),
            keep_verified=args.keep_verified,
        )
    elif args.command == "offhost":
        result = ns.offhost(
            json.loads(args.config.read_text()),
            args.generation,
            args.scratch,
            keep_restore=args.keep_restore,
        )
    elif args.command == "offhost-restore":
        snapshot = args.snapshot
        if snapshot == "latest":
            snapshot = ns.offhost_snapshots()[-1]["id"]
        result = ns.offhost_restore(snapshot, args.scratch, keep=args.keep)
        result["manifest"] = {k: v for k, v in result["manifest"].items() if k != "tables"}
    elif args.command == "offhost-snapshots":
        result = [
            {k: s.get(k) for k in ("id", "time", "paths", "tags", "summary")}
            for s in ns.offhost_snapshots()
        ]
    elif args.command == "scheduled":
        config = json.loads(args.config.read_text())
        source = json.loads(Path(config["source_descriptor"]).read_text())
        result = ns.scheduled(config, source)
    else:
        result = ns.restore(args.archive, json.loads(args.manifest.read_text()), args.destination)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
