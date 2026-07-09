"""Tests for verify_published (fully mocked; no network / GDAL)."""

import numpy as np
import pytest

from euroflood.exceptions import VerificationError
from euroflood.pipelines import verify as V

_MANIFEST = {
    "index_version": "1.0.0",
    "grid": {"width_px": 600, "height_px": 600},
    "files": {
        "europe_flood_index.tif": {"sha256": "cog", "size_bytes": 10},
        "events.parquet": {"sha256": "ev", "size_bytes": 4},
        "dictionary_meta.json": {"sha256": "dm", "size_bytes": 2},
    },
}


def _patch_manifest(mocker, manifest=_MANIFEST):
    resp = mocker.Mock()
    resp.json.return_value = manifest
    mocker.patch("euroflood.pipelines.verify.requests.get", return_value=resp)


def test_verify_happy_path(mocker):
    _patch_manifest(mocker)
    good = {"events.parquet": ("ev", 4), "dictionary_meta.json": ("dm", 2)}
    mocker.patch.object(
        V, "_sha256_stream", side_effect=lambda url, t: good[url.rsplit("/", 1)[1]]
    )
    mocker.patch.object(
        V, "_probe_cog", side_effect=lambda v, m, r: r.record("cog", True)
    )

    def _query(base, settings, timeout, report):
        report.n_events = 3
        report.record("query", True)

    mocker.patch.object(V, "_probe_query", side_effect=_query)
    report = V.verify_published("https://x/y/v1.0.0")
    assert report.ok
    assert report.n_events == 3
    # The COG's own bytes are skipped unless deep=True.
    assert any("skipped" in d for n, _, d in report.checks if "europe_flood" in n)


def test_verify_deep_hashes_the_cog(mocker):
    _patch_manifest(mocker)
    seen: list[str] = []

    def _sha(url, timeout):
        seen.append(url.rsplit("/", 1)[1])
        return _MANIFEST["files"][url.rsplit("/", 1)[1]]["sha256"], _MANIFEST["files"][
            url.rsplit("/", 1)[1]
        ]["size_bytes"]

    mocker.patch.object(V, "_sha256_stream", side_effect=_sha)
    mocker.patch.object(
        V, "_probe_cog", side_effect=lambda v, m, r: r.record("cog", True)
    )
    report = V.verify_published("https://x/y/v1.0.0", deep=True, run_query=False)
    assert report.ok
    assert "europe_flood_index.tif" in seen  # deep hashed the COG too


def test_verify_checksum_mismatch_raises(mocker):
    _patch_manifest(mocker)
    mocker.patch.object(V, "_sha256_stream", return_value=("WRONG", 999))
    mocker.patch.object(
        V, "_probe_cog", side_effect=lambda v, m, r: r.record("cog", True)
    )
    with pytest.raises(VerificationError, match="failed"):
        V.verify_published("https://x/y/v1.0.0", run_query=False)


def test_verify_no_raise_returns_failures(mocker):
    _patch_manifest(mocker)
    mocker.patch.object(V, "_sha256_stream", return_value=("WRONG", 999))
    mocker.patch.object(
        V, "_probe_cog", side_effect=lambda v, m, r: r.record("cog", True)
    )
    report = V.verify_published(
        "https://x/y/v1.0.0", run_query=False, raise_on_error=False
    )
    assert not report.ok
    assert any("sha256" in f for f in report.failures)


def _fake_cog(mocker, *, nodata=0):
    src = mocker.MagicMock()
    src.crs.to_epsg.return_value = 4326
    src.dtypes = ["uint32"]
    src.profile = {"tiled": True}
    src.nodata = nodata
    src.overviews.return_value = [2, 4, 8]
    src.width = 600
    src.height = 600
    src.transform = "T"
    src.read.return_value = np.array([[0, 5], [0, 3]])
    ctx = mocker.MagicMock()
    ctx.__enter__.return_value = src
    mocker.patch("rasterio.open", return_value=ctx)
    mocker.patch("rasterio.Env", return_value=mocker.MagicMock())
    mocker.patch("rasterio.windows.from_bounds", return_value="win")
    return src


def test_probe_cog_records_structure(mocker):
    _fake_cog(mocker)
    report = V.VerifyReport(base_url="b")
    V._probe_cog("/vsicurl/x", {"grid": {"width_px": 600, "height_px": 600}}, report)
    assert report.ok
    names = [n for n, _, _ in report.checks]
    assert "cog_crs_4326" in names
    assert "cog_grid_matches_manifest" in names
    assert "cog_probe_window_has_data" in names


def test_probe_cog_flags_bad_nodata(mocker):
    _fake_cog(mocker, nodata=-9999)
    report = V.VerifyReport(base_url="b")
    V._probe_cog("/vsicurl/x", {"grid": {"width_px": 600, "height_px": 600}}, report)
    assert not report.ok
    assert "cog_nodata_0" in report.failures[0] or any(
        "cog_nodata_0" in f for f in report.failures
    )


def test_probe_query_records_event_count(mocker):
    frame = mocker.MagicMock()
    frame.__len__.return_value = 7
    floods = mocker.patch("euroflood.api.floods", return_value=frame)
    report = V.VerifyReport(base_url="b")
    V._probe_query("https://x/y/v1", None, 60.0, report)
    assert report.n_events == 7
    assert report.ok
    assert floods.call_args.kwargs["bbox"] == V._PROBE_BBOX
    qs = floods.call_args.kwargs["settings"]
    assert qs.index_mode == "remote"
    assert qs.index_base_url == "https://x/y/v1"
