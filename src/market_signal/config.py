"""Configuration loading.

All tunable assumptions live in ``config/*.yaml``; secrets live in ``.env``.
The project root is found by walking up from the working directory looking for
``config/universe.yaml`` (override with ``PRISM_HOME``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from market_signal.models.domain import (
    Asset,
    AssetClass,
    Calendar,
    SeriesSpec,
    Timeframe,
)


class ConfigError(ValueError):
    pass


def find_project_root(start: Path | None = None) -> Path:
    env = os.environ.get("PRISM_HOME")
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "universe.yaml").exists():
            return candidate
    # fall back to the source checkout location
    pkg_root = Path(__file__).resolve().parents[2]
    if (pkg_root / "config" / "universe.yaml").exists():
        return pkg_root
    raise ConfigError("Cannot locate project root (config/universe.yaml). Set PRISM_HOME.")


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def db(self) -> Path:
        override = os.environ.get("PRISM_DB_PATH")
        return Path(override) if override else self.data / "prism.duckdb"

    @property
    def raw(self) -> Path:
        override = os.environ.get("PRISM_RAW_DIR")
        return Path(override) if override else self.data / "raw"

    @property
    def results(self) -> Path:
        return self.root / "results"


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Missing config file: {path}")
    with path.open() as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file must contain a mapping: {path}")
    return data


def config_hash(obj: Any) -> str:
    """Stable short hash of a config object (for research provenance)."""
    payload = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def parse_universe(data: dict[str, Any]) -> dict[str, Asset]:
    assets: dict[str, Asset] = {}
    for symbol, spec in (data.get("assets") or {}).items():
        try:
            series = {}
            for tf, s in (spec.get("series") or {}).items():
                tf_enum = Timeframe(tf)
                if tf_enum == Timeframe.W1:
                    raise ConfigError(
                        f"{symbol}: weekly bars are derived from daily; do not configure 1w"
                    )
                series[tf_enum] = SeriesSpec(tf_enum, s["provider"], str(s["symbol"]))
            if Timeframe.D1 not in series:
                raise ConfigError(f"{symbol}: a 1d series is required")
            assets[symbol] = Asset(
                symbol=symbol,
                name=spec["name"],
                asset_class=AssetClass(spec["asset_class"]),
                currency=spec.get("currency", "USD"),
                calendar=Calendar(spec["calendar"]),
                active=bool(spec.get("active", True)),
                series=series,
                provider_ids={k: str(v) for k, v in (spec.get("provider_ids") or {}).items()},
                tags=tuple(spec.get("tags") or ()),
                notes=spec.get("notes", ""),
            )
        except KeyError as exc:
            raise ConfigError(f"Asset {symbol} missing field {exc}") from exc
    return assets


@dataclass
class Settings:
    paths: Paths
    universe: dict[str, Asset]
    providers: dict[str, Any]

    def asset(self, symbol: str) -> Asset:
        try:
            return self.universe[symbol.upper()]
        except KeyError as exc:
            raise ConfigError(f"Unknown asset {symbol!r}. Known: {sorted(self.universe)}") from exc

    def active_assets(self) -> list[Asset]:
        return [a for a in self.universe.values() if a.active]

    def yaml(self, relative: str) -> dict[str, Any]:
        return load_yaml(self.paths.config / relative)

    def provider_cfg(self, name: str) -> dict[str, Any]:
        return dict((self.providers.get("providers") or {}).get(name) or {})

    def secret(self, env_name: str) -> str | None:
        value = os.environ.get(env_name, "").strip()
        return value or None


@cache
def _load_settings(root: str) -> Settings:
    paths = Paths(Path(root))
    load_dotenv(paths.root / ".env", override=False)
    universe = parse_universe(load_yaml(paths.config / "universe.yaml"))
    providers = load_yaml(paths.config / "providers.yaml")
    return Settings(paths=paths, universe=universe, providers=providers)


def get_settings(root: Path | None = None) -> Settings:
    return _load_settings(str(root or find_project_root()))
