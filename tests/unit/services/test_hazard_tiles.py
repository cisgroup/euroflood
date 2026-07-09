"""Tests for HazardTileIndex (lookup over the GLOFAS tile-extents GeoJSON)."""

import geopandas as gpd
import pytest
from shapely.geometry import box

from euroflood.exceptions import HazardError
from euroflood.services.hazard_tiles import (
    SUPPORTED_RETURN_PERIODS,
    HazardTileIndex,
)


def _index(mock_settings, index_path, downloader=None):
    """Build a HazardTileIndex pointed at a local fixture index (no download)."""
    mock_settings.hazard_index_path = index_path
    return HazardTileIndex(settings=mock_settings, downloader=downloader)


def test_tiles_for_single(mock_settings, hazard_tile_index_path):
    idx = _index(mock_settings, hazard_tile_index_path)
    tiles = idx.tiles_for(box(10.2, 50.2, 10.8, 50.8), 100)  # inside T_A only

    assert len(tiles) == 1
    tile = tiles[0]
    assert (tile.tile_id, tile.name, tile.return_period) == (1, "T_A", 100)
    assert tile.filename == "ID1_T_A_RP100_depth.tif"
    assert tile.download_url.endswith("RP100/ID1_T_A_RP100_depth.tif")


def test_tiles_for_multi(mock_settings, hazard_tile_index_path):
    idx = _index(mock_settings, hazard_tile_index_path)
    tiles = idx.tiles_for(box(10.5, 50.2, 11.5, 50.8), 50)  # straddles T_A | T_B

    assert {t.tile_id for t in tiles} == {1, 2}


def test_tiles_for_none(mock_settings, hazard_tile_index_path):
    idx = _index(mock_settings, hazard_tile_index_path)
    assert idx.tiles_for(box(20.0, 20.0, 21.0, 21.0), 100) == []


def test_all_tiles_returns_every_tile(mock_settings, hazard_tile_index_path):
    idx = _index(mock_settings, hazard_tile_index_path)
    assert len(idx.all_tiles(500)) == 4


def test_unsupported_return_period_raises(mock_settings, hazard_tile_index_path):
    idx = _index(mock_settings, hazard_tile_index_path)
    with pytest.raises(HazardError, match="Unsupported return period"):
        idx.tiles_for(box(10.2, 50.2, 10.8, 50.8), 33)
    assert 33 not in SUPPORTED_RETURN_PERIODS


def test_index_crs_normalized_to_4326(mock_settings, tmp_path):
    """A tile index authored in another CRS is reprojected to EPSG:4326 on load."""
    gdf = gpd.GeoDataFrame(
        {"id": [1], "name": ["T_A"]},
        geometry=[box(10.0, 50.0, 11.0, 51.0)],
        crs="EPSG:4326",
    ).to_crs(epsg=3857)
    path = tmp_path / "idx.gpkg"
    gdf.to_file(path, driver="GPKG")

    idx = _index(mock_settings, path)
    assert idx._load().crs.to_epsg() == 4326


def test_index_downloads_once_then_caches(
    mock_settings, hazard_tile_index_path, mocker
):
    """With no override, the index is fetched once and reused across queries."""
    target = mock_settings.get_hazard_index_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    def fake_download(url, filename):
        target.write_bytes(hazard_tile_index_path.read_bytes())
        return target

    downloader = mocker.Mock()
    downloader.download_file = mocker.Mock(side_effect=fake_download)

    idx = HazardTileIndex(settings=mock_settings, downloader=downloader)
    idx.tiles_for(box(10.2, 50.2, 10.8, 50.8), 100)
    idx.tiles_for(box(10.2, 50.2, 10.8, 50.8), 200)  # second query uses cached gdf

    assert downloader.download_file.call_count == 1


def test_missing_override_index_raises(mock_settings, tmp_path):
    """A set-but-missing hazard_index_path is a clear error, not a silent download."""
    idx = _index(mock_settings, tmp_path / "does_not_exist.geojson")
    with pytest.raises(HazardError, match="does not exist"):
        idx.tiles_for(box(10.2, 50.2, 10.8, 50.8), 100)


def test_index_download_failure_raises(mock_settings, mocker):
    """A failed index download (offline cluster node) surfaces a HazardError."""
    mock_settings.hazard_index_path = None
    downloader = mocker.Mock()
    downloader.download_file.return_value = None
    idx = HazardTileIndex(settings=mock_settings, downloader=downloader)
    with pytest.raises(HazardError, match="Could not fetch the GLOFAS tile index"):
        idx.tiles_for(box(10.2, 50.2, 10.8, 50.8), 100)
    downloader.download_file.assert_called_once()


def test_index_without_crs_is_assigned_4326(mock_settings, tmp_path, mocker):
    """A CRS-less tile index is assumed/assigned EPSG:4326 on load."""
    crsless = gpd.GeoDataFrame(
        {"id": [1], "name": ["T_A"]},
        geometry=[box(10.0, 50.0, 11.0, 51.0)],
    )
    crsless.crs = None
    path = tmp_path / "nocrs.geojson"
    path.write_text("{}")
    mocker.patch("euroflood.services.hazard_tiles.gpd.read_file", return_value=crsless)
    idx = _index(mock_settings, path)
    loaded = idx._load()
    assert loaded.crs is not None
    assert loaded.crs.to_epsg() == 4326
    assert len(idx.tiles_for(box(10.2, 50.2, 10.8, 50.8), 100)) == 1
