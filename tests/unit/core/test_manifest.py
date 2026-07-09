"""Tests for the publish manifest (provenance, checksums, COG attestation)."""

import json

import pytest

from euroflood.core.manifest import (
    CACHE_SCHEMA_VERSION,
    build_manifest,
    build_publish_manifest,
    file_records,
    grid_fingerprint,
    stamp_manifest,
    validate_manifest,
    validate_publish_manifest,
    write_manifest,
    write_publish_manifest,
)
from euroflood.exceptions import CacheSchemaError


def _write_file(path, content=b"hello"):
    path.write_bytes(content)
    return path


def test_file_records_has_size_and_sha256(tmp_path):
    rec = file_records({"a.bin": _write_file(tmp_path / "a.bin", b"abc")})
    assert rec["a.bin"]["size_bytes"] == 3
    assert len(rec["a.bin"]["sha256"]) == 64
    # Missing files are skipped.
    assert file_records({"ghost": tmp_path / "ghost"}) == {}


def test_build_publish_manifest_shape(tmp_path):
    f = _write_file(tmp_path / "index.tif")
    doc = build_publish_manifest(
        index_version="1.2.3",
        files={"index.tif": f},
        provenance={"n_source_files": 5},
        cog={"sparse": True},
    )
    assert doc["index_version"] == "1.2.3"
    assert doc["grid"] == grid_fingerprint()  # cache gate preserved
    assert doc["provenance"]["n_source_files"] == 5
    assert doc["cog"]["sparse"] is True
    assert doc["files"]["index.tif"]["size_bytes"] == 5


def test_stamp_manifest_updates_version_and_urls(tmp_path):
    """stamp_manifest rewrites index_version + source_urls; checksums stay valid."""
    f = _write_file(tmp_path / "index.tif")
    mpath = tmp_path / "manifest.json"
    write_publish_manifest(
        mpath,
        index_version="0.0.0-dev",
        files={"index.tif": f},
        source_urls={"source_coop": None, "zenodo_doi": None},
    )
    out = stamp_manifest(
        mpath,
        index_version="v1.0.0",
        source_urls={
            "source_coop": "https://data.source.coop/x/y/v1.0.0/",
            "zenodo_doi": "10.5281/zenodo.123",
        },
    )
    assert out["index_version"] == "v1.0.0"
    on_disk = json.loads(mpath.read_text())
    assert on_disk["index_version"] == "v1.0.0"
    assert on_disk["source_urls"]["zenodo_doi"] == "10.5281/zenodo.123"
    # Files/checksums untouched -> still validates.
    validate_publish_manifest(mpath, base_dir=tmp_path, verify_checksums=True)


def test_validate_publish_manifest_ok_and_checksum_tamper(tmp_path):
    f = _write_file(tmp_path / "index.tif", b"data")
    mpath = tmp_path / "manifest.json"
    write_publish_manifest(mpath, index_version="1", files={"index.tif": f})

    # Good bundle validates (with checksum verification).
    validate_publish_manifest(mpath, base_dir=tmp_path, verify_checksums=True)

    # Tamper the file -> checksum mismatch.
    f.write_bytes(b"tampered")
    with pytest.raises(CacheSchemaError, match="Checksum mismatch"):
        validate_publish_manifest(mpath, base_dir=tmp_path, verify_checksums=True)


def test_validate_publish_manifest_missing_file(tmp_path):
    f = _write_file(tmp_path / "index.tif")
    mpath = tmp_path / "manifest.json"
    write_publish_manifest(mpath, index_version="1", files={"index.tif": f})
    f.unlink()
    with pytest.raises(CacheSchemaError, match="missing"):
        validate_publish_manifest(mpath, base_dir=tmp_path, verify_checksums=True)


def test_validate_publish_manifest_rejects_grid_mismatch(tmp_path, mocker):
    f = _write_file(tmp_path / "index.tif")
    mpath = tmp_path / "manifest.json"
    write_publish_manifest(mpath, index_version="1", files={"index.tif": f})

    # Change the running grid fingerprint -> manifest no longer matches.
    from euroflood.core.grid import GlobalGrid

    mocker.patch.object(GlobalGrid, "WIDTH_PX", GlobalGrid.WIDTH_PX + 1)
    with pytest.raises(CacheSchemaError):
        validate_publish_manifest(mpath, base_dir=tmp_path)


def test_validate_publish_manifest_rejects_index_schema(tmp_path):
    f = _write_file(tmp_path / "index.tif")
    mpath = tmp_path / "manifest.json"
    write_publish_manifest(mpath, index_version="1", files={"index.tif": f})
    doc = json.loads(mpath.read_text())
    doc["index_schema_version"] = 999
    mpath.write_text(json.dumps(doc))
    with pytest.raises(CacheSchemaError, match="index schema"):
        validate_publish_manifest(mpath, base_dir=tmp_path)


def test_write_and_validate_cache_manifest_roundtrip(tmp_path):
    """write_manifest produces a manifest that validate_manifest accepts."""
    mpath = tmp_path / "manifest.json"
    write_manifest(mpath)
    doc = json.loads(mpath.read_text())
    assert doc == build_manifest()
    assert doc["cache_schema_version"] == CACHE_SCHEMA_VERSION
    validate_manifest(mpath)


def test_validate_manifest_missing_file_raises(tmp_path):
    """A missing cache manifest is rejected with an actionable message."""
    with pytest.raises(CacheSchemaError, match="Cache manifest is missing"):
        validate_manifest(tmp_path / "absent_manifest.json")


def test_validate_manifest_rejects_old_cache_schema(tmp_path):
    """An older/incompatible cache_schema_version is rejected."""
    mpath = tmp_path / "manifest.json"
    doc = build_manifest()
    doc["cache_schema_version"] = CACHE_SCHEMA_VERSION + 99
    mpath.write_text(json.dumps(doc))
    with pytest.raises(CacheSchemaError, match="incompatible"):
        validate_manifest(mpath)


def test_validate_publish_manifest_no_checksum_skips_file_loop(tmp_path):
    """With verify_checksums=False, a tampered/missing file is NOT re-hashed (152->exit)."""
    f = _write_file(tmp_path / "index.tif", b"data")
    mpath = tmp_path / "manifest.json"
    write_publish_manifest(mpath, index_version="1", files={"index.tif": f})
    f.write_bytes(b"tampered-but-not-checked")
    validate_publish_manifest(mpath, base_dir=tmp_path, verify_checksums=False)
