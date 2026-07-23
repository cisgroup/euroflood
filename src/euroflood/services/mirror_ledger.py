"""Shared machinery for the local offline/HPC data mirrors (floods + hazard).

A *mirror ledger* records, per cached raster, its ``sha256`` + ``size_bytes`` (and a
little provenance) so a later run can (a) skip / re-fetch corrupt files by size and
(b) answer "is my local mirror complete and uncorrupt for this region?", the
``verify`` readiness check. Both collections reuse this: the hazard ledger lives in
``hazard_manifest.json`` (under a ``mirror`` key, alongside the frozen reference
provenance), the flood ledger in a dedicated ``floods_mirror.json``.

The pieces here are deliberately collection-agnostic; each pipeline supplies its own
"which files are expected" resolution (``tiles_for`` / a flood query).
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from ..core.manifest import INDEX_SCHEMA_VERSION, file_record
from ..exceptions import CacheSchemaError

logger = structlog.get_logger(__name__)


def _utc_now() -> str:
    """An ISO-8601 UTC timestamp (seconds precision)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class MirrorResult:
    """What one mirror run produced: the count downloaded plus the detail.

    ``downloaded`` is the number of rasters fetched this run; ``n_expected`` the size
    of the resolved set; ``missing`` the files that could not be fetched (a bulk mirror
    is resumable, so a partial run is reported, not raised).
    """

    downloaded: int
    n_expected: int = 0
    bytes_total: int = 0
    missing: list[str] = field(default_factory=list)
    ledger_path: Path | None = None

    @property
    def ok(self) -> bool:
        """True when every expected file was fetched (nothing missing)."""
        return not self.missing


@dataclass
class MirrorReport:
    """The result of a local-mirror readiness check for one collection."""

    collection: str
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    corrupt: list[str] = field(default_factory=list)
    n_expected: int = 0
    deep: bool = False
    ledger_path: Path | None = None
    remediation_cmd: str = ""

    @property
    def ok(self) -> bool:
        """True when nothing is missing or corrupt."""
        return not self.missing and not self.corrupt

    def summary(self) -> str:
        """A one-line human summary."""
        kind = "deep" if self.deep else "shallow"
        return (
            f"{self.collection} mirror ({kind}): {len(self.present)}/{self.n_expected} "
            f"present, {len(self.missing)} missing, {len(self.corrupt)} corrupt"
        )

    def remediation(self) -> str:
        """The command that would repair a failed check (empty when ok)."""
        return "" if self.ok else self.remediation_cmd


# --- ledger read/write ------------------------------------------------------
def _read_doc(path: Path) -> dict[str, Any]:
    """Read a ledger/manifest JSON document, or ``{}`` if absent/unreadable."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        logger.warning("mirror_ledger_unreadable", path=str(path))
        return {}


def load_ledger(path: Path) -> dict[str, Any]:
    """Return the ``mirror`` section of a ledger doc (``tiles``/``regions``/...).

    Always returns a dict with at least ``tiles`` and ``regions`` keys, even when the
    file or section is absent, so callers can index it unconditionally.
    """
    mirror: dict[str, Any] = _read_doc(path).get("mirror", {})
    mirror.setdefault("tiles", {})
    mirror.setdefault("regions", [])
    return mirror


def ledger_size(ledger: dict[str, Any], filename: str) -> int | None:
    """The recorded byte size for ``filename`` (for a cache-integrity re-download)."""
    rec = ledger.get("tiles", {}).get(filename)
    return rec.get("size_bytes") if rec else None


def update_ledger(
    path: Path,
    records: dict[str, dict[str, Any]],
    *,
    region_meta: dict[str, Any] | None = None,
    model_version: str | None = None,
) -> Path:
    """Merge per-file integrity ``records`` into the ledger at ``path`` (atomic).

    Preserves every other top-level key in the document (so writing the hazard ledger
    never wipes its frozen reference provenance). ``records`` maps
    ``filename -> {sha256, size_bytes, ...}``; they are dict-merged into
    ``mirror.tiles`` (accumulating across incremental region mirrors). When
    ``model_version`` differs from the ledger's recorded one, the existing tile records
    are dropped first (a model bump silently changes tile content at the same URL).
    """
    doc = _read_doc(path)
    mirror = doc.setdefault("mirror", {})
    tiles: dict[str, Any] = mirror.setdefault("tiles", {})
    if model_version is not None and mirror.get("model_version") not in (
        None,
        model_version,
    ):
        logger.info(
            "mirror_ledger_model_changed",
            old=mirror.get("model_version"),
            new=model_version,
        )
        tiles = {}
    tiles.update(records)
    mirror["tiles"] = tiles
    mirror["updated_at"] = _utc_now()
    if model_version is not None:
        mirror["model_version"] = model_version
    if region_meta is not None:
        mirror.setdefault("regions", []).append({**region_meta, "at": _utc_now()})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        tmp.write_text(json.dumps(doc, indent=2))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()
    return path


# --- verification -----------------------------------------------------------
def verify_against_ledger(
    expected_files: list[str],
    data_dir: Path,
    ledger: dict[str, Any],
    *,
    collection: str,
    deep: bool = False,
    remediation_cmd: str = "",
) -> MirrorReport:
    """Classify each expected file as present / missing / corrupt.

    ``missing`` = absent or empty on disk. ``corrupt`` = present but its size (or, when
    ``deep``, its sha256) disagrees with the ledger. A present file with no ledger
    record is reported ``present`` (it exists; there is nothing to check it against).
    """
    tiles = ledger.get("tiles", {})
    present, missing, corrupt = [], [], []
    for name in expected_files:
        fp = data_dir / name
        if not (fp.exists() and fp.stat().st_size > 0):
            missing.append(name)
            continue
        rec = tiles.get(name)
        if rec is None:
            present.append(name)
            continue
        if rec.get("size_bytes") is not None and fp.stat().st_size != rec["size_bytes"]:
            corrupt.append(name)
            continue
        if deep and file_record(fp)["sha256"] != rec.get("sha256"):
            corrupt.append(name)
            continue
        present.append(name)
    return MirrorReport(
        collection=collection,
        present=sorted(present),
        missing=sorted(missing),
        corrupt=sorted(corrupt),
        n_expected=len(expected_files),
        deep=deep,
        ledger_path=None,
        remediation_cmd=remediation_cmd,
    )


def validate_mirror_manifest(
    path: Path, *, data_dir: Path, verify_checksums: bool = False
) -> None:
    """Gate a mirror ledger: index schema (if present) + optional full re-hash.

    Unlike ``core.manifest.validate_publish_manifest`` this does **not** apply the grid
    fingerprint gate (a mirror ledger has no grid). Raises ``CacheSchemaError`` on a
    schema mismatch, a missing ledgered file, or a checksum mismatch.
    """
    doc = _read_doc(path)
    if not doc:
        raise CacheSchemaError(f"Mirror ledger missing or unreadable: {path}")
    schema = doc.get("index_schema_version")
    if schema is not None and schema != INDEX_SCHEMA_VERSION:
        raise CacheSchemaError(
            f"Mirror ledger index schema {schema!r} is incompatible "
            f"(expected {INDEX_SCHEMA_VERSION})."
        )
    if verify_checksums:
        for name, rec in doc.get("mirror", {}).get("tiles", {}).items():
            fp = data_dir / name
            if not fp.exists():
                raise CacheSchemaError(f"Ledgered mirror file missing: {name}")
            if file_record(fp)["sha256"] != rec.get("sha256"):
                raise CacheSchemaError(f"Checksum mismatch for mirror file: {name}")
