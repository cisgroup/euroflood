"""Tests for spatial raster operations."""

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from euroflood.services.raster_ops import RasterOps


def test_crop_raster(sample_tif_path, tmp_path):
    """Test cropping a raster to a bounding box."""
    # sample tif covers roughly 10E-10.1E, 50N-49.9N
    crop_geom = box(10.02, 49.92, 10.08, 49.98)
    out_path = tmp_path / "cropped.tif"

    success = RasterOps.crop_raster(sample_tif_path, out_path, crop_geom)

    assert success is True
    assert out_path.exists()


def test_crop_raster_bbox_only(mocker, sample_tif_path, tmp_path):
    """Test cropping to bounding box (crop_to_poly=False) - Line 70."""
    crop_geom = box(10.02, 49.92, 10.08, 49.98)
    out_path = tmp_path / "cropped_bbox.tif"

    # Spy on from_bounds to ensure we hit the else block
    # Note: from_bounds is imported in the module, so we must patch it there
    mock_from_bounds = mocker.patch(
        "euroflood.services.raster_ops.from_bounds", wraps=rasterio.windows.from_bounds
    )

    success = RasterOps.crop_raster(
        sample_tif_path, out_path, crop_geom, crop_to_poly=False
    )

    assert success is True
    assert out_path.exists()
    mock_from_bounds.assert_called()


def test_crop_raster_no_overlap(sample_tif_path, tmp_path):
    """Test cropping with non-overlapping geometry."""
    crop_geom = box(0, 0, 1, 1)
    out_path = tmp_path / "empty.tif"

    success = RasterOps.crop_raster(sample_tif_path, out_path, crop_geom)

    assert success is False
    assert not out_path.exists()


def test_crop_raster_result_empty(mocker, sample_tif_path, tmp_path):
    """Test cropping where geometry overlaps but all pixels are NoData (Lines 77-80)."""
    crop_geom = box(10.0, 49.9, 10.1, 50.0)
    out_path = tmp_path / "nodata.tif"

    # Mock rasterio.mask.mask to return an array of zeros (assuming nodata is 0)
    mock_mask = mocker.patch("rasterio.mask.mask")
    # Return (data, transform)
    mock_mask.return_value = (np.zeros((1, 10, 10), dtype=np.uint8), mocker.Mock())

    success = RasterOps.crop_raster(sample_tif_path, out_path, crop_geom)

    assert success is False
    assert not out_path.exists()


def test_crop_profile_pop(mocker, sample_tif_path, tmp_path):
    """Test that conflicting keys are popped from profile (Line 86)."""
    crop_geom = box(10.02, 49.92, 10.08, 49.98)
    out_path = tmp_path / "popped.tif"

    # We spy on rasterio.open('w', ...) to check the kwargs
    spy_open = mocker.spy(rasterio, "open")

    RasterOps.crop_raster(sample_tif_path, out_path, crop_geom)

    # Get the calls. The second call to open is the write ('w')
    args, kwargs = spy_open.call_args_list[1]

    # "w" is passed as the second positional argument (index 1)
    assert args[1] == "w"
    assert "blockxsize" not in kwargs
    assert "blockysize" not in kwargs


def _float_tile(path, minx, miny, maxx, maxy, *, value=2.0, res=0.1, nodata=-9999.0):
    """A small float32 EPSG:4326 tile (GLOFAS-shaped) for mosaic tests."""
    width = round((maxx - minx) / res)
    height = round((maxy - miny) / res)
    data = np.full((height, width), value, dtype=np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(minx, maxy, res, res),
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return path


def test_mosaic_and_crop_two_tiles(tmp_path):
    """Two abutting tiles merge into one ROI-cropped output (no nodata seam)."""
    a = _float_tile(tmp_path / "a.tif", 10.0, 50.0, 11.0, 51.0)
    b = _float_tile(tmp_path / "b.tif", 11.0, 50.0, 12.0, 51.0)
    out = tmp_path / "mosaic.tif"

    ok = RasterOps.mosaic_and_crop(
        [a, b], out, box(10.5, 50.2, 11.5, 50.8), nodata=-9999.0
    )

    assert ok is True
    with rasterio.open(out) as src:
        assert src.bounds.left < 11.0 < src.bounds.right  # spans the seam
        assert (src.read(1) == 2.0).any()


def test_mosaic_and_crop_bbox_only_keeps_window(tmp_path):
    """crop_to_poly=False keeps the bbox window without polygon masking."""
    a = _float_tile(tmp_path / "a.tif", 10.0, 50.0, 11.0, 51.0)
    out = tmp_path / "bbox.tif"

    ok = RasterOps.mosaic_and_crop(
        [a], out, box(10.2, 50.2, 10.8, 50.8), nodata=-9999.0, crop_to_poly=False
    )

    assert ok is True
    with rasterio.open(out) as src:
        assert not (src.read(1) == -9999.0).any()  # no polygon mask applied


def test_mosaic_and_crop_no_overlap_returns_false(tmp_path):
    """An ROI disjoint from the tile yields an all-NoData result -> False."""
    a = _float_tile(tmp_path / "a.tif", 10.0, 50.0, 11.0, 51.0)
    out = tmp_path / "empty.tif"

    ok = RasterOps.mosaic_and_crop(
        [a], out, box(30.0, 30.0, 31.0, 31.0), nodata=-9999.0
    )

    assert ok is False
    assert not out.exists()


def test_mosaic_and_crop_empty_sources_returns_false(tmp_path):
    """No sources -> False (defensive)."""
    assert RasterOps.mosaic_and_crop([], tmp_path / "x.tif", box(0, 0, 1, 1)) is False


def _crsless_tif(path, *, value=1, nodata=0):
    """A uint8 raster with NO CRS (crs=None) for the CRS-less crop branch."""
    data = np.zeros((10, 10), dtype=np.uint8)
    data[1:9, 1:9] = value
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="uint8",
        transform=from_origin(0.0, 10.0, 1.0, 1.0),
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return path


def test_crop_raster_crsless_source_uses_geometry_as_is(tmp_path):
    """A source without a CRS skips reprojection and crops with the raw geometry."""
    src = _crsless_tif(tmp_path / "crsless.tif")
    out = tmp_path / "crsless_out.tif"
    ok = RasterOps.crop_raster(src, out, box(2, 2, 8, 8))
    assert ok is True
    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.crs is None
        assert (ds.read(1) == 1).any()


def test_mosaic_and_crop_value_error_returns_false_and_closes(mocker, tmp_path):
    """A ValueError inside merge is swallowed -> False, and sources are closed."""
    a = _float_tile(tmp_path / "a.tif", 10.0, 50.0, 11.0, 51.0)
    out = tmp_path / "boom.tif"
    mocker.patch(
        "rasterio.merge.merge", side_effect=ValueError("no overlap between bounds")
    )
    real_open = rasterio.open
    closed = {"count": 0}
    orig_close = rasterio.io.DatasetReader.close

    def _counting_close(self):
        closed["count"] += 1
        return orig_close(self)

    mocker.patch.object(rasterio.io.DatasetReader, "close", _counting_close)
    ok = RasterOps.mosaic_and_crop(
        [a], out, box(10.2, 50.2, 10.8, 50.8), nodata=-9999.0
    )
    assert ok is False
    assert not out.exists()
    assert closed["count"] >= 1
    assert real_open


def test_mosaic_and_crop_embeds_tags_and_leaves_no_temp(tmp_path):
    """Optional tags land as embedded GeoTIFF metadata; the atomic .part is cleaned up."""
    a = _float_tile(tmp_path / "a.tif", 10.0, 50.0, 11.0, 51.0)
    out = tmp_path / "tagged.tif"
    ok = RasterOps.mosaic_and_crop(
        [a],
        out,
        box(10.2, 50.2, 10.8, 50.8),
        nodata=-9999.0,
        tags={"EUROFLOOD_SOURCE_TILES": "a.tif", "EUROFLOOD_N_SOURCE_TILES": "1"},
    )
    assert ok is True
    assert not (tmp_path / "tagged.tif.part").exists()  # temp renamed away
    with rasterio.open(out) as src:
        assert src.tags()["EUROFLOOD_SOURCE_TILES"] == "a.tif"
        assert src.tags()["EUROFLOOD_N_SOURCE_TILES"] == "1"


def test_crop_raster_atomic_write_embeds_tags(sample_tif_path, tmp_path):
    """crop_raster writes atomically (no leftover .part) and embeds provenance tags."""
    out = tmp_path / "crop.tif"
    ok = RasterOps.crop_raster(
        sample_tif_path, out, box(10.02, 49.92, 10.08, 49.98), tags={"K": "v"}
    )
    assert ok is True
    assert not (tmp_path / "crop.tif.part").exists()
    with rasterio.open(out) as src:
        assert src.tags()["K"] == "v"


def test_mosaic_and_crop_no_nodata_writes_window(tmp_path):
    """nodata=None skips the polygon mask and the all-nodata check; data is written."""
    a = _float_tile(tmp_path / "a.tif", 10.0, 50.0, 11.0, 51.0, nodata=None)
    out = tmp_path / "nonodata.tif"
    ok = RasterOps.mosaic_and_crop([a], out, box(10.2, 50.2, 10.8, 50.8), nodata=None)
    assert ok is True
    with rasterio.open(out) as ds:
        assert (ds.read(1) == 2.0).any()


def test_crs_of_reads_header_without_reading_band(sample_tif_path, mocker):
    """crs_of returns the CRS from the header and never materializes a band."""
    read_spy = mocker.spy(rasterio.io.DatasetReader, "read")
    crs = RasterOps.crs_of(sample_tif_path)
    assert crs.to_epsg() == 4326
    read_spy.assert_not_called()  # no band read, just the header


def test_transformer_is_cached_per_crs_pair():
    """project_geometry's Transformer is memoized on the (from_crs, to_crs) pair."""
    from pyproj import CRS

    from euroflood.services.raster_ops import _transformer

    _transformer.cache_clear()
    t1 = _transformer(CRS("EPSG:4326"), CRS("EPSG:3857"))
    t2 = _transformer(CRS("EPSG:4326"), CRS("EPSG:3857"))
    assert t1 is t2  # same cached Transformer instance
    assert _transformer(CRS("EPSG:4326"), CRS("EPSG:6933")) is not t1
