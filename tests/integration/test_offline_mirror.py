"""Integration: hazard offline mirror -> verify -> offline download (real pipeline).

Exercises the real mirror/ledger/mosaic path with synthetic GLOFAS-shaped tiles
written to the real cache (only the HTTP fetch is stubbed), then reads them back
fully offline.
"""

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

import euroflood as ef
from euroflood.pipelines.hazard import mirror_hazard, verify_hazard_mirror

pytestmark = pytest.mark.integration

ROI_MULTI = (10.5, 50.2, 11.5, 50.8)  # spans the fixture's T_A | T_B tiles
_TILE_BBOX = {
    "ID1_T_A_RP100_depth.tif": (10.0, 50.0, 11.0, 51.0),
    "ID2_T_B_RP100_depth.tif": (11.0, 50.0, 12.0, 51.0),
}


def test_hazard_offline_mirror_verify_download_e2e(
    mock_settings, hazard_tile_index_path, mock_requests_get, mocker
):
    mock_settings.hazard_index_path = hazard_tile_index_path

    def fake_download(url, filename, **_kw):
        """Write a real GLOFAS-shaped tile into the hazard tiles cache dir."""
        dest_dir = mock_settings.get_hazard_tiles_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        minx, miny, maxx, maxy = _TILE_BBOX[filename]
        res = 0.1
        w, h = round((maxx - minx) / res), round((maxy - miny) / res)
        with rasterio.open(
            dest,
            "w",
            driver="GTiff",
            height=h,
            width=w,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=from_origin(minx, maxy, res, res),
            nodata=-9999.0,
        ) as d:
            d.write(np.full((h, w), 1.5, dtype="float32"), 1)
        return dest

    dl = mocker.patch(
        "euroflood.services.downloader.DownloadService.download_file",
        side_effect=fake_download,
    )

    # 1. Region-scoped mirror populates the cache + writes the ledger.
    res = mirror_hazard(bbox=ROI_MULTI, return_period=100)
    assert res.downloaded == 2 and res.n_expected == 2 and not res.missing
    assert dl.call_count == 2

    # 2. Deep verify passes against the just-written ledger.
    assert verify_hazard_mirror(bbox=ROI_MULTI, return_period=100, deep=True).ok

    # 3. Fully offline download reads cache-only, never the network.
    mock_settings.offline = True
    dl.reset_mock()
    out = ef.hazard(bbox=ROI_MULTI, return_period=100).download(
        mock_settings.output_dir
    )
    assert len(out.files) == 1
    with rasterio.open(out.files[0]) as src:
        assert src.tags()["EUROFLOOD_N_SOURCE_TILES"] == "2"  # both tiles mosaicked
    dl.assert_not_called()  # offline: no download_file
    mock_requests_get.assert_not_called()  # and no raw HTTP


def test_hazard_offline_missing_tile_is_remediable(
    mock_settings, hazard_tile_index_path, mock_requests_get
):
    """Offline with an un-mirrored ROI -> a clear 'mirror hazard' error, no network."""
    mock_settings.hazard_index_path = hazard_tile_index_path
    mock_settings.offline = True
    from euroflood.exceptions import HazardError

    with pytest.raises(HazardError, match="mirror hazard"):
        ef.hazard(bbox=ROI_MULTI, return_period=100).download(mock_settings.output_dir)
    mock_requests_get.assert_not_called()
