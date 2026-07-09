"""Tests for the StateLedger (resumable JSONL ingestion state)."""

from euroflood.pipelines.ledger import StateLedger


def test_record_and_is_done(tmp_path):
    led = StateLedger(tmp_path / "l.jsonl")
    assert not led.is_done(1)

    led.record(1, download_status="complete")
    assert not led.is_done(1)  # downloaded but not yet processed

    led.record(1, process_status="complete", points=5)
    assert led.is_done(1)


def test_empty_status_counts_as_done(tmp_path):
    led = StateLedger(tmp_path / "l.jsonl")
    led.record(2, process_status="empty")
    assert led.is_done(2)


def test_reloads_from_disk(tmp_path):
    path = tmp_path / "l.jsonl"
    StateLedger(path).record(7, process_status="complete")
    assert StateLedger(path).is_done(7)  # a fresh ledger reads prior state


def test_tolerates_torn_last_line(tmp_path):
    path = tmp_path / "l.jsonl"
    StateLedger(path).record(1, process_status="complete")
    with open(path, "a") as f:
        f.write('{"global_id": 2, "process_status": "comp')  # crash mid-write

    reloaded = StateLedger(path)
    assert reloaded.is_done(1)  # the good line survives
    assert not reloaded.is_done(2)  # the torn line is ignored


def test_failures(tmp_path):
    led = StateLedger(tmp_path / "l.jsonl")
    led.record(1, process_status="complete")
    led.record(2, download_status="failed", last_error="boom")
    led.record(3, process_status="failed", last_error="bad tif")

    assert {r["global_id"] for r in led.failures()} == {2, 3}


def test_tolerates_blank_lines(tmp_path):
    """Blank/whitespace-only lines (e.g. a torn write that flushed only '\\n')."""
    path = tmp_path / "l.jsonl"
    path.write_text(
        '{"global_id": 1, "process_status": "complete"}\n'
        "\n"  # blank line
        "   \n"  # whitespace-only line
        '{"global_id": 2, "process_status": "empty"}\n'
    )
    led = StateLedger(path)
    assert led.is_done(1)
    assert led.is_done(2)


def test_ignores_record_without_global_id(tmp_path):
    """A valid JSON line that lacks ``global_id`` is skipped, not stored as None."""
    path = tmp_path / "l.jsonl"
    path.write_text(
        '{"download_status": "complete"}\n'  # no global_id -> skipped
        '{"global_id": 5, "process_status": "complete"}\n'
    )
    led = StateLedger(path)
    assert led.is_done(5)
    assert None not in led.records
    assert set(led.records) == {5}


def test_record_merges_fields_across_appends(tmp_path):
    """Two records for one id merge (download then process) on reload (last wins)."""
    path = tmp_path / "l.jsonl"
    led = StateLedger(path)
    led.record(9, download_status="complete", filename="a.tif")
    led.record(9, process_status="complete", points=7)

    reloaded = StateLedger(path)
    rec = reloaded.records[9]
    assert rec["filename"] == "a.tif"  # earlier field preserved
    assert rec["download_status"] == "complete"
    assert rec["process_status"] == "complete"
    assert rec["points"] == 7
    assert reloaded.is_done(9)


def test_record_creates_parent_dir(tmp_path):
    """record() creates a missing parent directory before appending."""
    path = tmp_path / "deep" / "nested" / "state.jsonl"
    led = StateLedger(path)
    led.record(1, process_status="complete")
    assert path.exists()
    assert StateLedger(path).is_done(1)
