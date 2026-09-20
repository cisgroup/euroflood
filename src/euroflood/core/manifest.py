"""Cache manifest: version + grid fingerprint to detect incompatible caches.

The export pipeline writes a ``manifest.json`` next to the index that records the
cache schema version, the dictionary schema version, and a fingerprint of the
`GlobalGrid` used to build it. Consumers validate it
before trusting the index, so a cache produced by older/incompatible code (e.g.
before the grid-rounding or dtype fixes) is rejected with a clear message rather
than silently producing wrong results.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ..exceptions import CacheSchemaError
from ..schemas import DICTIONARY_SCHEMA_VERSION
from .grid import GlobalGrid

# Bump when the on-disk cache layout / pixel semantics change in an incompatible
# way (e.g. the grid math, the parquet flood_id dtype, or the dictionary shape).
CACHE_SCHEMA_VERSION = 1

# Bump when the *publish* manifest contract (provenance/checksums/COG attestation)
# changes incompatibly. Separate from the cache schema so a local cache and a
# published bundle version independently.
INDEX_SCHEMA_VERSION = 1


def grid_fingerprint() -> dict[str, Any]:
    """Return the identifying parameters of the current GlobalGrid."""
    return {
        "origin_x": GlobalGrid.ORIGIN_X,
        "origin_y": GlobalGrid.ORIGIN_Y,
        "resolution": GlobalGrid.RESOLUTION,
        "width_px": GlobalGrid.WIDTH_PX,
        "height_px": GlobalGrid.HEIGHT_PX,
    }


def build_manifest() -> dict[str, Any]:
    """Build the manifest document describing the current cache."""
    return {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "dictionary_schema_version": DICTIONARY_SCHEMA_VERSION,
        "grid": grid_fingerprint(),
    }


def write_manifest(path: Path) -> None:
    """Write the cache manifest to `path`."""
    path.write_text(json.dumps(build_manifest(), indent=2))


def validate_manifest(path: Path) -> None:
    """Raise `CacheSchemaError` if the cache is missing or incompatible.

    Args:
        path: Path to the cache ``manifest.json``.

    Raises:
        CacheSchemaError: If the manifest is absent, its schema version differs,
            or its grid fingerprint no longer matches the running code.
    """
    if not path.exists():
        raise CacheSchemaError(
            "Cache manifest is missing. Re-run the 'export' pipeline (or pull a "
            "published index) to generate a compatible cache."
        )
    data = json.loads(path.read_text())
    if data.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        raise CacheSchemaError(
            f"Cache schema version {data.get('cache_schema_version')!r} is "
            f"incompatible (expected {CACHE_SCHEMA_VERSION}); re-run 'export'."
        )
    if data.get("dictionary_schema_version") != DICTIONARY_SCHEMA_VERSION:
        raise CacheSchemaError(
            f"Dictionary schema version {data.get('dictionary_schema_version')!r} is "
            f"incompatible (expected {DICTIONARY_SCHEMA_VERSION}); re-run 'build-index' "
            "or pull the current published index."
        )
    if data.get("grid") != grid_fingerprint():
        raise CacheSchemaError(
            "Cache grid fingerprint does not match the current GlobalGrid; "
            "re-run 'ingest' and 'export' to rebuild the index."
        )


def file_record(path: Path) -> dict[str, Any]:
    """Return ``{size_bytes, sha256}`` for a file (integrity record)."""
    data = path.read_bytes()
    return {"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def file_records(files: dict[str, Path]) -> dict[str, dict[str, Any]]:
    """Map ``{relative_name: path}`` to per-file integrity records (skips missing)."""
    return {name: file_record(p) for name, p in files.items() if p.exists()}


def build_publish_manifest(
    *,
    index_version: str,
    files: dict[str, Path],
    provenance: dict[str, Any] | None = None,
    cog: dict[str, Any] | None = None,
    source_urls: dict[str, Any] | None = None,
    published_at: str | None = None,
    git_commit: str | None = None,
) -> dict[str, Any]:
    """Build the publish manifest: the contract publishing deploys and consumers validate.

    Extends the cache manifest (schema versions + grid fingerprint, so an
    incompatible grid is still rejected) with the publishing metadata: an
    ``index_version``, provenance, a COG attestation, per-file checksums/sizes,
    and templated ``source_urls`` (filled at publish time).
    """
    doc = build_manifest()  # cache_schema_version, dictionary_schema_version, grid
    doc["index_schema_version"] = INDEX_SCHEMA_VERSION
    doc["index_version"] = index_version
    doc["published_at"] = published_at
    doc["git_commit"] = git_commit
    doc["provenance"] = provenance or {}
    doc["cog"] = cog or {}
    doc["files"] = file_records(files)
    doc["source_urls"] = source_urls or {}
    return doc


def write_publish_manifest(path: Path, **kwargs: Any) -> None:
    """Write the publish manifest (see `build_publish_manifest`)."""
    path.write_text(json.dumps(build_publish_manifest(**kwargs), indent=2))


def stamp_manifest(
    path: Path,
    *,
    index_version: str | None = None,
    source_urls: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rewrite an existing manifest's ``index_version`` / ``source_urls`` in place.

    The published URLs and DOI are only known *after* upload, so the publish step
    builds the bundle with placeholder ``source_urls`` (null) and calls this to stamp
    the real values into ``manifest.json`` before re-uploading it. ``source_urls`` is
    *merged* (only the given keys change), so stamping one host's URL/DOI preserves
    another's already recorded, e.g. a later Zenodo publish keeps the Source
    Cooperative URL. Checksums, provenance, and the grid fingerprint are preserved.

    Returns the updated manifest document.
    """
    data: dict[str, Any] = json.loads(path.read_text())
    if index_version is not None:
        data["index_version"] = index_version
    if source_urls is not None:
        data["source_urls"] = {**(data.get("source_urls") or {}), **source_urls}
    path.write_text(json.dumps(data, indent=2))
    return data


def validate_publish_manifest(
    path: Path, *, base_dir: Path | None = None, verify_checksums: bool = False
) -> None:
    """Validate a publish manifest: cache gate + index schema + optional checksums.

    Args:
        path: Path to the manifest JSON.
        base_dir: Directory the manifest's ``files`` are relative to (defaults to
            the manifest's own directory).
        verify_checksums: If True, re-hash each listed file and compare.

    Raises:
        CacheSchemaError: On a missing/incompatible manifest, an index-schema
            mismatch, a missing file, or a checksum mismatch.
    """
    validate_manifest(path)  # schema versions + grid fingerprint gate
    data = json.loads(path.read_text())
    if data.get("index_schema_version") != INDEX_SCHEMA_VERSION:
        raise CacheSchemaError(
            f"Publish manifest index schema {data.get('index_schema_version')!r} is "
            f"incompatible (expected {INDEX_SCHEMA_VERSION})."
        )
    if verify_checksums:
        base = base_dir or path.parent
        for name, rec in data.get("files", {}).items():
            fp = base / name
            if not fp.exists():
                raise CacheSchemaError(f"Published file missing: {name}")
            if file_record(fp)["sha256"] != rec.get("sha256"):
                raise CacheSchemaError(f"Checksum mismatch for published file: {name}")
