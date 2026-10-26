"""OVH backup lifecycle glue. Uses installed verifiers and governed backup-retain.

Root coordinates; owner workers alone touch owner manifests, backup identity alone
accesses Drive credentials. No runtime/policy edits, no recovery-file unlink here.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import os
import pwd
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

AUTHORITIES = {
    "lab": dict(
        owner="prism",
        release="/opt/prism/current",
        env="/etc/prism/prism.env",
        secrets="/etc/prism/secrets.env",
        db="/data/prism.duckdb",
        root="/data/backups",
    ),
    "research": dict(
        owner="prism-research",
        release="/opt/prism/research/current",
        env="/etc/prism-research/runtime.env",
        secrets="/etc/prism-research/secrets.env",
        db="/var/lib/prism-research/import/prism.duckdb",
        root="/var/lib/prism-research/backup-sealed/registered",
    ),
}
BASE = Path("/var/lib/prism-research-backup/cadence")
SCRIPT = Path(__file__).resolve()


def now():
    return datetime.now(UTC).isoformat()


def bounded_connections():
    # Only this backup process. Avoid default host-sized DuckDB allocations inside
    # the 3G backup cgroup; never alter settings in a trading/collector process.
    import duckdb

    original = duckdb.connect

    def connect(*args, **kwargs):
        kwargs["config"] = {**kwargs.get("config", {}), "threads": "1", "memory_limit": "256MB"}
        return original(*args, **kwargs)

    duckdb.connect = connect


def invoke(authority, mode, payload=None, *, backup_identity=False):
    c = AUTHORITIES[authority]
    account = "prism-research-backup" if backup_identity else c["owner"]
    envfiles = (
        ["/etc/prism-research-backup/backup.env"] if backup_identity else [c["env"], c["secrets"]]
    )
    command = "set -eu; set -a; "
    for path in envfiles:
        command += f"if [ -f {shlex.quote(path)} ]; then . {shlex.quote(path)}; fi; "
    command += "set +a; "
    args = [
        "/usr/sbin/runuser",
        "-u",
        account,
        "--",
        "/usr/bin/env",
        "PATH=/opt/prism/research-backup/tools/bin:/usr/bin:/bin",
        "PYTHONPATH=" + c["release"] + "/src",
        c["release"] + "/.venv/bin/python",
        str(SCRIPT),
        mode,
        authority,
    ]
    if backup_identity:
        # Serialize only shared repository access, not authority backup success.
        position = args.index(c["release"] + "/.venv/bin/python")
        args[position:position] = [
            "/usr/bin/flock",
            "--wait",
            "1800",
            "/var/lib/prism-research-backup/repository.lifecycle.lock",
        ]
    command += "exec " + shlex.join(args)
    result = subprocess.run(
        ["/bin/bash", "-c", command],
        input=json.dumps(payload or {}),
        text=True,
        capture_output=True,
        timeout=3600,
    )
    if result.returncode:
        # Never relay provider stderr or environment/credential values.
        detail = " " + (result.stderr.splitlines() or ["worker failed"])[-1]
        raise RuntimeError(f"{authority}:{mode} failed (exit {result.returncode}){detail}")
    return json.loads(result.stdout)


def owner(mode, authority, data):
    from market_signal.ops import backups as b
    from market_signal.ops import research_namespace as ns
    from market_signal.ops import runtime as rt

    c = AUTHORITIES[authority]
    root, db = Path(c["root"]), Path(c["db"])
    os.environ["PRISM_BACKUP_DIR"] = str(root)
    if mode == "inventory":
        return b.inventory(root, db)
    if mode == "inspect":
        path = b.safe_path(root, Path(data["path"]), db)
        with rt.runtime_lock(db, "backup-cadence-inspect", 600):
            old = json.loads(b.sidecar(path).read_text()) if b.sidecar(path).exists() else {}
            if old:
                actual = b.inspect_backup(path)
                if old.get("sha256") != actual["sha256"]:
                    raise ValueError("registered recovery copy changed")
                if old.get("table_fingerprints") != actual["table_fingerprints"]:
                    raise ValueError("registered fingerprints changed")
                actual = old
            else:
                # register() itself performs the complete inspection.
                actual = b.register(root, path, db, origin="ovh-interrupted-backup-recovery")
            meta = ns.inspect(path)
            fingerprints = {
                k: {"rows": v["rows"], "hash": str(v["value_hash"])}
                for k, v in meta["tables"].items()
            }
            if meta["sha256"] != actual["sha256"] or fingerprints != actual["table_fingerprints"]:
                raise ValueError("backup verifier fingerprints differ")
            return {"registered": actual, "inspection": meta}
    if mode == "ack":
        with rt.runtime_lock(db, "backup-cadence-ack", 600):
            b.acknowledge(root, Path(data["path"]), db, data["receipt"])
        return {"acknowledged": data["path"]}
    if mode == "retain":
        # Execute the public governed command, carrying forward any rollback pin.
        # The command rechecks safety and takes the owner runtime lock itself.
        from market_signal.cli.main import app

        policy = b.policy(root)
        args = ["ops", "backup-retain", "--apply", "--keep", str(policy["keep"])]
        if policy.get("previous"):
            args += ["--previous", str(policy["previous"])]
        if policy.get("reason"):
            args += ["--reason", policy["reason"]]
        # A rollback pin may change while the public command waits for the owner
        # lock. Fail closed before its save_policy() can overwrite a newer pin.
        from contextlib import contextmanager

        original_lock = rt.runtime_lock

        @contextmanager
        def policy_checked_lock(*lock_args, **lock_kwargs):
            with original_lock(*lock_args, **lock_kwargs):
                if b.policy(root) != policy:
                    raise ValueError("retention policy changed while waiting; retry required")
                yield

        rt.runtime_lock = policy_checked_lock
        try:
            app(args=args, standalone_mode=False)
        finally:
            rt.runtime_lock = original_lock
        return None
    if mode == "create":
        return rt.backup_live(db, kind="manual", label="cadence-verification")
    if mode == "prepare":
        import runpy

        module = runpy.run_path(c["release"] + "/deploy/consolidated/research_backup_owner.py")
        module["prepare"]()
        return None
    raise ValueError("unknown owner action")


def scoped_offhost_tool(authority, tool, name, args, **kwargs):
    if authority == "lab" and name == "restic" and args[0] == "forget":
        # Lab historically archives every exported recovery state. Do not import
        # Research's expiry philosophy when reusing its integrity/restore helper.
        args = [*args, "--keep-tag", "prism-lab-backup-v1"]
    return tool(name, args, **kwargs)


def export_worker(authority, data):
    from market_signal.ops import research_namespace as ns

    classify = ns._classify

    def backup_failure(stderr):
        text = (stderr or "").lower()
        for phrase, label in (
            ("read-only file system", "sandbox filesystem"),
            ("permission denied", "filesystem permission"),
            ("already locked", "repository lock"),
        ):
            if phrase in text:
                return label
        return classify(stderr)

    ns._classify = backup_failure
    config = json.loads(Path("/etc/prism-research-backup/backup.json").read_text())
    if authority == "lab":
        ns.RESTIC_HOST, ns.RESTIC_TAG = "vps-prism-lab-backup", "prism-lab-backup-v1"
        original_tool = ns._tool
        ns._tool = lambda name, args, **kwargs: scoped_offhost_tool(
            authority, original_tool, name, args, **kwargs
        )
        config["manifest_remote"] = "prism-gdrive:Prism/backups/lab/manifests"
        config["destination_id"] = (
            "google-drive:Prism/backups/research/restic (isolated Lab host/tag)"
        )
    generation = ns.safe(data["generation"])
    manifest = json.loads((generation / "manifest.json").read_text())
    # Verify local compressed artifact before uploading. offhost() then reads back
    # from restic with no cache and verifies SHA, size, and every table fingerprint.
    scratch = generation.parent / ("restore-" + generation.name)
    ns.restore(generation / "research.duckdb.gz", manifest, scratch / "local.duckdb")
    # Only a redundant scratch restore is removed; recovery copies use backup-retain.
    import shutil

    shutil.rmtree(scratch)
    return ns.offhost(config, generation, scratch)


def stage_export(authority, row, proof):
    from market_signal.ops import backups as b
    from market_signal.ops import research_namespace as ns

    manifest = proof["registered"]
    generation = BASE / authority / ("generation-" + manifest["sha256"])
    generation.mkdir(parents=True, exist_ok=True, mode=0o700)
    account = pwd.getpwnam("prism-research-backup")
    for path in (BASE, BASE / authority, generation):
        os.chown(path, account.pw_uid, account.pw_gid)
    archive = generation / "research.duckdb.gz"
    if not archive.exists():
        partial = generation / "archive.part"
        if partial.exists():
            raise ValueError("interrupted compression requires inspection; source preserved")
        with Path(row["path"]).open("rb") as src, partial.open("xb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as dst:
                import shutil

                shutil.copyfileobj(src, dst, 2**20)
            raw.flush()
            os.fsync(raw.fileno())
        os.rename(partial, archive)
        b.sync_directory(generation)
    # Source stays immutable while compressed and checked against the inspected hash.
    if ns.sha256(Path(row["path"])) != manifest["sha256"]:
        raise ValueError("source changed during compression")
    meta = {
        **proof["inspection"],
        "compressed_sha256": ns.sha256(archive),
        "compressed_bytes": archive.stat().st_size,
        "source": manifest,
        "created_at": manifest["verified_at"],
    }
    descriptor = Path("/var/lib/prism-research/backup-sealed/source.json")
    if authority == "research" and descriptor.exists():
        extra = json.loads(descriptor.read_text())
        if extra["sha256"] == manifest["sha256"]:
            meta["source"] = extra  # preserve core/context recovery metadata
    target = generation / "manifest.json"
    if target.exists():
        # Never silently replace identity for a retry.
        if json.loads(target.read_text())["sha256"] != meta["sha256"]:
            raise ValueError("staged generation identity mismatch")
    else:
        b.write_json(target, meta)
    for path in (archive, target):
        os.chown(path, account.pw_uid, account.pw_gid)
    return generation


def acknowledged(row):
    manifest = row["manifest"]
    receipt = manifest.get("export_receipt", {})
    digest = manifest.get("sha256")
    return bool(
        digest
        and receipt.get("off_host_verified") is True
        and receipt.get("sha256") == digest
        and receipt.get("restore_test", {}).get("sha256") == digest
        and receipt.get("artifact_sha256")
        and receipt.get("archive_path")
        and receipt.get("export_download_timestamp")
    )


def drain(authority):
    rows = invoke(authority, "inventory")
    changed = False
    for row in rows:
        if acknowledged(row):
            from market_signal.ops import backups as b

            digest = row["manifest"]["sha256"]
            generation = BASE / authority / ("generation-" + digest)
            receipt_path = generation / "offhost-receipt.json"
            if receipt_path.exists():
                exported = json.loads(receipt_path.read_text())
                owner_receipt = row["manifest"]["export_receipt"]
                if not (generation / "owner-ack.json").exists() and (
                    exported.get("off_host_verified")
                    and exported["sha256"] == digest
                    and owner_receipt["archive_path"] == "restic:" + exported["snapshot_id"]
                ):
                    b.write_json(generation / "owner-ack.json", {"sha256": digest, "at": now()})
            continue
        changed = True
        proof = invoke(authority, "inspect", {"path": row["path"]})
        generation = stage_export(authority, row, proof)
        receipt = invoke(authority, "export", {"generation": str(generation)}, backup_identity=True)
        if not receipt.get("off_host_verified") or not receipt.get("restore_verified"):
            raise ValueError("matching successful off-host restore required")
        if receipt["sha256"] != proof["registered"]["sha256"]:
            raise ValueError("off-host identity mismatch")
        ack = {
            "off_host_verified": True,
            "sha256": receipt["sha256"],
            "restore_test": {"sha256": receipt["sha256"]},
            "artifact_sha256": receipt["compressed_sha256"],
            "archive_path": "restic:" + receipt["snapshot_id"],
            "export_download_timestamp": receipt["verified_at"],
        }
        invoke(authority, "ack", {"path": row["path"], "receipt": ack})
        from market_signal.ops import backups as b

        b.write_json(generation / "owner-ack.json", {"sha256": receipt["sha256"], "at": now()})
        if authority == "research":
            b.write_json(
                Path("/var/lib/prism-research-backup/state/last-run.json"),
                {"status": "ok", "stage": "done", "finished_at": now(), "receipt": receipt},
            )
    if changed or len(rows) > 1:
        retained = invoke(authority, "retain")
    else:
        # Healthy retries need no owner lock or full-table retention scan.
        retained = {
            "keep": [r["path"] for r in rows],
            "removed": [],
            "blocked": [],
            "dry_run": False,
            "already_reconciled": True,
        }
    prune_exports(authority)
    return retained


def prune_exports(authority):
    """Existing compressed-generation retention, only after all owner ACKs succeed."""
    from market_signal.ops import backups as b
    from market_signal.ops import research_namespace as ns

    root = BASE / authority
    generations = sorted(root.glob("generation-*"), key=lambda p: p.stat().st_mtime_ns)
    if not generations:
        return
    for generation in generations:
        receipt_path, ack_path = generation / "offhost-receipt.json", generation / "owner-ack.json"
        if not receipt_path.exists() or not ack_path.exists():
            raise ValueError("unacknowledged export staging preserved")
        receipt, ack = json.loads(receipt_path.read_text()), json.loads(ack_path.read_text())
        if not receipt.get("off_host_verified") or receipt["sha256"] != ack["sha256"]:
            raise ValueError("export staging identity uncertain")
    newest = generations[-1]
    b.write_json(root / "latest.json", {"generation": newest.name})
    ns.prune_local(root, keep_verified=False)


def report(authority):
    from market_signal.ops import backups as b
    from market_signal.ops import runtime as rt

    c = AUTHORITIES[authority]
    root, db = Path(c["root"]), Path(c["db"])
    rows = b.inventory(root, db)
    # Retention preserves tiny sidecars, so report the latest successful export
    # even while its full source has been replaced by a newer pending backup.
    receipts = []
    for kind in sorted(b.KINDS):
        for path in (root / kind).glob("prism-*.duckdb.manifest.json"):
            manifest = json.loads(b.safe_location(path).read_text())
            receipt = manifest.get("export_receipt", {})
            if (
                receipt.get("off_host_verified")
                and receipt.get("sha256") == manifest.get("sha256")
                and receipt.get("restore_test", {}).get("sha256") == manifest.get("sha256")
            ):
                receipts.append(receipt)
    latest = max(receipts, key=lambda r: r["export_download_timestamp"], default={})
    state = BASE / authority / "last-run.json"
    last = json.loads(state.read_text()) if state.exists() else {}
    policy = b.policy(root)
    disk = rt.disk_state(db)
    admissible = (
        len(rows) < 2 and disk.free - 1.2 * db.stat().st_size >= rt.DISK_CRITICAL * disk.total
    )
    reasons = []
    pending = sum(not acknowledged(r) for r in rows)
    if pending:
        reasons.append(
            f"{pending} local recovery copies awaiting verified off-host acknowledgement"
        )
    if len(rows) >= 2:
        reasons.append(
            "two recovery copies present"
            + ("; explicit rollback pin preserved" if policy["keep"] == 2 else "")
        )
    if not admissible and len(rows) < 2:
        reasons.append("backup disk reserve")
    if last.get("status") == "failed":
        reasons.append(last.get("stage", "unknown") + ": " + last.get("error", "failed"))
    log = root / "retention.jsonl"
    return {
        "authority": authority,
        "latest_local_backup": rows[-1]["path"] if rows else None,
        "local_backup_count": len(rows),
        "latest_offhost_export": latest.get("archive_path"),
        "latest_verification": latest.get("export_download_timestamp"),
        "next_backup_admissible": admissible,
        "retention_policy": policy,
        "last_retention_action": log.read_text().splitlines()[-1] if log.exists() else None,
        "blocked_reason": reasons,
        "last_run": last,
        "inventory": [
            {
                "path": r["path"],
                "bytes": r["bytes"],
                "acknowledged": acknowledged(r),
            }
            for r in rows
        ],
    }


def run(authority):
    from market_signal.ops import backups as b

    state = BASE / authority
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lifecycle.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stage = "reconcile"
        started = now()
        try:
            retained = drain(authority)
            result = {
                "status": "ok",
                "started_at": started,
                "finished_at": now(),
                "stage": "done",
                "retention": retained,
            }
        except Exception as exc:
            b.write_json(
                state / "last-run.json",
                {
                    "status": "failed",
                    "stage": stage,
                    "started_at": started,
                    "finished_at": now(),
                    "error": str(exc)[:250],
                },
            )
            raise
        b.write_json(state / "last-run.json", result)
        return result


def finish_research():
    """Existing root acknowledgement hook, using governed retention instead of unlink."""
    from market_signal.ops import backups as b

    descriptor = json.loads(Path("/var/lib/prism-research/backup-sealed/source.json").read_text())
    status = json.loads(Path("/var/lib/prism-research-backup/state/last-run.json").read_text())
    receipt = status["receipt"]
    if (
        status["status"] != "ok"
        or not receipt.get("off_host_verified")
        or not receipt.get("restore_verified")
        or receipt["sha256"] != descriptor["sha256"]
    ):
        raise ValueError("owner ACK requires matching successful Drive restore")
    ack = {
        "off_host_verified": True,
        "sha256": receipt["sha256"],
        "restore_test": {"sha256": receipt["sha256"]},
        "artifact_sha256": receipt["compressed_sha256"],
        "archive_path": "restic:" + receipt["snapshot_id"],
        "export_download_timestamp": receipt["verified_at"],
    }
    invoke("research", "ack", {"path": descriptor["registered_backup"], "receipt": ack})
    retained = invoke("research", "retain")
    result = {"status": "ok", "stage": "done", "finished_at": now(), "retention": retained}
    b.write_json(BASE / "research" / "last-run.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=(
            "run",
            "finish",
            "status",
            "inventory",
            "inspect",
            "ack",
            "retain",
            "create",
            "prepare",
            "export",
        ),
    )
    parser.add_argument("authority", choices=AUTHORITIES)
    args = parser.parse_args()
    bounded_connections()
    if args.mode == "finish":
        result = finish_research()
    elif args.mode == "run":
        result = run(args.authority)
    elif args.mode == "status":
        result = report(args.authority)
    else:
        data = {} if args.mode == "prepare" else json.load(sys.stdin)
        result = (
            export_worker(args.authority, data)
            if args.mode == "export"
            else owner(args.mode, args.authority, data)
        )
    if result is not None:
        print(json.dumps(result, default=str))


if __name__ == "__main__":
    main()
