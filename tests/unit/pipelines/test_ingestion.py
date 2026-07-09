"""Tests for the Ingestion Pipeline orchestrator."""

import json
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd

from euroflood.pipelines.ingestion import IngestionPipeline, _filter_key
from euroflood.pipelines.ledger import StateLedger


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
    pipe.processor.process = MagicMock(return_value=100)

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
    pipe.processor.process = MagicMock(return_value=10)

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
    pipe.processor.process = MagicMock(return_value=5)

    pipe.run(year=2020)

    downloaded = {c.args[1] for c in pipe.downloader.download_file.call_args_list}
    assert downloaded == {"f1.tif", "f2.tif"}
    assert pipe.processor.process.call_count == 2
