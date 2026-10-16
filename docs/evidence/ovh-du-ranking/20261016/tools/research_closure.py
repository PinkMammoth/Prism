"""Run as root: export only non-secret Research closure facts and secret-scan booleans."""

import configparser
import hashlib
import json
import pathlib
import pwd
import shlex
import subprocess
from datetime import UTC, datetime

ETC = pathlib.Path("/etc/prism-research-backup")
BASE = pathlib.Path("/var/lib/prism-research-backup")
out = {"at": datetime.now(UTC).isoformat()}
status = json.loads((BASE / "state/last-run.json").read_text())
out["run"] = {
    k: status.get(k)
    for k in ("status", "stage", "started_at", "finished_at", "receipt", "removed_local")
}
latest = json.loads((BASE / "generations/latest.json").read_text())["generation"]
manifest = json.loads((BASE / "generations" / latest / "manifest.json").read_text())
out["manifest"] = {
    k: manifest.get(k)
    for k in (
        "sha256",
        "bytes",
        "compressed_sha256",
        "compressed_bytes",
        "schema_version",
        "duckdb_version",
        "mode",
        "authority",
        "last_finished_cycle",
    )
}
out["all_tables_verified"] = len(manifest["tables"])
out["critical_table_counts"] = {
    k: v["rows"]
    for k, v in manifest["tables"].items()
    if k
    in (
        "context_events",
        "context_event_updates",
        "macro_observations",
        "perp_funding",
        "perp_oi_history",
        "perp_intraday_bars",
        "lab_strategies",
        "lab_experiments",
        "lab_research_results",
        "lab_prospective_checkpoints",
        "alert_events",
        "alert_rules",
        "journal_entries",
        "positions",
    )
}
out["generation_count"] = len(list((BASE / "generations").glob("generation-*")))
out["scratch_empty"] = not list((BASE / "scratch").iterdir())
env = {"PATH": "/opt/prism/research-backup/tools/bin:/usr/bin:/bin", "HOME": str(BASE)}
for line in (ETC / "backup.env").read_text().splitlines():
    if line.strip() and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        env[k] = shlex.split(v)[0]
user = pwd.getpwnam("prism-research-backup")


def restic(args):
    r = subprocess.run(
        ["restic", "--json", "--no-cache", *args],
        env=env,
        user=user.pw_uid,
        group=user.pw_gid,
        extra_groups=[],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if r.returncode:
        raise RuntimeError(f"restic {args[0]} returned {r.returncode}; details suppressed")
    return r.stdout


out["repository_locks_empty"] = not restic(["list", "locks"]).strip()
out["repository_config"] = {
    k: v for k, v in json.loads(restic(["cat", "config"])).items() if k in ("id", "version")
}
out["keys"] = [
    {k: d.get(k) for k in ("id", "current", "hostName", "userName", "created")}
    for d in json.loads(restic(["key", "list"]))
]
out["credential_permissions"] = {
    name: {"mode": oct((ETC / name).stat().st_mode & 0o777), "uid": (ETC / name).stat().st_uid}
    for name in ("rclone.conf", "restic-password", "backup.env")
}
secrets = [(ETC / "restic-password").read_bytes().strip()]
config = configparser.ConfigParser(interpolation=None)
config.read(ETC / "rclone.conf")
for section in config.sections():
    if config.has_option(section, "client_secret"):
        secrets.append(config.get(section, "client_secret").encode())
    if config.has_option(section, "token"):
        token = json.loads(config.get(section, "token"))
        secrets += [token[k].encode() for k in ("access_token", "refresh_token") if token.get(k)]
for line in pathlib.Path("/etc/prism/secrets.env").read_text().splitlines():
    if line.startswith("FRED_API_KEY="):
        secrets.append(shlex.split(line.split("=", 1)[1])[0].encode())
secrets = [s for s in secrets if len(s) >= 8]
journal = subprocess.run(
    [
        "journalctl",
        "-u",
        "prism-research-backup",
        "-u",
        "prism-scheduler",
        "--since",
        "2026-10-16 08:00:00",
        "--no-pager",
        "-o",
        "cat",
    ],
    capture_output=True,
    timeout=60,
).stdout
out["secret_value_found_in_journal"] = any(s in journal for s in secrets)
out["secret_value_found_in_runtime_log"] = any(
    s in pathlib.Path("/data/logs/runtime.log").read_bytes() for s in secrets
)
out["lab_config_hashes"] = {
    str(p): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in (pathlib.Path("/etc/prism/prism.env"), pathlib.Path("/etc/prism/secrets.env"))
}
out["research_release"] = str(pathlib.Path("/opt/prism/research/current").resolve())
out["backup_unit_status"] = subprocess.run(
    [
        "systemctl",
        "show",
        "prism-research-backup",
        "-p",
        "ActiveState,SubState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp,CPUUsageNSec",
    ],
    capture_output=True,
    text=True,
).stdout
out["research_units"] = subprocess.run(
    ["systemctl", "list-units", "prism-research*", "--all", "--no-pager", "--plain"],
    capture_output=True,
    text=True,
).stdout
assert status["status"] == "ok" and status["receipt"]["read_data"] == "full"
assert status["receipt"]["restore_verified"] and out["all_tables_verified"] == 146
assert out["repository_locks_empty"] and out["generation_count"] == 1 and out["scratch_empty"]
assert not out["secret_value_found_in_journal"] and not out["secret_value_found_in_runtime_log"]
assert (
    out["repository_config"]["id"]
    == "148cfeaa9c717db0e0d6d1c304891e44565a1ebc0d343dd0be3181e1709c6742"
)
assert {k["id"][:8] for k in out["keys"]} == {"b6a269bc", "6f78b5ae"}
print(json.dumps(out, indent=2))
