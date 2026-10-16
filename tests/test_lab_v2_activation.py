"""A pending remediation gate cannot mutate the Lab's activation configuration."""

import json
import sys

import pytest
from deploy.vps import activate_du_data as activate


@pytest.mark.parametrize(
    "gate",
    [
        {
            "clean": False,
            "duration_seconds": 26000,
            "scheduled_macro_successes": 2,
            "scheduled_backup_successes": 1,
        },
        {
            "clean": True,
            "duration_seconds": 21599,
            "scheduled_macro_successes": 2,
            "scheduled_backup_successes": 1,
        },
        {
            "clean": True,
            "duration_seconds": 26000,
            "scheduled_macro_successes": 1,
            "scheduled_backup_successes": 1,
        },
        {
            "clean": True,
            "duration_seconds": 26000,
            "scheduled_macro_successes": 2,
            "scheduled_backup_successes": 0,
        },
    ],
)
def test_pending_or_incomplete_gate_refuses_before_host_config_access(tmp_path, monkeypatch, gate):
    receipt = tmp_path / "gate.json"
    receipt.write_text(json.dumps(gate))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "activate_du_data.py",
            "ranking",
            "--budget",
            str(tmp_path / "budget.json"),
            "--gate",
            str(receipt),
        ],
    )
    monkeypatch.setattr(activate.os, "geteuid", lambda: 0)

    def no_mutation(*args, **kwargs):
        pytest.fail("incomplete gate must never perform a mutation")

    monkeypatch.setattr(activate.subprocess, "run", no_mutation)
    monkeypatch.setattr(activate.tempfile, "mkstemp", no_mutation)
    with pytest.raises(ValueError, match="gate has not passed"):
        activate.main()
