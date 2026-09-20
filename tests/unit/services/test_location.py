"""Tests for LocationResolver (fully offline; geocoder mocked)."""

import warnings

import geopandas as gpd
import pytest
from pyproj import CRS, Geod, Transformer
from pyproj.exceptions import CRSError as PyprojCRSError
from shapely.geometry import LineString, Point, Polygon, box

from euroflood.exceptions import CRSError, GeocodingError
from euroflood.services.location import LocationResolver, _metric_crs_for


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
    # A buffered bbox grows into a bigger box (mitre join), not a rounded rectangle:
    # it fills its own envelope, whereas a round join clips the corners off.
    rounded = resolver.buffer_metric(_TRIANGLE.envelope, 50_000)  # default round join
    assert buffered.area / buffered.envelope.area > 0.995
    assert rounded.area / rounded.envelope.area < 0.99


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


# --- metric buffers are ground metres (local UTM, not Web Mercator) ----------
_GEOD = Geod(ellps="WGS84")


def _ground_distances_m(lon, lat, ring_geom):
    """Geodesic distance from (lon, lat) to every exterior vertex of a polygon."""
    xs, ys = ring_geom.exterior.coords.xy
    return [_GEOD.inv(lon, lat, x, y)[2] for x, y in zip(xs, ys, strict=True)]


def _outward_offset_m(edge_mid, outward_end, buffered):
    """Ground metres from an edge midpoint to where its outward normal exits ``buffered``."""
    crossing = LineString([edge_mid, outward_end]).intersection(buffered.exterior)
    points = [crossing] if crossing.geom_type == "Point" else list(crossing.geoms)
    return min(_GEOD.inv(edge_mid[0], edge_mid[1], p.x, p.y)[2] for p in points)


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(52.14, 6.20), (60.17, 24.94), (69.65, 18.96)],
    ids=["zutphen", "helsinki", "tromso"],
)
def test_point_radius_is_ground_metres(resolver, lat, lon):
    """A 5 km radius is 5 km on the ground at every latitude.

    Buffering in Web Mercator (scale 1/cos(lat)) gave 3.07 / 2.49 / 1.74 km here.
    """
    circle = resolver.resolve(point=(lat, lon), radius_m=5000)
    distances = _ground_distances_m(lon, lat, circle)
    assert min(distances) == pytest.approx(5000, rel=0.005)
    assert max(distances) == pytest.approx(5000, rel=0.005)


def test_buffer_bbox_edges_are_ground_metres(resolver):
    """Every edge of a buffered country-sized lon/lat box is offset by the full distance.

    Without densifying the box before projecting, its north and south edges are
    buffered as UTM chords and end up ~3 % short at this size.
    """
    minx, miny, maxx, maxy = 3.4, 50.7, 7.2, 53.6  # roughly the Netherlands
    buffered = resolver.resolve(
        bbox=(minx, miny, maxx, maxy), buffer_m=10_000, shape="bbox"
    )
    midx, midy = (minx + maxx) / 2, (miny + maxy) / 2
    west = _outward_offset_m((minx, midy), (minx - 1.0, midy), buffered)
    north = _outward_offset_m((midx, maxy), (midx, maxy + 1.0), buffered)
    south = _outward_offset_m((midx, miny), (midx, miny - 1.0), buffered)
    assert west == pytest.approx(10_000, rel=0.005)
    assert north == pytest.approx(10_000, rel=0.005)
    assert south == pytest.approx(10_000, rel=0.005)
    assert buffered.area / buffered.envelope.area > 0.995  # still sharp-cornered


def test_buffer_metric_is_deterministic(resolver):
    """Two identical buffered queries yield byte-identical WKB (stable crop cache keys)."""
    first = resolver.resolve(point=(52.14, 6.20), radius_m=5000)
    second = resolver.resolve(point=(52.14, 6.20), radius_m=5000)
    assert first.wkb == second.wkb


def test_buffer_metric_empty_geometry_is_returned_unchanged(resolver):
    """An empty geometry has no UTM zone; it passes through instead of raising."""
    empty = Polygon()
    assert resolver.buffer_metric(empty, 1000) is empty


def test_metric_crs_is_the_local_utm_zone():
    assert _metric_crs_for(Point(6.20, 52.14)).to_epsg() == 32632  # Zutphen: UTM 32N
    assert _metric_crs_for(Point(-0.38, 39.47)).to_epsg() == 32630  # Valencia: UTM 30N


@pytest.mark.parametrize("lat", [85.0, -85.0], ids=["north", "south"])
def test_buffer_metric_polar_falls_back_to_ups(resolver, lat):
    """Beyond the UTM zones the buffer still works, in Universal Polar Stereographic."""
    circle = resolver.resolve(point=(lat, 20.0), radius_m=5000)
    assert circle.geom_type == "Polygon"
    assert circle.contains(Point(20.0, lat))
    distances = _ground_distances_m(20.0, lat, circle)
    assert min(distances) == pytest.approx(5000, rel=0.01)
    assert max(distances) == pytest.approx(5000, rel=0.01)


def test_metric_crs_for_uses_ups_when_no_utm_zone(mocker):
    mocker.patch(
        "geopandas.GeoSeries.estimate_utm_crs",
        side_effect=RuntimeError("Unable to determine UTM CRS"),
    )
    assert _metric_crs_for(Point(20.0, 85.0)).to_epsg() == 32661
    assert _metric_crs_for(Point(20.0, -85.0)).to_epsg() == 32761


# --- crs: inputs given in another coordinate reference system ----------------
RD = "EPSG:28992"  # Amersfoort / RD New: the Dutch national grid, in metres
RD_BOX = (200_000.0, 455_000.0, 220_000.0, 475_000.0)  # a ~20 km box around Zutphen
RD_POINT = (210_000.0, 465_000.0)
_RD_TO_WGS84 = Transformer.from_crs(RD, "EPSG:4326", always_xy=True)


def _no_crs_warning(caught):
    return not [w for w in caught if "no CRS" in str(w.message)]


def test_crs_bbox_projected_reprojects_and_densifies(resolver):
    geom = resolver.resolve(bbox=RD_BOX, crs=RD)
    # Expected values come from pyproj itself, so the test checks the wiring (axis
    # order, densification, envelope) rather than PROJ's choice of datum shift.
    assert geom.bounds == pytest.approx(
        _RD_TO_WGS84.transform_bounds(*RD_BOX), abs=1e-6
    )
    assert geom.bounds == pytest.approx(
        (6.043691, 52.079451, 6.339261, 52.261187), abs=1e-4
    )
    assert len(geom.exterior.coords) > 5  # densified: edges follow the true rectangle


def test_crs_region_tuple_matches_bbox(resolver):
    assert resolver.resolve(RD_BOX, crs=RD).equals(
        resolver.resolve(bbox=RD_BOX, crs=RD)
    )


def test_crs_bbox_shape_bbox_is_the_reprojected_envelope(resolver):
    geom = resolver.resolve(bbox=RD_BOX, crs=RD, shape="bbox")
    assert len(geom.exterior.coords) == 5
    assert geom.bounds == pytest.approx(
        _RD_TO_WGS84.transform_bounds(*RD_BOX), abs=1e-6
    )


def test_crs_shapely_region_is_reprojected_vertex_wise(resolver):
    geom = resolver.resolve(box(*RD_BOX), crs=RD)
    assert len(geom.exterior.coords) == 5  # the user's vertices, no densification
    dense = resolver.resolve(bbox=RD_BOX, crs=RD)
    assert geom.bounds == pytest.approx(dense.bounds, abs=2e-4)
    poly = Polygon([(0, 0), (1, 0), (1, 1)])
    assert resolver.resolve(poly) is poly  # crs=None stays the identity


def test_crs_empty_geometry_passes_through(resolver):
    assert resolver.resolve(Polygon(), crs=RD).is_empty


def test_crs_point_projected_is_easting_northing(resolver):
    geom = resolver.resolve(point=RD_POINT, crs=RD)
    assert (geom.x, geom.y) == pytest.approx(
        _RD_TO_WGS84.transform(*RD_POINT), abs=1e-9
    )
    assert (geom.x, geom.y) == pytest.approx((6.191181, 52.170408), abs=1e-4)


def test_crs_point_3035_is_easting_northing_despite_registry_order(resolver):
    """EPSG:3035 declares northing-first axes; the API still reads point as (x, y)."""
    to_laea = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    easting, northing = to_laea.transform(6.20, 52.14)
    geom = resolver.resolve(point=(easting, northing), crs="EPSG:3035")
    assert (geom.x, geom.y) == pytest.approx((6.20, 52.14), abs=1e-6)


def test_crs_point_geographic_keeps_lat_lon_order(resolver):
    geom = resolver.resolve(point=(52.14, 6.20), crs="EPSG:4258")  # ETRS89 lat/lon
    assert (geom.x, geom.y) == pytest.approx((6.20, 52.14), abs=1e-6)
    same = resolver.resolve(point=(52.14, 6.20), crs="EPSG:4326")
    assert same.equals(resolver.resolve(point=(52.14, 6.20)))


def test_crs_geographic_non_degree_units_skip_the_range_check(resolver):
    """NTF (Paris) is in grads, where |lat| may legitimately exceed 90."""
    geom = resolver.resolve(point=(52.0, 5.0), crs="EPSG:4807")
    assert -90 < geom.y < 90


def test_crs_point_with_radius_is_ground_metres(resolver):
    circle = resolver.resolve(point=RD_POINT, crs=RD, radius_m=5000)
    lon, lat = _RD_TO_WGS84.transform(*RD_POINT)
    assert circle.contains(Point(lon, lat))
    distances = _ground_distances_m(lon, lat, circle)
    assert min(distances) == pytest.approx(5000, rel=0.005)
    assert max(distances) == pytest.approx(5000, rel=0.005)


@pytest.mark.parametrize(
    "alias",
    ["EPSG:4326", 4326, "OGC:CRS84", "WGS84", CRS("EPSG:4326")],
    ids=["epsg-str", "epsg-int", "crs84", "wgs84", "crs-object"],
)
def test_crs_wgs84_aliases_are_the_identity(resolver, alias):
    plain = resolver.resolve(bbox=(6.0, 50.0, 7.0, 51.0))
    aliased = resolver.resolve(bbox=(6.0, 50.0, 7.0, 51.0), crs=alias)
    assert aliased.wkb == plain.wkb  # byte-identical -> identical crop cache keys


@pytest.mark.parametrize(
    "crs",
    [
        28992,
        CRS(RD),
        CRS(RD).to_wkt(),
        "+proj=sterea +lat_0=52.15616055555555 "
        "+lon_0=5.38763888888889 +k=0.9999079 +x_0=155000 +y_0=463000 +ellps=bessel "
        "+towgs84=565.417,50.3319,465.552,-0.398957,0.343988,-1.8774,4.0725 +units=m",
    ],
    ids=["int", "CRS", "wkt", "proj-string"],
)
def test_crs_accepts_any_pyproj_input(resolver, crs):
    reference = resolver.resolve(bbox=RD_BOX, crs=RD)
    assert resolver.resolve(bbox=RD_BOX, crs=crs).bounds == pytest.approx(
        reference.bounds, abs=1e-5
    )


@pytest.mark.parametrize("bad", ["EPSG:99999", "foo", ""])
def test_crs_invalid_raises(resolver, bad):
    with pytest.raises(CRSError, match="Unrecognised crs") as info:
        resolver.resolve("Cologne", crs=bad)
    assert isinstance(info.value.__cause__, PyprojCRSError)
    resolver.geocoder.get_geometry.assert_not_called()
    assert issubclass(CRSError, GeocodingError)  # existing handlers keep working


@pytest.mark.parametrize(
    "bad", ["EPSG:4978", "EPSG:5773"], ids=["geocentric", "vertical"]
)
def test_crs_non_horizontal_raises(resolver, bad):
    with pytest.raises(CRSError, match="not a horizontal"):
        resolver.resolve(bbox=RD_BOX, crs=bad)


def test_crs_geographic_range_is_checked(resolver):
    with pytest.raises(CRSError, match=r"\(lat, lon\) pair"):
        resolver.resolve(point=(200.0, 6.0))
    with pytest.raises(CRSError, match="latitude must be"):
        resolver.resolve(bbox=(6.0, 50.0, 7.0, 95.0))
    with pytest.raises(CRSError, match="pass crs="):
        resolver.resolve(bbox=RD_BOX)  # metres with no crs= (was a silent empty result)


def test_crs_degrees_typed_into_a_metre_crs_raise(resolver):
    with pytest.raises(CRSError, match="looks like degrees"):
        resolver.resolve(point=(52.14, 6.20), crs=RD)
    with pytest.raises(CRSError, match="looks like degrees"):
        resolver.resolve(bbox=(6.15, 52.10, 6.26, 52.17), crs=RD)


def test_crs_out_of_domain_coordinates_raise(resolver):
    with pytest.raises(CRSError, match="valid domain"):
        resolver.resolve(bbox=(1e9, 1e9, 1e9 + 1, 1e9 + 1), crs="EPSG:3035")


def test_crs_outside_area_of_use_warns(resolver):
    with pytest.warns(UserWarning, match="area of use"):
        geom = resolver.resolve(bbox=(1e6, 1e6, 1.01e6, 1.01e6), crs=RD)
    assert geom.is_valid


def test_crs_gdf_own_crs_wins_when_crs_is_none(resolver):
    gdf = gpd.GeoDataFrame(geometry=[box(*RD_BOX)], crs=RD)
    expected = resolver.resolve(box(*RD_BOX), crs=RD)
    assert resolver.resolve(gdf).bounds == pytest.approx(expected.bounds, abs=1e-9)


def test_crs_gdf_matching_crs_is_accepted(resolver):
    gdf = gpd.GeoDataFrame(geometry=[box(*RD_BOX)], crs=RD)
    assert resolver.resolve(gdf, crs=28992).bounds == pytest.approx(
        resolver.resolve(gdf).bounds
    )
    wgs = gpd.GeoDataFrame(geometry=[box(1, 1, 2, 2)], crs="EPSG:4326")
    assert resolver.resolve(wgs, crs="OGC:CRS84").bounds == pytest.approx((1, 1, 2, 2))


@pytest.mark.parametrize("other", ["EPSG:3035", "EPSG:4326"])
def test_crs_gdf_conflicting_crs_raises(resolver, other):
    gdf = gpd.GeoDataFrame(geometry=[box(*RD_BOX)], crs=RD)
    with pytest.raises(CRSError, match="CRS mismatch"):
        resolver.resolve(gdf, crs=other)


def test_crs_fills_in_a_missing_gdf_crs(resolver):
    naked = gpd.GeoDataFrame(geometry=[box(*RD_BOX)])  # no CRS at all
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        geom = resolver.resolve(naked, crs=RD)
    assert _no_crs_warning(caught)
    expected = resolver.resolve(box(*RD_BOX), crs=RD)
    assert geom.bounds == pytest.approx(expected.bounds, abs=1e-9)


def test_crs_declares_a_shapefile_without_prj(resolver, tmp_path):
    gpd.GeoDataFrame(geometry=[box(*RD_BOX)], crs=RD).to_file(tmp_path / "roi.shp")
    (tmp_path / "roi.prj").unlink()  # a .shp without its .prj reads back with crs=None
    assert gpd.read_file(tmp_path / "roi.shp").crs is None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        geom = resolver.resolve(shapefile=tmp_path / "roi.shp", crs=RD)
    assert _no_crs_warning(caught)
    expected = resolver.resolve(box(*RD_BOX), crs=RD)
    assert geom.bounds == pytest.approx(expected.bounds, abs=1e-6)


def test_crs_less_file_without_crs_warns_and_assumes_wgs84(resolver, tmp_path):
    gpd.GeoDataFrame(geometry=[box(3, 3, 4, 4)], crs="EPSG:4326").to_file(
        tmp_path / "roi.shp"
    )
    (tmp_path / "roi.prj").unlink()
    with pytest.warns(UserWarning, match="no CRS"):
        geom = resolver.resolve(shapefile=tmp_path / "roi.shp")
    assert geom.bounds == pytest.approx((3, 3, 4, 4))


def test_crs_with_place_name_raises(resolver):
    with pytest.raises(CRSError, match="does not apply to a place name"):
        resolver.resolve("Cologne", crs=RD)
    resolver.geocoder.get_geometry.assert_not_called()


def test_crs_area_of_use_crossing_the_antimeridian_is_not_checked(resolver):
    """Fiji Map Grid's area of use wraps the antimeridian; the advisory is skipped."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        geom = resolver.resolve(bbox=(1.9e6, 3.9e6, 1.91e6, 3.91e6), crs="EPSG:3460")
    assert geom.is_valid
    assert not [w for w in caught if "area of use" in str(w.message)]


# --- nuts: Eurostat NUTS identifiers ------------------------------------------
def test_resolve_nuts_uses_the_repository(mock_settings, mocker):
    geocoder, repo = mocker.Mock(), mocker.Mock()
    repo.geometry.return_value = box(5.0, 51.0, 7.0, 52.0)
    res = LocationResolver(settings=mock_settings, geocoder=geocoder, nuts=repo)
    assert res.resolve(nuts="NL22").bounds == pytest.approx((5.0, 51.0, 7.0, 52.0))
    repo.geometry.assert_called_once_with("NL22")
    res.resolve(nuts=["NL22", "NL21"])
    repo.geometry.assert_called_with(["NL22", "NL21"])
    geocoder.get_geometry.assert_not_called()


def test_resolve_nuts_is_one_of_the_exclusive_inputs(resolver):
    with pytest.raises(GeocodingError, match="exactly one"):
        resolver.resolve(nuts="NL22", bbox=(0, 0, 1, 1))
    with pytest.raises(GeocodingError, match="exactly one"):
        resolver.resolve("Cologne", nuts="NL22")


def test_resolve_nuts_with_crs_raises(mock_settings, mocker):
    repo = mocker.Mock()
    res = LocationResolver(settings=mock_settings, geocoder=mocker.Mock(), nuts=repo)
    with pytest.raises(CRSError, match="does not apply to a NUTS identifier"):
        res.resolve(nuts="NL22", crs="EPSG:28992")
    repo.geometry.assert_not_called()


def test_resolve_nuts_applies_shape_and_buffer(mock_settings, mocker):
    repo = mocker.Mock()
    repo.geometry.return_value = _TRIANGLE
    res = LocationResolver(settings=mock_settings, geocoder=mocker.Mock(), nuts=repo)
    out = res.resolve(nuts="XX", shape="bbox", buffer_m=50_000)
    assert out.area > _TRIANGLE.envelope.area
    assert out.area / out.envelope.area > 0.995  # still a sharp-cornered box
