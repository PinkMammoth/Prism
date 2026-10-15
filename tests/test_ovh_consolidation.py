"""Prepared OVH research boundary: distinct authority, paths and private ingress."""

from pathlib import Path

import pytest

from market_signal.config import Paths
from market_signal.context.gateway import launch
from market_signal.ops import profiles as pf


def research_env(mode):
    return {
        "PRISM_PROFILE": "research_vps",
        "PRISM_DEPLOYMENT_TARGET": "vps",
        "PRISM_AUTHORITY": mode,
        "PRISM_RUNTIME_ROLE": "scratch" if mode == "none" else "authoritative",
        "PRISM_RUNTIME_ID": {
            "none": "",
            "full": "vps-prism-research",
            "rehearsal": "vps-prism-research-rehearsal",
        }[mode],
        "PRISM_NIMBLE_RUNTIME": "off",
    }


@pytest.mark.parametrize("mode", ["none", "rehearsal", "full"])
def test_vps_research_authority_and_execution_fence(mode):
    resolved = pf.resolve(research_env(mode))
    assert not resolved.execution_allowed
    assert not resolved.job_allowed("paper_nimble_brief")
    assert not resolved.process_allowed("nimble_worker")
    assert not resolved.process_allowed("universe_radar")
    assert resolved.job_allowed("microdir_daily") == (mode != "none")
    assert resolved.process_allowed("context_gateway") == (mode == "full")


@pytest.mark.parametrize(
    "rid",
    ["railway-prism-runtime", "vps-prism-autotrader-lab", "vps-prism-autotrader-lab-rehearsal"],
)
def test_research_cannot_inherit_other_runtime_authority(rid):
    with pytest.raises(pf.ProfileError):
        pf.resolve({**research_env("full"), "PRISM_RUNTIME_ID": rid})


def test_research_cannot_enable_execution_by_flags():
    with pytest.raises(pf.ProfileError):
        pf.resolve({**research_env("full"), "PRISM_NIMBLE_RUNTIME": "on"})
    with pytest.raises(pf.ProfileError):
        pf.resolve({**research_env("full"), "PRISM_UNIVERSE_STAGE": "warming"})


def test_results_can_live_in_research_namespace(monkeypatch):
    monkeypatch.delenv("PRISM_RESULTS_DIR", raising=False)
    assert Paths(Path("/release")).results == Path("/release/results")
    monkeypatch.setenv("PRISM_RESULTS_DIR", "/data/research/results")
    assert Paths(Path("/release")).results == Path("/data/research/results")


def test_private_gateway_bind_keeps_legacy_default(monkeypatch):
    import uvicorn

    captured = []
    monkeypatch.setattr(launch.Verifier, "environment", lambda: object())
    monkeypatch.setattr(launch, "Spool", lambda _: object())
    monkeypatch.setattr(launch, "create_app", lambda *a, **kw: object())
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.append(kw))
    monkeypatch.delenv("PRISM_CONTEXT_GATEWAY_HOST", raising=False)
    launch.run()
    assert captured[-1]["host"] == "0.0.0.0"
    monkeypatch.setenv("PRISM_CONTEXT_GATEWAY_HOST", "127.0.0.1")
    launch.run()
    assert captured[-1]["host"] == "127.0.0.1"


@pytest.mark.parametrize("origin", ["gateway", "free"])
def test_research_ingest_keeps_enrollment_without_execution_wakeup(monkeypatch, origin):
    from market_signal.context import work_research
    from market_signal.context.work_monitors import PROVIDER_IDS
    from market_signal.ops import context_consumers, evaluation_triggers

    for key, value in research_env("full").items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    calls = []
    monkeypatch.setattr(evaluation_triggers, "gateway_wakeup", lambda *a: calls.append("execution"))
    monkeypatch.setattr(work_research, "enroll", lambda *a, **kw: calls.append("research"))
    completion = {
        "provider": next(iter(PROVIDER_IDS)),
        "receipt_id": "r",
        "gateway_received_at": "now",
    }
    if origin == "gateway":
        consumers = context_consumers.gateway_consumers(None, completion, {"payload": {}})
    else:
        consumers = context_consumers.free_source_consumers(None, completion, {})
    consumers[0](None)
    assert calls == ["research"]


@pytest.mark.parametrize("source", ["context", "microstructure", "positioning", "technical"])
def test_research_does_not_create_execution_outbox_rows(monkeypatch, source):
    from market_signal.ops import evaluation_triggers

    for key, value in research_env("full").items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_ID", raising=False)
    # No store is needed: no execution table or filesystem notice may be touched.
    assert evaluation_triggers.enqueue(None, source, "k", ["BTC"], "2026-10-15T00:00:00Z") is None
    assert evaluation_triggers.gateway_wakeup(None, {}, {}) is None
