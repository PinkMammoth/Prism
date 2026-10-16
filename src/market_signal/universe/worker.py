"""Data-only supervised radar process. HTTP never holds the runtime writer lock."""

import json
import logging
import os
import resource
import subprocess
import sys
import time

from market_signal.universe import cadence
from market_signal.universe.policy import POLICY

log = logging.getLogger("prism.universe")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    deadline = cadence.first_deadline(time.time())
    while True:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(30, remaining))
        started = time.time()
        missed_before_start = max(0, (cadence.slot(started) - deadline) // POLICY.cadence_seconds)
        if missed_before_start:
            deadline = cadence.slot(started)
        cmd = [sys.executable, "-m", "market_signal.cli.main", "universe", "refresh", "--json"]
        if os.environ.get("PRISM_UNIVERSE_STAGE") == "warming":
            cmd.append("--warming")
        event = {
            "event": "universe_refresh",
            "scheduler_version": cadence.VERSION,
            "scheduled_deadline": deadline,
            "evaluation_started_at": started,
            "missed_before_start": missed_before_start,
        }
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
            event["returncode"] = result.returncode
            if result.returncode == 0:
                value = json.loads(result.stdout)
                event.update(
                    status=value.get("status", "ACCEPTED"),
                    evaluation_id=value.get("evaluation_id"),
                    candidates=len(value.get("candidates", [])),
                    source_error_count=len(value.get("source_errors", {})),
                    telemetry=value.get("telemetry"),
                )
            else:
                event["status"] = "FAILED"
        except subprocess.TimeoutExpired:
            # subprocess.run kills and waits for its child before the next deadline.
            event["status"] = "TIMEOUT"
        except (ValueError, TypeError):
            event["status"] = "INVALID_CHILD_OUTPUT"
        event["child_completed_at"] = time.time()
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        event["child_cpu_seconds"] = (after.ru_utime + after.ru_stime) - (
            before.ru_utime + before.ru_stime
        )
        event["children_peak_rss_kib_since_worker_start"] = after.ru_maxrss
        deadline, skipped = cadence.next_deadline(deadline, time.time())
        event.update(next_scheduled_deadline=deadline, skipped_deadlines=skipped)
        log.info(json.dumps(event, sort_keys=True))


if __name__ == "__main__":
    from market_signal.ops.profiles import main_guard

    main_guard("universe_radar")
    main()
