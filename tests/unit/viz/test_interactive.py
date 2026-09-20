"""Tests for euroflood.viz._interactive (folium maps). Skipped without the extra."""

import pytest

folium = pytest.importorskip("folium")
pytest.importorskip("matplotlib")
import matplotlib  # noqa: E402

matplotlib.use("Agg", force=True)

from euroflood.viz._interactive import (  # noqa: E402
    _check_backend,
    explore_context,
    explore_depth,
    explore_footprints,
    explore_recurrence,
)


def _children_of(m, kind):
    return [c for c in m._children.values() if isinstance(c, kind)]


def test_explore_recurrence_has_tooltip(historic_frame):
    m = explore_recurrence(historic_frame)
    assert isinstance(m, folium.Map)
    html = m.get_root().render()
    assert "Recorded floods" in html  # the tooltip alias is rendered
    assert _children_of(m, folium.GeoJson)


def test_explore_recurrence_no_boundary(historic_frame):
    m = explore_recurrence(historic_frame, boundary=False)
    assert isinstance(m, folium.Map)


def test_explore_depth_has_image_overlay(sample_tif_path):
    m = explore_depth(sample_tif_path)
    assert isinstance(m, folium.Map)
    assert _children_of(m, folium.raster_layers.ImageOverlay)


def test_explore_depth_has_legend_and_toggleable_basemap(depth_tif_3035):
    from branca.colormap import ColorMap

    m = explore_depth(depth_tif_3035)
    assert _children_of(m, ColorMap)  # a depth colour legend
    tiles = _children_of(m, folium.TileLayer)
    assert tiles and tiles[0].overlay is True  # basemap can be toggled off


def test_explore_context_returns_map(hazard_frame):
    m = explore_context(hazard_frame)
    assert isinstance(m, folium.Map)


def test_check_backend_rejects_unknown():
    with pytest.raises(ValueError, match="lonboard"):
        _check_backend("lonboard")


def test_explore_context_empty_frame(mock_settings):
    from euroflood.pipelines.discovery import _make_frame

    m = explore_context(_make_frame([], mock_settings))
    assert isinstance(m, folium.Map)  # falls back to a default-centered map


def test_explore_depth_bounds_valid_latlon(depth_tif_3035):
    m = explore_depth(depth_tif_3035)  # source EPSG:3035
    overlays = _children_of(m, folium.raster_layers.ImageOverlay)
    assert overlays
    (south, west), (north, east) = overlays[0].bounds
    assert -90 <= south < north <= 90  # not metric coords fed as lat/lon
    assert -180 <= west < east <= 180


def test_explore_footprints_unique_sorted_layers_and_boundary(two_event_frame):
    m = explore_footprints(two_event_frame)
    names = [g.layer_name for g in _children_of(m, folium.FeatureGroup)]
    assert len(names) == 2 and len(set(names)) == 2  # one unique layer per event
    assert names == sorted(names)  # date-ordered
    assert any(
        g.layer_name == "query area" for g in _children_of(m, folium.GeoJson)
    )  # a toggleable query-area boundary is present


def test_explore_footprints_year_folders(two_event_frame):
    from folium.plugins import GroupedLayerControl

    m = explore_footprints(two_event_frame)
    assert isinstance(m, folium.Map)
    assert len(_children_of(m, folium.FeatureGroup)) == 2  # one per event
    assert _children_of(m, GroupedLayerControl)  # foldered (by year) control
    html = m.get_root().render()
    assert "2020-05-01" in html and "2021-07-01" in html  # per-event tooltips


def test_explore_grayscale_tiles(historic_frame):
    """The grayscale preset resolves to a keyless, unwatermarked provider."""
    m = explore_recurrence(historic_frame, tiles="grayscale")
    urls = [
        str(getattr(t, "tiles", "")).lower() for t in _children_of(m, folium.TileLayer)
    ]
    assert any("arcgisonline" in u for u in urls), urls


def test_explore_never_serves_watermarked_tiles(historic_frame):
    """CARTO stamps API KEY REQUIRED across anonymous tiles.

    The reader's browser fetches these URLs, so a watermarked provider defaces the map
    for whoever opens the notebook. This asserts the whole preset table stays clear of
    it -- the previous version of this test asserted the opposite and would have locked
    the defect in.
    """
    for preset in ("grayscale", "dark", "osm", "light", "grey"):
        m = explore_recurrence(historic_frame, tiles=preset)
        html = m.get_root().render().lower()
        assert "cartocdn" not in html, f"{preset} still routes through CARTO"


def test_explore_dark_inverts_only_the_tile_pane(historic_frame):
    """No keyless dark basemap exists, so dark is a CSS invert of a light one.

    Leaflet keeps overlays in separate panes, so filtering .leaflet-tile-pane must not
    touch the flood raster or the boundary.
    """
    html = explore_recurrence(historic_frame, tiles="dark").get_root().render()
    assert ".leaflet-tile-pane{filter:invert(1)" in html.replace(" ", "")
    light = explore_recurrence(historic_frame, tiles="grayscale").get_root().render()
    assert "leaflet-tile-pane" not in light
