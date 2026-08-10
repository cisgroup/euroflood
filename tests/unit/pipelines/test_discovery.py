"""Tests for the Discovery pipeline, the floods()/download() API, and FloodFrame."""

import json

import geopandas as gpd
import numpy as np
import pytest
import rasterio
import rasterio.mask
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box

import euroflood as ef
from euroflood.core.manifest import write_manifest
from euroflood.exceptions import CacheSchemaError
from euroflood.pipelines.discovery import (
    DiscoveryPipeline,
    FloodFrame,
    _dispatch_download,
    download_catalogue,
)
from euroflood.schemas import DICTIONARY_SCHEMA_VERSION


@pytest.fixture
def index_env(mock_settings, sample_tif_path):
    """Write a structured dictionary + an index raster (combo_id 1 on the diagonal)."""
    dic = {
        "schema_version": DICTIONARY_SCHEMA_VERSION,
        "combos": {
            "1": {
                "flood_ids": [10],
                "events": [
                    {
                        "global_id": 10,
                        "start_date": "2020-05-01",
                        "end_date": "2020-05-09",
                        "year": "2020",
                        "cluster_id": "3",
                        "filename": "WD_MERGE_2020_a.tif",
                        "download_url": "http://mock/2020/WD_MERGE_2020_a.tif",
                    }
                ],
            }
        },
    }
    mock_settings.get_dictionary_path().write_text(json.dumps(dic))

    with rasterio.open(sample_tif_path) as src:
        profile = src.profile
        data = src.read(1)
    data[data > 0] = 1
    profile.update(dtype="uint32")
    with rasterio.open(mock_settings.get_index_tif_path(), "w", **profile) as dst:
        dst.write(data.astype("uint32"), 1)
    write_manifest(mock_settings.get_manifest_path())
    return mock_settings


def _patch_geocoder(mocker, geom=None):
    mocker.patch(
        "euroflood.services.geocoding.GeocodingService.get_geometry",
        return_value=geom if geom is not None else box(9.0, 49.0, 11.0, 51.0),
    )


def _fake_crop(_src, out, _geom, *_a, **_k):
    """Stand-in for RasterOps.crop_raster: writes a stub output and reports success."""
    out.write_bytes(b"\x00")
    return True


def test_floods_returns_floodframe(index_env, mocker):
    _patch_geocoder(mocker)
    cat = ef.floods("Cologne")

    assert isinstance(cat, FloodFrame)
    assert cat.crs.to_epsg() == 4326
    assert len(cat) == 1
    row = cat.iloc[0]
    assert row["event_id"] == 10
    assert row["year"] == 2020
    assert row["date"] == "2020-05-01"
    assert row["filename"] == "WD_MERGE_2020_a.tif"
    assert cat["area_km2"].iloc[0] > 0


def test_floods_forwards_shape_to_resolver(index_env, mocker):
    """The public `shape=` kwarg reaches LocationResolver.resolve (end-to-end wiring)."""
    from euroflood.services.location import LocationResolver

    _patch_geocoder(mocker)
    spy = mocker.spy(LocationResolver, "resolve")
    ef.floods("X", shape="bbox")
    assert spy.call_args.kwargs.get("shape") == "bbox"


def test_floods_year_filter(index_env, mocker):
    _patch_geocoder(mocker)
    assert len(ef.floods("X", year=2020)) == 1
    assert len(ef.floods("X", year=1999)) == 0


def test_floods_period_filter(index_env, mocker):
    _patch_geocoder(mocker)
    assert len(ef.floods("X", start="2018", end="2021")) == 1
    assert len(ef.floods("X", start="2021", end="2022")) == 0


def test_filter_preserves_floodframe_and_chained_download(index_env, mocker):
    _patch_geocoder(mocker)
    mock_dl = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=index_env.cache_dir / "src.tif",
    )
    mock_crop = mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", side_effect=_fake_crop
    )

    cat = ef.floods("X")
    sub = cat[cat.year == 2020]
    assert isinstance(sub, FloodFrame)  # chaining is preserved after filtering

    dl = sub.download()  # returns the frame; writes into settings.output_dir
    assert isinstance(dl, FloodFrame)  # download() is fluent/actionable
    assert len(dl.files) == 1  # and remembers its file
    mock_dl.assert_called_once()
    mock_crop.assert_called_once()


def test_functional_download(index_env, mocker):
    _patch_geocoder(mocker)
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=index_env.cache_dir / "src.tif",
    )
    mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", return_value=True
    )
    cat = ef.floods("X")
    assert len(ef.download(cat)) == 1


def test_download_forwards_on_bytes_progress(index_env, mocker):
    """A progress callback reaches download_file (drives the CLI progress bar)."""
    _patch_geocoder(mocker)
    dl = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=index_env.cache_dir / "src.tif",
    )
    mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", side_effect=_fake_crop
    )
    cb = mocker.Mock()
    ef.floods("X").download(on_bytes=cb)
    assert dl.call_args.kwargs.get("on_bytes") is cb


def test_download_caches_crop_and_force_redownloads(index_env, mocker):
    _patch_geocoder(mocker)
    dl_file = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=index_env.cache_dir / "src.tif",
    )
    crop = mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", side_effect=_fake_crop
    )
    cat = ef.floods("X")

    first = cat.download()
    assert len(first.files) == 1 and crop.call_count == 1

    # Second call: the crop already exists -> cache hit, nothing re-downloaded.
    second = cat.download()
    assert second.files == first.files
    assert crop.call_count == 1  # not re-cropped
    assert dl_file.call_count == 1  # not re-fetched (the viz depth path relies on this)

    # force=True bypasses the cache and re-does the work.
    cat.download(force=True)
    assert crop.call_count == 2


def test_download_crop_name_is_roi_safe():
    from euroflood.pipelines.discovery import _historic_crop_name

    def _row(geom):
        gdf = gpd.GeoDataFrame(
            {"date": ["2020-05-01"], "event_id": [7]},
            geometry=[geom],
            crs="EPSG:4326",
        )
        return gdf.iloc[0]  # a Series, as .iterrows() yields

    n1 = _historic_crop_name(_row(box(0, 0, 1, 1)))
    n2 = _historic_crop_name(_row(box(0, 0, 2, 2)))
    assert n1 != n2  # same event, different ROI -> distinct files (no collision)
    assert n1.startswith("flood_2020-05-01_id7_") and n1.endswith(".tif")


def _historic_catalogue(settings, n, geom=None):
    """A multi-row historic FloodFrame (all rows share one ROI) for download tests."""
    if geom is None:
        geom = box(0, 0, 1, 1)
    rows = [
        {
            "collection": "historic",
            "event_id": i,
            "date": f"2020-01-0{i + 1}",
            "filename": f"f{i}.tif",
            "download_url": f"http://mock/f{i}.tif",
            "geometry": geom,
        }
        for i in range(n)
    ]
    cat = FloodFrame(gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326"))
    cat._settings = settings
    return cat


def test_download_parallel_preserves_order_and_skips_cache(mock_settings, mocker):
    """Downloads fan out across the pool; row order is preserved and cache hits skip it."""
    import threading

    from euroflood.pipelines.discovery import _frame_roi_key, _historic_crop_name

    mock_settings.max_workers_dl = 4  # allow real concurrency (conftest defaults to 1)
    cat = _historic_catalogue(mock_settings, 4)
    key = _frame_roi_key(cat)

    # Pre-create the crop for row 1 -> it must be a cache hit (never downloaded).
    cached = mock_settings.output_dir / _historic_crop_name(cat.iloc[1], key=key)
    cached.write_bytes(b"\x00")

    barrier = threading.Barrier(3, timeout=10)  # exactly the 3 uncached rows
    started: list[str] = []

    def fake_dl(url, filename, on_bytes=None):
        started.append(filename)
        barrier.wait()  # only releases if all 3 run concurrently -> proves the pool
        return mock_settings.cache_dir / filename

    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=fake_dl,
    )
    mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", side_effect=_fake_crop
    )

    paths = download_catalogue(cat, settings=mock_settings)

    expected = [
        mock_settings.output_dir / _historic_crop_name(cat.iloc[i], key=key)
        for i in range(4)
    ]
    assert paths == expected  # row order preserved (incl. the cache hit at index 1)
    assert "f1.tif" not in started  # the cached row skipped the pool
    assert sorted(started) == ["f0.tif", "f2.tif", "f3.tif"]  # 3 concurrent downloads


def test_query_autodetects_cached_downloads(index_env, mocker):
    _patch_geocoder(mocker)
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=index_env.cache_dir / "src.tif",
    )
    mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", side_effect=_fake_crop
    )
    # First query + download populates settings.output_dir with the ROI-safe crop.
    ef.floods("X").download()
    # A fresh query over the same ROI auto-detects it — no second .download().
    cat = ef.floods("X")
    assert cat.files  # path column populated straight from disk
    assert "downloaded" in repr(cat)


def test_query_autodetects_custom_output_dir(index_env, mocker, tmp_path):
    _patch_geocoder(mocker)
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=index_env.cache_dir / "src.tif",
    )
    mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", side_effect=_fake_crop
    )
    custom = tmp_path / "custom_out"
    ef.floods("X").download(custom)  # download to a NON-default directory
    # A plain re-query scans only settings.output_dir, so it misses the custom dir…
    assert "path" not in ef.floods("X").columns
    # …but passing the same output_dir the download used finds the cached crops.
    cat = ef.floods("X", output_dir=custom)
    assert cat.files and cat.files[0].parent == custom


def test_query_no_path_column_when_nothing_cached(index_env, mocker):
    _patch_geocoder(mocker)
    cat = ef.floods("X")  # nothing downloaded yet
    assert "path" not in cat.columns  # a clean frame, not an all-None path column
    assert cat.files == []


def test_query_autodetect_can_be_disabled(index_env, mocker):
    _patch_geocoder(mocker)
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=index_env.cache_dir / "src.tif",
    )
    mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", side_effect=_fake_crop
    )
    ef.floods("X").download()
    index_env.autodetect_downloads = False
    cat = ef.floods("X")
    assert "path" not in cat.columns  # opt-out honoured


def test_floods_parquet_index_parity(built_index, mocker):
    """floods() works end-to-end against a real COG + Parquet dictionary."""
    lon, lat = built_index.lon, built_index.lat
    mocker.patch(
        "euroflood.services.geocoding.GeocodingService.get_geometry",
        return_value=box(lon - 0.05, lat - 0.05, lon + 0.05, lat + 0.05),
    )
    cat = ef.floods("X")
    assert len(cat) == 1
    assert cat.iloc[0]["event_id"] == 1
    assert cat.iloc[0]["date"] == "2020-05-01"
    assert ef.floods("X", start="2018", end="2021").shape[0] == 1


def test_roi_outside_returns_empty_frame(index_env, mocker):
    _patch_geocoder(mocker, geom=box(0.0, 0.0, 0.1, 0.1))  # far from the index
    cat = ef.floods("Nowhere")
    assert isinstance(cat, FloodFrame)
    assert len(cat) == 0


def test_discovery_rejects_incompatible_grid(index_env, mocker):
    """A manifest with a mismatched grid fingerprint is rejected."""
    mpath = index_env.get_manifest_path()
    data = json.loads(mpath.read_text())
    data["grid"]["resolution"] = 0.5  # not the real GlobalGrid resolution
    mpath.write_text(json.dumps(data))

    _patch_geocoder(mocker)
    with pytest.raises(CacheSchemaError):
        ef.floods("X")


def test_discovery_rejects_missing_manifest(index_env, mocker):
    """A cache without a manifest is rejected (re-run export)."""
    index_env.get_manifest_path().unlink()

    _patch_geocoder(mocker)
    with pytest.raises(CacheSchemaError):
        ef.floods("X")


# --- FloodFrame._constructor --------------------------
def test_floodframe_constructor_preserved_through_reset_index(index_env, mocker):
    """A reshape (reset_index) keeps the FloodFrame type via _constructor."""
    _patch_geocoder(mocker)
    cat = ef.floods("X")
    reshaped = cat.reset_index(drop=True)
    assert isinstance(reshaped, FloodFrame)
    assert reshaped._constructor is FloodFrame


# --- index raster missing ----------------------------
def test_query_raises_when_index_raster_missing(index_env, mocker):
    """A valid manifest+dictionary but no index TIF -> FileNotFoundError."""
    _patch_geocoder(mocker)
    pipeline = DiscoveryPipeline(settings=index_env)  # built while the index exists
    index_env.get_index_tif_path().unlink()  # then it disappears (e.g. pull failed)
    with pytest.raises(FileNotFoundError, match="Index raster not found"):
        pipeline.query("X")


# --- no-floods empty frame (discovery 173-174) ----------------------------
def test_roi_inside_bounds_but_no_floods_returns_empty(index_env, mocker):
    """An ROI fully on the zero (non-flooded) region yields an empty frame."""
    # The sample index flags only the diagonal; this small box hits a 0 cell.
    _patch_geocoder(mocker, geom=box(10.055, 49.945, 10.06, 49.95))
    cat = ef.floods("DryCorner")
    assert isinstance(cat, FloodFrame)
    assert len(cat) == 0


# --- streamed index read ----------------------------------------------------
def _write_tiled_index(path, blocksize=16):
    """A 65x65 multi-block uint32 index (ragged edge blocks) with random combos."""
    rng = np.random.default_rng(42)
    data = rng.integers(0, 5, size=(65, 65)).astype(np.uint32)  # ids 0..4, 0 = dry
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=65,
        width=65,
        count=1,
        dtype="uint32",
        crs="EPSG:4326",
        transform=from_origin(10.0, 50.0, 0.01, 0.01),
        nodata=0,
        tiled=True,
        blockxsize=blocksize,
        blockysize=blocksize,
    ) as dst:
        dst.write(data, 1)


def _dense_reference(src, roi):
    """Reference result: mask the whole ROI window densely, pool nonzero counts."""
    try:
        out, _ = rasterio.mask.mask(src, [roi], crop=True)
    except ValueError:
        return None
    flat = out[out != 0]
    ids, counts = np.unique(flat, return_counts=True)
    return {int(i): int(c) for i, c in zip(ids, counts, strict=True)}


@pytest.mark.parametrize("chunk_px", [4096, 16, 32])  # 1 chunk / per-block / 2x2
@pytest.mark.parametrize(
    "roi",
    [
        Polygon([(10.02, 49.98), (10.62, 49.90), (10.10, 49.40)]),  # irregular
        box(10.003, 49.717, 10.417, 49.999),  # bbox not aligned to pixel edges
        box(9.0, 49.0, 11.0, 51.0),  # covers the whole raster (interior fast path)
        box(10.101, 49.899, 10.104, 49.902),  # sub-pixel sliver
    ],
    ids=["polygon", "bbox_unaligned", "covers_all", "subpixel"],
)
def test_stream_combo_counts_matches_dense_mask(tmp_path, mocker, roi, chunk_px):
    """The streamed read returns exactly what the dense mask+unique used to."""
    import euroflood.pipelines.discovery as disc

    tif = tmp_path / "index.tif"
    _write_tiled_index(tif)
    mocker.patch.object(disc, "_STREAM_CHUNK_PX", chunk_px)
    with rasterio.open(tif) as src:
        assert disc._stream_combo_counts(src, roi) == _dense_reference(src, roi)


def test_stream_combo_counts_disjoint_roi_returns_none(tmp_path):
    """An ROI entirely off the raster reports None (-> 'roi_outside_bounds')."""
    from euroflood.pipelines.discovery import _stream_combo_counts

    tif = tmp_path / "index.tif"
    _write_tiled_index(tif)
    with rasterio.open(tif) as src:
        assert _stream_combo_counts(src, box(0.0, 0.0, 1.0, 1.0)) is None


def test_query_exact_pixel_counts_with_tiny_chunks(index_env, mocker):
    """area_km2 reflects the exact pooled pixel count, also across many chunks."""
    import euroflood.pipelines.discovery as disc
    from euroflood.pipelines.discovery import _cell_area_km2

    roi = box(9.0, 49.0, 11.0, 51.0)
    _patch_geocoder(mocker, geom=roi)
    mocker.patch.object(disc, "_STREAM_CHUNK_PX", 1)  # snaps up to one block/chunk
    cat = ef.floods("X")
    # The sample index floods exactly the 10-px diagonal with combo 1 -> event 10.
    assert len(cat) == 1
    assert cat["area_km2"].iloc[0] == round(10 * _cell_area_km2(roi), 3)


# --- combo_id absent from dictionary -----------------
def test_query_skips_combo_missing_from_dictionary(index_env, mocker):
    """A combo_id present in the raster but absent from the dictionary is skipped."""
    # Empty the dictionary's combos so every sampled combo_id resolves to None.
    dic = json.loads(index_env.get_dictionary_path().read_text())
    dic["combos"] = {}
    index_env.get_dictionary_path().write_text(json.dumps(dic))
    _patch_geocoder(mocker)
    cat = ef.floods("X")
    assert len(cat) == 0  # nothing resolved -> no rows


# --- event missing global_id -------------------------
def test_query_skips_event_without_global_id(index_env, mocker):
    """An event entry lacking a global_id is skipped."""
    dic = json.loads(index_env.get_dictionary_path().read_text())
    # Drop global_id from the single event so the row is filtered out.
    dic["combos"]["1"]["events"][0].pop("global_id")
    index_env.get_dictionary_path().write_text(json.dumps(dic))
    _patch_geocoder(mocker)
    cat = ef.floods("X")
    assert len(cat) == 0


# --- download_catalogue edge/error paths ----------------------------------
def _dl_catalogue(settings, **overrides):
    """A 1-row download catalogue with sensible defaults, overridable per test."""
    row = {
        "collection": "historic",
        "event_id": 10,
        "date": "2020-05-01",
        "download_url": "http://mock/2020/WD_MERGE_2020_a.tif",
        "filename": "WD_MERGE_2020_a.tif",
        "geometry": box(10.0, 49.0, 11.0, 50.0),
    }
    row.update(overrides)
    return gpd.GeoDataFrame([row], geometry="geometry", crs="EPSG:4326")


def test_download_catalogue_skips_rows_without_url_or_filename(index_env, mocker):
    """Rows missing url or filename are skipped."""
    mock_dl = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file"
    )
    cat = _dl_catalogue(index_env, download_url=None, filename=None)
    paths = download_catalogue(cat, index_env.output_dir, settings=index_env)
    assert paths == []
    mock_dl.assert_not_called()  # we never even attempted a download


def test_download_catalogue_skips_when_download_returns_none(index_env, mocker):
    """A failed download (download_file -> None) is skipped."""
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=None,
    )
    crop = mocker.patch("euroflood.services.raster_ops.RasterOps.crop_raster")
    cat = _dl_catalogue(index_env)
    paths = download_catalogue(cat, index_env.output_dir, settings=index_env)
    assert paths == []
    crop.assert_not_called()  # never reached cropping


def test_download_catalogue_skips_when_crop_fails(index_env, mocker):
    """crop_raster returning False yields no output path."""
    src = index_env.cache_dir / "src.tif"
    src.write_bytes(b"x")
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=src,
    )
    mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", return_value=False
    )
    cat = _dl_catalogue(index_env)
    paths = download_catalogue(cat, index_env.output_dir, settings=index_env)
    assert paths == []


def test_download_catalogue_copies_whole_when_crop_false(index_env, mocker):
    """crop=False copies the source raster whole (discovery lines 263-264)."""
    src = index_env.cache_dir / "src.tif"
    src.write_bytes(b"whole-file-bytes")
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=src,
    )
    crop = mocker.patch("euroflood.services.raster_ops.RasterOps.crop_raster")
    cat = _dl_catalogue(index_env)
    paths = download_catalogue(
        cat, index_env.output_dir, crop=False, settings=index_env
    )
    assert len(paths) == 1
    assert paths[0].read_bytes() == b"whole-file-bytes"  # a real copy
    crop.assert_not_called()  # crop=False bypasses cropping entirely


def test_download_catalogue_copies_whole_when_geometry_missing(index_env, mocker):
    """crop=True but a None geometry also falls through to the whole-file copy."""
    src = index_env.cache_dir / "src.tif"
    src.write_bytes(b"data")
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        return_value=src,
    )
    cat = _dl_catalogue(index_env, geometry=None)
    paths = download_catalogue(cat, index_env.output_dir, settings=index_env)
    assert len(paths) == 1
    assert paths[0].read_bytes() == b"data"


# --- _dispatch_download routes hazard catalogues (discovery 286-288) -------
def test_dispatch_download_routes_to_hazard(index_env, mocker):
    """A catalogue with collection == 'hazard' is routed to the hazard path."""
    hz = mocker.patch(
        "euroflood.pipelines.hazard.download_hazard_catalogue",
        return_value=[index_env.output_dir / "hazard_RP100.tif"],
    )
    cat = gpd.GeoDataFrame(
        {
            "collection": ["hazard"],
            "return_period": [100],
            "filename": ["hazard_RP100.tif"],
            "geometry": [box(10.0, 49.0, 11.0, 50.0)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )
    paths = _dispatch_download(cat, index_env.output_dir, settings=index_env)
    assert len(paths) == 1
    hz.assert_called_once()


def test_dispatch_download_routes_to_historic(index_env, mocker):
    """A non-hazard catalogue falls through to download_catalogue."""
    dc = mocker.patch(
        "euroflood.pipelines.discovery.download_catalogue", return_value=[]
    )
    cat = _dl_catalogue(index_env)
    _dispatch_download(cat, index_env.output_dir, settings=index_env)
    dc.assert_called_once()


# --- offline mirror (floods depth maps) ------------------------------------
def _cache_source(settings, filename, data=b"depthraster"):
    """Write a fake source raster into the download cache; return its path."""
    dl_dir = settings.cache_dir / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    p = dl_dir / filename
    p.write_bytes(data)
    return p


def test_mirror_floods_downloads_event_sources(index_env, mocker):
    """mirror_floods fetches the ROI events' source rasters + writes a ledger."""
    from euroflood.pipelines.discovery import mirror_floods

    _patch_geocoder(mocker)  # "X" -> box covering the index
    dl_dir = index_env.cache_dir / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=lambda url, filename, **_k: _cache_source(index_env, filename),
    )
    res = mirror_floods("X")
    assert res.downloaded == 1 and res.n_expected == 1
    led = json.loads(index_env.get_floods_mirror_path().read_text())
    rec = led["mirror"]["tiles"]["WD_MERGE_2020_a.tif"]
    assert rec["event_id"] == 10 and rec["size_bytes"] > 0


def test_mirror_floods_dry_run_no_download(index_env, mocker):
    from euroflood.pipelines.discovery import mirror_floods

    _patch_geocoder(mocker)
    dl = mocker.patch("euroflood.services.downloader.DownloadService.download_file")
    res = mirror_floods("X", dry_run=True)
    assert res.downloaded == 0 and res.n_expected == 1
    assert res.missing == ["WD_MERGE_2020_a.tif"]
    dl.assert_not_called()


def test_floods_offline_missing_source_raises(index_env, mocker, mock_requests_get):
    from euroflood.exceptions import ProcessingError

    _patch_geocoder(mocker)
    index_env.offline = True  # nothing cached
    with pytest.raises(ProcessingError, match="EUROFLOOD_OFFLINE") as exc:
        ef.floods("X").download(index_env.output_dir)
    # The remediation names the offline switch, not a no-op index_mode change.
    assert "mirror floods" in str(exc.value) and "index_mode='auto'" not in str(
        exc.value
    )
    mock_requests_get.assert_not_called()


def test_floods_offline_uses_cached_source_no_network(
    index_env, mocker, mock_requests_get
):
    _patch_geocoder(mocker)
    _cache_source(index_env, "WD_MERGE_2020_a.tif")
    mocker.patch(
        "euroflood.services.raster_ops.RasterOps.crop_raster", side_effect=_fake_crop
    )
    index_env.offline = True
    dl = ef.floods("X").download(index_env.output_dir)
    assert len(dl.files) == 1
    mock_requests_get.assert_not_called()  # cache hit, no HTTP


def test_verify_floods_present_then_missing(index_env, mocker):
    from euroflood.pipelines.discovery import mirror_floods, verify_floods_mirror

    _patch_geocoder(mocker)
    mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=lambda url, filename, **_k: _cache_source(index_env, filename),
    )
    mirror_floods("X")
    assert verify_floods_mirror("X").ok
    (index_env.cache_dir / "downloads" / "WD_MERGE_2020_a.tif").unlink()
    rep = verify_floods_mirror("X")
    assert not rep.ok and "WD_MERGE_2020_a.tif" in rep.missing


# --- mirror index + offline toggle -----------------------------------------
def test_mirror_index_calls_repository(mock_settings, mocker):
    from euroflood.pipelines.discovery import mirror_index

    m = mocker.patch("euroflood.pipelines.discovery.IndexRepository.mirror")
    mirror_index()
    m.assert_called_once_with(include_cog=True)


def test_mirror_index_dry_run_no_network(mock_settings, mocker):
    from euroflood.pipelines.discovery import mirror_index

    m = mocker.patch("euroflood.pipelines.discovery.IndexRepository.mirror")
    res = mirror_index(dry_run=True)
    m.assert_not_called()
    assert res.downloaded == 0 and res.n_expected == 5


def test_offline_toggle_flips_both_collections(mock_settings):
    import euroflood as ef
    from euroflood.services.index_repository import IndexRepository

    assert not mock_settings.offline_floods and not mock_settings.offline_hazard
    ef.offline()
    assert mock_settings.offline
    assert mock_settings.offline_floods and mock_settings.offline_hazard
    assert mock_settings.effective_geocoder_backend == "local"
    assert not IndexRepository(settings=mock_settings).is_remote
    ef.offline(False)
    assert not mock_settings.offline
