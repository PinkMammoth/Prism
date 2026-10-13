"""Data-only supervised radar process. HTTP never holds the runtime writer lock."""

import logging
import os
import subprocess
import sys
import time

from market_signal.universe.policy import POLICY

log = logging.getLogger("prism.universe")


def main():
    while True:
        started = time.monotonic()
        cmd = [sys.executable, "-m", "market_signal.cli.main", "universe", "refresh", "--json"]
        if os.environ.get("PRISM_UNIVERSE_STAGE") == "warming":
            cmd.append("--warming")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        if result.returncode:
            log.error("radar refresh failed: %s", result.stderr[-1500:])
        time.sleep(max(1, POLICY.cadence_seconds - (time.monotonic() - started)))


if __name__ == "__main__":
    from market_signal.ops.profiles import main_guard

    main_guard("universe_radar")
    main()
