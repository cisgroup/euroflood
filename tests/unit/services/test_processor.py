"""Tests for RasterProcessor (TIF -> Parquet)."""

from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import rasterio
import rasterio.crs
from rasterio.transform import from_origin

from euroflood.exceptions import ProcessingError
from euroflood.services.processor import RasterProcessor


def test_process_valid_tif(sample_tif_path, mock_settings):
    """Test processing a valid TIF creates a parquet file."""
    processor = RasterProcessor()
    count = processor.process(sample_tif_path, global_id=99, year="2020")

    assert count > 0

    expected_path = (
        mock_settings.cache_dir / "parquet" / "2020" / "test_flood_id99.parquet"
    )
    assert expected_path.exists()

    df = pd.read_parquet(expected_path)
    assert df["flood_id"].iloc[0] == 99


def test_process_missing_file():
    """Test gracefull handling of missing input."""
    processor = RasterProcessor()
    count = processor.process(Path("ghost.tif"), 1, "2020")
    assert count == 0


def test_process_cache_hit(sample_tif_path, mock_settings):
    """Test that existing parquet files are not re-processed."""
    processor = RasterProcessor()
    year_dir = mock_settings.cache_dir / "parquet" / "2020"
    year_dir.mkdir(parents=True)
    parquet_path = year_dir / "test_flood_id1.parquet"
    parquet_path.touch()

    count = processor.process(sample_tif_path, global_id=1, year="2020")
    assert count == 0


def test_process_empty_data(mocker, sample_tif_path):
    """Test processing a TIF with no valid pixels (all 0 or NoData)."""
    processor = RasterProcessor()

    with rasterio.open(sample_tif_path) as src:
        profile = src.profile

    mock_src = mocker.MagicMock()
    mock_src.read.return_value = np.zeros((10, 10), dtype=np.uint8)
    mock_src.width = 10
    mock_src.height = 10
    mock_src.nodata = None
    mock_src.profile = profile
    mock_src.__enter__.return_value = mock_src

    mocker.patch("rasterio.open", return_value=mock_src)

    count = processor.process(sample_tif_path, 1, "2020")
    assert count == 0


def test_process_reprojection_path(mocker, sample_tif_path):
    """Test that non-WGS84 CRSs trigger the transform logic."""
    processor = RasterProcessor()

    data = np.zeros((10, 10), dtype=np.uint8)
    data[0, 0] = 1

    mock_src = mocker.MagicMock()
    mock_src.read.return_value = data
    mock_src.width = 10
    mock_src.height = 10
    mock_src.nodata = 0
    mock_src.crs = rasterio.crs.CRS.from_epsg(3857)
    mock_src.transform = rasterio.transform.from_origin(0, 0, 1, 1)
    mock_src.__enter__.return_value = mock_src

    mocker.patch("rasterio.open", return_value=mock_src)

    mock_transform = mocker.patch("euroflood.services.processor.transform")
    mock_transform.return_value = ([10.0], [50.0])

    mocker.patch(
        "euroflood.services.processor.GlobalGrid.latlon_to_grid",
        return_value=(np.array([1]), np.array([1])),
    )
    mocker.patch(
        "euroflood.services.processor.GlobalGrid.is_valid",
        return_value=np.array([True]),
    )

    count = processor.process(sample_tif_path, 1, "2020")

    mock_transform.assert_called_once()
    assert count == 1


def _mock_src(mocker, data, *, nodata, crs):
    src = mocker.MagicMock()
    src.read.return_value = data
    src.height, src.width = data.shape
    src.nodata = nodata
    src.crs = crs
    src.transform = rasterio.transform.from_origin(10.0, 50.0, 0.01, 0.01)
    src.__enter__.return_value = src
    mocker.patch("rasterio.open", return_value=src)
    return src


def test_process_missing_crs_is_skipped(mocker, sample_tif_path):
    """A raster without a CRS is skipped, not silently treated as WGS84."""
    data = np.zeros((10, 10), dtype=np.uint8)
    data[0, 0] = 1
    _mock_src(mocker, data, nodata=0, crs=None)

    assert RasterProcessor().process(sample_tif_path, 1, "2020") == 0


def test_process_keeps_values_when_nodata_absent(mocker, sample_tif_path):
    """With no declared NoData, a 9999 value is kept (no fabricated sentinel)."""
    data = np.zeros((10, 10), dtype=np.int32)
    data[0, 0] = 9999  # the old hardcoded 9999 fallback would have dropped this
    _mock_src(mocker, data, nodata=None, crs=rasterio.crs.CRS.from_epsg(4326))

    assert RasterProcessor().process(sample_tif_path, 1, "2020") == 1


def test_process_flood_id_uint32_no_overflow(sample_tif_path, mock_settings):
    """flood_id is uint32: a global_id above 65535 is stored without wrapping."""
    count = RasterProcessor().process(sample_tif_path, global_id=70000, year="2020")
    assert count > 0

    parquet = (
        mock_settings.cache_dir / "parquet" / "2020" / "test_flood_id70000.parquet"
    )
    df = pd.read_parquet(parquet)
    assert df["flood_id"].iloc[0] == 70000  # not 70000 % 65536 == 4464
    assert str(df["flood_id"].dtype) == "uint32"


def test_process_exception_handling(mocker, sample_tif_path):
    """Test that exceptions during processing are caught and re-raised."""
    processor = RasterProcessor()
    mocker.patch("rasterio.open", side_effect=Exception("Corrupt TIF"))

    with pytest.raises(ProcessingError, match="Failed to process"):
        processor.process(sample_tif_path, 1, "2020")


def test_process_points_outside_grid(mocker, sample_tif_path):
    """Test points that are valid in TIF but invalid in Grid."""
    processor = RasterProcessor()

    # 1. Mock GlobalGrid.is_valid to always return False (all points out of bounds)
    # The sample_tif from conftest has 10 valid pixels (diagonal of 10x10 grid).
    # So we need a mask of length 10.
    mocker.patch(
        "euroflood.services.processor.GlobalGrid.is_valid",
        return_value=np.full(10, False),
    )

    # 2. Run
    # sample_tif_path has valid points, but we force them to be "invalid" via the mock
    count = processor.process(sample_tif_path, 1, "2020")

    # 3. Assert 0 points processed
    assert count == 0


def test_process_dedups_grid_cells(mocker, sample_tif_path):
    """Source pixels that fall in the same grid cell collapse to one row."""
    # sample_tif has 10 wet diagonal pixels; force them all to one grid cell.
    mocker.patch(
        "euroflood.services.processor.GlobalGrid.latlon_to_grid",
        return_value=(np.full(10, 100, dtype="int32"), np.full(10, 200, dtype="int32")),
    )
    mocker.patch(
        "euroflood.services.processor.GlobalGrid.is_valid",
        return_value=np.full(10, True),
    )

    count = RasterProcessor().process(sample_tif_path, 1, "2020")
    assert count == 1  # 10 source pixels, one cell -> a single de-duplicated row


def test_process_equi7_real_crs(tmp_path, mock_settings):
    """A tile in the real Equi7 Europe CRS (EPSG:27704) reprojects and maps to the grid."""
    data = np.zeros((10, 10), dtype=np.uint16)
    data[5, 5] = 150  # 1.5 m depth (centimetres), one wet pixel
    tif = tmp_path / "equi7.tif"
    with rasterio.open(
        tif,
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="uint16",
        crs=rasterio.crs.CRS.from_epsg(27704),  # WGS 84 / Equi7 Europe (metres)
        transform=from_origin(7_950_000, 1_060_000, 20, 20),
        nodata=0,
    ) as dst:
        dst.write(data, 1)

    count = RasterProcessor().process(tif, global_id=5, year="2024")
    assert count == 1  # reprojected from Equi7 -> inside the grid -> one cell

    df = pd.read_parquet(
        mock_settings.cache_dir / "parquet" / "2024" / "equi7_id5.parquet"
    )
    assert str(df["flood_id"].dtype) == "uint32"


def test_processor_runs_in_real_process_pool(sample_tif_path, mock_settings):
    """RasterProcessor.process is picklable and runs in a real ProcessPoolExecutor.

    Other ingestion tests swap in a synchronous executor; this exercises the
    actual CPU (process) path and confirms the injected settings travel to the
    worker via pickling (the 1G dependency-injection change), so the worker
    writes parquet into the sandboxed cache dir.
    """
    processor = RasterProcessor()

    with ProcessPoolExecutor(max_workers=1) as pool:
        future = pool.submit(processor.process, sample_tif_path, 7, "2021")
        count = future.result(timeout=120)

    assert count > 0
    parquet_dir = mock_settings.cache_dir / "parquet" / "2021"
    assert any(parquet_dir.glob("*.parquet"))


def test_process_global_id_overflow_raises(sample_tif_path):
    """A global_id above the uint32 max is rejected up front."""
    processor = RasterProcessor()
    too_big = int(np.iinfo("uint32").max) + 1
    with pytest.raises(ProcessingError, match="exceeds the uint32 range"):
        processor.process(sample_tif_path, global_id=too_big, year="2020")


def test_process_global_id_at_uint32_max_is_allowed(sample_tif_path, mock_settings):
    """Exactly uint32 max is the inclusive boundary and must still process."""
    gid = int(np.iinfo("uint32").max)
    count = RasterProcessor().process(sample_tif_path, global_id=gid, year="2020")
    assert count > 0
    df = pd.read_parquet(
        mock_settings.cache_dir / "parquet" / "2020" / f"test_flood_id{gid}.parquet"
    )
    assert df["flood_id"].iloc[0] == gid


def test_process_max_plausible_depth_filters_deep_pixels(
    mocker, sample_tif_path, mock_settings
):
    """An implausibly deep pixel above max_plausible_depth is dropped."""
    mock_settings.max_plausible_depth = 5.0
    data = np.zeros((10, 10), dtype=np.float32)
    data[2, 2] = 3.0
    data[7, 7] = 9999.0
    _mock_src(mocker, data, nodata=-1.0, crs=rasterio.crs.CRS.from_epsg(4326))
    count = RasterProcessor().process(sample_tif_path, global_id=1, year="2020")
    assert count == 1


def test_process_max_plausible_depth_all_filtered_returns_zero(
    mocker, sample_tif_path, mock_settings
):
    """If every wet pixel exceeds the depth cap, nothing is written."""
    mock_settings.max_plausible_depth = 5.0
    data = np.zeros((10, 10), dtype=np.float32)
    data[3, 3] = 50.0
    _mock_src(mocker, data, nodata=-1.0, crs=rasterio.crs.CRS.from_epsg(4326))
    assert RasterProcessor().process(sample_tif_path, global_id=1, year="2020") == 0
