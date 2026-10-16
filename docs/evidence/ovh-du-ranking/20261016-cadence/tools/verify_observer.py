"""Exercise safety and rollback guards against copied metadata and a temporary filesystem."""

import copy
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("ranking_watch_checks", HERE / "ranking_watch.py")
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

baseline = json.loads((HERE.parent / "observer-baseline.json").read_text())
baseline["release_revision"] = (
    "c6b6e9da7e0d2e04f265e5fec9d1dde9a0a7d67c"  # fixture expectation only, not host evidence
)
assert not watch.concerns(baseline, baseline, baseline["epoch"])
cases = []
changes = {
    "budget identity": lambda s: s.update(budget_sha256="changed"),
    "configuration identity": lambda s: s.update(configuration_sha256={"changed": "changed"}),
    "release": lambda s: s.update(release_revision="unreviewed"),
    "authority": lambda s: s["profile"].update(authority="full"),
    "authority claim timestamp": lambda s: s["db"]["authority"].__setitem__(1, "new claim"),
    "execution": lambda s: s["profile"].update(execution_allowed=True),
    "warming": lambda s: s["profile"].update(collector_dynamic=True),
    "service restart": lambda s: s["units"]["prism-universe"].update(NRestarts="1"),
    "disk safety": lambda s: s["disk"].update(free=1),
    "actual collector expansion": lambda s: s["micro"]["coins"].update(SAT={}),
    "collector drop": lambda s: s["micro"].update(dropped_records=1),
    "paper account": lambda s: s["db"]["paper_rows"].update(paper_accounts=1),
    "unexpected NOT_DUE": lambda s: s.update(worker_events=[{"status": "NOT_DUE"}]),
    "worker failure": lambda s: s.update(worker_events=[{"status": "FAILED"}]),
    "provider repetition": lambda s: s.update(
        worker_events=[{"status": "ACCEPTED", "source_error_count": 1}] * 2
    ),
    "in-window continuity gap": lambda s: s.update(
        worker_events=[
            {
                "status": "ACCEPTED",
                "telemetry": {
                    "continuity_gap_seconds": 901,
                    "previous_evidence_clock": baseline["epoch"] + 600,
                },
            }
        ]
    ),
    "Research backup": lambda s: s["research_backup"].update(status="failed"),
}
for name, change in changes.items():
    sample = copy.deepcopy(baseline)
    change(sample)
    assert watch.concerns(sample, baseline, baseline["epoch"]), name
    cases.append({"condition": name, "detected": True})

rollback_cases = []
for scenario in ("published core plan", "unpublished initial plan", "unexpected pin"):
    with tempfile.TemporaryDirectory(prefix="du-rollback-config-") as tmp:
        base = Path(tmp)

        def mapped(path, base=base):
            return (
                base / str(path).lstrip("/")
                if str(path).startswith(("/etc/prism/", "/data/"))
                else Path(path)
            )

        env = mapped("/etc/prism/prism.env")
        saved = mapped("/etc/prism/prism.env.pre-du-v1")
        env.parent.mkdir(parents=True)
        v1 = (
            "\n".join(
                f"{k}={v}"
                for k, v in {
                    "PRISM_PROFILE": "autotrader_lab",
                    "PRISM_AUTHORITY": "rehearsal",
                    "PRISM_RUNTIME_ROLE": "authoritative",
                    "PRISM_RUNTIME_ID": "vps-prism-autotrader-lab-rehearsal",
                    "PRISM_DEPLOYMENT_TARGET": "vps",
                    "PRISM_DB_PATH": "/data/prism.duckdb",
                }.items()
            )
            + "\n"
        )
        saved.write_text(v1)
        env.write_text(v1.replace("autotrader_lab\n", "autotrader_lab_v2\n"))
        initial = env.read_bytes()
        micro = mapped("/data/microstructure/status.json")
        micro.parent.mkdir(parents=True)
        micro.write_text(
            json.dumps({"coins": {coin: {} for coin in watch.CORE}, "dynamic_universe": False})
        )
        plan = mapped("/data/microstructure/universe_subscriptions.json")
        if scenario != "unpublished initial plan":
            plan.write_text(
                json.dumps(
                    {
                        "coins": watch.CORE,
                        "pins": {"BTC": ["risk"]} if scenario == "unexpected pin" else {},
                    }
                )
            )
        db = mapped("/data/prism.duckdb")
        db.write_bytes(b"PRESERVE DB AND CANDIDATE HISTORY")
        evidence = base / "evidence"
        evidence.mkdir()
        commands = []

        def run(args, commands=commands, mapped=mapped, **kwargs):
            commands.append(args)
            if args[0] == "install":
                shutil.copyfile(args[-2], mapped(args[-1]))

        original_replace = watch.os.replace

        def replace(src, dest, mapped=mapped, original_replace=original_replace):
            original_replace(mapped(src), mapped(dest))

        error = None
        with (
            patch.object(watch, "Path", side_effect=mapped),
            patch.object(watch.subprocess, "run", side_effect=run),
            patch.object(watch.os, "replace", side_effect=replace),
        ):
            try:
                watch.rollback(evidence, ["fixture only"])
            except RuntimeError as exc:
                error = str(exc)
        assert db.read_bytes() == b"PRESERVE DB AND CANDIDATE HISTORY"
        assert commands[0] == ["systemctl", "stop", "prism-universe"]
        assert not any("prism-microstructure" in c for c in commands)
        if scenario == "unexpected pin":
            assert error and env.read_bytes() == initial and len(commands) == 1
        else:
            assert error is None and env.read_text() == v1, error
            assert commands[-1] == [
                "systemctl",
                "restart",
                "prism-scheduler",
                "prism-free-source-ingest",
                "prism-free-sources",
            ]
        rollback_cases.append(
            {
                "scenario": scenario,
                "passed": True,
                "db_history_preserved": True,
                "core_not_restarted": True,
                "guard_rejected_restore": bool(error),
            }
        )

print(
    json.dumps(
        {
            "fixture_only": True,
            "no_host_mutations": True,
            "healthy_fixture_passed": True,
            "stop_cases": cases,
            "rollback_config_cases": rollback_cases,
        },
        indent=2,
    )
)
