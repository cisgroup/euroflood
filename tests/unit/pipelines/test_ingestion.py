"""Tests for the Ingestion Pipeline orchestrator."""

import ast
import json
import logging
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

from euroflood.pipelines.ingestion import IngestionPipeline, _filter_key
from euroflood.pipelines.ledger import StateLedger
from euroflood.services.processor import ProcessOutcome


class MockExecutor:
    """A synchronous executor for testing."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def submit(self, fn, *args, **kwargs):
        f = Future()
        try:
            res = fn(*args, **kwargs)
            f.set_result(res)
        except Exception as e:
            f.set_exception(e)
        return f


def test_ingestion_run_full_flow(mocker, mock_settings, sample_inventory_data):
    """Test the full flow: Inventory -> Download -> Process."""
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)

    pipe = IngestionPipeline()

    data = sample_inventory_data.copy()
    data[0]["year"] = 2020

    pipe.inventory.load = MagicMock(return_value=pd.DataFrame(data))
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock(return_value=Path("fake_local.tif"))
    pipe.processor.process = MagicMock(return_value=ProcessOutcome("complete", 100))

    pipe.run(year=2020)

    pipe.inventory.load.assert_called()
    pipe.downloader.download_file.assert_called()
    pipe.processor.process.assert_called()


def test_ingestion_update_trigger(mocker, mock_settings):
    """Test that --update triggers scraping."""
    pipe = IngestionPipeline()
    pipe.scraper.fetch_all_records = MagicMock(return_value=[])
    pipe.inventory.save = MagicMock()

    pipe.run(update=True)

    pipe.scraper.fetch_all_records.assert_called_once()
    pipe.inventory.save.assert_called_once()


def test_ingestion_filters_and_errors(mocker, mock_settings, sample_inventory_data):
    """Test filtering logic and process error handling."""
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)

    pipe = IngestionPipeline()

    # Inventory has 1 item with date "2020-01-01"
    data = sample_inventory_data.copy()
    data[0]["year"] = 2020

    pipe.inventory.load = MagicMock(return_value=pd.DataFrame(data))
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock(return_value=Path("file.tif"))

    # 1. Test Filter Empty Result (Line 137)
    pipe.run(year=1999)  # No match
    # No crash implies success

    # 2. Test Month Filter (Line 133)
    pipe.run(year=2020, month="05")  # No match

    # 3. Test Process Error (Line 181)
    # Force processor to raise exception
    pipe.processor.process = MagicMock(side_effect=Exception("Processing Failed"))

    # Should catch exception and log error, not crash
    pipe.run(year=2020, month="01")


def test_ingestion_resume_skips_done(mocker, mock_settings, sample_inventory_data):
    """A file already marked done in the ledger is skipped on re-run."""
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)

    gid = sample_inventory_data[0]["global_id"]
    ledger_path = (
        mock_settings.cache_dir / "state" / f"ingest_{_filter_key(2020, None)}.jsonl"
    )
    StateLedger(ledger_path).record(gid, process_status="complete")

    pipe = IngestionPipeline()
    data = sample_inventory_data.copy()
    data[0]["year"] = 2020
    pipe.inventory.load = MagicMock(return_value=pd.DataFrame(data))
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock()
    pipe.processor.process = MagicMock()

    pipe.run(year=2020)

    pipe.downloader.download_file.assert_not_called()
    pipe.processor.process.assert_not_called()


def test_ingestion_writes_dead_letter_report(
    mocker, mock_settings, sample_inventory_data
):
    """A processing failure is recorded and a dead-letter report is written."""
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)

    pipe = IngestionPipeline()
    data = sample_inventory_data.copy()
    data[0]["year"] = 2020
    pipe.inventory.load = MagicMock(return_value=pd.DataFrame(data))
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock(return_value=Path("f.tif"))
    pipe.processor.process = MagicMock(side_effect=Exception("bad tif"))

    pipe.run(year=2020)

    report = (
        mock_settings.cache_dir / "state" / f"failed_{_filter_key(2020, None)}.json"
    )
    assert report.exists()
    failures = json.loads(report.read_text())
    assert failures[0]["process_status"] == "failed"


def test_ingest_plan(mock_settings, sample_inventory_data):
    """plan() reports the work-set without touching the network or processing."""
    pd.DataFrame(sample_inventory_data).to_csv(
        mock_settings.get_inventory_path(), index=False
    )
    p = IngestionPipeline().plan()
    assert p["total"] == 1
    assert p["pending"] == 1
    assert p["done"] == 0


def test_ingest_plan_no_inventory(mock_settings):
    p = IngestionPipeline().plan()
    assert p["total"] == 0
    assert "note" in p


def test_ingest_plan_limit(mock_settings, sample_inventory_data):
    data = [
        *sample_inventory_data,
        {**sample_inventory_data[0], "global_id": 2, "filename": "b.tif"},
    ]
    pd.DataFrame(data).to_csv(mock_settings.get_inventory_path(), index=False)
    assert IngestionPipeline().plan(limit=1)["total"] == 1


def _shard_df(rows=12):
    return pd.DataFrame(
        [
            {
                "global_id": i,
                "filename": f"f{i}.tif",
                "year": 2020,
                "start_date": "2020-01-01",
                "end_date": "2020-01-10",
                "cluster_id": "1",
                "download_url": f"http://mock/f{i}.tif",
            }
            for i in range(rows)
        ]
    )


def test_shard_split_is_stable_and_disjoint(mock_settings):
    """Modulo sharding partitions the inventory: stable, disjoint, exhaustive."""
    df = _shard_df(12)
    shard_count = 3
    seen: set[int] = set()
    for idx in range(shard_count):
        mock_settings.ingest_shard_count = shard_count
        mock_settings.ingest_shard_index = idx
        pipe = IngestionPipeline()
        out = pipe._candidates(df, year=None, month=None, limit=None)
        ids = set(out["global_id"])
        assert all(i % shard_count == idx for i in ids)
        assert seen.isdisjoint(ids)
        seen |= ids
        again = set(pipe._candidates(df, None, None, None)["global_id"])
        assert again == ids
    assert seen == set(range(12))


def test_shard_ledger_suffix(mock_settings):
    """A sharded run uses a per-shard ledger suffix (sole-writer per shard)."""
    mock_settings.ingest_shard_count = 4
    mock_settings.ingest_shard_index = 2
    pipe = IngestionPipeline()
    assert pipe._ledger_suffix() == "_shard2of4"
    mock_settings.ledger_suffix = "_custom"
    assert IngestionPipeline()._ledger_suffix() == "_custom"


def test_shard_run_uses_shard_ledger_path(mocker, mock_settings):
    """run() under a shard writes its ledger to the shard-suffixed path (lines 111,119)."""
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)

    mock_settings.ingest_shard_count = 2
    mock_settings.ingest_shard_index = 1  # keeps odd global_ids only
    pipe = IngestionPipeline()
    pipe.inventory.load = MagicMock(return_value=_shard_df(4))
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock(return_value=Path("f.tif"))
    pipe.processor.process = MagicMock(return_value=ProcessOutcome("complete", 10))

    pipe.run(year=2020)

    ledger_path = (
        mock_settings.cache_dir
        / "state"
        / f"ingest_{_filter_key(2020, None)}_shard1of2.jsonl"
    )
    assert ledger_path.exists()
    led = StateLedger(ledger_path)
    assert set(led.records) == {1, 3}


def test_ingestion_download_exception_recorded(
    mocker, mock_settings, sample_inventory_data
):
    """A download that raises is recorded as failed download (lines 235-242)."""
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)

    pipe = IngestionPipeline()
    data = sample_inventory_data.copy()
    data[0]["year"] = 2020
    pipe.inventory.load = MagicMock(return_value=pd.DataFrame(data))
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock(side_effect=OSError("connection reset"))
    pipe.processor.process = MagicMock()

    pipe.run(year=2020)

    pipe.processor.process.assert_not_called()
    report = (
        mock_settings.cache_dir / "state" / f"failed_{_filter_key(2020, None)}.json"
    )
    assert report.exists()
    failures = json.loads(report.read_text())
    assert failures[0]["download_status"] == "failed"
    assert "connection reset" in failures[0]["last_error"]


def test_ingestion_download_returns_none_recorded(
    mocker, mock_settings, sample_inventory_data
):
    """A download returning None (no file) is recorded as failed."""
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)

    pipe = IngestionPipeline()
    data = sample_inventory_data.copy()
    data[0]["year"] = 2020
    pipe.inventory.load = MagicMock(return_value=pd.DataFrame(data))
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock(return_value=None)
    pipe.processor.process = MagicMock()

    pipe.run(year=2020)

    pipe.processor.process.assert_not_called()
    gid = sample_inventory_data[0]["global_id"]
    ledger_path = (
        mock_settings.cache_dir / "state" / f"ingest_{_filter_key(2020, None)}.jsonl"
    )
    led = StateLedger(ledger_path)
    assert led.records[gid]["download_status"] == "failed"
    assert led.records[gid]["last_error"] == "download returned no file"


def test_ingestion_resume_after_partial_crash(
    mocker, mock_settings, sample_inventory_data
):
    """Kill-and-resume: a previously-completed file is skipped, only the rest runs."""
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)

    df = _shard_df(3)  # global_ids 0, 1, 2 for year 2020

    ledger_path = (
        mock_settings.cache_dir / "state" / f"ingest_{_filter_key(2020, None)}.jsonl"
    )
    StateLedger(ledger_path).record(0, process_status="complete")

    pipe = IngestionPipeline()
    pipe.inventory.load = MagicMock(return_value=df)
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock(return_value=Path("f.tif"))
    pipe.processor.process = MagicMock(return_value=ProcessOutcome("complete", 5))

    pipe.run(year=2020)

    downloaded = {c.args[1] for c in pipe.downloader.download_file.call_args_list}
    assert downloaded == {"f1.tif", "f2.tif"}
    assert pipe.processor.process.call_count == 2


# --- run accounting --------------------------------------------------------
def _rows(n: int) -> list[dict]:
    """n inventory rows, all in year 2020."""
    return [
        {
            "global_id": i,
            "filename": f"WD_MERGE_2020-01-0{i}---2020-01-1{i}_cluster_{i}.tif",
            "year": 2020,
            "start_date": "2020-01-01",
            "end_date": "2020-01-10",
            "cluster_id": str(i),
            "download_url": f"http://mock/2020/f{i}.tif",
        }
        for i in range(1, n + 1)
    ]


def _completion(caplog) -> dict:
    """Fields of the single pipeline_completed event (structlog renders a dict)."""
    line = next(m for m in caplog.text.splitlines() if "pipeline_completed" in m)
    payload = ast.literal_eval(line[line.index("{") :])
    return dict(payload)


def _pipeline(mocker, rows, *, process=None):
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", MockExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", MockExecutor)
    pipe = IngestionPipeline()
    pipe.inventory.load = MagicMock(return_value=pd.DataFrame(rows))
    pipe.inventory.exists = MagicMock(return_value=True)
    pipe.downloader.download_file = MagicMock(return_value=Path("f.tif"))
    pipe.processor.process = process or MagicMock(
        return_value=ProcessOutcome("complete", 7)
    )
    return pipe


def test_completion_log_accounts_for_every_file(mocker, mock_settings, caplog):
    """considered = skipped + ingested + failures, so nothing can look 'missing'.

    The old log reported a single `processed` count meaning "handled by this
    invocation", which reads as "of the whole archive": a build that ingested a few
    files first and then resumed looked like it had silently lost them.
    """
    rows = _rows(4)
    ledger_path = (
        mock_settings.cache_dir / "state" / f"ingest_{_filter_key(2020, None)}.jsonl"
    )
    StateLedger(ledger_path).record(rows[0]["global_id"], process_status="complete")

    with caplog.at_level(logging.INFO):
        _pipeline(mocker, rows).run(year=2020)

    c = _completion(caplog)
    assert c["considered"] == 4
    assert c["skipped_already_done"] == 1
    assert c["ingested"] == 3
    assert (
        c["skipped_already_done"]
        + c["ingested"]
        + c["cached"]
        + c["empty"]
        + c["download_failed"]
        + c["process_failed"]
        + c["process_skipped"]
        == c["considered"]
    )


def test_completion_log_does_not_count_a_failed_process_as_ingested(
    mocker, mock_settings, caplog
):
    """The old count was len(downloaded_files), so a processing failure inflated it."""
    rows = _rows(3)
    failing = MagicMock(side_effect=Exception("bad tif"))
    with caplog.at_level(logging.INFO):
        _pipeline(mocker, rows, process=failing).run(year=2020)

    c = _completion(caplog)
    assert c["ingested"] == 0, "a file that failed to process was counted as ingested"
    assert c["process_failed"] == 3
    assert c["considered"] == 3


def test_fully_resumed_run_still_reports_accounting(mocker, mock_settings, caplog):
    """A run with nothing left to do used to log only 'nothing_to_do'."""
    rows = _rows(3)
    ledger_path = (
        mock_settings.cache_dir / "state" / f"ingest_{_filter_key(2020, None)}.jsonl"
    )
    ledger = StateLedger(ledger_path)
    for row in rows:
        ledger.record(row["global_id"], process_status="complete")

    with caplog.at_level(logging.INFO):
        _pipeline(mocker, rows).run(year=2020)

    c = _completion(caplog)
    assert c["considered"] == c["skipped_already_done"] == 3
    assert c["ingested"] == 0


def test_cache_hit_is_not_logged_as_empty(mocker, mock_settings, caplog):
    """A file whose parquet already exists is `complete` with its real point count.

    processor.process() used to return a bare 0 for a cache hit, which this
    pipeline then wrote to the ledger as process_status="empty", points=0 --
    indistinguishable from a raster with no wet pixels. A build spread over
    several resumed runs therefore produced a ledger summing far short of the
    index it had just written.
    """
    rows = _rows(2)
    cached = MagicMock(return_value=ProcessOutcome("cached", 4242))
    with caplog.at_level(logging.INFO):
        _pipeline(mocker, rows, process=cached).run(year=2020)

    ledger_path = (
        mock_settings.cache_dir / "state" / f"ingest_{_filter_key(2020, None)}.jsonl"
    )
    led = StateLedger(ledger_path)
    for row in rows:
        record = led.records[row["global_id"]]
        assert record["process_status"] == "complete"
        assert record["points"] == 4242

    c = _completion(caplog)
    assert c["cached"] == 2
    assert c["empty"] == 0
    assert c["ingested"] == 0
    assert c["total_points"] == 2 * 4242, "cached points must reach the total"


def test_empty_raster_is_still_logged_as_empty(mocker, mock_settings, caplog):
    """A genuinely pixel-free raster keeps the `empty` ledger status (and is done)."""
    rows = _rows(1)
    empty = MagicMock(return_value=ProcessOutcome("empty", 0))
    with caplog.at_level(logging.INFO):
        _pipeline(mocker, rows, process=empty).run(year=2020)

    ledger_path = (
        mock_settings.cache_dir / "state" / f"ingest_{_filter_key(2020, None)}.jsonl"
    )
    led = StateLedger(ledger_path)
    assert led.records[rows[0]["global_id"]]["process_status"] == "empty"
    assert led.is_done(rows[0]["global_id"])
    assert _completion(caplog)["empty"] == 1


@pytest.mark.parametrize("status", ["missing_file", "missing_crs"])
def test_skipped_file_is_a_failure_not_an_empty_raster(
    mocker, mock_settings, caplog, status
):
    """An absent tif / untrusted CRS is a failure, so it is retried, not 'done'.

    Both used to return 0 and be filed as `empty`, which `is_done()` treats as
    complete -- so a file that was never actually read was never retried and
    never appeared in the dead-letter report.
    """
    rows = _rows(1)
    skipped = MagicMock(return_value=ProcessOutcome(status, 0))
    with caplog.at_level(logging.INFO):
        _pipeline(mocker, rows, process=skipped).run(year=2020)

    gid = rows[0]["global_id"]
    ledger_path = (
        mock_settings.cache_dir / "state" / f"ingest_{_filter_key(2020, None)}.jsonl"
    )
    led = StateLedger(ledger_path)
    assert led.records[gid]["process_status"] == "failed"
    assert led.records[gid]["last_error"] == status
    assert not led.is_done(gid), "a skipped file must be retried on the next run"
    assert [r["global_id"] for r in led.failures()] == [gid]

    c = _completion(caplog)
    assert c["process_skipped"] == 1
    assert c["ingested"] == c["empty"] == c["cached"] == 0
