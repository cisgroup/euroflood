"""Golden end-to-end regression test: process -> export -> floods().

Locks in the Phase 3 corrections against future drift on a tiny, fully-controlled
flood raster near Cologne: grid mapping (floor), flood_id dtype (uint32), the
structured dictionary, the stable global_id, the cache manifest, and the floods()
query. A change to any of those flips a golden assertion red.
"""

import numpy as np
import pandas as pd
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import box

import euroflood as ef
from euroflood.pipelines.export import ExportPipeline
from euroflood.services.inventory import InventoryRepository
from euroflood.services.processor import RasterProcessor
from euroflood.services.scraper import _stable_global_id

pytestmark = pytest.mark.integration

FILENAME = "WD_MERGE_2020-05-01---2020-05-09_cluster_3.tif"


def test_golden_process_export_floods(mock_settings, mocker, tmp_path):
    # --- a tiny, fully-controlled flood raster: 3 wet pixels near Cologne ---
    data = np.zeros((5, 5), dtype=np.float32)
    data[1, 1] = 1.2
    data[2, 2] = 0.8
    data[3, 3] = 2.5
    src_tif = tmp_path / FILENAME
    transform = from_origin(6.90, 51.00, 0.01, 0.01)
    with rasterio.open(
        src_tif,
        "w",
        driver="GTiff",
        height=5,
        width=5,
        count=1,
        dtype="float32",
        crs=CRS.from_epsg(4326),
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(data, 1)

    gid = _stable_global_id(FILENAME)

    # --- inventory + process -> parquet ---
    InventoryRepository().save(
        [
            {
                "global_id": gid,
                "filename": FILENAME,
                "year": "2020",
                "start_date": "2020-05-01",
                "end_date": "2020-05-09",
                "cluster_id": "3",
                "download_url": "http://mock/2020/" + FILENAME,
            }
        ]
    )

    outcome = RasterProcessor().process(src_tif, gid, "2020")
    assert outcome.status == "complete"
    assert outcome.points == 3  # GOLDEN: 3 wet pixels -> 3 valid grid cells

    parquet = (
        mock_settings.cache_dir
        / "parquet"
        / "2020"
        / FILENAME.replace(".tif", f"_id{gid}.parquet")
    )
    df = pd.read_parquet(parquet)
    assert len(df) == 3
    assert str(df["flood_id"].dtype) == "uint32"
    assert df["flood_id"].iloc[0] == gid  # uint32 holds the (possibly large) hash id

    # --- export -> index + structured dict + manifest ---
    ExportPipeline().run()
    assert mock_settings.get_manifest_path().exists()

    # --- floods() query over the ROI ---
    mocker.patch(
        "euroflood.services.geocoding.GeocodingService.get_geometry",
        return_value=box(6.85, 50.90, 7.00, 51.05),
    )
    cat = ef.floods("Cologne, Germany")

    assert len(cat) == 1
    row = cat.iloc[0]
    assert row["event_id"] == gid  # GOLDEN: stable id survives the whole pipeline
    assert row["date"] == "2020-05-01"
    assert row["year"] == 2020
    assert row["cluster_id"] == "3"
    assert row["filename"] == FILENAME
    assert row["area_km2"] > 0
