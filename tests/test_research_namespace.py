"""Migration candidates never grant authority; recovery failures preserve evidence."""

import json
import shutil
import subprocess
from pathlib import Path

import duckdb
import pytest

from market_signal.data.store import NotAuthoritative, Store
from market_signal.ops import profiles
from market_signal.ops import research_namespace as ns


@pytest.fixture
def candidate(tmp_path):
    db = tmp_path / "import" / "research.duckdb"
    db.parent.mkdir()
    with duckdb.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE schema_version(version INT); INSERT INTO schema_version VALUES(30)"
        )
        con.execute(
            "CREATE TABLE runtime_events(event_id VARCHAR, event_type VARCHAR, runtime_id VARCHAR,recorded_at TIMESTAMPTZ)"
        )
        con.execute(
            "INSERT INTO runtime_events VALUES('claim','authority_claimed','railway-prism-runtime','2026-10-04T17:46:19Z')"
        )
        con.execute("CREATE TABLE runtime_cycles(finished_at TIMESTAMPTZ)")
        con.execute("INSERT INTO runtime_cycles VALUES('2026-10-15T11:31:40Z')")
        con.execute(
            "CREATE TABLE scientific_evidence(id INT, value DOUBLE); INSERT INTO scientific_evidence VALUES(1,42)"
        )
    return db


def test_read_only_import_and_wrong_owner_fail_closed(candidate, monkeypatch):
    before = ns.sha256(candidate)
    with ns.connect(candidate) as con, pytest.raises(duckdb.InvalidInputException):
        con.execute("INSERT INTO scientific_evidence VALUES(2,0)")
    monkeypatch.setenv("PRISM_RUNTIME_ROLE", "reader")
    with pytest.raises(NotAuthoritative):
        Store(candidate)
    for identity in ("vps-prism-research", "vps-prism-autotrader-lab-rehearsal"):
        monkeypatch.setenv("PRISM_RUNTIME_ROLE", "authoritative")
        monkeypatch.setenv("PRISM_RUNTIME_ID", identity)
        with pytest.raises(NotAuthoritative):
            Store(candidate)
    assert ns.sha256(candidate) == before


def test_disabled_environment_cannot_schedule_collect_or_act():
    text = Path("deploy/consolidated/research.env.example").read_text()
    env = dict(
        line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#")
    )
    result = profiles.resolve(env)
    assert not result.processes
    assert not result.scheduled_jobs()
    assert not result.external_actions_allowed
    assert not result.execution_allowed
    assert result.role == "reader"


def test_backup_roundtrip_and_one_generation(candidate, tmp_path):
    source = {"sha256": ns.sha256(candidate), "source_runtime": "railway-prism-runtime"}
    root = tmp_path / "backups"
    first = ns.backup_sealed(candidate, root, source)
    second = ns.backup_sealed(candidate, root, source)
    generations = list(root.glob("generation-*"))
    assert len(generations) == 1
    assert first["sha256"] == second["sha256"]
    assert not second["off_host_verified"]
    proof = ns.restore(generations[0] / "research.duckdb.gz", second, tmp_path / "restore.duckdb")
    assert proof["verified"]
    assert ns.inspect(candidate)["tables"] == proof["inspection"]["tables"]


def test_corruption_existing_restore_and_failed_rotation_preserve_previous(candidate, tmp_path):
    root = tmp_path / "backups"
    source = {"sha256": ns.sha256(candidate)}
    manifest = ns.backup_sealed(candidate, root, source)
    gen = next(root.glob("generation-*"))
    existing = tmp_path / "existing.duckdb"
    existing.write_bytes(b"preserve")
    with pytest.raises(ValueError):
        ns.restore(gen / "research.duckdb.gz", manifest, existing)
    assert existing.read_bytes() == b"preserve"
    pointer = (root / "latest.json").read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        ns.backup_sealed(candidate, root, {"sha256": "bad"})
    assert (root / "latest.json").read_bytes() == pointer
    assert gen.exists()
    corrupt = tmp_path / "corrupt.gz"
    corrupt.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        ns.restore(corrupt, manifest, tmp_path / "new.duckdb")


def test_restore_size_limit_and_partial_preservation(candidate, tmp_path):
    root = tmp_path / "backups"
    manifest = ns.backup_sealed(candidate, root, {"sha256": ns.sha256(candidate)})
    archive = next(root.glob("generation-*")) / "research.duckdb.gz"
    destination = tmp_path / "new.duckdb"
    with pytest.raises(ValueError, match="exceeds"):
        ns.restore(archive, {**manifest, "bytes": 1}, destination)
    assert not destination.exists()
    partial = destination.with_suffix(".duckdb.part")
    partial.write_bytes(b"existing")
    with pytest.raises(ValueError, match="existing partial"):
        ns.restore(archive, manifest, destination)
    assert partial.read_bytes() == b"existing"


def test_no_offhost_without_explicit_independent_destination(tmp_path, monkeypatch):
    monkeypatch.setattr(ns.subprocess, "run", lambda *a, **k: pytest.fail("no external operation"))
    config = {**DRIVE_CONFIG, "approved_independent_destination": False}
    with pytest.raises(ValueError, match="approval"):
        ns.offhost(config, tmp_path, tmp_path / "restore")
    monkeypatch.setenv("RESTIC_REPOSITORY", "/local/not-independent")
    with pytest.raises(ValueError, match="remote repository"):
        ns.offhost(
            {**config, "approved_independent_destination": True, "dedicated_repository": True},
            tmp_path,
            tmp_path / "restore",
        )


class FakeRepository:
    """Minimal restic/rclone stand-in: snapshots are directory copies."""

    def __init__(self, tmp_path):
        self.root, self.calls, self.snapshots = tmp_path / "fake-remote", [], {}
        self.root.mkdir()
        self.manifests, self.forget_all, self.listing_extra = {}, False, {}

    def __call__(self, command, **kwargs):
        assert kwargs["capture_output"]
        self.calls.append(command)
        if command[0] == "rclone":
            return self.rclone(command[1:])
        assert command[1:3] == ["--json", "--no-cache"]
        op, args = command[3], command[4:]
        out = ""
        if op == "backup":
            sid = f"snap{len(self.snapshots)}"
            shutil.copytree(kwargs["cwd"], self.root / sid)
            self.snapshots[sid] = str(kwargs["cwd"])
            out = json.dumps({"message_type": "summary", "snapshot_id": sid}) + "\n"
        elif op == "ls":
            nodes = [{"struct_type": "snapshot"}] + [
                {"struct_type": "node", "type": "file", "name": f.name, "size": f.stat().st_size}
                for f in (self.root / args[0]).iterdir()
            ]
            nodes += [{"struct_type": "node", "type": "file", **x} for x in self.listing_extra]
            out = "\n".join(json.dumps(n) for n in nodes)
        elif op == "restore":
            shutil.copytree(self.root / args[0], Path(args[args.index("--target") + 1]))
        elif op == "forget" and self.forget_all:
            self.snapshots.clear()
        elif op == "snapshots":
            out = json.dumps([{"id": k, "paths": [v]} for k, v in self.snapshots.items()])
        return subprocess.CompletedProcess(command, 0, out, "")

    def rclone(self, args):
        out = ""
        if args[0] == "copyto":
            self.manifests[args[2].rsplit("/", 1)[1]] = Path(args[1]).read_text()
        elif args[0] == "cat":
            out = self.manifests[args[1].rsplit("/", 1)[1]]
        elif args[0] == "lsf":
            out = "\n".join(self.manifests)
        elif args[0] == "deletefile":
            del self.manifests[args[1].rsplit("/", 1)[1]]
        return subprocess.CompletedProcess(args, 0, out, "")


DRIVE_CONFIG = {
    "approved_independent_destination": True,
    "dedicated_repository": True,
    "destination_id": "test-only",
    "manifest_remote": "fake:Prism/backups/research/manifests",
}


@pytest.fixture
def drive_env(monkeypatch, tmp_path):
    monkeypatch.setenv("RESTIC_REPOSITORY", "rclone:fake:Prism/backups/research/restic")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/protected/password")
    monkeypatch.setenv("RCLONE_CONFIG", "/protected/rclone.conf")
    fake = FakeRepository(tmp_path)
    monkeypatch.setattr(ns.subprocess, "run", fake)
    return fake


def test_offhost_upload_recovery_retention_and_redacted_failure(candidate, tmp_path, drive_env):
    root = tmp_path / "backups"
    ns.backup_sealed(candidate, root, {"sha256": ns.sha256(candidate)})
    generation = next(root.glob("generation-*"))
    scratch = tmp_path / "offhost-restore"
    receipt = ns.offhost(DRIVE_CONFIG, generation, scratch)
    assert receipt["off_host_verified"] and receipt["restore_verified"]
    assert not scratch.exists(), "proof restore is transient unless kept"
    ops = [c[3] if c[0] == "restic" else c[1] for c in drive_env.calls]
    assert ops == [
        "backup",
        "ls",
        "check",
        "restore",
        "copyto",
        "cat",
        "forget",
        "snapshots",
        "lsf",
    ]
    restic = [c for c in drive_env.calls if c[0] == "restic"]
    assert "--read-data" in restic[2] and "--prune" in restic[4]
    assert "--group-by" in restic[4] and "--keep-monthly" in restic[4]
    assert receipt["manifest_copy"].endswith(generation.name + ".json")
    assert receipt["remote_listing"]["research.duckdb.gz"] > 0

    def failed(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "invalid_grant DO_NOT_PRINT_SECRET")

    ns.subprocess.run = failed
    with pytest.raises(RuntimeError, match="authentication failure") as exc:
        ns.offhost(DRIVE_CONFIG, generation, tmp_path / "different-restore")
    assert "DO_NOT_PRINT_SECRET" not in str(exc.value)


def test_rclone_repository_requires_protected_config(tmp_path, monkeypatch):
    monkeypatch.setattr(ns.subprocess, "run", lambda *a, **k: pytest.fail("no external operation"))
    monkeypatch.setenv("RESTIC_REPOSITORY", "rclone:gdrive:Prism/backups/research/restic")
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", "/protected/password")
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    with pytest.raises(ValueError, match="rclone config"):
        ns.offhost(DRIVE_CONFIG, tmp_path, tmp_path / "restore")


def test_remote_size_mismatch_and_retention_loss_fail_visibly(candidate, tmp_path, drive_env):
    root = tmp_path / "backups"
    ns.backup_sealed(candidate, root, {"sha256": ns.sha256(candidate)})
    generation = next(root.glob("generation-*"))
    drive_env.listing_extra = [{"name": "unexpected.bin", "size": 1}]
    with pytest.raises(ValueError, match="size mismatch"):
        ns.offhost(DRIVE_CONFIG, generation, tmp_path / "r1")
    drive_env.listing_extra = []
    drive_env.forget_all = True
    with pytest.raises(RuntimeError, match="removed the newly verified"):
        ns.offhost(DRIVE_CONFIG, generation, tmp_path / "r2")
    assert not (generation / "offhost-receipt.json").exists()


def test_offhost_restore_needs_no_local_generation(candidate, tmp_path, drive_env):
    root = tmp_path / "backups"
    manifest = ns.backup_sealed(candidate, root, {"sha256": ns.sha256(candidate)})
    generation = next(root.glob("generation-*"))
    receipt = ns.offhost(DRIVE_CONFIG, generation, tmp_path / "proof")
    shutil.rmtree(root)
    recovered = ns.offhost_restore(receipt["snapshot_id"], tmp_path / "fresh", keep=True)
    assert recovered["verified"] and recovered["manifest"] == manifest
    assert ns.inspect(Path(recovered["restored_db"]))["tables"] == manifest["tables"]
    with pytest.raises(ValueError, match="fresh"):
        ns.offhost_restore(receipt["snapshot_id"], tmp_path / "fresh")


def test_scheduled_keeps_last_verified_until_new_generation_verified(
    candidate, tmp_path, drive_env
):
    config = {
        **DRIVE_CONFIG,
        "db": str(candidate),
        "local_root": str(tmp_path / "backups"),
        "scratch_root": str(tmp_path / "scratch"),
        "status_path": str(tmp_path / "status.json"),
    }
    source = {"sha256": ns.sha256(candidate)}
    first = ns.scheduled(config, source)
    assert first["status"] == "ok"
    root = Path(config["local_root"])
    verified = [p.name for p in root.glob("generation-*")]
    assert len(verified) == 1 and (root / verified[0] / "offhost-receipt.json").exists()

    real = ns.subprocess.run

    def upload_fails(command, **kwargs):
        if command[0] == "restic" and command[3] == "backup":
            return subprocess.CompletedProcess(
                command, 1, "", "googleapi: Error 403: rateLimitExceeded"
            )
        return real(command, **kwargs)

    ns.subprocess.run = upload_fails
    with pytest.raises(RuntimeError, match="quota failure"):
        ns.scheduled(config, source)
    status = json.loads(Path(config["status_path"]).read_text())
    assert status["status"] == "failed" and status["stage"] == "offhost"
    names = sorted(p.name for p in root.glob("generation-*"))
    assert verified[0] in names and len(names) == 2, "last verified copy must survive"
    # A retry after failure replaces the unverified generation, still keeping the verified one.
    with pytest.raises(RuntimeError):
        ns.scheduled(config, source)
    assert verified[0] in [p.name for p in root.glob("generation-*")]
    assert len(list(root.glob("generation-*"))) == 2
    ns.subprocess.run = real
    assert ns.scheduled(config, source)["status"] == "ok"
    remaining = [p.name for p in root.glob("generation-*")]
    assert len(remaining) == 1 and remaining[0] != verified[0]
    assert len(drive_env.manifests) == len(drive_env.snapshots)


@pytest.mark.skipif(
    not (shutil.which("restic") and shutil.which("rclone")), reason="restic/rclone not installed"
)
def test_real_restic_rclone_roundtrip(candidate, tmp_path, monkeypatch):
    """Actual tools, rclone 'local' remote standing in for Drive; no network."""
    conf = tmp_path / "rclone.conf"
    conf.write_text("[stand-in]\ntype = local\n")
    password = tmp_path / "password"
    password.write_text("test-only-password\n")
    remote = tmp_path / "remote"
    monkeypatch.setenv("RCLONE_CONFIG", str(conf))
    monkeypatch.setenv("RESTIC_PASSWORD_FILE", str(password))
    monkeypatch.setenv("RESTIC_REPOSITORY", f"rclone:stand-in:{remote}/restic")
    subprocess.run(["restic", "init", "--no-cache"], check=True, capture_output=True)
    root = tmp_path / "backups"
    manifest = ns.backup_sealed(candidate, root, {"sha256": ns.sha256(candidate)})
    generation = next(root.glob("generation-*"))
    config = {**DRIVE_CONFIG, "manifest_remote": f"stand-in:{remote}/manifests"}
    receipt = ns.offhost(config, generation, tmp_path / "proof")
    assert receipt["restore_verified"] and receipt["retained_snapshots"] == 1
    assert (remote / "manifests" / f"{generation.name}.json").exists()
    assert b"scientific_evidence" not in b"".join(
        f.read_bytes() for f in (remote / "restic" / "data").rglob("*") if f.is_file()
    ), "repository content must be client-side encrypted"
    shutil.rmtree(root)
    recovered = ns.offhost_restore(receipt["snapshot_id"], tmp_path / "fresh", keep=True)
    assert recovered["manifest"] == manifest and recovered["verified"]
    password.write_text("wrong\n")
    with pytest.raises(RuntimeError, match="repository key failure"):
        ns.offhost_snapshots()


def test_paths_and_runtime_state_do_not_transfer(candidate, tmp_path):
    from scripts.ovh_research_validate import ownership

    with pytest.raises(ValueError, match="traversal"):
        ns.safe(tmp_path / ".." / "elsewhere")
    result = ownership(
        {"tables": {"runtime_events": {"disposition": "RETAIN_WITH_EXPLICIT_HANDOFF"}}},
        ns.inspect(candidate),
    )
    state = result["tables"]["runtime_events"]
    assert not state["may_resume_authority"]
    assert state["import_role"] == "read-only imported history"
