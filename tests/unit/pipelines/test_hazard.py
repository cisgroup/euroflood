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


def test_download_raises_when_a_required_tile_is_missing(hazard_env, mocker, tmp_path):
    """A wholly-failed request fails loudly (issue #25): no truncated raster, a clear error."""
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=None,  # the tile fetch fails
    )
    out = tmp_path / "out"
    with pytest.raises(HazardError, match="No hazard rasters could be produced"):
        ef.hazard(bbox=ROI_SINGLE, return_period=100).download(out)
    # No truncated output file (and no leftover atomic-temp) was left behind.
    assert list(out.glob("hazard_RP100_*.tif")) == []
    assert list(out.glob("*.part")) == []


def test_download_multi_tile_one_tile_fails_is_fail_closed(
    hazard_env, synth_rp_tile, mocker, tmp_path
):
    """The reported bug: an ROI spanning two tiles where the east tile fails must NOT
    silently mosaic the surviving west tile into a truncated hazard (issue #25)."""
    a = synth_rp_tile("ID1_T_A_RP100_depth", 10.0, 50.0, 11.0, 51.0)
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=lambda url, filename, **_kw: a if "T_A" in filename else None,
    )
    out = tmp_path / "out"
    # A single requested RP with an incomplete tile set produces nothing -> raise.
    with pytest.raises(HazardError, match="RP100"):
        ef.hazard(bbox=ROI_MULTI, return_period=100).download(out)
    assert list(out.glob("hazard_RP100_*.tif")) == []  # no truncated raster written


def test_download_multi_rp_partial_failure_returns_complete_siblings(
    hazard_env, synth_rp_tile, mocker, tmp_path
):
    """A transient failure on one return period must not discard the complete siblings.

    RP100's east tile fails (its raster is skipped, not truncated), while RP500 fetches
    both tiles and is written — so the sweep returns RP500 rather than aborting wholesale."""

    def _dl(url, filename, **_kw):
        if "T_B" in filename and "RP100" in filename:
            return None  # the east tile is transiently unavailable, for RP100 only
        bbox = (
            (10.0, 50.0, 11.0, 51.0) if "T_A" in filename else (11.0, 50.0, 12.0, 51.0)
        )
        return synth_rp_tile(filename.removesuffix(".tif"), *bbox)

    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file", side_effect=_dl
    )
    out = tmp_path / "out"
    dl = ef.hazard(bbox=ROI_MULTI, return_period=[100, 500]).download(out)

    assert len(dl.files) == 1  # RP500 survived; RP100 was skipped, not aborted
    assert dl.files[0].name.startswith("hazard_RP500_")
    assert list(out.glob("hazard_RP100_*.tif")) == []  # no truncated RP100 raster


def test_download_stamps_source_tile_provenance(
    hazard_env, synth_rp_tile, mocker, tmp_path
):
    """A successful hazard crop embeds the tile set it was built from (auditable)."""
    _patch_tiles(
        mocker,
        synth_rp_tile,
        {
            "ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0),
            "ID2_T_B_RP100_depth.tif": (11.0, 50.0, 12.0, 51.0),
        },
    )
    dl = ef.hazard(bbox=ROI_MULTI, return_period=100).download(tmp_path / "out")
    with rasterio.open(dl.files[0]) as src:
        tags = src.tags()
    assert tags["EUROFLOOD_RETURN_PERIOD"] == "100"
    assert tags["EUROFLOOD_N_SOURCE_TILES"] == "2"
    assert "ID1_T_A_RP100_depth.tif" in tags["EUROFLOOD_SOURCE_TILES"]
    assert "ID2_T_B_RP100_depth.tif" in tags["EUROFLOOD_SOURCE_TILES"]


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


def test_tile_sources_partial_download_fails_closed(mock_settings, mocker):
    # Some tiles fetch, some fail (None): fail closed. Mosaicking only the surviving
    # subset would be a silently truncated hazard (issue #25), so this must raise.
    mock_settings.hazard_cache_tiles = True
    downloader = mocker.Mock()
    downloader.download_file.side_effect = [Path("a.tif"), None]
    with pytest.raises(HazardError, match="Incomplete hazard tile set"):
        _tile_sources([_tile(tid=1), _tile(tid=2)], downloader, mock_settings)


def test_tile_sources_all_tiles_present_returns_sources(mock_settings, mocker):
    """The happy path: every required tile fetches -> all become sources, in order."""
    mock_settings.hazard_cache_tiles = True
    downloader = mocker.Mock()
    downloader.download_file.side_effect = [Path("a.tif"), Path("b.tif")]
    sources = _tile_sources([_tile(tid=1), _tile(tid=2)], downloader, mock_settings)
    assert sources == ["a.tif", "b.tif"]


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
    res = mirror_hazard(return_period=[100, 500])  # 4 tiles x 2 return periods

    assert res.downloaded == 8
    assert dl.call_count == 8


def test_mirror_hazard_via_api(hazard_env, mocker):
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=Path("tile.tif"),
    )
    assert ef.mirror("hazard", return_period=100).downloaded == 4


# --- region-scoped mirror + ledger -----------------------------------------
def _seed_hazard_cache(settings, synth_rp_tile, mapping):
    """Write real synth tiles into the hazard tiles cache dir (offline substrate)."""
    tdir = settings.get_hazard_tiles_dir()
    tdir.mkdir(parents=True, exist_ok=True)
    for fn, bbox in mapping.items():
        src = synth_rp_tile(fn.removesuffix(".tif"), *bbox)
        (tdir / fn).write_bytes(src.read_bytes())
    return tdir


def test_mirror_region_downloads_only_intersecting_tiles(
    hazard_env, synth_rp_tile, mocker, tmp_path
):
    """ROI_MULTI spans T_A|T_B -> exactly 2 tiles fetched (not all 4)."""
    dl = _patch_tiles(
        mocker,
        synth_rp_tile,
        {
            "ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0),
            "ID2_T_B_RP100_depth.tif": (11.0, 50.0, 12.0, 51.0),
        },
    )
    res = mirror_hazard(bbox=ROI_MULTI, return_period=100)
    assert res.downloaded == 2 and res.n_expected == 2
    assert {c.args[1] for c in dl.call_args_list} == {
        "ID1_T_A_RP100_depth.tif",
        "ID2_T_B_RP100_depth.tif",
    }


def test_mirror_region_single_tile(hazard_env, synth_rp_tile, mocker):
    dl = _patch_tiles(
        mocker, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )
    assert mirror_hazard(bbox=ROI_SINGLE, return_period=100).downloaded == 1
    assert dl.call_count == 1


def test_mirror_writes_ledger_with_checksums(hazard_env, synth_rp_tile, mocker):
    import hashlib
    import json

    _patch_tiles(
        mocker, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )
    mirror_hazard(bbox=ROI_SINGLE, return_period=100)
    doc = json.loads(hazard_env.get_hazard_manifest_path().read_text())
    rec = doc["mirror"]["tiles"]["ID1_T_A_RP100_depth.tif"]
    assert rec["return_period"] == 100 and rec["size_bytes"] > 0
    assert len(rec["sha256"]) == len(hashlib.sha256(b"").hexdigest())
    assert doc["mirror"]["model_version"] == hazard_env.hazard_model_version


def test_mirror_ledger_merges_across_regions(hazard_env, synth_rp_tile, mocker):
    import json

    _patch_tiles(
        mocker,
        synth_rp_tile,
        {
            "ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0),
            "ID2_T_B_RP100_depth.tif": (11.0, 50.0, 12.0, 51.0),
        },
    )
    mirror_hazard(bbox=ROI_SINGLE, return_period=100)  # T_A only
    mirror_hazard(bbox=(11.5, 50.2, 11.9, 50.8), return_period=100)  # T_B only
    tiles = json.loads(hazard_env.get_hazard_manifest_path().read_text())["mirror"][
        "tiles"
    ]
    assert set(tiles) == {"ID1_T_A_RP100_depth.tif", "ID2_T_B_RP100_depth.tif"}


def test_mirror_dry_run_plans_without_downloading(hazard_env, synth_rp_tile, mocker):
    dl = _patch_tiles(
        mocker,
        synth_rp_tile,
        {
            "ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0),
            "ID2_T_B_RP100_depth.tif": (11.0, 50.0, 12.0, 51.0),
        },
    )
    res = mirror_hazard(bbox=ROI_MULTI, return_period=100, dry_run=True)
    assert res.downloaded == 0 and res.n_expected == 2 and len(res.missing) == 2
    assert res.bytes_total > 0  # estimate
    dl.assert_not_called()


def test_mirror_redownloads_size_mismatched_cache(hazard_env, synth_rp_tile, mocker):
    """A cached tile whose size disagrees with the ledger is re-fetched (expected_size)."""
    import json

    dl = _patch_tiles(
        mocker, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )
    # Pre-seed a ledger claiming a different size for the tile.
    led = hazard_env.get_hazard_manifest_path()
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(
        json.dumps(
            {"mirror": {"tiles": {"ID1_T_A_RP100_depth.tif": {"size_bytes": 999999}}}}
        )
    )
    mirror_hazard(bbox=ROI_SINGLE, return_period=100)
    assert dl.call_args.kwargs.get("expected_size") == 999999  # threaded through


# --- offline mode ----------------------------------------------------------
def test_local_mode_uses_cached_tiles_no_network(
    hazard_env, synth_rp_tile, mocker, mock_requests_get, tmp_path
):
    _seed_hazard_cache(
        hazard_env, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )
    hazard_env.hazard_mode = "local"
    dl = mocker.spy(RasterOps, "mosaic_and_crop")
    net = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=AssertionError("no network in local mode"),
    )
    out = ef.hazard(bbox=ROI_SINGLE, return_period=100).download(tmp_path / "out")
    assert len(out.files) == 1
    dl.assert_called_once()
    net.assert_not_called()
    mock_requests_get.assert_not_called()


def test_local_mode_missing_tile_raises_with_remediation(
    hazard_env, mocker, mock_requests_get, tmp_path
):
    hazard_env.hazard_mode = "local"  # nothing cached
    net = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=AssertionError("no network"),
    )
    out = tmp_path / "out"
    with pytest.raises(HazardError, match="mirror hazard"):
        ef.hazard(bbox=ROI_SINGLE, return_period=100).download(out)
    net.assert_not_called()
    mock_requests_get.assert_not_called()
    assert list(out.glob("hazard_RP100_*.tif")) == []


def test_local_mode_missing_index_offline_error(hazard_env, mock_requests_get):
    # No cached tile_extents (hazard_index_path points at the fixture, so unset it).
    hazard_env.hazard_index_path = None
    hazard_env.hazard_mode = "local"
    with pytest.raises(HazardError, match="not cached"):
        ef.hazard(bbox=ROI_SINGLE, return_period=100)
    mock_requests_get.assert_not_called()


def test_tile_sources_local_mode_is_cache_only(mock_settings, synth_rp_tile):
    mock_settings.hazard_index_path = None
    mock_settings.hazard_mode = "local"
    tdir = mock_settings.get_hazard_tiles_dir()
    tdir.mkdir(parents=True)
    (tdir / "ID1_T_A_RP100_depth.tif").write_bytes(b"x")
    import unittest.mock as um

    downloader = um.Mock()
    sources = _tile_sources([_tile()], downloader, mock_settings)
    assert sources == [str(tdir / "ID1_T_A_RP100_depth.tif")]
    downloader.download_file.assert_not_called()


# --- verify ----------------------------------------------------------------
def test_verify_hazard_present_missing_corrupt(hazard_env, synth_rp_tile, mocker):
    from euroflood.pipelines.hazard import verify_hazard_mirror

    _patch_tiles(
        mocker,
        synth_rp_tile,
        {
            "ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0),
            "ID2_T_B_RP100_depth.tif": (11.0, 50.0, 12.0, 51.0),
        },
    )
    # Mirror straight into the cache dir so verify has real files + a ledger.
    _seed_hazard_cache(
        hazard_env,
        synth_rp_tile,
        {
            "ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0),
            "ID2_T_B_RP100_depth.tif": (11.0, 50.0, 12.0, 51.0),
        },
    )
    mirror_hazard(bbox=ROI_MULTI, return_period=100)  # writes the ledger

    rep = verify_hazard_mirror(bbox=ROI_MULTI, return_period=100)
    assert rep.ok and len(rep.present) == 2

    (hazard_env.get_hazard_tiles_dir() / "ID2_T_B_RP100_depth.tif").unlink()
    rep2 = verify_hazard_mirror(bbox=ROI_MULTI, return_period=100)
    assert not rep2.ok and "ID2_T_B_RP100_depth.tif" in rep2.missing


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


def test_mirror_model_version_change_evicts_stale_cache(
    hazard_env, synth_rp_tile, mocker
):
    """A model_version bump evicts stale cached tiles so they are re-fetched, not reused."""
    import json

    tiles_dir = _seed_hazard_cache(
        hazard_env, synth_rp_tile, {"ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0)}
    )
    cached = tiles_dir / "ID1_T_A_RP100_depth.tif"
    assert cached.exists()
    led = hazard_env.get_hazard_manifest_path()
    led.parent.mkdir(parents=True, exist_ok=True)
    led.write_text(json.dumps({"mirror": {"model_version": "OLD", "tiles": {}}}))
    # download_file returns a path OUTSIDE the cache, so an evicted tile stays gone.
    other = synth_rp_tile("other", 10.0, 50.0, 11.0, 51.0)
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=other,
    )
    mirror_hazard(bbox=ROI_SINGLE, return_period=100)  # default model != "OLD"
    assert not cached.exists()  # stale tile evicted (forced re-fetch)
    doc = json.loads(led.read_text())
    assert doc["mirror"]["model_version"] == hazard_env.hazard_model_version


def test_offline_master_switch_message_names_env(hazard_env, mock_requests_get):
    """Under EUROFLOOD_OFFLINE (not hazard_mode), the error names the master switch."""
    hazard_env.offline = True  # master switch; hazard_mode stays 'auto'
    with pytest.raises(HazardError, match="EUROFLOOD_OFFLINE"):
        ef.hazard(bbox=ROI_SINGLE, return_period=100).download(hazard_env.output_dir)
    mock_requests_get.assert_not_called()
