"""Tests for euroflood.services.statistics (depth stats over downloaded rasters)."""

import geopandas as gpd
import pandas as pd
import pytest
from rasterio.warp import Resampling
from shapely.geometry import box

from euroflood.exceptions import ProcessingError
from euroflood.services.raster_ops import RasterOps
from euroflood.services.statistics import (
    catalogue_stats,
    catalogue_summary,
    depth_raster_stats,
    depth_scale_for,
)


def test_depth_raster_stats_native_metric(depth_tif_3035):
    # 4x4 wet patch of 2.5 m at 100 m pixels, EPSG:3035 (metric -> used natively).
    stats = depth_raster_stats(depth_tif_3035, scale=1.0)
    assert stats["wet_pixels"] == 16
    assert stats["max_depth_m"] == 2.5
    assert stats["mean_depth_m"] == 2.5
    assert stats["p95_depth_m"] == 2.5
    # 16 px * (100 m)^2 = 160_000 m^2 = 0.16 km^2
    assert stats["flooded_area_km2"] == pytest.approx(0.16)
    # volume = 16 * 2.5 m * 10_000 m^2 = 400_000 m^3
    assert stats["volume_m3"] == pytest.approx(400_000.0)
    assert stats["volume_Mm3"] == pytest.approx(0.4)


def test_depth_raster_stats_scale_cm_to_m(depth_tif_3035):
    # Default scale (0.01) treats values as centimetres: 2.5 cm -> 0.025 m.
    stats = depth_raster_stats(depth_tif_3035)  # scale=0.01
    assert stats["max_depth_m"] == pytest.approx(0.025)


def test_depth_raster_stats_max_depth_guard(depth_tif_3035):
    # The 2.5 m wet patch is dropped as suspect when max_depth < 2.5 (raster units).
    stats = depth_raster_stats(depth_tif_3035, scale=1.0, max_depth=2.0)
    assert stats["wet_pixels"] == 0
    assert stats["volume_m3"] == 0.0


def test_depth_raster_stats_geographic_reprojects(sample_tif_path):
    # EPSG:4326 (degrees) -> reprojected to an equal-area metric CRS for stats.
    stats = depth_raster_stats(sample_tif_path, scale=1.0)
    assert stats["wet_pixels"] > 0
    assert stats["max_depth_m"] == 1.0  # nearest resampling preserves the value
    assert stats["flooded_area_km2"] > 0
    assert stats["volume_m3"] > 0


def test_depth_raster_stats_reads_band_once_geographic(sample_tif_path, mocker):
    """A geographic raster is inspected via crs_of, then its band is read exactly once."""
    read_spy = mocker.spy(RasterOps, "read_array")
    crs_spy = mocker.spy(RasterOps, "crs_of")
    depth_raster_stats(sample_tif_path, scale=1.0)
    assert crs_spy.call_count == 1  # header inspected once (no band read)
    assert read_spy.call_count == 1  # and the band materialized only once


def test_depth_raster_stats_reads_band_once_metric(depth_tif_3035, mocker):
    """A metric raster skips reprojection and is likewise read only once."""
    read_spy = mocker.spy(RasterOps, "read_array")
    depth_raster_stats(depth_tif_3035, scale=1.0)
    assert read_spy.call_count == 1


def test_depth_raster_stats_all_dry_is_zero(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "dry.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=5,
        width=5,
        count=1,
        dtype="float32",
        crs="EPSG:3035",
        transform=from_origin(3_200_000.0, 2_000_000.0, 100.0, 100.0),
        nodata=0.0,
    ) as dst:
        dst.write(np.zeros((5, 5), dtype="float32"), 1)
    stats = depth_raster_stats(path)
    assert stats["wet_pixels"] == 0
    assert stats["max_depth_m"] == 0.0
    assert stats["volume_m3"] == 0.0


def _frame_with_paths(*paths):
    """A minimal catalogue-like frame carrying a ``path`` column."""
    n = len(paths)
    return gpd.GeoDataFrame(
        {
            "event_id": list(range(10, 10 + n)),
            "date": ["2020-05-01"] * n,
            "path": [str(p) for p in paths],
            "geometry": [box(0, 0, 1, 1)] * n,
        },
        geometry="geometry",
        crs="EPSG:4326",
    )


def test_catalogue_stats_one_row_per_event(depth_tif_3035):
    frame = _frame_with_paths(depth_tif_3035, depth_tif_3035)
    df = catalogue_stats(frame, scale=1.0)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 2
    assert list(df["event_id"]) == [10, 11]
    assert (df["max_depth_m"] == 2.5).all()
    assert df["flooded_area_km2"].tolist() == pytest.approx([0.16, 0.16])


def test_catalogue_summary_envelope(depth_tif_3035):
    frame = _frame_with_paths(depth_tif_3035, depth_tif_3035)
    summary = catalogue_summary(frame, scale=1.0)
    assert summary["n_events"] == 2
    assert summary["max_depth_m"] == 2.5  # overall max across the composite
    assert summary["flooded_area_km2"] > 0  # union footprint (not double-counted)


def test_catalogue_stats_skips_undownloaded_rows(depth_tif_3035):
    # A mix of downloaded / not-yet-downloaded rows -> only the downloaded ones.
    frame = _frame_with_paths(depth_tif_3035)
    frame = frame.reindex([0, 1])  # add a second, path-less row (NaN)
    frame.loc[1, "event_id"] = 11
    df = catalogue_stats(frame, scale=1.0)
    assert len(df) == 1 and df["event_id"].iloc[0] == 10


def test_catalogue_summary_single_event(depth_tif_3035):
    frame = _frame_with_paths(depth_tif_3035)
    summary = catalogue_summary(frame, scale=1.0)
    assert summary["n_events"] == 1
    assert summary["max_depth_m"] == 2.5


def test_stats_raise_without_downloads():
    # No `path` column at all.
    bare = gpd.GeoDataFrame({"event_id": [10], "geometry": [box(0, 0, 1, 1)]})
    with pytest.raises(ProcessingError, match="download"):
        catalogue_stats(bare)
    with pytest.raises(ProcessingError, match="download"):
        catalogue_summary(bare)
    # A `path` column that is entirely empty also counts as "nothing downloaded".
    empty = gpd.GeoDataFrame(
        {"event_id": [10], "path": [None], "geometry": [box(0, 0, 1, 1)]}
    )
    with pytest.raises(ProcessingError, match="download"):
        catalogue_stats(empty)


def test_read_array_nearest_preserves_values(depth_tif_3035):
    # The stats path reads with nearest resampling; check the option is honoured.
    arr, _transform, crs, _nodata = RasterOps.read_array(
        depth_tif_3035, to_crs="EPSG:4326", resampling=Resampling.nearest
    )
    assert crs.to_epsg() == 4326
    assert float(arr.max()) == 2.5  # exact value survived (no bilinear smearing)


# --- scale inferred from catalogue kind (EFAS cm vs GLOFAS m) ---------------
def _hazard_frame_with_path(depth_path):
    """A hazard-flavoured frame (return_period column) carrying a metres raster."""
    return gpd.GeoDataFrame(
        {
            "return_period": [100],
            "path": [str(depth_path)],
            "geometry": [box(0, 0, 1, 1)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )


def test_depth_scale_for_by_kind():
    hazard = gpd.GeoDataFrame({"return_period": [100], "geometry": [box(0, 0, 1, 1)]})
    historic = gpd.GeoDataFrame({"event_id": [10], "geometry": [box(0, 0, 1, 1)]})
    assert depth_scale_for(hazard) == 1.0  # GLOFAS metres
    assert depth_scale_for(historic) == 0.01  # EFAS centimetres


def test_catalogue_stats_infers_hazard_scale(depth_tif_3035):
    # depth_tif_3035 holds raw value 2.5; a hazard frame must treat it as metres.
    frame = _hazard_frame_with_path(depth_tif_3035)
    df = catalogue_stats(frame)  # scale=None -> inferred 1.0 for hazard
    assert df["max_depth_m"].iloc[0] == 2.5  # NOT 0.025
    assert df["return_period"].iloc[0] == 100  # hazard identity column
    assert "event_id" not in df.columns  # hazard rows have no event_id


def test_catalogue_stats_infers_historic_scale(depth_tif_3035):
    # The same raster under a historic frame is centimetres -> 0.025 m.
    frame = _frame_with_paths(depth_tif_3035)
    df = catalogue_stats(frame)  # scale=None -> inferred 0.01 for historic
    assert df["max_depth_m"].iloc[0] == pytest.approx(0.025)


def test_catalogue_summary_infers_hazard_scale(depth_tif_3035):
    frame = _hazard_frame_with_path(depth_tif_3035)
    summary = catalogue_summary(frame)  # inferred 1.0
    assert summary["max_depth_m"] == 2.5


def test_catalogue_stats_skips_unreadable_raster(depth_tif_3035, tmp_path):
    # One bad path must not sink the whole frame — it's logged and skipped.
    bad = tmp_path / "corrupt.tif"
    bad.write_text("not a raster")
    frame = gpd.GeoDataFrame(
        {
            "event_id": [10, 11],
            "path": [str(depth_tif_3035), str(bad)],
            "geometry": [box(0, 0, 1, 1)] * 2,
        },
        geometry="geometry",
        crs="EPSG:4326",
    )
    df = catalogue_stats(frame, scale=1.0)
    assert len(df) == 1 and df["event_id"].iloc[0] == 10


def test_catalogue_stats_applies_max_plausible_depth(depth_tif_3035, mock_settings):
    # The catalogue path reads max_plausible_depth from settings (raster units).
    mock_settings.max_plausible_depth = 2.0  # drops the 2.5 wet patch as suspect
    frame = _frame_with_paths(depth_tif_3035)
    df = catalogue_stats(frame, scale=1.0)
    assert df["wet_pixels"].iloc[0] == 0  # all wet pixels exceeded the guard
