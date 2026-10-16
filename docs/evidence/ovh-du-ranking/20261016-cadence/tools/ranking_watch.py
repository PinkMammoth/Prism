"""Bounded root observer; rollback only on the user's explicit ranking stop conditions."""

import argparse
import fcntl
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, "/opt/prism/current/src")
from db_probe import collect as db_collect
from dotenv import dotenv_values

from market_signal.ops import profiles
from market_signal.universe.policy import CORE

UNITS = (
    "prism-microstructure",
    "prism-free-sources",
    "prism-free-source-ingest",
    "prism-scheduler",
    "prism-universe",
    "prism-nimble",
)


def save(root, name, data):
    temp = root / (name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    temp.replace(root / name)


def unit(name):
    props = "Id,ActiveState,SubState,MainPID,NRestarts,CPUUsageNSec,MemoryCurrent"
    r = subprocess.run(
        ["systemctl", "show", name, "-p", props], capture_output=True, text=True, check=True
    )
    d = dict(x.split("=", 1) for x in r.stdout.splitlines() if "=" in x)
    cg = Path("/sys/fs/cgroup/system.slice") / (name + ".service")
    for file in ("memory.peak", "memory.events"):
        if (cg / file).exists():
            d[file] = (cg / file).read_text().strip()
    rss = []
    if (cg / "cgroup.procs").exists():
        for pid in (cg / "cgroup.procs").read_text().split():
            try:
                fields = {
                    x.split(":")[0]: x.split(":", 1)[1].strip()
                    for x in Path("/proc", pid, "status").read_text().splitlines()
                    if x.startswith(("VmRSS:", "VmHWM:"))
                }
                rss.append({"pid": pid, **fields})
            except FileNotFoundError:
                pass
    d["rss"] = rss
    return d


def worker_events(start):
    r = subprocess.run(
        [
            "journalctl",
            "-u",
            "prism-universe",
            "--since",
            "@" + str(start),
            "-o",
            "json",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    events = []
    for line in r.stdout.splitlines():
        message = json.loads(line).get("MESSAGE", "")
        try:
            ev = json.loads(message)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict) and ev.get("event") == "universe_refresh":
            events.append(ev)
    return events


def sample(start, cached_db=None, last_db_at=0, last_event_count=-1):
    disk = shutil.disk_usage("/data")
    out = {
        "at": datetime.now(UTC).isoformat(),
        "release_revision": Path("/opt/prism/current/REVISION").read_text().strip(),
        "epoch": time.time(),
        "budget_sha256": hashlib.sha256(
            Path("/data/state/du-budget.json").read_bytes()
        ).hexdigest(),
        "configuration_sha256": {
            name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
            for name in ("/etc/prism/prism.env", "/etc/prism/secrets.env")
        },
        "memory": {
            x.split(":")[0]: int(x.split()[1]) * 1024
            for x in Path("/proc/meminfo").read_text().splitlines()
            if x.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"))
        },
        "disk": disk._asdict(),
        "load": os.getloadavg(),
        "cpu_jiffies": [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]],
        "units": {name: unit(name) for name in UNITS},
        "db_bytes": Path("/data/prism.duckdb").stat().st_size,
        "profile": json.loads(Path("/data/state/profile.json").read_text()),
        "micro": json.loads(Path("/data/microstructure/status.json").read_text()),
        "free_ingest": {
            k: v
            for k, v in json.loads(Path("/data/free_event_sources/worker.json").read_text()).items()
            if k in ("heartbeat_at", "backlog", "ingested")
        },
        "worker_events": worker_events(start),
    }
    usage = resource.getrusage(resource.RUSAGE_SELF)
    out["observer_resources"] = {
        "cpu_seconds": usage.ru_utime + usage.ru_stime,
        "max_rss_kib": usage.ru_maxrss,
    }
    child_active = len(out["units"]["prism-universe"].get("rss", [])) > 1
    audit_due = (
        cached_db is None
        or len(out["worker_events"]) != last_event_count
        or time.time() - last_db_at >= 120
    )
    if audit_due and (cached_db is None or not child_active):
        try:
            out["db"] = json.loads(json.dumps(db_collect(), default=str))
            out["db_audit_at"] = time.time()
            out["db_audit_event_count"] = len(out["worker_events"])
        except Exception as exc:
            out["db_error_type"] = type(exc).__name__
    if "db" not in out and cached_db is not None:
        out["db"] = cached_db
        out["db_audit_at"] = last_db_at
        out["db_audit_event_count"] = last_event_count
        out["db_cache_reused"] = True
    backup = json.loads(Path("/var/lib/prism-research-backup/state/last-run.json").read_text())
    out["research_backup"] = {
        k: backup.get(k) for k in ("status", "stage", "started_at", "finished_at")
    }
    return out


def rollback(root, reason):
    receipt = {"at": datetime.now(UTC).isoformat(), "reason": reason, "history_preserved": True}
    # Stop the radar first; never stop the DB-free core collector.
    subprocess.run(["systemctl", "stop", "prism-universe"], check=True)
    plan_path = Path("/data/microstructure/universe_subscriptions.json")
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
    else:
        # Before the first evaluation no plan has been published. Prove the
        # unchanged static collector is core-only before restoring v1.
        micro = json.loads(Path("/data/microstructure/status.json").read_text())
        if micro.get("dynamic_universe") or set(micro["coins"]) != set(CORE):
            raise RuntimeError("unpublished plan with non-static/non-core collector")
        plan = {"coins": list(micro["coins"]), "pins": {}}
    if set(plan["coins"]) != set(CORE) or plan["pins"]:
        raise RuntimeError(
            "unexpected satellite/pin: preserve collection; do not restore environment"
        )
    saved = Path("/etc/prism/prism.env.pre-du-v1")
    resolved = profiles.resolve(dict(dotenv_values(saved)))
    if (
        resolved.profile.identity != "prism_autotrader_lab_v1"
        or resolved.authority != "rehearsal"
        or resolved.execution_allowed
        or resolved.external_actions_allowed
    ):
        raise RuntimeError("rollback environment identity/capability mismatch")
    # Do not interrupt a core job. A stopped radar is safe while waiting for idle.
    with Path("/data/prism.duckdb.runtime.lock").open("a") as handle:
        deadline = time.monotonic() + 300
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError("core writer not idle; radar remains stopped") from None
                time.sleep(2)
        subprocess.run(
            [
                "install",
                "-o",
                "root",
                "-g",
                "prism",
                "-m",
                "0640",
                str(saved),
                "/etc/prism/prism.env.rollback",
            ],
            check=True,
        )
        os.replace("/etc/prism/prism.env.rollback", "/etc/prism/prism.env")
        subprocess.run(
            [
                "systemctl",
                "restart",
                "prism-scheduler",
                "prism-free-source-ingest",
                "prism-free-sources",
            ],
            check=True,
        )
    receipt["v1_restored"] = True
    receipt["completed_at"] = datetime.now(UTC).isoformat()
    save(root, "rollback.json", receipt)


def concerns(s, baseline, start):
    errors = []
    if s.get("release_revision") != "c6b6e9da7e0d2e04f265e5fec9d1dde9a0a7d67c":
        errors.append("release revision mismatch")
    if s.get("budget_sha256") != baseline.get("budget_sha256"):
        errors.append("ranking budget changed during window")
    if s.get("configuration_sha256") != baseline.get("configuration_sha256"):
        errors.append("configuration/secrets changed during window")
    p = s["profile"]
    if (
        p["identity"] != "prism_autotrader_lab_v2"
        or p["authority"] != "rehearsal"
        or p["execution_allowed"]
        or p["external_actions_allowed"]
        or p["collector_dynamic"]
    ):
        errors.append("authority/profile/warming mismatch")
    for name, u in s["units"].items():
        if name == "prism-nimble":
            if u["ActiveState"] == "active":
                errors.append("unexpected Nimble activation")
            continue
        b = baseline["units"][name]
        if (
            u["ActiveState"] != "active"
            or u["MainPID"] != b["MainPID"]
            or u["NRestarts"] != b["NRestarts"]
        ):
            errors.append("service continuity " + name)
        if "oom_kill 0" not in u.get("memory.events", "oom_kill 0"):
            errors.append("OOM " + name)
    if s["disk"]["free"] < 0.7 * s["disk"]["total"] + 7_000_000_000 + 30 * 250_000_000:
        errors.append("disk/backup/runway safety floor")
    if s["memory"]["SwapTotal"] - s["memory"]["SwapFree"] > 0:
        errors.append("swap pressure")
    m = s["micro"]
    if set(m.get("coins", {})) != set(CORE) or m.get("dynamic_universe"):
        errors.append("unexpected rich satellite collector expansion")
    if m.get("spool_errors", 0) or m.get("dropped_records", 0):
        errors.append("core spool error/drop")
    if s["epoch"] - m["written_ms"] / 1000 > 60:
        errors.append("collector heartbeat stale")
    if s["epoch"] - s.get("db_audit_at", start) > 300:
        errors.append("read-only DB audit unavailable for5min")
    db = s.get("db", {})
    if db.get("authority", [None])[0] != "vps-prism-autotrader-lab-rehearsal" and "db" in s:
        errors.append("DB authority changed")
    if (
        db.get("authority")
        and baseline.get("db", {}).get("authority")
        and db["authority"] != baseline["db"]["authority"]
    ):
        errors.append("new authority claim timestamp")
    if any(db.get("paper_rows", {}).values()) or any(db.get("sizing_tables", {}).values()):
        errors.append("execution/sizing state created")
    for ev in db.get("evaluations", []):
        if not all(ev["checks"].values()):
            errors.append("ranking correctness " + ev["id"])
    for cycle in db.get("cycles", []):
        if datetime.fromisoformat(cycle[2]).timestamp() >= start and cycle[4] not in (
            "ok",
            "running",
        ):
            errors.append("scheduler failure " + cycle[0])
    events = s["worker_events"]
    if any(e.get("status") in ("FAILED", "TIMEOUT", "INVALID_CHILD_OUTPUT") for e in events):
        errors.append("supervised refresh failed or timed out")
    if any(e.get("status") == "NOT_DUE" for e in events):
        errors.append("unexpected supervised NOT_DUE")
    if len(events) >= 2 and all(
        e.get("status") != "ACCEPTED" or e.get("source_error_count", 0) > 0 for e in events[-2:]
    ):
        errors.append("repeated refresh/provider failures")
    for e in events:
        timing = e.get("telemetry") or {}
        gap = timing.get("continuity_gap_seconds")
        previous_clock = timing.get("previous_evidence_clock")
        # An intentional stopped interval predating this window is a genuine gap.
        # Preserve its frozen reset; never count it as healthy window continuity.
        if gap and gap > 900 and previous_clock is not None and previous_clock >= start:
            errors.append("in-window accepted continuity gap")
    if s["research_backup"]["status"] != "ok":
        errors.append("Research backup regression")
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rollback-only", action="store_true")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.rollback_only:
        rollback(args.root, ["observer systemd failure/OOM safety fallback"])
        return
    start = time.time()
    baseline = sample(start)
    count = 0
    warnings = set()
    low_ram_since = None
    disconnected_since = None
    sustained = {}
    previous = None
    save(args.root, "baseline.json", baseline)
    if args.check:
        assert "db" in baseline, "read-only observer must open before arming"
        for cycle in baseline["db"]["cycles"]:
            datetime.fromisoformat(cycle[2])
        print(
            json.dumps(
                {
                    "checks_only": True,
                    "db_accessible": "db" in baseline,
                    "profile": baseline["profile"]["identity"],
                    "no_activation": True,
                }
            )
        )
        return
    save(
        args.root,
        "window.json",
        {
            "start": datetime.fromtimestamp(start, UTC).isoformat(),
            "minimum_accepted_span_seconds": 7200,
            "minimum_evaluations": 13,
            "maximum_seconds": 14400,
        },
    )
    while True:
        s = (
            baseline
            if count == 0
            else sample(
                start,
                cached_db=previous.get("db"),
                last_db_at=previous.get("db_audit_at", 0),
                last_event_count=previous.get("db_audit_event_count", -1),
            )
        )
        count += 1
        errors = concerns(s, baseline, start)
        if previous:
            delta = [b - a for a, b in zip(previous["cpu_jiffies"], s["cpu_jiffies"], strict=True)]
            total = sum(delta)
            s["cpu_busy_percent"] = 100 * (total - delta[3] - delta[4]) / total if total else 0
            s["cpu_steal_percent"] = 100 * delta[7] / total if total else 0
            elapsed = s["epoch"] - previous["epoch"]
            radar = s["units"]["prism-universe"]
            prior = previous["units"]["prism-universe"]
            cpu = (int(radar["CPUUsageNSec"]) - int(prior["CPUUsageNSec"])) / 1e9 / elapsed * 60
            s["radar_cpu_seconds_per_minute"] = cpu
            if cpu >= 30:
                errors.append("radar CPU budget exceeded")
            for name, condition, seconds in [
                ("CPU busy", s["cpu_busy_percent"] > 80, 600),
                ("CPU steal", s["cpu_steal_percent"] > 5, 300),
                ("load", s["load"][1] > 6, 300),
            ]:
                if condition:
                    sustained[name] = sustained.get(name, s["epoch"])
                    if s["epoch"] - sustained[name] >= seconds:
                        errors.append("sustained " + name)
                else:
                    sustained.pop(name, None)
        if s["memory"]["MemAvailable"] < 0.4 * s["memory"]["MemTotal"]:
            low_ram_since = low_ram_since or s["epoch"]
            if s["epoch"] - low_ram_since >= 300:
                errors.append("RAM below40% for5min")
        else:
            low_ram_since = None
        if not s["micro"]["connected"] or (s["micro"].get("last_message_age_s") or 0) > 60:
            disconnected_since = disconnected_since or s["epoch"]
            if s["epoch"] - disconnected_since >= 60:
                errors.append("core feed unhealthy >60s")
        else:
            disconnected_since = None
        if "db_error_type" in s:
            warnings.add("read-only observer retry " + s["db_error_type"])
        if any(e.get("source_error_count", 0) for e in s["worker_events"]):
            warnings.add("isolated public source error; preserve missingness")
        s["stop_conditions"] = errors
        with (args.root / "samples.jsonl").open("a") as f:
            f.write(json.dumps(s, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        save(args.root, "latest.json", s)
        evaluations = [e for e in s.get("db", {}).get("evaluations", []) if e["at"] >= start]
        span = evaluations[-1]["at"] - evaluations[0]["at"] if evaluations else 0
        done = len(evaluations) >= 13 and span >= 7200
        timed_out = s["epoch"] - start > 14400
        if errors or done or timed_out:
            result = {
                "start": datetime.fromtimestamp(start, UTC).isoformat(),
                "end": datetime.now(UTC).isoformat(),
                "duration_seconds": time.time() - start,
                "evaluations": len(evaluations),
                "accepted_span_seconds": span,
                "warnings": sorted(warnings),
                "stop_conditions": errors,
                "clean": done and not errors and not timed_out,
                "rollback_performed": False,
            }
            save(args.root, "final.json", result)
            if errors or timed_out:
                try:
                    rollback(args.root, errors or ["window timed out"])
                    result["rollback_performed"] = True
                except Exception as exc:
                    result["rollback_error_type"] = type(exc).__name__
                save(args.root, "final.json", result)
            print(json.dumps(result), flush=True)
            return
        previous = s
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if "--check" not in sys.argv:
            root = Path(sys.argv[sys.argv.index("--root") + 1])
            save(
                root,
                "observer-error.json",
                {"at": datetime.now(UTC).isoformat(), "error_type": type(exc).__name__},
            )
            rollback(root, ["observer failed: " + type(exc).__name__])
            # A completed rollback must not trigger a second OnFailure rollback.
            sys.exit(0)
        raise
