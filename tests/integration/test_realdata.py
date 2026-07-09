"""Tests against the committed small REAL (CC-BY-4.0) data fixtures.

These exercise the true code paths that the synthetic/mock fixtures never hit:
reprojection from the real per-tile metric CRS, real centimetre depth values, a real
``crop_raster`` reprojection, the real Eurostat NUTS schema, and an end-to-end query
over a real (clipped) index. See tests/fixtures/realdata/ATTRIBUTION.md.

Run just these with ``pytest -m realdata`` (or exclude with ``-m "not realdata"``).
"""

import geopandas as gpd
import pytest
import rasterio
from rasterio.transform import array_bounds
from shapely.geometry import box

import euroflood as ef
from euroflood.exceptions import GeocodingError
from euroflood.services.geocoding import GeocodingService
from euroflood.services.raster_ops import RasterOps
from euroflood.services.statistics import catalogue_stats, depth_raster_stats

pytestmark = pytest.mark.realdata


# --- real EFAS depth (native metric CRS, centimetres) ----------------------
def test_real_depth_is_native_metric_crs(realdata_depth_path):
    with rasterio.open(realdata_depth_path) as s:
        assert not s.crs.is_geographic  # a real projected (metric) CRS, not 4326
        assert s.dtypes[0] == "uint16" and s.nodata == 0  # real EFAS shape


def test_real_depth_reprojects_to_4326(realdata_depth_path):
    arr, transform, crs, _ = RasterOps.read_array(
        realdata_depth_path, to_crs="EPSG:4326"
    )
    assert crs.to_epsg() == 4326
    w, s, e, n = array_bounds(arr.shape[0], arr.shape[1], transform)
    assert -180 <= w < e <= 180 and -90 <= s < n <= 90  # real reprojection → degrees


def test_real_depth_stats_are_plausible_metres(realdata_depth_path):
    # Real centimetre values → metres via the default 0.01 scale.
    stats = depth_raster_stats(realdata_depth_path)
    assert stats["wet_pixels"] > 0
    assert 0.0 < stats["max_depth_m"] < 20.0  # a sane real flood depth (m), not cm
    assert 0.0 < stats["mean_depth_m"] <= stats["max_depth_m"]
    assert stats["flooded_area_km2"] > 0 and stats["volume_m3"] > 0


def test_real_crop_raster_reprojects_geometry(realdata_depth_path, tmp_path):
    # A WGS84 ROI cropped against a raster in a *different* (metric) CRS exercises the
    # real project_geometry + rasterio.mask reprojection branch — mocks skip this.
    with rasterio.open(realdata_depth_path) as s:
        src_epsg = s.crs.to_epsg()
    roi = box(0.71, 40.67, 0.815, 40.75)  # overlaps the clip's 4326 footprint
    out = tmp_path / "cropped.tif"
    assert RasterOps.crop_raster(realdata_depth_path, out, roi) is True  # non-empty
    with rasterio.open(out) as s:
        assert s.crs.to_epsg() == src_epsg  # stayed in the raster's metric CRS
        assert s.width <= 384 and s.height <= 384
        assert int((s.read(1) > 0).sum()) > 0  # kept real wet pixels


def test_real_depth_frame_stats(realdata_depth_path):
    # The catalogue stats path over a real raster (scale inferred: historic cm→m).
    frame = gpd.GeoDataFrame(
        {
            "event_id": [1],
            "path": [str(realdata_depth_path)],
            "geometry": [box(0, 0, 1, 1)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )
    df = catalogue_stats(frame)
    assert len(df) == 1 and 0.0 < df["max_depth_m"].iloc[0] < 20.0


# --- real offline geocoder (Eurostat NUTS schema) --------------------------
@pytest.mark.parametrize(
    "name",
    [
        "Köln",  # exact
        "España",  # endonym
        "Deutschland",  # endonym
        "Cologne",  # exonym -> Köln
        "Germany",  # exonym -> Deutschland
        "Spain",  # exonym -> España
        "Valencia",  # bilingual "Valencia/València" resolved by the local half
        "Zutphen",  # appended OSM municipality boundary (not a NUTS region)
        "Zutphen, Netherlands",  # the comma-suffixed form the tutorials use
        "Gelderland",  # Dutch NUTS2 region (the level-filter demo)
    ],
)
def test_real_geocoder_resolves_nuts_names(realdata_nuts_path, name):
    ef.settings.geocoder_backend = "local"
    ef.settings.boundary_dataset_path = realdata_nuts_path
    geom = GeocodingService(settings=ef.settings).get_geometry(name)
    assert not geom.is_empty
    w, s, _e, _n = geom.bounds
    assert -20 < w < 20 and 27 < s < 56  # a real European extent


def test_real_geocoder_unknown_name_gives_hint(realdata_nuts_path):
    # A genuinely unknown place still misses offline, with a difflib "Did you mean".
    ef.settings.geocoder_backend = "local"
    ef.settings.boundary_dataset_path = realdata_nuts_path
    with pytest.raises(GeocodingError, match="Did you mean"):
        GeocodingService(settings=ef.settings).get_geometry("Kolnx")


# --- real end-to-end query over a clipped real index -----------------------
_ROI = (
    6.16,
    52.11,
    6.24,
    52.16,
)  # over the IJssel, within the committed Zutphen window


def test_real_index_query_returns_events(realdata_index_env):
    cat = ef.floods(bbox=_ROI)
    assert len(cat) >= 5  # real Zutphen/IJssel flood events
    row = cat.iloc[0]
    assert row["filename"].startswith("WD_MERGE_") and row["date"]
    assert cat["year"].min() >= 2015


def test_real_index_footprints_have_area(realdata_index_env):
    cat = ef.floods(bbox=_ROI)
    fp = cat.footprints()
    assert len(fp) == len(cat)
    assert (fp["extent_km2"] > 0).any()


def test_real_index_recurrence_renders(realdata_index_env):
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg", force=True)
    ax = ef.floods(bbox=_ROI).plot()  # real recurrence heatmap over real combos
    assert ax.get_images()


def test_real_depth_renders(realdata_depth_path):
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg", force=True)
    ax = ef.plot_depth(realdata_depth_path)
    assert "depth" in ax.get_title().lower()
