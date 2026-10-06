"""Canonical asset/entity resolver (explicit, versioned; never fuzzy).

``config/context/entities.yaml`` is the only source of event→asset relationships. The mapping
version recorded on every link is the SHA-256 of that file's canonical content, so a later
edit to the map never silently re-labels old links.

Link types:
  direct       the event is about the asset itself (or an entity whose ``assets`` list it)
  ecosystem    the event is about an entity whose ``ecosystem`` lists the asset
  market_wide  the event is market-wide (macro, systemic): linked to ``market_wide_assets``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

from market_signal.research.lab.common import content_id

LINK_TYPES = ("direct", "ecosystem", "market_wide")


@dataclass(frozen=True)
class Entity:
    entity_id: str
    kind: str
    assets: tuple[str, ...]
    ecosystem: tuple[str, ...]
    aliases: tuple[str, ...]
    ticker_aliases: tuple[str, ...]
    sectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Link:
    asset: str
    link_type: str
    entity: str  # entity id, or "market" / "explicit"


@dataclass
class EntityMap:
    version: str
    entities: dict[str, Entity]
    market_wide_assets: tuple[str, ...]
    sectors: dict[str, tuple[str, ...]]
    _patterns: list[tuple[re.Pattern, str]] = field(default_factory=list, repr=False)

    def __post_init__(self):
        pats = []
        for e in self.entities.values():
            for a in e.aliases:
                pats.append((re.compile(rf"(?<![\w-]){re.escape(a)}(?![\w-])", re.I), e.entity_id))
            for t in e.ticker_aliases:
                pats.append((re.compile(rf"(?<![\w$-])\$?{re.escape(t)}(?![\w-])"), e.entity_id))
        self._patterns = pats

    # ------------------------------------------------------------------ resolution
    def entities_in(self, text: str) -> list[str]:
        """Entity ids named in ``text`` (exact whole-word alias/ticker match), sorted."""
        found = {eid for pat, eid in self._patterns if pat.search(text or "")}
        return sorted(found)

    def asset_entity(self, asset: str) -> str | None:
        """The entity whose ``assets`` list ``asset`` (None if unmapped)."""
        hits = sorted(e.entity_id for e in self.entities.values() if asset in e.assets)
        return hits[0] if hits else None

    def links(self, *, entities: list[str] = (), assets: list[str] = (),
              market_wide: bool = False) -> list[Link]:  # fmt: skip
        """Deterministic asset links for an event. Unknown entity ids are ignored (never
        guessed). Explicit ``assets`` (a provider's structured ticker) are direct links."""
        out: dict[tuple[str, str], Link] = {}

        def add(asset: str, lt: str, ent: str) -> None:
            key = (asset, lt)
            if key not in out:
                out[key] = Link(asset, lt, ent)

        for a in assets:
            add(a.upper(), "direct", self.asset_entity(a.upper()) or "explicit")
        for eid in entities:
            e = self.entities.get(eid)
            if e is None:
                continue
            for a in e.assets:
                add(a, "direct", eid)
            for a in e.ecosystem:
                add(a, "ecosystem", eid)
        if market_wide:
            for a in self.market_wide_assets:
                add(a, "market_wide", "market")
        # a direct link outranks a weaker link to the same asset
        direct = {a for (a, lt) in out if lt == "direct"}
        return sorted((v for (a, lt), v in out.items() if lt == "direct" or a not in direct),
                      key=lambda x: (x.asset, LINK_TYPES.index(x.link_type)))  # fmt: skip

    def sector_assets(self, sector: str) -> tuple[str, ...]:
        return self.sectors.get(sector, ())


def default_path() -> Path:
    from market_signal.config import find_project_root

    return find_project_root() / "config" / "context" / "entities.yaml"


def parse(raw: dict) -> EntityMap:
    ents = {}
    for eid, d in (raw.get("entities") or {}).items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", eid):
            raise ValueError(f"bad entity id {eid!r}")
        ents[eid] = Entity(
            entity_id=eid, kind=str(d["kind"]),
            assets=tuple(str(a).upper() for a in d.get("assets") or ()),
            ecosystem=tuple(str(a).upper() for a in d.get("ecosystem") or ()),
            aliases=tuple(str(a) for a in d.get("aliases") or ()),
            ticker_aliases=tuple(str(a) for a in d.get("ticker_aliases") or ()),
            sectors=tuple(d.get("sectors") or ()),
        )  # fmt: skip
    version = f"{raw.get('version', 'entities')}:" + content_id("", raw)[:16]
    return EntityMap(version=version, entities=ents,
                     market_wide_assets=tuple(str(a).upper() for a in raw.get("market_wide_assets") or ()),
                     sectors={k: tuple(v) for k, v in (raw.get("sectors") or {}).items()})  # fmt: skip


@cache
def _load(path: str) -> EntityMap:
    return parse(yaml.safe_load(Path(path).read_text()))


def load_entities(path: Path | None = None) -> EntityMap:
    return _load(str(path or default_path()))
