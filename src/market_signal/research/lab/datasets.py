"""Content-addressed snapshots of explicit stored series; no data transformations.

The Lab retains selected rows, including provenance columns, in compressed DuckDB blobs.
Hashes describe these snapshots, not the trustworthiness/completeness/PIT of a provider.
Metadata-only and unavailable fingerprints can be catalogued but cannot be preregistered
under the initial evaluation-plan contract.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from market_signal.data.store import Store
from market_signal.models.domain import Timeframe
from market_signal.research.lab.common import (
    Count,
    Digest,
    LabModel,
    Symbol,
    Text,
    UTCDateTime,
    canonical_json,
    content_id,
    revalidate,
)

DatasetKind = Literal[
    "bars",
    "corporate_actions",
    "perp_bars",
    "perp_funding",
    "perp_intraday_bars",
    "perp_intraday_revisions",
    "perp_oi_history",
    "perp_snapshots",
]
INTRADAY_KINDS = ("perp_intraday_bars", "perp_intraday_revisions")
# Open interest (Phase 19). Binance statistics keep their provider timestamp and period
# (``timeframe`` selects ``period``); Hyperliquid OI lives in Prism's own snapshots, whose
# capture time is the timestamp. The two venues are separate kinds and never merged.
OI_KINDS = ("perp_oi_history", "perp_snapshots")
FingerprintStrength = Literal["content_sha256", "metadata_only", "unavailable"]

# Identifiers are code-owned; only values are interpolated through SQL parameters.
_TABLES = {
    "bars": ("symbol", "close_time", "close_time, ts"),
    "corporate_actions": ("symbol", "CAST(date AS TIMESTAMPTZ)", "date"),
    "perp_bars": ("coin", "close_time", "close_time, ts"),
    "perp_funding": ("coin", "available_at", "available_at, time"),
    # Intraday rows carry their own availability (first_observed_at, observed_live) and
    # revision state, so a snapshot freezes what was known as well as the final values; the
    # revisions kind freezes the superseded values needed to reconstruct earlier knowledge.
    "perp_intraday_bars": ("coin", "close_time", "close_time, open_time"),
    "perp_intraday_revisions": ("coin", "close_time", "close_time, open_time, revision"),
    "perp_oi_history": ("coin", "observed_at", "observed_at"),
    "perp_snapshots": ("coin", "snapshot_at", "snapshot_at"),
}
# The column a selection's ``timeframe`` filters on (bars: candle interval; OI: period).
_PERIOD_COLUMN = {"perp_oi_history": "period"}
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class SeriesSelection(LabModel):
    """Exact source selection in [start, end).

    Bars use close_time, funding uses available_at, Binance OI its statistics timestamp
    (observed_at), Hyperliquid snapshots their capture time, actions use their effective date
    at UTC midnight (not a claim of publication availability). No synthetic fallback.
    """

    kind: DatasetKind
    symbol: Symbol
    source: Text
    timeframe: Timeframe | None = None
    start: UTCDateTime
    end: UTCDateTime

    @model_validator(mode="after")
    def valid_selection(self) -> Self:
        if self.start >= self.end:
            raise ValueError("selection start must precede end")
        if self.kind in ("bars", "perp_bars"):
            if self.timeframe is None or self.timeframe in (Timeframe.W1, Timeframe.M15):
                raise ValueError("stored bars require a native 1h, 4h or 1d timeframe")
        elif self.kind in INTRADAY_KINDS:
            if self.timeframe not in (Timeframe.M15, Timeframe.H1, Timeframe.H4):
                raise ValueError("intraday bars require a 15m, 1h or 4h timeframe")
        elif self.kind == "perp_oi_history":
            if self.timeframe != Timeframe.H1:
                raise ValueError("Binance OI history is stored at the 1h statistics period")
        elif self.timeframe is not None:
            raise ValueError("non-bar series do not have a candle timeframe")
        return self

    @property
    def market(self) -> str:
        return "perp" if self.kind.startswith("perp_") else "spot"


class SeriesFingerprint(LabModel):
    selection: SeriesSelection
    strength: FingerprintStrength
    row_count: Count | None = None
    columns: tuple[tuple[str, str], ...] = ()  # column name and DuckDB logical type
    first_observed: UTCDateTime | None = None
    last_observed: UTCDateTime | None = None
    sha256: Digest | None = None
    ingestion_run_ids: tuple[str, ...] = ()
    source_revision: Text | None = None
    limitation: Text | None = None

    @model_validator(mode="after")
    def truthful_strength(self) -> Self:
        if self.strength == "content_sha256":
            if self.sha256 is None or self.row_count is None or not self.columns:
                raise ValueError("content fingerprint requires hash, row count and schema")
            if self.row_count > 0 and (self.first_observed is None or self.last_observed is None):
                raise ValueError("nonempty snapshot requires observed bounds")
            if self.row_count == 0 and (self.first_observed or self.last_observed):
                raise ValueError("empty snapshot cannot have observed bounds")
        elif self.sha256 is not None or self.limitation is None:
            raise ValueError(
                "weak fingerprints require a limitation and cannot claim a content hash"
            )
        if (
            self.first_observed is not None
            and self.last_observed is not None
            and not self.selection.start
            <= self.first_observed
            <= self.last_observed
            < self.selection.end
        ):
            raise ValueError("observed bounds must lie within the selection")
        return self


class DatasetManifest(LabModel):
    schema_version: Literal["1"] = "1"
    series: Annotated[tuple[SeriesFingerprint, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_series(self) -> Self:
        keys = [canonical_json(s.selection.model_dump(mode="python")) for s in self.series]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate dataset selections")
        return self

    @property
    def strength(self) -> FingerprintStrength:
        strengths = {s.strength for s in self.series}
        if "unavailable" in strengths:
            return "unavailable"
        return "metadata_only" if "metadata_only" in strengths else "content_sha256"

    def canonical_json(self) -> str:
        data = self.model_dump(mode="python")
        for series in data["series"]:
            series["ingestion_run_ids"] = sorted(set(series["ingestion_run_ids"]))
        data["series"] = sorted(data["series"], key=canonical_json)
        return canonical_json(data)

    @property
    def dataset_id(self) -> str:
        return content_id("dataset_", json.loads(self.canonical_json()))


@dataclass(frozen=True)
class DatasetCapture:
    manifest: DatasetManifest
    blobs: tuple[tuple[str, bytes], ...]  # SHA-256 -> gzip of canonical rows


def _cell(value):
    # FLOAT/DOUBLE are encoded losslessly, including signed zero and nonfinite values.
    # Schema tells the reader which strings are encoded floats/dates/timestamps.
    if isinstance(value, float):
        return value.hex()
    if isinstance(value, datetime):
        if value.tzinfo is None:  # astimezone would silently assume local time
            raise ValueError("naive timestamps cannot be snapshotted")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _header(selection: SeriesSelection, columns) -> dict:
    return {
        "format": "prism_lab_rows_v1",
        "selection": selection.model_dump(mode="python"),
        "columns": columns,
    }


def capture_dataset(
    store: Store,
    selections: tuple[SeriesSelection, ...],
    *,
    max_rows: int = 250_000,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
) -> DatasetCapture:
    """Read all components in one DB transaction; fail on resource limits, never truncate.

    Empty selections are retained as strong empty snapshots, allowing later insufficient-
    data/error outcomes to be recorded. No local market data or ingestion rows are edited.
    """
    if not selections:
        raise ValueError("at least one series selection is required")
    if max_rows < 0 or not 0 < max_bytes <= MAX_SNAPSHOT_BYTES:
        raise ValueError("invalid snapshot resource limits")
    fingerprints, blobs = [], []
    total_rows = total_bytes = 0
    with store.transaction():
        for selection in selections:
            selection = revalidate(selection)
            symbol_col, time_col, ordering = _TABLES[selection.kind]
            sql = f"SELECT * FROM {selection.kind} WHERE {symbol_col}=? AND source=? AND {time_col}>=? AND {time_col}<?"
            args = [selection.symbol, selection.source, selection.start, selection.end]
            if selection.timeframe is not None:
                sql += f" AND {_PERIOD_COLUMN.get(selection.kind, 'timeframe')}=?"
                args.append(selection.timeframe.value)
            cursor = store.con.execute(sql + f" ORDER BY {ordering}", args)
            columns = tuple((d[0], str(d[1])) for d in cursor.description)
            names = [name for name, _ in columns]
            clock = "date" if selection.kind == "corporate_actions" else time_col
            time_index = names.index(clock)
            run_index = names.index("ingest_run_id")
            payload = bytearray((canonical_json(_header(selection, columns)) + "\n").encode())
            total_bytes += len(payload)
            count, first, last, runs = 0, None, None, set()
            while rows := cursor.fetchmany(1000):
                for row in rows:
                    count += 1
                    total_rows += 1
                    line = (canonical_json([_cell(v) for v in row]) + "\n").encode("utf-8")
                    total_bytes += len(line)
                    if total_rows > max_rows or total_bytes > max_bytes:
                        raise ValueError(
                            "dataset snapshot resource limit exceeded; narrow selections"
                        )
                    payload.extend(line)
                    observed = row[time_index]
                    if not isinstance(observed, datetime):
                        observed = datetime.combine(observed, datetime.min.time(), tzinfo=UTC)
                    first = first or observed
                    last = observed
                    runs.add(row[run_index])
            if total_bytes > max_bytes:
                raise ValueError("dataset snapshot resource limit exceeded; narrow selections")
            digest = hashlib.sha256(payload).hexdigest()
            fingerprints.append(
                SeriesFingerprint(
                    selection=selection,
                    strength="content_sha256",
                    row_count=count,
                    columns=columns,
                    first_observed=first,
                    last_observed=last,
                    sha256=digest,
                    ingestion_run_ids=tuple(sorted(runs)),
                )
            )
            blobs.append((digest, gzip.compress(bytes(payload), mtime=0)))
    return DatasetCapture(DatasetManifest(series=tuple(fingerprints)), tuple(blobs))


def snapshot_rows(fingerprint: SeriesFingerprint, compressed: bytes) -> list[dict]:
    """Verify and decode retained rows; independent of today's mutable source tables."""
    import io

    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        payload = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot exceeds supported size")
    if hashlib.sha256(payload).hexdigest() != fingerprint.sha256:
        raise ValueError("snapshot content hash mismatch")
    lines = payload.splitlines()
    header = json.loads(lines[0])
    if canonical_json(header) != canonical_json(
        _header(fingerprint.selection, fingerprint.columns)
    ):
        raise ValueError("snapshot schema/selection mismatch")
    if len(lines) - 1 != fingerprint.row_count:
        raise ValueError("snapshot row count mismatch")
    rows = []
    for line in lines[1:]:
        values = json.loads(line)
        row = {}
        for (name, dtype), value in zip(fingerprint.columns, values, strict=True):
            if value is not None:
                if dtype in ("FLOAT", "DOUBLE"):
                    value = float.fromhex(value)
                elif dtype.startswith("TIMESTAMP"):
                    value = datetime.fromisoformat(value)
                elif dtype == "DATE":
                    value = date.fromisoformat(value)
            row[name] = value
        rows.append(row)
    return rows
