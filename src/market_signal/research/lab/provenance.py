"""Software identities captured independently of strategy definitions and datasets."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import subprocess
from contextlib import suppress
from pathlib import Path

from market_signal.research.lab.common import Digest, LabModel, Text, content_id


class SoftwareIdentity(LabModel):
    label: Text
    python_version: Text
    git_commit: str | None = None
    source_sha256: Digest | None = None
    lock_sha256: Digest | None = None
    packages: tuple[tuple[str, str], ...] = ()

    @property
    def software_id(self) -> str:
        data = self.model_dump(mode="python")
        data["packages"] = sorted(data["packages"])
        return content_id("software_", data)


def capture_software(root: Path) -> SoftwareIdentity:
    """Hash working source (including uncommitted files), pyproject and the lockfile.

    This records identities, not a source archive or a guarantee that the environment
    was installed from the lockfile. Never reads .env or other secret files.
    """
    files = sorted((root / "src").rglob("*.py"))
    if (root / "pyproject.toml").exists():
        files.append(root / "pyproject.toml")
    tree = [(str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files]
    commit = None
    with suppress(OSError, subprocess.CalledProcessError):
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
    packages = []
    for name in (
        "prism-market-signal",
        "duckdb",
        "pandas",
        "numpy",
        "pyarrow",
        "pydantic",
        "pyyaml",
    ):
        try:
            packages.append((name, importlib.metadata.version(name)))
        except importlib.metadata.PackageNotFoundError:
            packages.append((name, "unavailable"))
    lock = root / "uv.lock"
    return SoftwareIdentity(
        label="prism_working_tree",
        python_version=platform.python_version(),
        git_commit=commit,
        source_sha256=content_id("", tree) if files else None,
        lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None,
        packages=tuple(packages),
    )
