"""Versioned runtime profiles: one codebase, separate runtimes (docs/RUNTIME_PROFILE_SPLIT_AND_VPS_LAB.md).

A profile is a frozen, content-addressed definition of which supervised processes and which
scheduled jobs a deployment may run. It decides nothing about research, hypotheses, trading
logic or evidence; it only fences *where* existing workloads are allowed to run.

Selection is explicit (``PRISM_PROFILE``), never inferred from a hostname. Three inputs are
validated together at startup and every mismatch refuses (exit 78, EX_CONFIG):

- ``PRISM_PROFILE``: ``research`` | ``autotrader_lab``. Unset = the pre-split combined runtime
  (``legacy``), which reproduces the existing Railway entrypoint flags exactly and adds no new
  refusals, so production behaviour is unchanged until an operator sets a profile.
- ``PRISM_AUTHORITY``: ``none`` | ``rehearsal`` | ``full`` (explicit profiles only; unset =
  ``none``). ``none`` runs passive public-data collectors only (spool files, never the DB,
  ``PRISM_RUNTIME_ROLE`` must not be authoritative). ``rehearsal`` adds ingestion/research jobs
  against a *rehearsal* database claimed by the profile's rehearsal runtime id; it can never
  execute paper trades, accept external submissions, send notifications or ping the heartbeat.
  ``full`` is the profile's single authoritative runtime id.
- ``PRISM_DEPLOYMENT_TARGET``: ``railway`` | ``vps`` | ``local``; must be one the profile allows.

Legacy kill-switch flags (``PRISM_MICROSTRUCTURE``, ``PRISM_CONTEXT_GATEWAY``,
``PRISM_FREE_SOURCES``, ``PRISM_NIMBLE_RUNTIME``) may only switch a profile process *off*
under an explicit profile; a flag that would enable anything outside the profile refuses.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, replace
from typing import Any

PROFILE_ENV = "PRISM_PROFILE"
AUTHORITY_ENV = "PRISM_AUTHORITY"
TARGET_ENV = "PRISM_DEPLOYMENT_TARGET"
EXIT_PROFILE = 78  # EX_CONFIG: systemd RestartPreventExitStatus; never a crash loop of retries

AUTHORITY_MODES = ("none", "rehearsal", "full")
TARGETS = ("railway", "vps", "local")

# Workload classes. Authority modes permit classes; profiles permit names.
PASSIVE = "passive"  # public data -> local spool files only; never opens DuckDB
INGEST = "ingest"  # writes this runtime's own DB (data/research/maintenance)
INGRESS = "ingress"  # accepts external authenticated submissions (gateway HTTP)
EXECUTION = "execution"  # paper accounts, fills, trade lifecycle, execution reporting

MODE_CLASSES = {
    "none": frozenset({PASSIVE}),
    "rehearsal": frozenset({PASSIVE, INGEST}),
    "full": frozenset({PASSIVE, INGEST, INGRESS, EXECUTION}),
}


@dataclass(frozen=True)
class Process:
    name: str
    klass: str
    unit: str  # systemd unit on a VPS; the Railway entrypoint supervises the same commands
    command: tuple[str, ...]
    legacy_flag: str | None  # Railway entrypoint flag that controlled it before profiles
    legacy_default: str  # that flag's default ("on"/"off")
    legacy_on: tuple[str, ...] = ("on",)  # flag values that start it


PROCESSES: dict[str, Process] = {
    p.name: p
    for p in (
        Process("microstructure_collector", PASSIVE, "prism-microstructure.service",
                ("market", "microstructure", "collect"), "PRISM_MICROSTRUCTURE", "on"),
        Process("context_gateway", INGRESS, "prism-context-gateway.service",
                ("python", "-m", "market_signal.context.gateway.launch"),
                "PRISM_CONTEXT_GATEWAY", "off"),
        # the gateway ingest worker also drains the free-source spool (shared ingest path)
        Process("context_ingest", INGEST, "prism-context-ingest.service",
                ("python", "-m", "market_signal.context.gateway.worker"),
                "PRISM_CONTEXT_GATEWAY", "off"),
        Process("free_source_collector", PASSIVE, "prism-free-sources.service",
                ("python", "-m", "market_signal.context.free_sources.collector"),
                "PRISM_FREE_SOURCES", "off"),
        # only where context_ingest is absent (exactly one ingest of the free-source spool)
        Process("free_source_ingest", INGEST, "prism-free-source-ingest.service",
                ("python", "-m", "market_signal.context.free_sources.worker"),
                "PRISM_FREE_SOURCES", "off"),
        Process("nimble_worker", EXECUTION, "prism-nimble.service",
                ("python", "-m", "market_signal.paper.nimble.worker"),
                "PRISM_NIMBLE_RUNTIME", "on"),
        # Dynamic Universe Tier 0 radar (ranking ledger writer). Gated: no profile starts it.
        Process("universe_radar", INGEST, "prism-universe.service",
                ("python", "-m", "market_signal.universe.worker"),
                "PRISM_UNIVERSE_STAGE", "off", ("ranking", "warming")),
        Process("scheduler", INGEST, "prism-scheduler.service",
                ("supercronic", "-passthrough-logs", "<crontab>"), None, "on"),
    )
}  # fmt: skip

# Scheduled jobs (market_signal.ops.runtime.JOBS) by class. "research" = Prism research/copilot
# authority; "data" = shared acquisition either runtime may own for itself; "execution" =
# paper trading lifecycle/reporting; "maintenance" = backups; "retired_drain" = Paper v2's
# frozen exits, which stay with the database holding its history (no-ops once RETIRED and
# never able to open a position, so they grant no execution authority).
JOB_CLASS: dict[str, str] = {
    "prospective": "research",
    "daily": "research",
    "microdir_daily": "research",
    "microdir_weekly": "research",
    "oi": "data",
    "intraday": "data",
    "paper_v2_funding": "data",  # legacy name: the hourly settled-funding feed
    "positioning": "data",
    "context_macro": "data",
    "context_news": "data",
    "microstructure": "data",
    "paper_v2": "retired_drain",
    "paper_v2_brief": "retired_drain",
    "paper_nimble_brief": "execution",
    "backup": "maintenance",
}
JOB_MODE_CLASSES = {
    "none": frozenset(),
    "rehearsal": frozenset({"research", "data", "maintenance"}),
    "full": frozenset({"research", "data", "maintenance", "execution", "retired_drain"}),
}

# Dynamic Perp Universe tiers (docs/DYNAMIC_HYPERLIQUID_UNIVERSE_V1.md). Declared so the lab
# owns them; every tier stays GATED (never started by any profile) until VPS validation passes
# and a later approval activates it with a new profile version.
DYNAMIC_UNIVERSE_TIERS = ("tier0_discovery", "tier1_warming", "tier2_rich", "tier3_risk_pins")


@dataclass(frozen=True)
class Profile:
    name: str
    identity: str
    description: str
    targets: tuple[str, ...]
    processes: tuple[str, ...]
    jobs: tuple[str, ...]
    runtime_ids: dict[str, str] = field(default_factory=dict)  # authority mode -> runtime id
    boot_catchup_job: str | None = None
    unconditional_jobs: tuple[str, ...] = ()  # run even without an existing paper run
    dynamic_universe: dict[str, str] = field(default_factory=dict)
    explicit: bool = True

    def definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "identity": self.identity,
            "targets": sorted(self.targets),
            "processes": sorted(self.processes),
            "process_classes": {p: PROCESSES[p].klass for p in sorted(self.processes)},
            "jobs": sorted(self.jobs),
            "job_classes": {j: JOB_CLASS[j] for j in sorted(self.jobs)},
            "runtime_ids": dict(sorted(self.runtime_ids.items())),
            "boot_catchup_job": self.boot_catchup_job,
            "unconditional_jobs": sorted(self.unconditional_jobs),
            "dynamic_universe": dict(sorted(self.dynamic_universe.items())),
        }

    def digest(self) -> str:
        blob = json.dumps(self.definition(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def execution_capable(self) -> bool:
        return any(PROCESSES[p].klass == EXECUTION for p in self.processes) or any(
            JOB_CLASS[j] == "execution" for j in self.jobs
        )


_ALL_JOBS = tuple(JOB_CLASS)
_DATA_JOBS = tuple(j for j, c in JOB_CLASS.items() if c == "data")

PROFILES: dict[str, Profile] = {
    "research": Profile(
        name="research",
        identity="prism_research_runtime_v1",
        description="Railway research/copilot: context, macro/events, OI/funding, strategy lab, "
        "prospective research, core-six microstructure collection; no autonomous execution.",
        targets=("railway", "local"),
        processes=(
            "microstructure_collector",
            "context_gateway",
            "context_ingest",
            "free_source_collector",
            "scheduler",
        ),
        jobs=(
            "prospective",
            "daily",
            "microdir_daily",
            "microdir_weekly",
            *_DATA_JOBS,
            "paper_v2",
            "paper_v2_brief",
            "backup",
        ),
        # the existing claim on the Railway database: research inherits it, no re-claim
        runtime_ids={"full": "railway-prism-runtime", "rehearsal": "prism-research-rehearsal"},
        boot_catchup_job="prospective",
        dynamic_universe=dict.fromkeys(DYNAMIC_UNIVERSE_TIERS, "absent"),
    ),
    "autotrader_lab": Profile(
        name="autotrader_lab",
        identity="prism_autotrader_lab_v1",
        description="VPS autonomous-trader laboratory: independent market/context inputs, "
        "Nimble paper execution, trade lifecycle and execution evidence; no research authority.",
        targets=("vps", "local"),
        processes=(
            "microstructure_collector",
            "free_source_collector",
            "free_source_ingest",
            "nimble_worker",
            "scheduler",
        ),
        jobs=(*_DATA_JOBS, "paper_nimble_brief", "backup"),
        runtime_ids={
            "full": "vps-prism-autotrader-lab",
            "rehearsal": "vps-prism-autotrader-lab-rehearsal",
        },
        # funding is a trading input here even before the first lab run exists
        unconditional_jobs=("paper_v2_funding",),
        dynamic_universe=dict.fromkeys(DYNAMIC_UNIVERSE_TIERS, "gated"),
    ),
}

# New, separate identity for the prepared OVH research runtime. Existing Railway and
# lab definitions/digests are frozen. Installing this definition does not claim a DB.
PROFILES["research_vps"] = replace(
    PROFILES["research"],
    name="research_vps",
    identity="prism_research_vps_runtime_v1",
    description="OVH Research/Intelligence/Context authority, isolated from the lab database.",
    targets=("vps", "local"),
    runtime_ids={"full": "vps-prism-research", "rehearsal": "vps-prism-research-rehearsal"},
    # Research owns funding even when old Nimble accounts are archived, not imported.
    unconditional_jobs=("paper_v2_funding",),
)

# The pre-split combined Railway runtime, described (not enforced) for evidence records.
LEGACY = Profile(
    name="legacy",
    identity="prism_legacy_combined_v0",
    description="Pre-split combined runtime driven by entrypoint flags (no PRISM_PROFILE).",
    targets=TARGETS,
    processes=tuple(PROCESSES),
    jobs=_ALL_JOBS,
    boot_catchup_job="prospective",
    explicit=False,
)


class ProfileError(RuntimeError):
    """Invalid or contradictory profile configuration, or a workload outside the profile."""

    exit_code = EXIT_PROFILE


@dataclass(frozen=True)
class Resolved:
    profile: Profile
    authority: str  # none | rehearsal | full (legacy: derived from the runtime role)
    target: str | None
    role: str
    runtime_id: str
    processes: tuple[str, ...]  # effective, after authority and kill-switch flags
    disabled_by_flag: tuple[str, ...] = ()
    # Dynamic Universe Tier 1/2 warming: the collector follows the desired-set plan. Only the
    # legacy PRISM_UNIVERSE_STAGE=warming enables it; explicit profiles keep it gated.
    collector_dynamic: bool = False

    @property
    def explicit(self) -> bool:
        return self.profile.explicit

    def process_allowed(self, name: str) -> bool:
        return name in self.processes

    def job_allowed(self, job: str) -> bool:
        if not self.explicit:
            return True
        return job in self.profile.jobs and JOB_CLASS[job] in JOB_MODE_CLASSES[self.authority]

    def scheduled_jobs(self) -> tuple[str, ...]:
        return tuple(j for j in JOB_CLASS if self.job_allowed(j))

    @property
    def execution_allowed(self) -> bool:
        if not self.explicit:
            return True
        return self.authority == "full" and self.profile.execution_capable

    @property
    def external_actions_allowed(self) -> bool:
        """Telegram, heartbeat pings: only the profile's one authoritative runtime."""
        return not self.explicit or self.authority == "full"

    def record(self) -> dict[str, Any]:
        """Compact identity stored in runtime evidence (cycles, deploy records, seeds)."""
        return {
            "profile": self.profile.name,
            "identity": self.profile.identity,
            "digest": self.profile.digest(),
            "authority": self.authority,
            "target": self.target,
            "runtime_id": self.runtime_id or None,
        }

    def describe(self) -> dict[str, Any]:
        return {
            **self.record(),
            "explicit": self.explicit,
            "role": self.role or None,
            "processes": list(self.processes),
            "disabled_by_flag": list(self.disabled_by_flag),
            "collector_dynamic": self.collector_dynamic,
            "scheduled_jobs": list(self.scheduled_jobs()),
            "execution_allowed": self.execution_allowed,
            "external_actions_allowed": self.external_actions_allowed,
            "boot_catchup_job": self.boot_catchup_job(),
            "dynamic_universe": dict(self.profile.dynamic_universe),
            "definition": self.profile.definition(),
        }

    def boot_catchup_job(self) -> str | None:
        job = self.profile.boot_catchup_job
        return job if job and self.job_allowed(job) else None


def _env(env, key: str) -> str:
    return (env.get(key) or "").strip()


def _legacy(env) -> Resolved:
    """Exactly the pre-split entrypoint: every long-running process needs the authoritative
    role; microstructure/Nimble default on, gateway/free sources default off; the free-source
    ingest runs only when the gateway worker (which also drains it) is off."""

    def sh(key: str, default: str = "") -> str:  # shell ${KEY:-default}, exact comparison
        return env.get(key) or default

    on = {
        "PRISM_MICROSTRUCTURE": sh("PRISM_MICROSTRUCTURE", "on") != "off",
        "PRISM_CONTEXT_GATEWAY": sh("PRISM_CONTEXT_GATEWAY", "off") == "on",
        "PRISM_FREE_SOURCES": sh("PRISM_FREE_SOURCES", "off") == "on",
        "PRISM_NIMBLE_RUNTIME": sh("PRISM_NIMBLE_RUNTIME", "on") != "off",
    }
    role = sh("PRISM_RUNTIME_ROLE")
    rid = _env(env, "PRISM_RUNTIME_ID")
    procs = ["scheduler"]
    if role == "authoritative":
        if on["PRISM_MICROSTRUCTURE"]:
            procs.append("microstructure_collector")
        if on["PRISM_CONTEXT_GATEWAY"]:
            procs += ["context_gateway", "context_ingest"]
        if on["PRISM_FREE_SOURCES"]:
            procs.append("free_source_collector")
            if not on["PRISM_CONTEXT_GATEWAY"]:
                procs.append("free_source_ingest")
        if on["PRISM_NIMBLE_RUNTIME"]:
            procs.append("nimble_worker")
    # Dynamic Universe opt-in: the radar starts on the stage flag alone (no role test), and
    # warming switches the collector to the desired-set plan.
    stage = sh("PRISM_UNIVERSE_STAGE", "off")
    if stage in ("ranking", "warming"):
        procs.append("universe_radar")
    authority = "full" if role == "authoritative" and rid else "none"
    target = _env(env, TARGET_ENV) or None
    return Resolved(LEGACY, authority, target, role.strip().lower(), rid, tuple(procs),
                    collector_dynamic=stage == "warming")  # fmt: skip


def resolve(env=None) -> Resolved:
    """The validated profile of this process. Raises ``ProfileError`` (fail closed)."""
    env = os.environ if env is None else env
    name = _env(env, PROFILE_ENV).lower()
    if not name:
        return _legacy(env)
    if name not in PROFILES:
        raise ProfileError(f"{PROFILE_ENV}={name!r} is not a known profile ({sorted(PROFILES)})")
    profile = PROFILES[name]
    authority = _env(env, AUTHORITY_ENV).lower() or "none"
    if authority not in AUTHORITY_MODES:
        raise ProfileError(f"{AUTHORITY_ENV}={authority!r}; expected one of {AUTHORITY_MODES}")
    target = _env(env, TARGET_ENV).lower()
    if target not in TARGETS:
        raise ProfileError(f"{TARGET_ENV} must be one of {TARGETS} under an explicit profile")
    if target not in profile.targets:
        raise ProfileError(f"profile {name} does not deploy to {target} (only {profile.targets})")
    if _env(env, "RAILWAY_ENVIRONMENT_ID") and target != "railway":
        raise ProfileError(f"running on Railway but {TARGET_ENV}={target}")
    role = _env(env, "PRISM_RUNTIME_ROLE").lower()
    rid = _env(env, "PRISM_RUNTIME_ID")
    if authority == "none":
        if role == "authoritative":
            raise ProfileError(
                f"{AUTHORITY_ENV}=none contradicts PRISM_RUNTIME_ROLE=authoritative; zero "
                "authority uses PRISM_RUNTIME_ROLE=scratch (passive data never becomes production)"
            )
    else:
        expected = profile.runtime_ids[authority]
        if role != "authoritative" or rid != expected:
            raise ProfileError(
                f"{name}/{authority} requires PRISM_RUNTIME_ROLE=authoritative and "
                f"PRISM_RUNTIME_ID={expected} (got role={role or 'unset'}, id={rid or 'unset'})"
            )
    # Legacy flags: they may only switch a profile process off.
    disabled = []
    for proc in PROCESSES.values():
        value = _env(env, proc.legacy_flag).lower() if proc.legacy_flag else ""
        if not value:
            continue
        if value not in ("off", *proc.legacy_on):
            raise ProfileError(
                f"{proc.legacy_flag}={value!r}; expected one of {('off', *proc.legacy_on)}"
            )
        if value in proc.legacy_on and proc.name not in profile.processes:
            # a flag shared by two processes is satisfied if either is in the profile
            partners = [
                p.name
                for p in PROCESSES.values()
                if p.legacy_flag == proc.legacy_flag and p.name in profile.processes
            ]
            if not partners:
                raise ProfileError(
                    f"{proc.legacy_flag}={value} would start {proc.name}, which profile {name} "
                    "excludes; remove the flag"
                )
        if value == "off" and proc.name in profile.processes:
            disabled.append(proc.name)
    allowed = MODE_CLASSES[authority]
    procs = tuple(
        p
        for p in profile.processes
        if PROCESSES[p].klass in allowed
        and p not in disabled
        and (p != "scheduler" or JOB_MODE_CLASSES[authority])
    )
    return Resolved(profile, authority, target, role, rid, procs, tuple(sorted(disabled)))


def current() -> Resolved:
    return resolve()


def require_process(name: str, env=None) -> Resolved:
    """Called by each long-running program before it does anything. Under an explicit profile
    a process outside the effective set refuses; legacy keeps the existing entrypoint gates."""
    if name not in PROCESSES:
        raise ValueError(f"unknown process {name!r}")
    r = resolve(env)
    if r.explicit and not r.process_allowed(name):
        raise ProfileError(
            f"{name} is not enabled in profile {r.profile.identity} at authority {r.authority}"
        )
    return r


def require_execution(action: str, env=None) -> Resolved:
    """Paper run creation/mutation: only a full-authority execution profile (or legacy)."""
    r = resolve(env)
    if not r.execution_allowed:
        raise ProfileError(
            f"{action} refused: profile {r.profile.identity} at authority {r.authority} has no "
            "paper execution authority"
        )
    return r


def main_guard(name: str) -> Resolved:
    """For ``__main__`` entrypoints: print the refusal and exit 78 (systemd won't restart)."""
    import sys

    try:
        return require_process(name)
    except ProfileError as exc:
        print(f"profile refused: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(EXIT_PROFILE) from None


def free_sources_enabled(env=None) -> bool:
    """Phase 29 public collection owns its feeds here (the legacy RSS provider skips them and
    the gateway worker drains its spool). Legacy: ``PRISM_FREE_SOURCES=on`` exactly as before.
    Explicit: the profile includes the collector and no kill-switch disabled it. Invalid
    configuration reads as off (the processes themselves refuse to start)."""
    env = os.environ if env is None else env
    try:
        r = resolve(env)
    except ProfileError:
        return False
    if not r.explicit:
        return env.get("PRISM_FREE_SOURCES", "off") == "on"
    return (
        "free_source_collector" in r.profile.processes
        and "free_source_collector" not in r.disabled_by_flag
    )
