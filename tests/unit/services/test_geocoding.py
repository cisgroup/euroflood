"""Tests for the hybrid GeocodingService (offline dataset + Nominatim fallback).

All tests run fully offline: the local backend reads a tiny fixture GeoJSON and
the Nominatim backend has `requests.get` mocked.
"""

import json

import pytest
import requests
from shapely.geometry import Polygon, mapping

from euroflood.exceptions import GeocodingError
from euroflood.services.geocoding import (
    _EXONYMS,
    GeocodingService,
    _name_variants,
    _normalize,
)


def _feature(name, level, poly):
    return {
        "type": "Feature",
        "properties": {"NAME_LATN": name, "LEVL_CODE": level},
        "geometry": mapping(poly),
    }


@pytest.fixture
def boundary_dataset(tmp_path):
    """Write a tiny NUTS-like boundary GeoJSON and return its path."""
    koln = Polygon([(6.8, 50.8), (7.1, 50.8), (7.1, 51.0), (6.8, 51.0)])
    germany = Polygon([(6.0, 47.0), (15.0, 47.0), (15.0, 55.0), (6.0, 55.0)])
    fc = {
        "type": "FeatureCollection",
        "features": [_feature("Köln", 3, koln), _feature("Deutschland", 0, germany)],
    }
    path = tmp_path / "nuts_sample.geojson"
    path.write_text(json.dumps(fc))
    return path


@pytest.fixture
def local_service(boundary_dataset, mock_settings):
    """A GeocodingService pointed at the offline fixture dataset."""
    mock_settings.boundary_dataset_path = boundary_dataset
    return GeocodingService(settings=mock_settings)


def _mock_nominatim(mocker, poly):
    resp = mocker.Mock()
    resp.json.return_value = {"features": [{"geometry": mapping(poly)}]}
    resp.raise_for_status = mocker.Mock()
    return mocker.patch("requests.get", return_value=resp)


def test_normalize_strips_accents_and_case():
    assert _normalize("Köln") == _normalize("KOLN") == "koln"


def test_local_exact_match(local_service):
    geom = local_service.get_geometry("Köln, Germany")
    assert geom.bounds == pytest.approx((6.8, 50.8, 7.1, 51.0))


def test_local_accent_insensitive(local_service):
    # "Koln" (no umlaut) still matches "Köln" via normalization
    assert not local_service.get_geometry("Koln").is_empty


def test_local_level_filter(local_service):
    # Deutschland is level 0; filtering to level 3 misses, level 0 resolves.
    with pytest.raises(GeocodingError):
        local_service.get_geometry("Deutschland", level=3)
    assert not local_service.get_geometry("Deutschland", level=0).is_empty


def test_local_miss_suggests(local_service):
    with pytest.raises(GeocodingError, match="Did you mean"):
        local_service.get_geometry("Kolnx")


def test_local_miss_no_fallback_when_disabled(local_service):
    with pytest.raises(GeocodingError):
        local_service.get_geometry("Atlantis")


# --- forgiving matching (exonyms, bilingual/suffix names, close typos) ------
@pytest.mark.parametrize(
    ("query", "expected_bounds"),
    [
        ("Cologne", (6.8, 50.8, 7.1, 51.0)),  # exonym -> "Köln"
        ("Germany", (6.0, 47.0, 15.0, 55.0)),  # exonym -> "Deutschland"
    ],
)
def test_local_resolves_exonym(local_service, query, expected_bounds):
    assert local_service.get_geometry(query).bounds == pytest.approx(expected_bounds)


def _service_for(tmp_path, mock_settings, name):
    poly = Polygon([(6.0, 50.0), (7.0, 50.0), (7.0, 51.0), (6.0, 51.0)])
    fc = {"type": "FeatureCollection", "features": [_feature(name, 3, poly)]}
    path = tmp_path / "one.geojson"
    path.write_text(json.dumps(fc))
    mock_settings.boundary_dataset_path = path
    return GeocodingService(settings=mock_settings)


def test_local_resolves_bilingual_slash_name(tmp_path, mock_settings):
    # "Valencia/València" is findable by either half of the '/'-joined name.
    svc = _service_for(tmp_path, mock_settings, "Valencia/València")
    assert not svc.get_geometry("Valencia").is_empty
    assert not svc.get_geometry("València").is_empty


def test_local_resolves_suffixed_name_via_precomma(tmp_path, mock_settings):
    # "München, Kreisfreie Stadt" resolves by "München" (pre-comma) and "Munich" (exonym).
    svc = _service_for(tmp_path, mock_settings, "München, Kreisfreie Stadt")
    assert not svc.get_geometry("München").is_empty
    assert not svc.get_geometry("Munich").is_empty


def test_local_fuzzy_accepts_very_close_typo(local_service):
    # A one-char typo of "Deutschland" (ratio > 0.9) auto-resolves…
    assert not local_service.get_geometry("Deutschlan").is_empty


def test_local_fuzzy_rejects_far_miss(local_service):
    # …but "Kolnx" (~0.89 vs "koln") stays below the cutoff and misses.
    with pytest.raises(GeocodingError, match="Did you mean"):
        local_service.get_geometry("Kolnx")


def test_name_variants_splits_slash_and_precomma():
    assert _name_variants("Valencia/València") == {"valencia"}
    assert _name_variants("München, Kreisfreie Stadt") == {
        "munchen",
        "munchen, kreisfreie stadt",
    }


def test_exonym_map_is_normalized():
    for key, value in _EXONYMS.items():
        assert key == _normalize(key) and value == _normalize(value)
        assert key != value  # an exonym must differ from its endonym


def test_nominatim_backend(mock_settings, mocker):
    mock_settings.geocoder_backend = "nominatim"
    _mock_nominatim(mocker, Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]))

    geom = GeocodingService(settings=mock_settings).get_geometry("Cologne, Germany")
    assert geom.bounds == pytest.approx((0, 0, 1, 1))


def test_nominatim_empty_raises(mock_settings, mocker):
    mock_settings.geocoder_backend = "nominatim"
    resp = mocker.Mock()
    resp.json.return_value = {"features": []}
    resp.raise_for_status = mocker.Mock()
    mocker.patch("requests.get", return_value=resp)

    with pytest.raises(GeocodingError, match="no result"):
        GeocodingService(settings=mock_settings).get_geometry("Nowhere")


def test_local_miss_falls_back_to_nominatim(local_service, mocker):
    local_service.settings.allow_remote_geocoding = True
    spy = _mock_nominatim(mocker, Polygon([(2, 2), (3, 2), (3, 3), (2, 3)]))

    # A genuinely unknown place misses the local dataset -> Nominatim fallback.
    geom = local_service.get_geometry("Nowhereland")
    assert geom.bounds == pytest.approx((2, 2, 3, 3))
    spy.assert_called_once()


def test_local_download_on_first_use(mock_settings, boundary_dataset, mocker):
    """With no override path and an empty cache, the dataset is downloaded once."""
    mock_settings.boundary_dataset_path = None  # force the download branch
    downloader = mocker.Mock()
    downloader.download_file.return_value = boundary_dataset

    svc = GeocodingService(settings=mock_settings, downloader=downloader)
    assert not svc.get_geometry("Köln").is_empty
    downloader.download_file.assert_called_once()


def test_local_download_failure_raises(mock_settings, mocker):
    """A failed dataset download surfaces an actionable GeocodingError."""
    mock_settings.boundary_dataset_path = None
    downloader = mocker.Mock()
    downloader.download_file.return_value = None  # download failed

    svc = GeocodingService(settings=mock_settings, downloader=downloader)
    with pytest.raises(GeocodingError, match="Failed to download"):
        svc.get_geometry("Köln")


def test_local_missing_name_column(mock_settings, tmp_path):
    """A dataset without a recognizable name column raises GeocodingError."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"foo": "bar"},
                "geometry": mapping(Polygon([(0, 0), (1, 0), (1, 1)])),
            }
        ],
    }
    path = tmp_path / "noname.geojson"
    path.write_text(json.dumps(fc))
    mock_settings.boundary_dataset_path = path

    with pytest.raises(GeocodingError, match="recognizable name column"):
        GeocodingService(settings=mock_settings).get_geometry("Anything")


def test_nominatim_network_error(mock_settings, mocker):
    """A Nominatim network failure is wrapped as GeocodingError."""
    mock_settings.geocoder_backend = "nominatim"
    mocker.patch("requests.get", side_effect=requests.ConnectionError("down"))

    with pytest.raises(GeocodingError, match="Nominatim request failed"):
        GeocodingService(settings=mock_settings).get_geometry("Anywhere")


def test_local_uses_cached_dataset_without_download(
    mock_settings, boundary_dataset, mocker
):
    """When the default cache file already exists, no download is attempted."""
    mock_settings.boundary_dataset_path = None
    cached = (
        mock_settings.cache_dir / "boundaries" / mock_settings.boundary_dataset_filename
    )
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(boundary_dataset.read_bytes())
    downloader = mocker.Mock()
    svc = GeocodingService(settings=mock_settings, downloader=downloader)
    assert not svc.get_geometry("Köln").is_empty
    downloader.download_file.assert_not_called()


def test_local_reprojects_non_wgs84_dataset(mock_settings, tmp_path):
    """A boundary dataset authored in another CRS is reprojected to 4326."""
    import geopandas as gpd
    from shapely.geometry import box

    gdf = gpd.GeoDataFrame(
        {"NAME_LATN": ["Testland"]},
        geometry=[box(6.0, 50.0, 7.0, 51.0)],
        crs="EPSG:4326",
    ).to_crs(epsg=3857)
    path = tmp_path / "merc.gpkg"
    gdf.to_file(path, driver="GPKG")
    mock_settings.boundary_dataset_path = path
    geom = GeocodingService(settings=mock_settings).get_geometry("Testland")
    assert geom.bounds == pytest.approx((6.0, 50.0, 7.0, 51.0), abs=1e-6)


def test_online_first_prefers_nominatim(boundary_dataset, mock_settings, mocker):
    """online_first resolves via Nominatim even when the name is in the local set."""
    mock_settings.geocoder_backend = "online_first"
    mock_settings.boundary_dataset_path = boundary_dataset
    spy = _mock_nominatim(mocker, Polygon([(2, 2), (3, 2), (3, 3), (2, 3)]))

    geom = GeocodingService(settings=mock_settings).get_geometry("Köln")
    # Nominatim's polygon wins over the local "Köln" box (6.8..7.1, 50.8..51.0).
    assert geom.bounds == pytest.approx((2, 2, 3, 3))
    spy.assert_called_once()


def test_online_first_falls_back_to_local_on_network_error(
    boundary_dataset, mock_settings, mocker
):
    """A Nominatim network failure drops through to the offline dataset."""
    mock_settings.geocoder_backend = "online_first"
    mock_settings.boundary_dataset_path = boundary_dataset
    mocker.patch("requests.get", side_effect=requests.ConnectionError("down"))

    geom = GeocodingService(settings=mock_settings).get_geometry("Köln")
    assert geom.bounds == pytest.approx((6.8, 50.8, 7.1, 51.0))


def test_online_first_falls_back_to_local_on_empty(
    boundary_dataset, mock_settings, mocker
):
    """An empty Nominatim result drops through to the offline dataset."""
    mock_settings.geocoder_backend = "online_first"
    mock_settings.boundary_dataset_path = boundary_dataset
    resp = mocker.Mock()
    resp.json.return_value = {"features": []}
    resp.raise_for_status = mocker.Mock()
    mocker.patch("requests.get", return_value=resp)

    geom = GeocodingService(settings=mock_settings).get_geometry("Köln")
    assert geom.bounds == pytest.approx((6.8, 50.8, 7.1, 51.0))


def test_online_first_both_fail_raises_combined(
    boundary_dataset, mock_settings, mocker
):
    """When Nominatim and the local dataset both miss, the error names both."""
    mock_settings.geocoder_backend = "online_first"
    mock_settings.boundary_dataset_path = boundary_dataset
    resp = mocker.Mock()
    resp.json.return_value = {"features": []}
    resp.raise_for_status = mocker.Mock()
    mocker.patch("requests.get", return_value=resp)

    with pytest.raises(GeocodingError, match="local fallback also failed"):
        GeocodingService(settings=mock_settings).get_geometry("Atlantis")


# --- persistent geocode cache -----------------------------------------------
def _local_service(mock_settings, boundary_dataset):
    mock_settings.geocoder_backend = "local"
    mock_settings.boundary_dataset_path = boundary_dataset
    return GeocodingService(settings=mock_settings)


def test_geocode_cache_hit_skips_second_resolve(
    mock_settings, boundary_dataset, mocker
):
    """A repeat lookup is served from the disk cache (no second resolve)."""
    svc = _local_service(mock_settings, boundary_dataset)
    spy = mocker.spy(svc, "_resolve")
    g1 = svc.get_geometry("Köln")
    g2 = svc.get_geometry("Köln")
    assert spy.call_count == 1  # second call hit the cache
    assert g1.equals(g2)
    assert (mock_settings.cache_dir / "geocode").exists()


def test_geocode_cache_disabled_re_resolves(mock_settings, boundary_dataset, mocker):
    mock_settings.geocode_cache = False
    svc = _local_service(mock_settings, boundary_dataset)
    spy = mocker.spy(svc, "_resolve")
    svc.get_geometry("Köln")
    svc.get_geometry("Köln")
    assert spy.call_count == 2
    assert not (mock_settings.cache_dir / "geocode").exists()


def test_geocode_cache_corrupt_entry_re_resolves(mock_settings, boundary_dataset):
    """A corrupt/partial cache entry is ignored, not fatal."""
    svc = _local_service(mock_settings, boundary_dataset)
    g1 = svc.get_geometry("Köln")
    svc._cache_path("Köln", None).write_bytes(b"not-valid-wkb")
    g2 = svc.get_geometry("Köln")  # must not raise
    assert g2.equals(g1)


def test_load_nuts_is_cached_across_instances(boundary_dataset, mocker):
    """The ~14 MB NUTS load is read + indexed once per path, not per query."""
    from euroflood.services import geocoding

    geocoding._load_nuts.cache_clear()
    spy = mocker.spy(geocoding.gpd, "read_file")
    gdf1, index1 = geocoding._load_nuts(str(boundary_dataset))
    gdf2, index2 = geocoding._load_nuts(str(boundary_dataset))
    assert gdf1 is gdf2 and index1 is index2  # same cached objects
    assert spy.call_count == 1  # read + indexed only once


def test_repeated_local_geocode_reads_dataset_once(
    mock_settings, boundary_dataset, mocker
):
    """Two queries via fresh GeocodingService instances share the cached NUTS load."""
    from euroflood.services import geocoding

    geocoding._load_nuts.cache_clear()
    mock_settings.boundary_dataset_path = boundary_dataset
    mock_settings.geocode_cache = False  # exercise the NUTS path, not the disk cache
    spy = mocker.spy(geocoding.gpd, "read_file")
    GeocodingService(settings=mock_settings).get_geometry("Köln")
    GeocodingService(settings=mock_settings).get_geometry("Deutschland", level=0)
    assert spy.call_count == 1  # per-instance rebuild is gone
