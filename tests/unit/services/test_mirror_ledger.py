"""Tests for the shared offline-mirror ledger machinery."""

import hashlib

import pytest

from euroflood.core.manifest import file_record
from euroflood.exceptions import CacheSchemaError
from euroflood.services.mirror_ledger import (
    MirrorReport,
    MirrorResult,
    ledger_size,
    load_ledger,
    update_ledger,
    validate_mirror_manifest,
    verify_against_ledger,
)


def test_mirror_result_fields():
    """MirrorResult carries the run detail; .ok reflects an empty missing list."""
    r = MirrorResult(8, n_expected=10, missing=["x.tif"], bytes_total=99)
    assert r.downloaded == 8 and r.n_expected == 10
    assert r.missing == ["x.tif"] and r.bytes_total == 99 and not r.ok
    assert MirrorResult(10, n_expected=10).ok


def _write(dirpath, name, data):
    p = dirpath / name
    p.write_bytes(data)
    return p


def test_update_ledger_merges_and_is_atomic(tmp_path):
    """Records accumulate across calls; no leftover temp; other keys preserved."""
    data = tmp_path / "data"
    data.mkdir()
    ledger = tmp_path / "led.json"
    a = _write(data, "a.tif", b"AAAA")
    b = _write(data, "b.tif", b"BBBBBB")

    update_ledger(ledger, {"a.tif": file_record(a)}, region_meta={"bbox": [0, 0, 1, 1]})
    update_ledger(ledger, {"b.tif": file_record(b)}, region_meta={"bbox": [1, 0, 2, 1]})

    led = load_ledger(ledger)
    assert set(led["tiles"]) == {"a.tif", "b.tif"}  # accumulated, not overwritten
    assert len(led["regions"]) == 2
    assert ledger_size(led, "a.tif") == 4 and ledger_size(led, "b.tif") == 6
    assert not list(tmp_path.glob("*.part"))  # atomic temp cleaned up


def test_update_ledger_model_change_drops_stale(tmp_path):
    """A model_version bump clears the old tile records."""
    data = tmp_path / "d"
    data.mkdir()
    ledger = tmp_path / "led.json"
    update_ledger(
        ledger,
        {"old.tif": file_record(_write(data, "old.tif", b"x"))},
        model_version="v1",
    )
    update_ledger(
        ledger,
        {"new.tif": file_record(_write(data, "new.tif", b"y"))},
        model_version="v2",
    )
    led = load_ledger(ledger)
    assert set(led["tiles"]) == {"new.tif"}  # v1 record dropped
    assert led["model_version"] == "v2"


def test_verify_present_missing_corrupt(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    ledger = tmp_path / "led.json"
    a = _write(data, "a.tif", b"AAAA")
    b = _write(data, "b.tif", b"BBBB")
    update_ledger(ledger, {"a.tif": file_record(a), "b.tif": file_record(b)})
    led = load_ledger(ledger)

    rep = verify_against_ledger(["a.tif", "b.tif"], data, led, collection="x")
    assert rep.ok and rep.present == ["a.tif", "b.tif"]

    b.unlink()  # missing
    (data / "a.tif").write_bytes(b"ZZZZ")  # same size, different bytes
    rep2 = verify_against_ledger(
        ["a.tif", "b.tif"], data, led, collection="x", deep=True
    )
    assert not rep2.ok
    assert rep2.missing == ["b.tif"] and rep2.corrupt == ["a.tif"]


def test_verify_shallow_misses_same_size_corruption(tmp_path):
    """Shallow verify only checks presence + size, so same-size corruption passes."""
    data = tmp_path / "d"
    data.mkdir()
    ledger = tmp_path / "led.json"
    a = _write(data, "a.tif", b"AAAA")
    update_ledger(ledger, {"a.tif": file_record(a)})
    (data / "a.tif").write_bytes(b"ZZZZ")  # same size
    rep = verify_against_ledger(["a.tif"], data, load_ledger(ledger), collection="x")
    assert rep.ok  # shallow passes; deep would flag it


def test_mirror_report_summary_and_remediation():
    rep = MirrorReport(
        collection="hazard",
        present=["a"],
        missing=["b"],
        n_expected=2,
        remediation_cmd="euroflood mirror hazard --bbox ...",
    )
    assert not rep.ok
    assert "1/2 present" in rep.summary()
    assert rep.remediation().startswith("euroflood mirror hazard")
    ok = MirrorReport(collection="hazard", present=["a"], n_expected=1)
    assert ok.ok and ok.remediation() == ""


def test_validate_mirror_manifest_checksums(tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    ledger = tmp_path / "led.json"
    a = _write(data, "a.tif", b"AAAA")
    update_ledger(ledger, {"a.tif": file_record(a)})
    validate_mirror_manifest(ledger, data_dir=data, verify_checksums=True)  # ok

    (data / "a.tif").write_bytes(b"ZZZZ")  # corrupt
    with pytest.raises(CacheSchemaError, match="Checksum mismatch"):
        validate_mirror_manifest(ledger, data_dir=data, verify_checksums=True)


def test_validate_mirror_manifest_missing_raises(tmp_path):
    with pytest.raises(CacheSchemaError, match="missing or unreadable"):
        validate_mirror_manifest(tmp_path / "nope.json", data_dir=tmp_path)


def test_load_ledger_absent_is_empty(tmp_path):
    led = load_ledger(tmp_path / "nope.json")
    assert led["tiles"] == {} and led["regions"] == []
    assert ledger_size(led, "x.tif") is None


def test_file_record_matches_hashlib(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello")
    rec = file_record(p)
    assert rec["size_bytes"] == 5
    assert rec["sha256"] == hashlib.sha256(b"hello").hexdigest()
