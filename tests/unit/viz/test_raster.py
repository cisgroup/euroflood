"""Tests for euroflood.viz._raster (recurrence grid/regions; depth reader).

These exercise only core deps (numpy/rasterio/geopandas) — no matplotlib/folium.
"""

import pytest

from euroflood.exceptions import VisualizationError
from euroflood.pipelines.discovery import _make_frame
from euroflood.viz._raster import (
    depth_array,
    event_footprints,
    recurrence_grid,
    recurrence_regions,
)


def test_recurrence_grid_counts_from_index(historic_frame):
    counts, _transform, _crs, _roi = recurrence_grid(historic_frame)
    # combo 1 -> 1 recorded flood; only the flooded (diagonal) pixels are > 0.
    assert counts.max() == 1.0
    assert (counts > 0).any()


def test_recurrence_grid_event_membership(historic_frame):
    hit, *_ = recurrence_grid(historic_frame, event_id=10)
    miss, *_ = recurrence_grid(historic_frame, event_id=999)
    assert hit.max() == 1.0  # event 10 is present
    assert miss.max() == 0.0  # event 999 is not


def test_recurrence_regions_have_tooltip_fields(historic_frame):
    gdf = recurrence_regions(historic_frame)
    assert {"count", "events", "geometry"} <= set(gdf.columns)
    assert len(gdf) >= 1
    assert (gdf["count"] >= 1).all()
    assert str(gdf.crs) == "EPSG:4326"


def test_recurrence_regions_event_filter_empty(historic_frame):
    gdf = recurrence_regions(historic_frame, event_id=999)
    assert len(gdf) == 0  # no region contains event 999


def test_recurrence_empty_frame_raises(mock_settings):
    with pytest.raises(VisualizationError, match="Empty"):
        recurrence_grid(_make_frame([], mock_settings))


def test_recurrence_hazard_frame_raises(hazard_frame):
    with pytest.raises(VisualizationError, match="historic-only"):
        recurrence_grid(hazard_frame)


def test_depth_array_reads_georeferencing(sample_tif_path):
    array, _transform, crs, nodata = depth_array(sample_tif_path)
    assert array.shape == (10, 10)
    assert crs.to_epsg() == 4326
    assert nodata == 0


def test_recurrence_roi_outside_index_raises(historic_frame):
    from shapely.geometry import box

    far = _make_frame(
        [{**historic_frame.iloc[0].to_dict(), "geometry": box(0, 0, 0.01, 0.01)}],
        historic_frame._settings,
    )
    with pytest.raises(VisualizationError, match="overlap"):
        recurrence_grid(far)


def test_remap_empty_returns_zeros():
    import numpy as np

    from euroflood.viz._raster import _remap

    combo = np.array([[0, 1], [2, 0]], dtype="uint32")
    assert (_remap(combo, {}) == 0).all()


def test_event_footprints_per_event_geometry(two_event_frame):
    gdf = event_footprints(two_event_frame)
    assert len(gdf) == 2
    assert set(gdf["event_id"]) == {10, 11}
    assert (gdf["extent_km2"] > 0).all()
    assert str(gdf.crs) == "EPSG:4326"
    g10 = gdf[gdf["event_id"] == 10].geometry.iloc[0]
    g11 = gdf[gdf["event_id"] == 11].geometry.iloc[0]
    assert not g10.is_empty and not g11.is_empty
    assert not g10.equals(g11)  # top half vs bottom half


def test_event_footprints_single(historic_frame):
    gdf = event_footprints(historic_frame)
    assert len(gdf) == 1
    assert gdf["extent_km2"].iloc[0] > 0


def test_event_footprints_empty_raises(mock_settings):
    with pytest.raises(VisualizationError):
        event_footprints(_make_frame([], mock_settings))


def test_event_footprints_hazard_raises(hazard_frame):
    with pytest.raises(VisualizationError, match="historic-only"):
        event_footprints(hazard_frame)


def test_read_array_reprojects_to_4326(depth_tif_3035):
    from rasterio.transform import array_bounds

    from euroflood.services.raster_ops import RasterOps

    arr, transform, crs, _nodata = RasterOps.read_array(
        depth_tif_3035, to_crs="EPSG:4326"
    )
    assert crs.to_epsg() == 4326  # reprojected from 3035
    west, south, east, north = array_bounds(arr.shape[0], arr.shape[1], transform)
    assert -180 <= west < east <= 180  # degrees, not metres
    assert -90 <= south < north <= 90


def test_event_footprints_sorted_with_duration(two_event_frame):
    gdf = event_footprints(two_event_frame)
    assert list(gdf["date"]) == sorted(gdf["date"])  # date-sorted
    assert "duration_days" in gdf.columns
    assert (gdf["duration_days"] > 0).all()  # 8 and 4 days


def test_recurrence_regions_hover_fields(multicount_frame):
    gdf = recurrence_regions(multicount_frame)
    assert {"count", "when", "events"} <= set(gdf.columns)
    assert (gdf["count"] == 3).any()  # the 3-event combo region


def test_depth_composite_takes_max(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    from euroflood.viz._raster import depth_composite

    transform = from_origin(0.0, 1.0, 0.1, 0.1)  # EPSG:4326

    def _write(name, value):
        path = tmp_path / name
        data = np.zeros((10, 10), dtype="float32")
        data[3:7, 3:7] = value  # overlapping wet patch
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=10,
            width=10,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=transform,
            nodata=0.0,
        ) as dst:
            dst.write(data, 1)
        return path

    out = depth_composite(
        [_write("a.tif", 1.0), _write("b.tif", 3.0)], tmp_path / "comp.tif"
    )
    with rasterio.open(out) as src:
        assert src.crs.to_epsg() == 4326
        assert float(src.read(1).max()) == 3.0  # per-pixel maximum
