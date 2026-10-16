"""Attest immutable candidate then invoke its unchanged prepared ranking activation tool."""

import hashlib
import json
import pwd
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/opt/prism/current/src")
from dotenv import dotenv_values

from market_signal.data.store import Store, authority_claim
from market_signal.ops import profiles
from market_signal.ops.runtime import runtime_lock

root = Path("/data/validation/du-ranking-cadence/attempt-2")
release = json.loads((root / "release-candidate.json").read_text())
budget = Path("/data/state/du-budget.json")
assert Path("/opt/prism/current/REVISION").read_text().strip() == release["revision"]
for name, sha in release["source_sha256"].items():
    assert hashlib.sha256((Path("/opt/prism/current") / name).read_bytes()).hexdigest() == sha
assert (
    hashlib.sha256(Path("/etc/prism/prism.env").read_bytes()).hexdigest()
    == "93116e83a9169e6ee8cb0cbcb5d7532766465c3569412ffce39540e0f5071439"
)
assert (
    hashlib.sha256(Path("/etc/prism/secrets.env").read_bytes()).hexdigest()
    == "f2ae9d85ebe3b837d53ec0a9a9987524e4cc2a51ef0e24d69d766c2f90c8c126"
)
assert hashlib.sha256(budget.read_bytes()).hexdigest() == sys.argv[1]
b = json.loads(budget.read_text())
assert b["rich_assets"] == 6 and b["release_revision"] == release["revision"]
env = dict(dotenv_values("/etc/prism/prism.env"))
assert env["PRISM_PROFILE"] == "autotrader_lab" and env["PRISM_AUTHORITY"] == "rehearsal"
resolved = profiles.resolve(env)
assert not resolved.execution_allowed and not resolved.external_actions_allowed
assert Path("/data/prism.duckdb.runtime.lock").stat().st_uid == pwd.getpwnam("prism").pw_uid
with runtime_lock(Path("/data/prism.duckdb"), "du-ranking-activation", wait=60):
    store = Store("/data/prism.duckdb", read_only=True, lock_timeout=20)
    try:
        claim = authority_claim(store.con)
        assert claim[0] == "vps-prism-autotrader-lab-rehearsal"
        assert str(claim[1]) == "2026-10-14 15:43:52.816356+00:00"
        assert store.con.execute("SELECT count(*) FROM universe_evaluations").fetchone()[0] == 1
        for (table,) in store.con.execute("SHOW TABLES").fetchall():
            if "cohort" in table or (
                table.startswith("paper_")
                and table not in ("paper_nimble_policies", "paper_nimble_triggers")
            ):
                assert store.con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] == 0
    finally:
        store.close()
    subprocess.run(
        [
            "/opt/prism/current/.venv/bin/python",
            "/opt/prism/current/deploy/vps/activate_du_data.py",
            "ranking",
            "--budget",
            str(budget),
        ],
        check=True,
    )
print(
    json.dumps(
        {
            "at": datetime.now(UTC).isoformat(),
            "revision": release["revision"],
            "budget_sha256": sys.argv[1],
            "prepared_tool_used": True,
            "stage": "ranking",
            "no_execution_or_warming": True,
        }
    )
)
