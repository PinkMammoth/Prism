"""Runtime profile split (docs/RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md): profile identities,
fail-closed startup, forbidden services, authority fencing, run identity, seed isolation,
legacy entrypoint equivalence, systemd units and scheduler uniqueness."""

from __future__ import annotations

import configparser
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_signal.data.store import Store, authority_claim
from market_signal.ops import lab
from market_signal.ops import profiles as pf
from market_signal.ops import runtime as rt

REPO = Path(__file__).resolve().parents[1]
VPS = REPO / "deploy" / "vps"
LAB_FULL, LAB_REHEARSAL = "vps-prism-autotrader-lab", "vps-prism-autotrader-lab-rehearsal"
RAILWAY = "railway-prism-runtime"

# Frozen identities: changing a profile's definition requires a new identity/version.
DIGESTS = {
    "research_vps": "46e81ec9a60ab07acaecd55675e477c65aa976ad978890e0c197cd8744f8be70",
    "research": "8d6431ba5d6088b90df3e94a2edf3b9415f03a01c8d6f79416921eb5d141e6d9",
    "autotrader_lab": "f898e2f01ccc5f744ae52e67fbc8edce7428cdf0256647fffa77d926ea102d19",
}
SEED_POLICY_DIGEST = "e4d467b9eefa666f794a3fc97117093354040650e18f67213ff541eb1f490236"
# Phase 30 report: the released definitions the lab descends from must be unchanged.
PHASE30 = {
    "nimble": "paperexecution_nimble_ec40a2968bcc33f7c7654a1c8d7dcc200361e88bca99079cb4f45738ba785501",
    "event": "paperexecution_event_12208f088bec2bcfaa8dfe10af41ccac81975d012cd2e5b0919ec5918712c885",
    "immediate": "paperuniverse_event_1caa53ff64e6f2afe727bbf1c9df485acccac8ea9054eb292292175f2ba7324f",
    "confirmed": "paperuniverse_event_4a76847cbd314501804511b4278e4a878c8ac0e79454683cd7514339f0cb180a",
}  # fmt: skip
ENV_KEYS = ("PRISM_PROFILE", "PRISM_AUTHORITY", "PRISM_DEPLOYMENT_TARGET", "PRISM_RUNTIME_ROLE",
            "PRISM_RUNTIME_ID", "PRISM_MICROSTRUCTURE", "PRISM_CONTEXT_GATEWAY",
            "PRISM_FREE_SOURCES", "PRISM_NIMBLE_RUNTIME", "PRISM_UNIVERSE_STAGE",
            "RAILWAY_ENVIRONMENT_ID",
            "PRISM_HEARTBEAT_URL")  # fmt: skip


def env(profile=None, authority=None, target="local", role=None, rid=None, **flags):
    e = {}
    if profile:
        e["PRISM_PROFILE"] = profile
        e["PRISM_DEPLOYMENT_TARGET"] = target
    if authority:
        e["PRISM_AUTHORITY"] = authority
    if role:
        e["PRISM_RUNTIME_ROLE"] = role
    if rid:
        e["PRISM_RUNTIME_ID"] = rid
    e.update(flags)
    return e


def lab_env(authority):
    if authority == "none":
        return env("autotrader_lab", "none", role="scratch")
    rid = LAB_FULL if authority == "full" else LAB_REHEARSAL
    return env("autotrader_lab", authority, "vps", "authoritative", rid)


def research_env(authority):
    if authority == "none":
        return env("research", "none", role="scratch")
    rid = RAILWAY if authority == "full" else "prism-research-rehearsal"
    return env("research", authority, "railway", "authoritative", rid)


@pytest.fixture
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRISM_LOCK_TIMEOUT", "5")
    return monkeypatch


def apply(monkeypatch, e):
    for k, v in e.items():
        monkeypatch.setenv(k, v)


# ----------------------------------------------------------------------------- identities


def test_profile_identities_are_frozen_and_versioned():
    assert {k: p.digest() for k, p in pf.PROFILES.items()} == DIGESTS
    assert pf.PROFILES["research"].identity == "prism_research_runtime_v1"
    assert pf.PROFILES["autotrader_lab"].identity == "prism_autotrader_lab_v1"
    for p in pf.PROFILES.values():
        assert set(p.processes) <= set(pf.PROCESSES) and set(p.jobs) <= set(rt.JOBS)
    # every scheduled job has a class, so a new job forces a profile decision
    assert set(pf.JOB_CLASS) == set(rt.JOBS) == set(rt.SCHEDULE)


def test_research_profile_never_contains_autonomous_execution():
    r = pf.PROFILES["research"]
    assert "nimble_worker" not in r.processes and "paper_nimble_brief" not in r.jobs
    assert not any(pf.PROCESSES[p].klass == pf.EXECUTION for p in r.processes)
    assert set(r.dynamic_universe.values()) == {"absent"}
    # research keeps its authority, gateway and core-six microstructure collection
    assert {"prospective", "daily", "microdir_daily", "microdir_weekly", "microstructure"} <= set(
        r.jobs
    )
    assert {"context_gateway", "context_ingest", "microstructure_collector"} <= set(r.processes)


def test_lab_profile_has_its_inputs_and_no_research_authority():
    lab_p = pf.PROFILES["autotrader_lab"]
    # every input the Nimble/event adapters read is collected by the lab itself
    assert {"intraday", "paper_v2_funding", "positioning", "oi", "context_macro",
            "context_news", "microstructure"} <= set(lab_p.jobs)  # fmt: skip
    assert {"microstructure_collector", "free_source_collector", "free_source_ingest",
            "nimble_worker", "scheduler"} == set(lab_p.processes)  # fmt: skip
    assert not {"prospective", "daily", "microdir_daily", "microdir_weekly", "paper_v2"} & set(
        lab_p.jobs
    )
    assert "context_gateway" not in lab_p.processes  # Work ingress stays on Railway
    assert set(lab_p.dynamic_universe.values()) == {"gated"}
    # disjoint runtime identities: the lab can never claim the Railway database
    assert not set(lab_p.runtime_ids.values()) & set(pf.PROFILES["research"].runtime_ids.values())


# ----------------------------------------------------------------------------- fail closed


@pytest.mark.parametrize(
    "e, match",
    [
        (env("prod"), "not a known profile"),
        ({"PRISM_PROFILE": "research"}, "PRISM_DEPLOYMENT_TARGET"),
        (env("autotrader_lab", target="railway"), "does not deploy to railway"),
        (env("research", target="vps"), "does not deploy to vps"),
        ({**env("research", target="local"), "RAILWAY_ENVIRONMENT_ID": "x"}, "running on Railway"),
        (env("research", "bogus"), "PRISM_AUTHORITY"),
        (env("autotrader_lab", "none", role="authoritative", rid=LAB_FULL), "contradicts"),
        (env("autotrader_lab", "full", "vps", "authoritative", RAILWAY), "requires"),
        (env("autotrader_lab", "full", "vps", "authoritative", LAB_REHEARSAL), "requires"),
        (env("autotrader_lab", "rehearsal", "vps", "authoritative", LAB_FULL), "requires"),
        (env("autotrader_lab", "full", "vps", "scratch", LAB_FULL), "requires"),
        (env("research", "full", "railway", "authoritative", LAB_FULL), "requires"),
        # one mistaken flag can never start autonomous execution
        ({**research_env("full"), "PRISM_NIMBLE_RUNTIME": "on"}, "excludes"),
        ({**lab_env("full"), "PRISM_CONTEXT_GATEWAY": "on"}, "excludes"),
        ({**lab_env("full"), "PRISM_FREE_SOURCES": "yes"}, "expected one of"),
        # Dynamic Universe stays gated in every explicit profile
        ({**lab_env("full"), "PRISM_UNIVERSE_STAGE": "ranking"}, "excludes"),
        ({**lab_env("full"), "PRISM_UNIVERSE_STAGE": "warming"}, "excludes"),
        ({**research_env("full"), "PRISM_UNIVERSE_STAGE": "ranking"}, "excludes"),
        ({**lab_env("none"), "PRISM_UNIVERSE_STAGE": "bogus"}, "expected one of"),
    ],
)
def test_invalid_configuration_refuses(e, match):
    with pytest.raises(pf.ProfileError, match=match):
        pf.resolve(e)


def test_unset_authority_is_zero_authority():
    r = pf.resolve(env("autotrader_lab"))
    assert r.authority == "none" and not r.execution_allowed and not r.external_actions_allowed
    assert r.processes == ("microstructure_collector", "free_source_collector")
    assert r.scheduled_jobs() == ()


def test_kill_switch_flags_only_disable():
    r = pf.resolve({**lab_env("full"), "PRISM_MICROSTRUCTURE": "off", "PRISM_FREE_SOURCES": "on"})
    assert "microstructure_collector" not in r.processes
    assert r.disabled_by_flag == ("microstructure_collector",)
    # a flag naming a process pair is accepted when the profile holds one of the pair
    r = pf.resolve({**research_env("full"), "PRISM_FREE_SOURCES": "on"})
    assert "free_source_collector" in r.processes and "free_source_ingest" not in r.processes


# ----------------------------------------------------------------------------- matrix


ALL = list(itertools.product(("research", "autotrader_lab"), pf.AUTHORITY_MODES))


@pytest.mark.parametrize("name, authority", ALL)
def test_process_and_job_matrix(name, authority):
    r = pf.resolve((research_env if name == "research" else lab_env)(authority))
    klasses = {pf.PROCESSES[p].klass for p in r.processes}
    assert klasses <= pf.MODE_CLASSES[authority]
    jobs = set(r.scheduled_jobs())
    assert jobs <= set(r.profile.jobs)
    if authority == "none":
        assert jobs == set() and "scheduler" not in r.processes
        assert klasses <= {pf.PASSIVE}
    if authority != "full":
        assert not {j for j in jobs if pf.JOB_CLASS[j] == "execution"}
        assert "nimble_worker" not in r.processes and "context_gateway" not in r.processes
        assert not r.execution_allowed and not r.external_actions_allowed
    if name == "research":
        assert "nimble_worker" not in r.processes and "paper_nimble_brief" not in jobs
        assert not r.execution_allowed
    if name == "autotrader_lab" and authority == "full":
        assert r.execution_allowed and "nimble_worker" in r.processes
        assert "paper_nimble_brief" in jobs


def _legacy_shell(e):
    """Direct transcription of the pre-split deploy/railway/entrypoint.sh conditions."""
    role = e.get("PRISM_RUNTIME_ROLE") or ""
    v = lambda k, d: e.get(k) or d  # noqa: E731
    out = {"scheduler"}
    if role == "authoritative" and v("PRISM_MICROSTRUCTURE", "on") != "off":
        out.add("microstructure_collector")
    gw = v("PRISM_CONTEXT_GATEWAY", "off") == "on"
    if role == "authoritative" and gw:
        out |= {"context_gateway", "context_ingest"}
    if role == "authoritative" and v("PRISM_FREE_SOURCES", "off") == "on":
        out.add("free_source_collector")
        if not gw:
            out.add("free_source_ingest")
    if role == "authoritative" and v("PRISM_NIMBLE_RUNTIME", "on") != "off":
        out.add("nimble_worker")
    if v("PRISM_UNIVERSE_STAGE", "off") in ("ranking", "warming"):  # no role test
        out.add("universe_radar")
    return out


def test_legacy_profile_reproduces_pre_split_flags_exactly():
    values = (None, "on", "off", "")
    flags = ("PRISM_MICROSTRUCTURE", "PRISM_CONTEXT_GATEWAY", "PRISM_FREE_SOURCES",
             "PRISM_NIMBLE_RUNTIME", "PRISM_UNIVERSE_STAGE")  # fmt: skip
    stages = (None, "off", "ranking", "warming")
    n = 0
    for role in (None, "authoritative", "scratch"):
        for combo in itertools.product(values, values, values, values, stages):
            e = {k: x for k, x in zip(flags, combo, strict=True) if x is not None}
            if role:
                e.update(PRISM_RUNTIME_ROLE=role, PRISM_RUNTIME_ID=RAILWAY)
            r = pf.resolve(e)
            assert not r.explicit and set(r.processes) == _legacy_shell(e), e
            assert set(r.scheduled_jobs()) == set(rt.JOBS) and r.external_actions_allowed
            assert r.collector_dynamic == (e.get("PRISM_UNIVERSE_STAGE") == "warming")
            n += 1
    assert n == 3 * 4**5


# ----------------------------------------------------------------------------- jobs/fencing


@pytest.fixture
def lab_db(project, clean_env):
    db = project / "data" / "prism.duckdb"
    Store(db).close()
    rt.claim_authority(db, LAB_REHEARSAL, "rehearsal")
    clean_env.setattr(rt, "infra_alert", lambda text: None)
    apply(clean_env, lab_env("rehearsal"))
    return db


def _steps(calls):
    def run(job, name, args):
        calls.append((job, name))
        return {"step": name, "exit": 0, "seconds": 0.0}

    return run


def test_job_outside_profile_is_refused_before_anything_runs(lab_db):
    for job in ("prospective", "microdir_daily", "paper_nimble_brief", "paper_v2"):
        calls = []
        with pytest.raises(rt.RuntimeRefused, match="not part of profile"):
            rt.run_job(job, trigger="manual", wait=0, step_runner=_steps(calls))
        assert calls == []
    s = Store(lab_db, read_only=True)
    assert s.con.execute("SELECT count(*) FROM runtime_cycles").fetchone()[0] == 0
    s.close()


def test_rehearsal_runs_data_jobs_and_records_profile(lab_db):
    calls = []
    out = rt.run_job("paper_v2_funding", trigger="manual", wait=0, step_runner=_steps(calls))
    # the lab collects funding even with no paper run (unconditional data job)
    assert out["exit"] == 0 and calls == [("paper_v2_funding", "funding")]
    s = Store(lab_db, read_only=True)
    payload = json.loads(s.con.execute("SELECT payload FROM runtime_cycles").fetchone()[0])
    s.close()
    assert payload["profile"]["identity"] == "prism_autotrader_lab_v1"
    assert payload["profile"]["authority"] == "rehearsal"
    assert payload["profile"]["runtime_id"] == LAB_REHEARSAL
    assert payload["profile"]["digest"] == DIGESTS["autotrader_lab"]


def test_wrong_profile_cannot_write_a_database_claimed_by_another_runtime(lab_db, clean_env):
    # research identity on the lab's rehearsal database: refused by the existing claim guard
    apply(clean_env, research_env("full"))
    clean_env.setenv("PRISM_DEPLOYMENT_TARGET", "local")
    with pytest.raises(Exception, match="live database of runtime"):
        rt.run_job("oi", trigger="manual", wait=0, step_runner=_steps([]))


def test_invalid_profile_refuses_jobs(project, clean_env):
    apply(clean_env, {"PRISM_PROFILE": "nope"})
    with pytest.raises(rt.RuntimeRefused, match="not a known profile"):
        rt.run_job("oi", trigger="manual", wait=0, step_runner=_steps([]))


def test_rehearsal_never_sends_notifications_or_heartbeats(clean_env):
    import httpx

    from market_signal.portfolio.telegram import TelegramClient, TelegramError

    hits = []
    transport = httpx.MockTransport(lambda req: hits.append(req) or httpx.Response(200, json={}))
    apply(clean_env, lab_env("rehearsal"))
    with pytest.raises(TelegramError, match="no external-action authority"):
        TelegramClient("123:abc", "42", transport=transport).send("hello")
    clean_env.setenv("PRISM_HEARTBEAT_URL", "https://hc.example/ping")
    clean_env.setattr(httpx, "get", lambda *a, **k: hits.append(a))
    rt.heartbeat(True)
    rt.infra_alert("would duplicate")
    assert hits == []
    # the profile's full-authority runtime (and legacy) still sends
    apply(clean_env, lab_env("full"))
    rt.heartbeat(True)
    assert len(hits) == 1


def test_execution_commands_need_full_execution_authority(clean_env):
    for e in (research_env("full"), lab_env("rehearsal"), lab_env("none")):
        with pytest.raises(pf.ProfileError, match="no paper execution authority"):
            pf.require_execution("paper nimble create", e)
    assert pf.require_execution("x", lab_env("full")).execution_allowed
    assert pf.require_execution("x", {}).execution_allowed  # legacy unchanged


def test_cli_refuses_paper_run_creation_outside_lab_authority(project, clean_env):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    apply(clean_env, research_env("full"))
    clean_env.setenv("PRISM_DEPLOYMENT_TARGET", "local")
    for args in (["paper", "nimble", "create"], ["paper", "nimble", "register"]):
        res = CliRunner().invoke(app, args)
        assert res.exit_code == pf.EXIT_PROFILE and "no paper execution authority" in res.output
    assert not (project / "data" / "prism.duckdb").exists()


def test_lab_cannot_claim_the_railway_identity(project, clean_env):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    db = project / "data" / "prism.duckdb"
    Store(db).close()
    apply(clean_env, lab_env("none"))
    res = CliRunner().invoke(app, ["ops", "claim-authority", "--runtime-id", RAILWAY,
                                   "--reason", "x"])  # fmt: skip
    assert res.exit_code == 2 and "may only claim" in res.output
    s = Store(db, read_only=True)
    assert authority_claim(s.con) is None
    s.close()


@pytest.mark.parametrize(
    "module, e",
    [
        ("market_signal.paper.nimble.worker", research_env("full")),
        ("market_signal.paper.nimble.worker", lab_env("rehearsal")),
        ("market_signal.context.gateway.launch", lab_env("full")),
        ("market_signal.context.gateway.worker", lab_env("full")),
        ("market_signal.context.free_sources.worker", research_env("full")),
        ("market_signal.context.free_sources.collector", {"PRISM_PROFILE": "bogus"}),
        ("market_signal.universe.worker", lab_env("full")),
        ("market_signal.universe.worker", research_env("full")),
    ],
)
def test_long_running_programs_refuse_outside_their_profile(module, e, tmp_path):
    base = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    e = {**e, "PRISM_DEPLOYMENT_TARGET": "local"} if "PRISM_DEPLOYMENT_TARGET" in e else e
    p = subprocess.run([sys.executable, "-m", module], env={**base, **e, "PRISM_HOME": str(REPO),
                       "PRISM_DB_PATH": str(tmp_path / "x.duckdb")}, capture_output=True,
                       text=True, timeout=120)  # fmt: skip
    assert p.returncode == pf.EXIT_PROFILE, p.stderr
    assert "profile refused" in p.stderr
    assert not (tmp_path / "x.duckdb").exists()


def test_free_source_ownership_follows_the_profile():
    assert pf.free_sources_enabled({"PRISM_FREE_SOURCES": "on"})
    assert not pf.free_sources_enabled({})
    assert pf.free_sources_enabled(research_env("full"))
    assert pf.free_sources_enabled(lab_env("none"))
    assert not pf.free_sources_enabled({**lab_env("full"), "PRISM_FREE_SOURCES": "off"})
    assert not pf.free_sources_enabled({"PRISM_PROFILE": "bogus"})


# ----------------------------------------------------------------------------- crontab


def _crontab(clean_env, e):
    from typer.testing import CliRunner

    from market_signal.cli.main import app

    for k in ENV_KEYS:
        clean_env.delenv(k, raising=False)
    apply(clean_env, e)
    res = CliRunner().invoke(app, ["ops", "crontab"])
    return res.exit_code, res.output


def _jobs(tab):
    return {
        line.split(" ops cycle ")[1].split()[0]
        for line in tab.splitlines()
        if " ops cycle " in line
    }


def test_crontab_holds_exactly_the_profiles_jobs(project, clean_env):
    code, tab = _crontab(clean_env, lab_env("full"))
    assert code == 0 and _jobs(tab) == set(pf.PROFILES["autotrader_lab"].jobs)
    assert "context gateway health" not in tab and "context sources status --check" in tab
    code, tab = _crontab(clean_env, research_env("full"))
    assert _jobs(tab) == set(pf.PROFILES["research"].jobs)
    assert "context gateway health" in tab and "paper_nimble_brief" not in tab
    code, tab = _crontab(clean_env, lab_env("rehearsal"))
    assert "paper_nimble_brief" not in _jobs(tab) and "intraday" in _jobs(tab)
    code, tab = _crontab(clean_env, lab_env("none"))
    assert code == 0 and _jobs(tab) == set()
    code, tab = _crontab(clean_env, {"PRISM_PROFILE": "bogus"})
    assert code == pf.EXIT_PROFILE and " ops cycle " not in tab


def test_legacy_crontab_matches_the_pre_split_entrypoint(project, clean_env):
    prod = {"PRISM_RUNTIME_ROLE": "authoritative", "PRISM_RUNTIME_ID": RAILWAY,
            "PRISM_CONTEXT_GATEWAY": "on", "PRISM_FREE_SOURCES": "on"}  # fmt: skip
    code, tab = _crontab(clean_env, prod)
    old = (rt.crontab() + "* * * * * market context gateway health --check --json\n"
           "* * * * * market context sources status --check\n")  # fmt: skip
    assert code == 0 and tab == old


# ----------------------------------------------------------------------------- entrypoint


# The last pre-split entrypoint: approved main with Dynamic Universe (cc311d0).
OLD_ENTRYPOINT_COMMIT = "cc311d0"


def _run_entrypoint(script: str, tmp: Path, e: dict) -> set[str]:
    bin_ = tmp / "bin"
    bin_.mkdir(exist_ok=True)
    log = tmp / "calls.log"
    log.write_text("")
    real = sys.executable
    (bin_ / "market").write_text(f"""#!/bin/bash
case "$1 $2" in
  "ops profile"|"ops crontab") exec {real} -m market_signal.cli.main "$@" ;;
  "microstructure collect") echo "proc market $*" >> {log}; kill $PPID; exit 0 ;;
  "ops cycle"|"ops boot-catchup") echo "boot $1 $2 $3" >> {log} ;;
esac
exit 0
""")
    (bin_ / "python").write_text(f"""#!/bin/bash
echo "proc python $*" >> {log}; kill $PPID; exit 0
""")
    (bin_ / "supercronic").write_text(f"#!/bin/bash\necho 'proc supercronic' >> {log}\n")
    (bin_ / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    app = tmp / "app"
    app.mkdir(exist_ok=True)
    (app / "REVISION").write_text("test\n")
    text = (script.replace("cd /app", f"cd {app}").replace("/tmp/prism.crontab", f"{tmp}/c")
            .replace("/data/", f"{tmp}/data/"))  # fmt: skip
    (tmp / "entry.sh").write_text(text)
    base = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    subprocess.run(["sh", str(tmp / "entry.sh")], env={**base, **e, "PATH": f"{bin_}:{os.environ['PATH']}",
                   "PRISM_HOME": str(REPO), "PRISM_DB_PATH": str(tmp / "none.duckdb")},
                   capture_output=True, text=True, timeout=180, check=True)  # fmt: skip
    seen, stable = None, 0
    while stable < 3:  # background supervisors finish within moments
        time.sleep(0.3)
        cur = log.read_text()
        stable = stable + 1 if cur == seen else 0
        seen = cur
    lines = {line.replace(" --trigger boot", "") for line in seen.splitlines()}
    return {"boot" if line.startswith("boot") else line for line in lines}


@pytest.mark.parametrize(
    "e",
    [
        {"PRISM_RUNTIME_ROLE": "authoritative", "PRISM_RUNTIME_ID": RAILWAY,
         "PRISM_CONTEXT_GATEWAY": "on", "PRISM_FREE_SOURCES": "on"},  # current production
        {"PRISM_RUNTIME_ROLE": "authoritative", "PRISM_RUNTIME_ID": RAILWAY},
        {"PRISM_RUNTIME_ROLE": "authoritative", "PRISM_RUNTIME_ID": RAILWAY,
         "PRISM_FREE_SOURCES": "on", "PRISM_NIMBLE_RUNTIME": "off", "PRISM_MICROSTRUCTURE": "off"},
        {"PRISM_CONTEXT_GATEWAY": "on", "PRISM_FREE_SOURCES": "on"},  # not authoritative
        {"PRISM_RUNTIME_ROLE": "authoritative", "PRISM_RUNTIME_ID": RAILWAY,
         "PRISM_UNIVERSE_STAGE": "ranking"},
        {"PRISM_RUNTIME_ROLE": "authoritative", "PRISM_RUNTIME_ID": RAILWAY,
         "PRISM_CONTEXT_GATEWAY": "on", "PRISM_FREE_SOURCES": "on", "PRISM_UNIVERSE_STAGE": "warming"},
        {"PRISM_UNIVERSE_STAGE": "warming"},  # radar without authority, as before
    ],
)  # fmt: skip
def test_legacy_entrypoint_starts_exactly_what_the_pre_split_one_did(e, tmp_path):
    try:
        old = subprocess.run(["git", "show", f"{OLD_ENTRYPOINT_COMMIT}:deploy/railway/entrypoint.sh"],
                             cwd=REPO, capture_output=True, text=True, check=True).stdout  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("pre-split entrypoint needs git history")
    new = (REPO / "deploy" / "railway" / "entrypoint.sh").read_text()
    (tmp_path / "o").mkdir()
    (tmp_path / "n").mkdir()
    assert _run_entrypoint(new, tmp_path / "n", e) == _run_entrypoint(old, tmp_path / "o", e)


def test_entrypoint_fails_closed_and_research_profile_starts_no_execution(tmp_path):
    new = (REPO / "deploy" / "railway" / "entrypoint.sh").read_text()
    with pytest.raises(subprocess.CalledProcessError):
        _run_entrypoint(new, tmp_path, {"PRISM_PROFILE": "bogus"})
    started = _run_entrypoint(
        new, tmp_path, {**research_env("full"), "PRISM_CONTEXT_GATEWAY": "on"}
    )
    assert not any("nimble" in s for s in started)
    assert "proc market microstructure collect" in started and "boot" in started
    assert any("gateway.launch" in s for s in started)


# ----------------------------------------------------------------------------- run identity


def test_lab_experiment_identity_descends_from_unchanged_released_policies():
    x = lab.experiment()
    assert x["experiment_id"] == "paper_nimble_dynamic_lab_v1"
    assert x["profile"] == "prism_autotrader_lab_v1" and x["runtime_id"] == LAB_FULL
    assert x["base_policies"]["nimble"] == PHASE30["nimble"]
    assert x["base_policies"]["event"] == PHASE30["event"]
    assert x["base_policies"]["event_universes"] == {"immediate": PHASE30["immediate"],
                                                     "confirmed": PHASE30["confirmed"]}  # fmt: skip
    assert x["continuation"].startswith("none")
    assert lab.experiment() == x  # deterministic


# ----------------------------------------------------------------------------- seed


T = datetime(2026, 10, 9, 1, 30, tzinfo=UTC)


def _insert(con, table, **values):
    """Insert one row, filling other NOT NULL columns with type-appropriate placeholders."""
    fill = {"VARCHAR": "x", "JSON": "{}", "BOOLEAN": False, "DATE": T.date()}
    for name, typ, null, *_ in con.execute(f"DESCRIBE {table}").fetchall():
        if name not in values and null == "NO":
            values[name] = fill.get(typ, T if "TIMESTAMP" in typ else 0)
    cols = ", ".join(values)
    con.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(values))})",
        list(values.values()),
    )


@pytest.fixture
def source(project, clean_env):
    from market_signal.paper.nimble import engine

    db = project / "data" / "snapshot.duckdb"
    s = Store(db)
    c = s.con
    for i, t in enumerate((T - timedelta(days=60), T - timedelta(days=1), T + timedelta(hours=1))):
        _insert(c, "perp_funding", coin="BTC", source="hyperliquid", time=t,
                funding_rate=0.0001 * i, available_at=t)  # fmt: skip
        _insert(
            c,
            "microstructure_ingest_offsets",
            **{
                next(
                    n for n, *_ in c.execute("DESCRIBE microstructure_ingest_offsets").fetchall()
                ): f"f{i}"
            },
        )
    engine.register(s, now=T - timedelta(days=2))
    _insert(c, "assets", symbol="BTC")
    s.close()
    rt.claim_authority(db, RAILWAY, "source is a production snapshot")
    return db


def test_seed_copies_only_causal_inputs_and_no_authority(source, project):
    target = project / "lab" / "prism.duckdb"
    target.parent.mkdir()
    before = lab._sha256(source)
    m = lab.build_seed(source, target, T, now=T + timedelta(days=1))
    assert lab._sha256(source) == before == m["source"]["sha256"]
    assert m["seed_policy_digest"] == SEED_POLICY_DIGEST == lab.policy_digest()
    t = m["tables"]
    # complete history up to the frontier: the 60-day-old row is kept, the post-frontier one never
    assert t["perp_funding"]["source_rows"] == 3 and t["perp_funding"]["seeded_rows"] == 2
    assert t["paper_nimble_policies"]["seeded_rows"] == 1 and t["assets"]["seeded_rows"] == 1
    for excluded in ("microstructure_ingest_offsets", "runtime_events", "paper_nimble_runs",
                     "microstructure_minutes", "copilot_runs", "lab_forward_runs",
                     "universe_policies", "universe_evaluations", "universe_outcomes",
                     "microstructure_subscription_events"):  # fmt: skip
        assert t[excluded]["rule"] == "exclude"
    s = Store(target, read_only=True)
    try:
        assert authority_claim(s.con) is None  # unclaimed: no copied authority
        assert (
            s.con.execute("SELECT count(*) FROM microstructure_ingest_offsets").fetchone()[0] == 0
        )
        assert s.con.execute("SELECT count(*) FROM paper_nimble_runs").fetchone()[0] == 0
        assert s.con.execute("SELECT max(time) FROM perp_funding").fetchone()[0] <= T
        ev = s.con.execute("SELECT event_type, runtime_id FROM runtime_events").fetchall()
        assert ev == [("lab_seeded", "lab-seed")]
    finally:
        s.close()


def test_every_table_has_a_seed_decision(source):
    s = Store(source, read_only=True)
    tables = [r[0] for r in s.con.execute("SHOW TABLES").fetchall()]
    s.close()
    assert set(lab.SEED_POLICY) <= set(tables)
    seeded = {t for t in tables if lab.rule(t).kind != "exclude"}
    assert seeded == set(lab.SEED_POLICY)
    assert not any(
        t.startswith(("paper_nimble_runs", "runtime_", "microstructure_")) for t in seeded
    )


def test_seed_refusals(source, project):
    target = project / "lab.duckdb"
    with pytest.raises(lab.SeedRefused, match="future"):
        lab.build_seed(source, target, T + timedelta(days=5), now=T)
    with pytest.raises(lab.SeedRefused, match="never the live database"):
        lab.build_seed(source, target, T, live_db=source, now=T + timedelta(days=1))
    target.write_bytes(b"")
    with pytest.raises(lab.SeedRefused, match="exists"):
        lab.build_seed(source, target, T, now=T + timedelta(days=1))
    with pytest.raises(lab.SeedRefused, match="timezone"):
        lab.build_seed(source, project / "x.duckdb", T.replace(tzinfo=None))


def test_lab_backup_verifies_without_a_v1_paper_run(source, project, clean_env):
    target = project / "lab.duckdb"
    lab.build_seed(source, target, T, now=T + timedelta(days=1))
    with pytest.raises(RuntimeError, match="no paper run"):
        rt.verify_backup(target)  # research/legacy databases always hold the v1 run
    apply(clean_env, lab_env("none"))
    from market_signal.data.store import MIGRATIONS

    assert rt.verify_backup(target)["schema_version"] == len(MIGRATIONS)
    assert rt.restore_test(target, project)["restored_ok"]


def test_lab_backup_job_registers_exports_and_restores_without_paper_runs(
    source, project, clean_env, tmp_path
):
    from market_signal.ops import backups

    target = project / "lab.duckdb"
    lab.build_seed(source, target, T, now=T + timedelta(days=1))
    apply(clean_env, lab_env("rehearsal"))
    rt.claim_authority(target, LAB_REHEARSAL, "test Lab rehearsal")
    clean_env.setenv("PRISM_DB_PATH", str(target))
    clean_env.setenv("PRISM_BACKUP_DIR", str(project / "lab-backups"))
    result = rt.run_job("backup", trigger="schedule", wait=0)
    assert result["status"] == "ok"
    rows = backups.inventory(rt.backup_dir(target), target)
    assert len(rows) == 1
    manifest = rows[0]["manifest"]
    assert manifest["snapshot_kind"] == "autotrader_lab"
    assert not any(manifest["run_ids"].values())
    assert manifest["source_authority"]["runtime_id"] == LAB_REHEARSAL
    assert manifest["source_frontier"]["tables"]["perp_funding"]["at"]
    assert manifest["table_fingerprints"]["perp_funding"]["rows"] == 2
    assert rt.restore_test(Path(rows[0]["path"]), project)["restored_ok"]
    # Off-host inspection/export must work even outside a Lab-selected environment.
    clean_env.delenv("PRISM_PROFILE")
    receipt = backups.export_backup(Path(rows[0]["path"]), tmp_path / "lab-offhost.gz", manifest)
    assert receipt["restore_test"]["table_fingerprints"] == manifest["table_fingerprints"]
    assert receipt["restore_test"]["sha256"] == manifest["sha256"]


def test_no_run_exception_requires_persisted_lab_seed_and_authority(project, clean_env):
    from market_signal.ops import backups

    target = project / "not-lab.duckdb"
    Store(target).close()
    apply(clean_env, lab_env("none"))
    with pytest.raises(ValueError, match="no run metadata"):
        backups.inspect_backup(target)
    # A Lab-looking shell or claim alone cannot bless an empty/unrelated database.
    rt.claim_authority(target, LAB_REHEARSAL, "no seed provenance")
    with pytest.raises(ValueError, match="no run metadata"):
        backups.inspect_backup(target)


def test_activation_record_holds_full_provenance(source, project):
    target = project / "lab.duckdb"
    m = lab.build_seed(source, target, T, now=T + timedelta(days=1))
    s = Store(target)
    try:
        prof = pf.resolve(lab_env("full")).record()
        lab.record_activation(s, run_ids=["paperrun_nimble_x"], seed_manifest=m,
                              profile_record=prof, revision="abc", now=T + timedelta(days=2))  # fmt: skip
        p = json.loads(s.con.execute("SELECT payload FROM runtime_events WHERE event_type="
                                     "'lab_experiment_activated'").fetchone()[0])  # fmt: skip
    finally:
        s.close()
    assert p["seed_frontier"] == T.isoformat() and p["profile"]["runtime_id"] == LAB_FULL
    assert p["experiment"]["experiment_id"] == "paper_nimble_dynamic_lab_v1"
    assert p["run_ids"] == ["paperrun_nimble_x"] and p["host"]


# ----------------------------------------------------------------------------- deployment


def _units():
    for f in sorted((VPS / "systemd").glob("*.service")):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read(f)
        yield f, cp


def test_systemd_units_are_profile_gated_and_unprivileged():
    names = {}
    for f, cp in _units():
        svc = cp["Service"]
        assert svc["User"] == "prism" and svc["NoNewPrivileges"] == "yes"
        assert svc["ProtectSystem"] == "strict" and svc["ReadWritePaths"] == "/data"
        assert svc["RestartPreventExitStatus"] == "78"
        cond = svc["ExecCondition"].split()
        assert cond[1:5] == ["ops", "profile", "--process", cond[4]] and cond[-1] == "--condition"
        names[cond[4]] = f.name
        assert "EnvironmentFile" in svc
    assert {n: pf.PROCESSES[n].unit for n in names} == names
    assert set(names) == set(pf.PROCESSES)


def test_exactly_one_scheduling_authority():
    assert not list(VPS.rglob("*.timer"))
    runners = [f.name for f, cp in _units() if "supercronic" in cp["Service"]["ExecStart"]
               or "run_scheduler" in cp["Service"]["ExecStart"]]  # fmt: skip
    assert runners == ["prism-scheduler.service"]
    script = (VPS / "run_scheduler.sh").read_text()
    assert script.count("exec supercronic") == 1 and "market ops crontab" in script


def test_systemd_analyze_accepts_units():
    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze unavailable")
    units = [str(p) for p in (VPS / "systemd").iterdir()]
    p = subprocess.run(["systemd-analyze", "verify", "--man=no", *units], capture_output=True,
                       text=True)  # fmt: skip
    bad = [line for line in p.stderr.splitlines() if line.strip() and "is not executable" not in line
           and "No such file" not in line]  # fmt: skip
    assert not bad, bad


def test_shell_scripts_parse():
    for f in [*VPS.rglob("*.sh"), REPO / "deploy" / "railway" / "entrypoint.sh"]:
        shell = "sh" if f.name in ("entrypoint.sh", "run_scheduler.sh") else "bash"
        subprocess.run([shell, "-n", str(f)], check=True)


def test_vps_env_template_starts_with_zero_authority():
    lines = (VPS / "etc" / "prism.env.example").read_text().splitlines()
    e = dict(line.split("=", 1) for line in lines if line and not line.startswith("#"))
    r = pf.resolve(e)
    assert r.profile.name == "autotrader_lab" and r.authority == "none" and r.target == "vps"
    assert e["PRISM_DB_PATH"] == "/data/prism.duckdb"
    assert not r.execution_allowed and r.scheduled_jobs() == ()
