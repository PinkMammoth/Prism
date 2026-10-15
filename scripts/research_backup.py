"""Explicit sealed-candidate backup/recovery CLI. No scheduling or live authority."""

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
    remote = commands.add_parser("offhost")
    remote.add_argument("--config", type=Path, required=True)
    remote.add_argument("--generation", type=Path, required=True)
    remote.add_argument("--scratch", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "local":
        result = ns.backup_sealed(args.db, args.root, json.loads(args.source.read_text()))
    elif args.command == "offhost":
        result = ns.offhost(json.loads(args.config.read_text()), args.generation, args.scratch)
    else:
        result = ns.restore(args.archive, json.loads(args.manifest.read_text()), args.destination)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
