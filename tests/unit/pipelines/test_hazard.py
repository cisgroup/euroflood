"""Tests for the hazard pipeline: hazard() query, download, and mirror."""

from pathlib import Path

import pytest
import rasterio
from shapely.geometry import box

import euroflood as ef
from euroflood.exceptions import HazardError
from euroflood.pipelines.discovery import FloodFrame
from euroflood.pipelines.hazard import (
    HAZARD_CATALOGUE_COLUMNS,
    _tile_sources,
    mirror_hazard,
)
from euroflood.services.hazard_tiles import (
    SUPPORTED_RETURN_PERIODS,
    HazardTile,
)
from euroflood.services.raster_ops import RasterOps

# An ROI fully inside tile T_A; and one straddling T_A | T_B.
ROI_SINGLE = (10.2, 50.2, 10.8, 50.8)
ROI_MULTI = (10.5, 50.2, 11.5, 50.8)


@pytest.fixture
def hazard_env(mock_settings, hazard_tile_index_path):
    """Point settings at the fixture tile index so everything runs offline."""
    mock_settings.hazard_index_path = hazard_tile_index_path
    return mock_settings


def _patch_tiles(mocker, synth_rp_tile, mapping):
    """Patch download_file to return local fixture tiles keyed by filename.

    `mapping` maps a tile filename to its (minx, miny, maxx, maxy) bbox.
    """
    paths = {
        fn: synth_rp_tile(fn.removesuffix(".tif"), *bbox)
        for fn, bbox in mapping.items()
    }
    return mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=lambda url, filename, **_kw: paths.get(filename),
    )


# --- query -----------------------------------------------------------------
def test_query_one_row_per_return_period(hazard_env):
    cat = ef.hazard(bbox=ROI_SINGLE, return_period=[100, 500])

    assert isinstance(cat, FloodFrame)
    assert cat.crs.to_epsg() == 4326
    assert len(cat) == 2
    assert list(cat.columns) == HAZARD_CATALOGUE_COLUMNS
    assert set(cat["return_period"]) == {100, 500}
    assert (cat["collection"] == "hazard").all()
    assert (cat["n_tiles"] == 1).all()
    assert cat["area_km2"].iloc[0] > 0
    assert cat["filename"].iloc[0] == "hazard_RP100.tif"


def test_query_single_int_return_period(hazard_env):
    cat = ef.hazard(bbox=ROI_SINGLE, return_period=100)
    assert len(cat) == 1
    assert cat["return_period"].iloc[0] == 100


def test_query_defaults_to_all_return_periods(hazard_env):
    cat = ef.hazard(bbox=ROI_SINGLE)  # return_period=None
    assert len(cat) == len(SUPPORTED_RETURN_PERIODS)


def test_query_multi_tile_counts_tiles(hazard_env):
    cat = ef.hazard(bbox=ROI_MULTI, return_period=100)
    assert cat["n_tiles"].iloc[0] == 2


def test_query_no_tile_region_is_empty_frame(hazard_env):
    cat = ef.hazard(bbox=(20.0, 20.0, 21.0, 21.0), return_period=100)
    assert len(cat) == 0
    assert isinstance(cat, FloodFrame)
    assert list(cat.columns) == HAZARD_CATALOGUE_COLUMNS  # empty hazard schema


def test_query_unsupported_rp_raises(hazard_env):
    with pytest.raises(HazardError):
        ef.hazard(bbox=ROI_SINGLE, return_period=33)


def test_filter_preserves_frame_and_settings(hazard_env):
    cat = ef.hazard(bbox=ROI_SINGLE, return_period=[100, 500])
    sub = cat[cat.return_period == 100]
    assert isinstance(sub, FloodFrame)
    assert sub._settings is hazard_env


# --- download --------------------------------------------------------------
def test_download_single_tile_crops(hazard_env, synth_rp_tile, mocker, tmp_path):
    _patch_tiles(
        mocker, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )

    dl = ef.hazard(bbox=ROI_SINGLE, return_period=100).download(tmp_path / "out")

    assert len(dl.files) == 1
    # ROI-safe name: hazard_RP{rp}_{roihash}.tif
    assert dl.files[0].name.startswith("hazard_RP100_")
    assert dl.files[0].name.endswith(".tif")
    with rasterio.open(dl.files[0]) as src:
        assert src.crs.to_epsg() == 4326
        assert src.dtypes[0] == "float32"
        assert src.nodata == -9999.0
        # Cropped to the ROI -> smaller than the 10x10 source tile.
        assert src.width < 10 and src.height < 10


def test_download_multi_tile_mosaics(hazard_env, synth_rp_tile, mocker, tmp_path):
    spy = mocker.spy(RasterOps, "mosaic_and_crop")
    _patch_tiles(
        mocker,
        synth_rp_tile,
        {
            "ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0),
            "ID2_T_B_RP100_depth.tif": (11.0, 50.0, 12.0, 51.0),
        },
    )

    dl = ef.hazard(bbox=ROI_MULTI, return_period=100).download(tmp_path / "out")

    assert len(dl.files) == 1
    spy.assert_called_once()
    with rasterio.open(dl.files[0]) as src:
        # Spans the T_A | T_B seam at lon 11.0, with valid (non-nodata) data.
        assert src.bounds.left < 11.0 < src.bounds.right
        assert (src.read(1) != -9999.0).any()


def test_download_skips_when_no_tiles_fetched(hazard_env, mocker, tmp_path):
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=None,  # every tile fetch fails
    )
    dl = ef.hazard(bbox=ROI_SINGLE, return_period=100).download(tmp_path / "out")
    assert dl.files == []


def test_hazard_query_autodetects_cached(hazard_env, synth_rp_tile, mocker):
    _patch_tiles(
        mocker, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )
    # Download to the default settings.output_dir (the dir the auto-detect scans).
    ef.hazard(bbox=ROI_SINGLE, return_period=100).download()
    # A fresh hazard query over the same ROI carries the cached file automatically.
    cat = ef.hazard(bbox=ROI_SINGLE, return_period=100)
    assert cat.files  # path column populated without a new .download()
    assert "downloaded" in repr(cat)


def test_functional_download_dispatches_hazard(
    hazard_env, synth_rp_tile, mocker, tmp_path
):
    _patch_tiles(
        mocker, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )
    cat = ef.hazard(bbox=ROI_SINGLE, return_period=100)
    assert len(ef.download(cat, tmp_path / "out")) == 1  # routes by collection


# --- tile-source toggle ----------------------------------------------------
def _tile(name="T_A", tid=1, rp=100):
    fn = f"ID{tid}_{name}_RP{rp}_depth.tif"
    url = f"https://x/RP{rp}/{fn}"
    return HazardTile(tid, name, rp, url, fn, box(10.0, 50.0, 11.0, 51.0))


def test_tile_sources_vsicurl_mode(mock_settings, mocker):
    mock_settings.hazard_cache_tiles = False
    tile = _tile()
    downloader = mocker.Mock()

    sources = _tile_sources([tile], downloader, mock_settings)

    assert sources == [f"/vsicurl/{tile.download_url}"]
    downloader.download_file.assert_not_called()


def test_tile_sources_partial_download_filters_failures(mock_settings, mocker):
    # Some tiles fetch, some fail (None) — only the successful ones become sources.
    mock_settings.hazard_cache_tiles = True
    downloader = mocker.Mock()
    downloader.download_file.side_effect = [Path("a.tif"), None]
    sources = _tile_sources([_tile(tid=1), _tile(tid=2)], downloader, mock_settings)
    assert sources == ["a.tif"]


def test_hazard_download_forwards_on_bytes(hazard_env, synth_rp_tile, mocker):
    dl = _patch_tiles(
        mocker, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )
    cb = mocker.Mock()
    ef.hazard(bbox=ROI_SINGLE, return_period=100).download(on_bytes=cb)
    assert dl.call_args.kwargs.get("on_bytes") is cb  # threaded to the tile fetch


def test_tile_sources_cache_mode(mock_settings, mocker, tmp_path):
    mock_settings.hazard_cache_tiles = True
    tile = _tile()
    local = tmp_path / "t.tif"
    local.write_bytes(b"x")
    downloader = mocker.Mock()
    downloader.download_file.return_value = local

    sources = _tile_sources([tile], downloader, mock_settings)

    assert sources == [str(local)]
    downloader.download_file.assert_called_once_with(
        tile.download_url, tile.filename, on_bytes=None
    )


def test_tile_sources_fetches_concurrently_in_order(mock_settings, mocker):
    """Cached-mode tile fetch fans out across the pool and reassembles in tile order."""
    import threading

    mock_settings.hazard_cache_tiles = True
    mock_settings.max_workers_dl = 3  # allow concurrency (conftest defaults to 1)
    tiles = [_tile(tid=i, name=f"T{i}") for i in range(3)]
    barrier = threading.Barrier(3, timeout=10)  # only releases if all 3 run at once
    downloader = mocker.Mock()

    def fake_dl(url, filename, on_bytes=None):
        barrier.wait()  # proves the tiles are fetched in parallel, not serially
        return Path(f"/cache/{filename}")

    downloader.download_file.side_effect = fake_dl

    sources = _tile_sources(tiles, downloader, mock_settings)

    assert sources == [f"/cache/{t.filename}" for t in tiles]  # tile order preserved


# --- mirror ----------------------------------------------------------------
def test_mirror_hazard_downloads_all_tiles(hazard_env, mocker):
    dl = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=Path("tile.tif"),
    )
    n = mirror_hazard(return_period=[100, 500])  # 4 tiles x 2 return periods

    assert n == 8
    assert dl.call_count == 8


def test_mirror_hazard_via_api(hazard_env, mocker):
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=Path("tile.tif"),
    )
    assert ef.mirror_hazard(return_period=100) == 4


# --- reference manifest ----------------------------------------------------
def test_build_hazard_manifest(hazard_env):
    import json

    from euroflood.pipelines.hazard import build_hazard_manifest

    path = build_hazard_manifest(settings=hazard_env)
    assert path.exists()
    doc = json.loads(path.read_text())
    assert doc["collection"] == "hazard"
    assert doc["model_version"] == hazard_env.hazard_model_version
    assert doc["provenance"]["n_tiles"] == 4  # the 4-tile fixture index
    assert doc["provenance"]["return_periods"][0] == 10
    # The frozen tile_extents.geojson is referenced with an integrity record.
    assert "tile_extents.geojson" in doc["files"]
    assert doc["source_urls"]["jrc"] == hazard_env.hazard_base_url
