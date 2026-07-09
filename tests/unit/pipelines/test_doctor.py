"""Tests for IndexDoctor (the pre-publish validation + reporting gate)."""

import numpy as np
import pytest
import rasterio

from euroflood.core.manifest import write_publish_manifest
from euroflood.exceptions import CacheSchemaError
from euroflood.pipelines.doctor import IndexDoctor


def test_doctor_passes_and_reports(built_index):
    report = IndexDoctor(settings=built_index.settings).run()
    assert report["overviews"]  # true COG overviews
    assert report["n_combos"] >= 1
    assert report["populated_cells"] == 2  # the two flooded cells in the fixture
    assert report["cog_size_bytes"] > 0
    assert report["sparse_parquet_estimate_bytes"] == 2 * 12


def test_doctor_fails_on_missing_cog(built_index):
    built_index.settings.get_index_tif_path().unlink()
    with pytest.raises(CacheSchemaError, match="missing"):
        IndexDoctor(settings=built_index.settings).run()


def test_doctor_fails_on_checksum_mismatch(built_index):
    # Corrupt the COG so its checksum no longer matches the manifest.
    built_index.settings.get_index_tif_path().write_bytes(b"corrupt")
    with pytest.raises(CacheSchemaError, match="Checksum mismatch"):
        IndexDoctor(settings=built_index.settings).run()


def _resync_manifest(settings) -> None:
    """Rewrite the publish manifest so its checksums match the on-disk files.

    The doctor validates the manifest (checksums) *before* its own COG checks, so
    after tampering the COG we must re-author the manifest from what's actually on
    disk. Otherwise the manifest checksum/missing-file gate fires first and we
    never reach the doctor's tiled/nodata/overview/dictionary branches.
    """
    files = {
        settings.index_filename: settings.get_index_tif_path(),
        settings.dictionary_meta_filename: settings.get_dictionary_meta_path(),
    }
    dict_path = settings.get_dictionary_parquet_path()
    if dict_path.exists():
        files[settings.dictionary_parquet_filename] = dict_path
    write_publish_manifest(
        settings.get_manifest_path(),
        index_version=settings.index_version,
        files={k: v for k, v in files.items() if v.exists()},
    )


def _rewrite_cog(settings, data, *, tiled, nodata, overviews):
    """Overwrite the index COG with a controlled raster, then resync the manifest.

    Lets each test isolate a single non-conforming property (untiled / wrong
    nodata / missing overviews) without the others getting in the way.
    """
    path = settings.get_index_tif_path()
    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": str(data.dtype),
        "crs": "EPSG:4326",
        "transform": rasterio.transform.from_origin(10.0, 50.0, 0.001, 0.001),
        "nodata": nodata,
        "tiled": tiled,
    }
    if tiled:
        profile["blockxsize"] = 512
        profile["blockysize"] = 512
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
        if overviews:
            dst.build_overviews([2, 4], rasterio.enums.Resampling.nearest)
    _resync_manifest(settings)


def test_doctor_fails_when_cog_absent_after_manifest_resync(built_index):
    """COG-missing branch: manifest doesn't reference the COG.

    The existing missing-COG test trips the manifest's own "Published file
    missing" gate; here we drop the COG *and* re-author the manifest without it,
    so the doctor's own ``not cog.exists()`` check is what raises.
    """
    s = built_index.settings
    s.get_index_tif_path().unlink()
    _resync_manifest(s)  # manifest no longer lists the (now absent) COG
    with pytest.raises(CacheSchemaError, match="COG is missing"):
        IndexDoctor(settings=s).run()


def test_doctor_fails_on_untiled_raster(built_index):
    """Untiled raster -> 'not tiled (not a COG)'."""
    s = built_index.settings
    data = np.ones((600, 600), dtype=np.uint32)
    _rewrite_cog(s, data, tiled=False, nodata=0, overviews=False)
    with pytest.raises(CacheSchemaError, match="not tiled"):
        IndexDoctor(settings=s).run()


def test_doctor_fails_on_wrong_nodata(built_index):
    """nodata != 0 -> the nodata branch."""
    s = built_index.settings
    data = np.ones((600, 600), dtype=np.uint32)
    _rewrite_cog(s, data, tiled=True, nodata=255, overviews=True)
    with pytest.raises(CacheSchemaError, match="nodata is"):
        IndexDoctor(settings=s).run()


def test_doctor_fails_on_missing_overviews(built_index):
    """A large tiled COG with no overviews -> 'no overviews'."""
    s = built_index.settings
    data = np.ones((600, 600), dtype=np.uint32)  # > 512 so overviews are required
    _rewrite_cog(s, data, tiled=True, nodata=0, overviews=False)
    with pytest.raises(CacheSchemaError, match="no overviews"):
        IndexDoctor(settings=s).run()


def test_doctor_fails_on_missing_combo_in_dictionary(built_index):
    """A sampled combo_id absent from the dictionary -> raise.

    We keep the COG valid and instead inflate ``n_combos`` in the meta sidecar so
    the doctor samples combo_ids that the dictionary can't resolve.
    """
    s = built_index.settings
    import json

    meta_path = s.get_dictionary_meta_path()
    meta = json.loads(meta_path.read_text())
    meta["n_combos"] = 9999  # forces sampling of ids the dictionary lacks
    meta_path.write_text(json.dumps(meta))
    _resync_manifest(s)  # keep manifest checksums consistent with the new meta
    with pytest.raises(CacheSchemaError, match="combo_ids missing"):
        IndexDoctor(settings=s).run()


def test_doctor_populated_cells_zero_when_no_parquet(built_index):
    """_populated_cells returns 0 when the Parquet lake is gone (doctor 99-100).

    Simulates validating a *pulled* bundle (COG + dictionary, no raw parquet).
    The report should still succeed and just report 0 populated cells.
    """
    import shutil

    s = built_index.settings
    shutil.rmtree(s.cache_dir / "parquet")  # remove the raw lake -> duckdb IOException
    report = IndexDoctor(settings=s).run()
    assert report["populated_cells"] == 0
    assert report["sparse_parquet_estimate_bytes"] == 0
