"""Tests for LocationResolver (fully offline; geocoder mocked)."""

import geopandas as gpd
import pytest
from shapely.geometry import Polygon, box

from euroflood.exceptions import GeocodingError
from euroflood.services.location import LocationResolver


@pytest.fixture
def resolver(mock_settings, mocker):
    """A resolver with a mocked geocoder so place names need no dataset/network."""
    geocoder = mocker.Mock()
    geocoder.get_geometry.return_value = box(6.0, 50.0, 7.0, 51.0)
    return LocationResolver(settings=mock_settings, geocoder=geocoder)


def test_resolve_bbox(resolver):
    geom = resolver.resolve(bbox=(6.0, 50.0, 7.0, 51.0))
    assert geom.bounds == pytest.approx((6.0, 50.0, 7.0, 51.0))


def test_resolve_place_uses_geocoder(resolver):
    geom = resolver.resolve("Cologne")
    resolver.geocoder.get_geometry.assert_called_once_with("Cologne", level=None)
    assert geom.bounds == pytest.approx((6.0, 50.0, 7.0, 51.0))


def test_resolve_geometry_passthrough(resolver):
    poly = Polygon([(0, 0), (1, 0), (1, 1)])
    assert resolver.resolve(poly) is poly


def test_resolve_point_radius_buffers(resolver):
    geom = resolver.resolve(point=(50.94, 6.96), radius_m=5000)
    assert geom.geom_type in ("Polygon", "MultiPolygon")
    minx, miny, maxx, maxy = geom.bounds
    assert minx < 6.96 < maxx and miny < 50.94 < maxy


def test_resolve_geodataframe(resolver):
    gdf = gpd.GeoDataFrame(geometry=[box(1, 1, 2, 2)], crs="EPSG:4326")
    assert resolver.resolve(gdf).bounds == pytest.approx((1, 1, 2, 2))


def test_resolve_shapefile(resolver, tmp_path):
    gdf = gpd.GeoDataFrame(geometry=[box(3, 3, 4, 4)], crs="EPSG:4326")
    path = tmp_path / "roi.geojson"
    gdf.to_file(path, driver="GeoJSON")
    assert resolver.resolve(shapefile=path).bounds == pytest.approx((3, 3, 4, 4))


def test_resolve_requires_exactly_one(resolver):
    with pytest.raises(GeocodingError, match="exactly one"):
        resolver.resolve()
    with pytest.raises(GeocodingError, match="exactly one"):
        resolver.resolve("Cologne", bbox=(0, 0, 1, 1))


def test_buffer_m_grows_geometry(resolver):
    base = resolver.resolve(bbox=(6.0, 50.0, 6.0, 50.0))
    buffered = resolver.resolve(bbox=(6.0, 50.0, 6.0, 50.0), buffer_m=1000)
    assert buffered.area > base.area


def test_resolve_region_bbox_tuple(resolver):
    geom = resolver.resolve((6.0, 50.0, 7.0, 51.0))  # region as a 4-tuple
    assert geom.bounds == pytest.approx((6.0, 50.0, 7.0, 51.0))


def test_resolve_reprojects_non_wgs84(resolver):
    # A box in Web Mercator (metres) should be reprojected to lon/lat degrees.
    gdf = gpd.GeoDataFrame(
        geometry=[box(700_000, 6_500_000, 800_000, 6_600_000)], crs="EPSG:3857"
    )
    minx, miny, maxx, maxy = resolver.resolve(gdf).bounds
    assert -180 < minx < maxx < 180 and -90 < miny < maxy < 90


def test_resolve_unsupported_type(resolver):
    with pytest.raises(GeocodingError, match="Unsupported region type"):
        resolver.resolve(12345)


def test_buffer_metric_zero_returns_same_geometry(resolver):
    """buffer_metric with meters=0 short-circuits and returns the input."""
    poly = box(6.0, 50.0, 7.0, 51.0)
    assert resolver.buffer_metric(poly, 0) is poly
    assert resolver.buffer_metric(poly, 0.0) is poly


def test_base_defensive_point_none_raises(resolver, mocker):
    """The defensive `point is None` guard raises clearly."""
    mocker.patch("euroflood.services.location.sum", return_value=1, create=True)
    with pytest.raises(GeocodingError, match="exactly one location input"):
        resolver._base(None, None, 0.0, None, None, None)


def test_resolve_point_without_radius_returns_point(resolver):
    """A point with no radius resolves to a bare Point (no buffering)."""
    geom = resolver.resolve(point=(50.5, 6.5))
    assert geom.geom_type == "Point"
    assert (geom.x, geom.y) == pytest.approx((6.5, 50.5))


# --- shape modes (exact / bbox / hull) -------------------------------------
_TRIANGLE = Polygon([(0, 0), (2, 0), (0, 2)])  # non-rectangular, so bbox differs


def test_shape_exact_is_identity_passthrough(resolver):
    """The default shape='exact' returns the base geometry unchanged (same object)."""
    assert resolver.resolve(_TRIANGLE) is _TRIANGLE
    assert resolver.resolve(_TRIANGLE, shape="exact") is _TRIANGLE


def test_shape_bbox_returns_bounding_rectangle(resolver):
    out = resolver.resolve(_TRIANGLE, shape="bbox")
    assert out.equals(_TRIANGLE.envelope)
    assert out.bounds == pytest.approx((0.0, 0.0, 2.0, 2.0))
    assert out.area == pytest.approx(4.0)  # rectangle, not the triangle's 2.0


def test_shape_hull_returns_convex_hull(resolver):
    concave = Polygon([(0, 0), (2, 0), (2, 2), (1, 1), (0, 2)])  # notched (concave)
    out = resolver.resolve(concave, shape="hull")
    assert out.equals(concave.convex_hull)
    assert out.area > concave.area  # the hull fills the notch


def test_shape_bbox_then_buffer_stays_a_sharp_rectangle(resolver):
    box_only = resolver.resolve(_TRIANGLE, shape="bbox")
    buffered = resolver.resolve(_TRIANGLE, shape="bbox", buffer_m=50_000)
    assert buffered.area > box_only.area  # buffer still applies on top of the bbox
    # A buffered bbox grows into a bigger box (mitre join), not a rounded rectangle.
    assert len(buffered.exterior.coords) == 5


def test_shape_exact_buffer_rounds_corners(resolver):
    """The default 'exact' shape keeps the organic round-join buffer (many points)."""
    buffered = resolver.resolve(_TRIANGLE, shape="exact", buffer_m=50_000)
    assert len(buffered.exterior.coords) > 5  # rounded, not a sharp polygon


def test_shape_bbox_on_point_radius_is_bounding_square(resolver):
    """point+radius (a circle) + shape='bbox' -> its bounding square (OSMnx parity)."""
    circle = resolver.resolve(point=(50.94, 6.96), radius_m=5000)
    square = resolver.resolve(point=(50.94, 6.96), radius_m=5000, shape="bbox")
    assert square.equals(circle.envelope)
    assert len(square.exterior.coords) == 5  # a closed rectangle ring


def test_shape_unknown_raises(resolver):
    with pytest.raises(GeocodingError, match="Unknown shape"):
        resolver.resolve("Cologne", shape="circle")


def test_shape_does_not_leak_into_geocoder_call(resolver):
    """shape is applied after geocoding, so the geocoder call is unchanged."""
    resolver.resolve("Cologne", shape="bbox")
    resolver.geocoder.get_geometry.assert_called_once_with("Cologne", level=None)
