"""Tests for FloodFrame.__repr__ / _repr_html_ (notebook-friendly summaries)."""

from shapely.geometry import box

from euroflood.pipelines.discovery import _make_frame


def _historic_rows():
    return [
        {
            "collection": "historic",
            "event_id": 1,
            "date": "2020-05-01",
            "year": 2020,
            "end_date": "2020-05-09",
            "cluster_id": "3",
            "filename": "a.tif",
            "download_url": "http://x",
            "area_km2": 8.0,
            "geometry": box(0, 0, 1, 1),
        },
        {
            "collection": "historic",
            "event_id": 2,
            "date": "2021-07-01",
            "year": 2021,
            "end_date": None,
            "cluster_id": "4",
            "filename": "b.tif",
            "download_url": "http://y",
            "area_km2": 12.5,
            "geometry": box(0, 0, 1, 1),
        },
    ]


def _hazard_rows():
    return [
        {
            "collection": "hazard",
            "return_period": 100,
            "n_tiles": 2,
            "filename": "hazard_RP100.tif",
            "area_km2": 891.2,
            "geometry": box(0, 0, 1, 1),
        }
    ]


def test_repr_historic_summary(mock_settings):
    r = repr(_make_frame(_historic_rows(), mock_settings))
    assert "EuroFlood catalogue: 2 flood events" in r
    assert "2020-05-01 … 2021-07-01" in r
    assert "km² total" in r


def test_repr_singular_event(mock_settings):
    r = repr(_make_frame(_historic_rows()[:1], mock_settings))
    assert "1 flood event" in r
    assert "flood events" not in r


def test_repr_empty(mock_settings):
    r = repr(_make_frame([], mock_settings))
    assert "0 flood events" in r


def test_repr_html_wraps_summary(mock_settings):
    html = _make_frame(_historic_rows(), mock_settings)._repr_html_()
    assert "EuroFlood catalogue" in html
    assert "<div" in html
    assert "<table" in html  # the standard GeoDataFrame HTML is still present


def test_repr_hazard_summary(mock_settings):
    r = repr(_make_frame(_hazard_rows(), mock_settings))
    assert "hazard layer" in r
    assert "RP 100 yr" in r
