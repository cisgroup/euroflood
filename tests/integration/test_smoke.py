"""End-to-End Smoke Test.

Runs all pipelines in sequence with mocked external dependencies (Internet)
but real disk I/O and real logic.
"""

import pytest

import euroflood as ef
from euroflood.pipelines.export import ExportPipeline
from euroflood.pipelines.ingestion import IngestionPipeline

pytestmark = pytest.mark.integration


def test_end_to_end_smoke(mocker, mock_settings, sample_tif_path, mock_requests_get):
    """Run Ingest -> Export (COG + Parquet dictionary) -> floods().download()."""

    # --- SETUP MOCKS ---

    # 1. Scraper Mock (Return 1 file)
    mock_requests_get.side_effect = None  # Reset side effects

    recs = [
        {
            "global_id": 1,
            "filename": "WD_MERGE_2020-01-01_cluster_1.tif",
            "year": "2020",
            "start_date": "2020-01-01",
            "cluster_id": "1",
            "download_url": "http://mock/file.tif",
        }
    ]
    mocker.patch(
        "euroflood.services.scraper.ScraperService.fetch_all_records", return_value=recs
    )

    # 2. Downloader Mock (Copy sample TIF to cache instead of download)
    def fake_download(url, fname, **_kw):
        dest = mock_settings.cache_dir / "downloads" / fname
        dest.parent.mkdir(exist_ok=True)
        # Verify sample tif is readable
        assert sample_tif_path.exists()
        dest.write_bytes(sample_tif_path.read_bytes())
        return dest

    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=fake_download,
    )

    # 3. Geocoder Mock
    from shapely.geometry import box

    # Overlap sample TIF (10.0, 50.0)
    mocker.patch(
        "euroflood.services.geocoding.GeocodingService.get_geometry",
        return_value=box(9.0, 49.0, 11.0, 51.0),
    )

    # --- EXECUTION ---

    # 1. Ingest
    ingestor = IngestionPipeline()

    # Mock Executors in the correct namespace!
    from concurrent.futures import Future

    class SyncExecutor:
        def __init__(self, *args, **kw):
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

    mocker.patch("euroflood.pipelines.ingestion.ProcessPoolExecutor", SyncExecutor)
    mocker.patch("euroflood.pipelines.ingestion.ThreadPoolExecutor", SyncExecutor)

    ingestor.run(year=2020, update=True)

    # Check parquet created
    # Note: inventory CSV saves "2020" as int usually, so run(year=2020) works
    parquet_files = list((mock_settings.cache_dir / "parquet").rglob("*.parquet"))
    assert len(parquet_files) == 1, (
        "Parquet not found. Ingestion failed to process downloaded TIF."
    )

    # 2. Export -> single sparse COG + single Parquet dictionary + manifest.
    mock_settings.index_write_chunk_rows = 2000  # bound peak memory on the full grid
    ExportPipeline().run()
    assert mock_settings.get_index_tif_path().exists()
    assert mock_settings.get_dictionary_parquet_path().is_file()

    # 3. Discover + download via the modern API (reads the COG + Parquet dictionary).
    catalogue = ef.floods("Test Place")
    assert len(catalogue) == 1
    dl = catalogue.download()  # returns the catalogue, now carrying its file

    assert len(dl.files) == 1
    output_files = list(mock_settings.output_dir.glob("*.tif"))
    assert len(output_files) == 1
    print(f"Smoke test generated: {output_files[0]}")
