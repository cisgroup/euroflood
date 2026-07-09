"""Tests for the download-only MirrorPipeline."""

import json
from pathlib import Path

import pandas as pd
import pytest

from euroflood.pipelines.ingestion import _filter_key
from euroflood.pipelines.ledger import StateLedger
from euroflood.pipelines.mirror import MirrorPipeline


@pytest.fixture
def inventory_csv(mock_settings):
    """A 2-row inventory with file sizes (for --verify)."""
    pd.DataFrame(
        [
            {
                "global_id": 1,
                "filename": "a.tif",
                "year": "2020",
                "start_date": "2020-01-01",
                "end_date": None,
                "cluster_id": "1",
                "download_url": "http://m/a.tif",
                "size_bytes": 100,
            },
            {
                "global_id": 2,
                "filename": "b.tif",
                "year": "2021",
                "start_date": "2021-01-01",
                "end_date": None,
                "cluster_id": "2",
                "download_url": "http://m/b.tif",
                "size_bytes": 200,
            },
        ]
    ).to_csv(mock_settings.get_inventory_path(), index=False)
    return mock_settings


def _patch_dl(mocker, return_value=Path("x.tif")):
    return mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=return_value,
    )


def test_mirror_downloads_all(inventory_csv, mocker):
    dl = _patch_dl(mocker)
    assert MirrorPipeline().run() == 2
    assert dl.call_count == 2


def test_mirror_year_filter(inventory_csv, mocker):
    dl = _patch_dl(mocker)
    assert MirrorPipeline().run(year=2020) == 1
    assert dl.call_count == 1


def test_mirror_limit(inventory_csv, mocker):
    dl = _patch_dl(mocker)
    assert MirrorPipeline().run(limit=1) == 1
    assert dl.call_count == 1


def test_mirror_plan(inventory_csv):
    p = MirrorPipeline().plan()
    assert p["tiles"] == 2
    assert p["to_download"] == 2
    assert p["present"] == 0
    assert p["total_gb"] == round((100 + 200) / 1e9, 2)


def test_mirror_plan_limit(inventory_csv):
    assert MirrorPipeline().plan(limit=1)["tiles"] == 1


def test_mirror_plan_no_inventory(mock_settings):
    p = MirrorPipeline().plan()
    assert p["tiles"] == 0
    assert "note" in p


def test_mirror_verify_forwards_expected_size(inventory_csv, mocker):
    dl = _patch_dl(mocker)
    MirrorPipeline().run(verify=True)
    sizes = {call.kwargs.get("expected_size") for call in dl.call_args_list}
    assert sizes == {100, 200}


def test_mirror_no_verify_omits_expected_size(inventory_csv, mocker):
    dl = _patch_dl(mocker)
    MirrorPipeline().run()
    assert all(call.kwargs.get("expected_size") is None for call in dl.call_args_list)


def test_mirror_records_failures_and_retry(inventory_csv, mocker):
    # First run: every download fails -> dead-letter written, count 0.
    _patch_dl(mocker, return_value=None)
    assert MirrorPipeline().run() == 0
    report = inventory_csv.cache_dir / "state" / "failed_mirror_all.json"
    assert report.exists()
    assert len(json.loads(report.read_text())) == 2

    # retry-failed: re-attempts only the 2 failed ids, now succeeding.
    dl = _patch_dl(mocker)
    assert MirrorPipeline().run(retry_failed=True) == 2
    assert dl.call_count == 2


def test_mirror_update_scrapes_inventory(mock_settings, mocker):
    recs = [
        {
            "global_id": 1,
            "filename": "a.tif",
            "year": "2020",
            "start_date": "2020-01-01",
            "cluster_id": "1",
            "download_url": "http://m/a.tif",
            "size_bytes": 50,
        }
    ]
    mocker.patch(
        "euroflood.services.scraper.ScraperService.fetch_all_records", return_value=recs
    )
    _patch_dl(mocker)
    assert MirrorPipeline().run(update=True) == 1
    assert mock_settings.get_inventory_path().exists()


def test_mirror_plan_year_filter(inventory_csv):
    """plan(year=) narrows the tile set and totals only that year's bytes."""
    p = MirrorPipeline().plan(year=2020)
    assert p["tiles"] == 1
    assert p["to_download"] == 1
    assert p["total_gb"] == round(100 / 1e9, 2)


def test_mirror_plan_counts_present_files(inventory_csv, mocker):
    """plan() counts an already-downloaded, non-empty cached file as present."""
    pipe = MirrorPipeline()
    (pipe.downloader.download_dir / "a.tif").write_bytes(b"cached-data")
    p = pipe.plan()
    assert p["present"] == 1
    assert p["to_download"] == 1


def test_mirror_plan_handles_missing_size(mock_settings):
    """A NaN/absent size_bytes is skipped in the byte total."""
    pd.DataFrame(
        [
            {
                "global_id": 1,
                "filename": "a.tif",
                "year": "2020",
                "start_date": "2020-01-01",
                "end_date": None,
                "cluster_id": "1",
                "download_url": "http://m/a.tif",
                "size_bytes": None,  # -> NaN after CSV round-trip
            }
        ]
    ).to_csv(mock_settings.get_inventory_path(), index=False)
    p = MirrorPipeline().plan()
    assert p["tiles"] == 1
    assert p["total_gb"] == 0.0  # NaN size contributed nothing


def test_mirror_run_empty_inventory(mock_settings, mocker):
    """An empty (but existing) inventory short-circuits run() to 0 (lines 110-111)."""
    pd.DataFrame(
        columns=[
            "global_id",
            "filename",
            "year",
            "start_date",
            "end_date",
            "cluster_id",
            "download_url",
            "size_bytes",
        ]
    ).to_csv(mock_settings.get_inventory_path(), index=False)
    dl = _patch_dl(mocker)
    assert MirrorPipeline().run() == 0
    dl.assert_not_called()


def test_mirror_retry_failed_nothing_to_retry(inventory_csv, mocker, caplog):
    """retry_failed with an empty/clean ledger logs nothing_to_retry, count 0 (123-124)."""
    dl = _patch_dl(mocker)
    assert MirrorPipeline().run(retry_failed=True) == 0
    dl.assert_not_called()
    assert "nothing_to_retry" in caplog.text


def test_mirror_verify_redownloads_wrong_size(inventory_csv, mocker):
    """--verify re-downloads a cached file whose on-disk size != the listed size.

    Robustness: a truncated/corrupt cache file must be replaced, not trusted.
    """
    pipe = MirrorPipeline()
    cached = pipe.downloader.download_dir / "a.tif"
    cached.write_bytes(b"x" * 10)  # listed size is 100 -> mismatch

    captured = {}

    def fake_download(url, filename, expected_size=None):
        captured[filename] = expected_size
        path = pipe.downloader.download_dir / filename
        path.write_bytes(b"y" * (expected_size or 0))
        return path

    mocker.patch.object(pipe.downloader, "download_file", side_effect=fake_download)
    assert pipe.run(verify=True) == 2
    assert captured["a.tif"] == 100
    assert captured["b.tif"] == 200


def test_mirror_resume_via_ledger(inventory_csv, mocker):
    """Kill-and-resume: a re-run still re-attempts every tile and re-records state.

    The mirror ledger is download-only (no process_status), so is_done() is never
    True for it; a resumed run simply re-confirms the cache, idempotently.
    """

    # First (partial) run: only tile 1 succeeds, tile 2 "crashes" (returns None).
    def first(url, filename, expected_size=None):
        return Path(filename) if filename == "a.tif" else None

    dl1 = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=first,
    )
    assert MirrorPipeline().run() == 1
    assert dl1.call_count == 2

    ledger_path = (
        inventory_csv.cache_dir / "state" / f"mirror_{_filter_key(None, None)}.jsonl"
    )
    led = StateLedger(ledger_path)
    assert {r["global_id"] for r in led.failures()} == {2}

    dl2 = _patch_dl(mocker)
    assert MirrorPipeline().run(retry_failed=True) == 1
    assert dl2.call_count == 1
