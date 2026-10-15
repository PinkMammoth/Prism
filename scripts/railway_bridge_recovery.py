"""Explicit bridge exception: remove one backup only after exact off-host restore proof.

Normal retention protects the newest volume backup. This command is for the authorized
temporary Railway bridge, where an off-host recovery point replaces that volume copy.
It preserves all sidecars/policies, logs intent/completion, and never opens the DB writable.
Default is a reviewable plan. --apply requires the plan's source identity to still match.
"""

import argparse
import gzip
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from market_signal.ops import backups as b
from market_signal.ops import runtime as rt

ACTION = r"""
from pathlib import Path
from market_signal.ops import runtime as rt
root=Path('/data/backups'); db=Path('/data/prism.duckdb')
request=json.loads(sys.argv[1]); path=Path(request['source_path'])
with rt.runtime_lock(db,'verified-offhost-bridge-backup-removal',45):
    rt.require_authoritative(db)
    ns['safe_path'](root,path,db)
    st=path.stat(); identity=[st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns]
    verified=ns['inspect_backup'](path)
    if verified['sha256']!=request['archive_sha256']:
        raise ValueError('exact off-host backup mismatch')
    metadata=ns['sidecar'](path).read_text()
    result={'source_path':str(path),'source_identity':identity,
      'logical_bytes':st.st_size,'allocated_bytes':st.st_blocks*512,
      'sha256':verified['sha256'],'source_verification':verified,
      'source_manifest_text':metadata,'source_metadata_sha256':rt._sha256(ns['sidecar'](path)),
      'replacement':request['replacement'],'exact_archive':request['exact_archive'],
      'free_before':rt.disk_state(db).free,'at':ns['now']()}
    if request.get('apply'):
        if identity!=request['plan']['source_identity'] or result['source_metadata_sha256']!=request['plan']['source_metadata_sha256']:
            raise ValueError('source changed since reviewed plan')
        logpath=root/'retention.jsonl'
        if logpath.is_symlink(): raise ValueError('unsafe deletion log')
        record={k:v for k,v in result.items() if k not in ('source_manifest_text','source_verification')}
        with logpath.open('a') as log:
            log.write(json.dumps({**record,'action':'bridge_delete_intent'})+'\n');log.flush();ns['os'].fsync(log.fileno())
            path.unlink();ns['sync_directory'](path.parent)
            log.write(json.dumps({**record,'action':'bridge_deleted'})+'\n');log.flush();ns['os'].fsync(log.fileno())
        result['deleted']=True
    else: result['deleted']=False
    result['free_after']=rt.disk_state(db).free
print(json.dumps(result,default=str))
"""


def verify_local(archive):
    receipt = json.loads(b.sidecar(archive).read_text())
    if not receipt.get("off_host_verified") or rt._sha256(archive) != receipt["artifact_sha256"]:
        raise ValueError("verified local archive required")
    with tempfile.TemporaryDirectory(dir=archive.parent) as temp:
        restored = Path(temp) / "restore.duckdb"
        with gzip.open(archive, "rb") as fi, restored.open("wb") as fo:
            shutil.copyfileobj(fi, fo, 2**20)
        proof = b.inspect_backup(restored)
        if proof["sha256"] != receipt["sha256"]:
            raise ValueError("restore checksum mismatch")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_path", type=Path)
    parser.add_argument("exact_archive", type=Path)
    parser.add_argument("replacement", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.environ.get("PRISM_RUNTIME_ROLE") == "authoritative":
        raise ValueError("off-host operator only")
    exact, replacement = verify_local(args.exact_archive), verify_local(args.replacement)
    if replacement.get("authority", [None])[0] != "railway-prism-runtime":
        raise ValueError("replacement authority/frontier required")
    if replacement["schema_version"] != exact["schema_version"]:
        raise ValueError("replacement schema mismatch")
    request = {
        "source_path": str(args.source_path),
        "archive_sha256": exact["sha256"],
        "exact_archive": exact,
        "replacement": replacement,
        "apply": args.apply,
    }
    if args.apply:
        plan = json.loads(args.plan.read_text())
        if plan["source_path"] != str(args.source_path) or plan["sha256"] != exact["sha256"]:
            raise ValueError("wrong reviewed deletion plan")
        request["plan"] = plan
    code = (
        "import sys,json;ns={'__name__':'bridge_ops'};exec("
        + repr(Path(b.__file__).read_text())
        + ",ns);"
        + ACTION
    )
    result = subprocess.run(
        [
            "railway",
            "ssh",
            "--service",
            "prism-runtime",
            "--",
            "/app/.venv/bin/python",
            "-c",
            code,
            json.dumps(request),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    out = json.loads(next(line for line in result.stdout.splitlines() if line.startswith("{")))
    target = args.plan.with_suffix(".applied.json") if args.apply else args.plan
    target.parent.mkdir(parents=True, exist_ok=True)
    b.write_json(target, out)
    print(
        json.dumps(
            {
                k: out[k]
                for k in (
                    "source_path",
                    "logical_bytes",
                    "allocated_bytes",
                    "deleted",
                    "free_before",
                    "free_after",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
