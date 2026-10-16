"""Exercise the unmodified prepared worker and real CLI due gate with a virtual clock.

No network or database write is permitted. A sentinel at HTTP construction identifies
when the real CLI admits a fetch; only fetch duration is simulated.
"""

import contextlib
import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from market_signal.cli import universe_cmds
from market_signal.data import http
from market_signal.ops import profiles
from market_signal.universe import source, worker
from market_signal.universe.policy import POLICY


class FetchAdmitted(Exception):
    pass


class Finished(Exception):
    pass


def main():
    clock, previous, records = [0.0], [None], []
    epoch = 1792137600

    class Clock:
        @staticmethod
        def now(tz):
            return datetime.fromtimestamp(epoch + clock[0], UTC)

    def prepare(*args):
        return {"previous": previous[0]}

    def invoke(*args, **kwargs):
        start = clock[0]
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                universe_cmds.refresh(warming=False, as_json=True)
        except FetchAdmitted:
            clock[0] += 36.0
            old = previous[0]
            previous[0] = {"at": epoch + int(clock[0])}
            records.append(
                {
                    "worker_started_seconds": start,
                    "status": "FETCH_ADMITTED",
                    "simulated_commit_seconds": clock[0],
                    "previous_commit_gap_seconds": previous[0]["at"] - old["at"] if old else None,
                }
            )
        else:
            status = json.loads(captured.getvalue())["status"]
            records.append(
                {
                    "worker_started_seconds": start,
                    "status": status,
                    "age_since_previous_commit_seconds": epoch + int(start) - previous[0]["at"],
                }
            )
            clock[0] += 0.1
        return SimpleNamespace(returncode=0)

    def sleep(seconds):
        clock[0] += seconds
        if len(records) >= 5:
            raise Finished

    with (
        patch.object(universe_cmds, "datetime", Clock),
        patch.object(
            universe_cmds,
            "open_store",
            lambda **kw: contextlib.nullcontext(
                (None, SimpleNamespace(path=Path("/tmp/never-opened-cadence-audit")))
            ),
        ),
        patch.object(profiles, "current", lambda: SimpleNamespace(explicit=False)),
        patch.object(source, "prepare", prepare),
        patch.object(http, "HttpClient", side_effect=FetchAdmitted),
        patch.object(worker.subprocess, "run", invoke),
        patch.object(worker.time, "monotonic", lambda: clock[0]),
        patch.object(worker.time, "sleep", sleep),
        patch.dict(
            "os.environ", {"PRISM_RUNTIME_ROLE": "reader", "PRISM_UNIVERSE_STAGE": "ranking"}
        ),
        contextlib.suppress(Finished),
    ):
        worker.main()
    result = {
        "policy_cadence_seconds": POLICY.cadence_seconds,
        "policy_max_light_gap_seconds": POLICY.light_max_gap_seconds,
        "simulated_fetch_wall_seconds": 36,
        "records": records,
        "source_sha256": {
            str(Path(module.__file__)): hashlib.sha256(
                Path(module.__file__).read_bytes()
            ).hexdigest()
            for module in (worker, universe_cmds)
        },
        "defect_reproduced": records[1]["status"] == "NOT_DUE"
        and records[2]["previous_commit_gap_seconds"] > POLICY.light_max_gap_seconds,
        "database_opened": False,
        "network_called": False,
    }
    assert result["defect_reproduced"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
