"""Global fixtures for EuroFlood test suite.

This module provides the testing harness, including:
1.  Temp Directory overrides for settings (Sandbox environment).
2.  Mock Data Generators (Synthetic TIFs, HTML).
3.  Network Mocking helpers.
"""

import logging
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

# Import the global settings object to override it
from euroflood.config import settings
from euroflood.logging import _configure_structlog

# Committed small REAL (CC-BY-4.0) data samples — see tests/fixtures/realdata/ATTRIBUTION.md.
REALDATA_DIR = Path(__file__).parent / "fixtures" / "realdata"


@pytest.fixture(autouse=True)
def route_logs_to_stdlib():
    """Route structlog events through stdlib logging so ``caplog`` works.

    The library only configures logging when ``setup_logging`` is called, and
    that intentionally sets ``propagate=False``. Tests instead need events to
    reach the root logger that pytest's ``caplog`` listens on, so we enable
    stdlib routing and keep the ``euroflood`` logger propagating at INFO.
    """
    _configure_structlog()
    lg = logging.getLogger("euroflood")
    lg.handlers.clear()
    lg.setLevel(logging.INFO)
    lg.propagate = True
    yield


@pytest.fixture(autouse=True)
def _reset_progress_factory():
    """Undo the CLI's ``_progress.set_factory`` so a UI factory can't leak between tests."""
    from euroflood import _progress

    yield
    _progress.reset_factory()


@pytest.fixture(autouse=True)
def _reset_index_dataset_cache():
    """Close cached open index datasets so a per-test tmp COG isn't reused or leaked."""
    from euroflood.services.index_repository import close_cached_datasets

    yield
    close_cached_datasets()


@pytest.fixture(autouse=True)
def _reset_service_caches():
    """Reset per-process service caches (DuckDB connection, NUTS load) between tests.

    Mirrors :func:`_reset_index_dataset_cache` so a per-test tmp dataset or a reused
    connection can't leak across tests.
    """
    from euroflood.services._duckdb import close_cached_connections
    from euroflood.services.geocoding import _load_nuts

    yield
    close_cached_connections()
    _load_nuts.cache_clear()


@pytest.fixture(autouse=True)
def mock_settings(tmp_path):
    """Automatically sandbox settings for every test.

    Overrides the `cache_dir` and `output_dir` to point to a temporary
    directory created by pytest. This ensures no tests touch the real
    user cache or filesystem.
    """
    # Create temp structure
    cache = tmp_path / "cache"
    output = tmp_path / "output"
    cache.mkdir()
    output.mkdir()

    # Apply overrides
    # We use the 'validated' assignment to update the Pydantic model
    settings.cache_dir = cache
    settings.output_dir = output
    settings.max_workers_dl = 1  # Sequential for easy debugging
    settings.max_workers_cpu = 1
    settings.retries = 1  # Faster failure in tests
    settings.timeout_seconds = 1

    # Reset geocoding fields to defaults so tests that mutate the singleton
    # (e.g. enabling the Nominatim fallback) don't leak into later tests.
    settings.geocoder_backend = "local"
    settings.boundary_dataset_path = None
    settings.allow_remote_geocoding = False
    settings.geocode_cache = True

    # Reset the progress toggle (tests may disable it on the singleton).
    settings.show_progress = True

    # Reset GLOFAS hazard fields for the same reason.
    settings.hazard_index_path = None
    settings.hazard_cache_tiles = True

    # Reset the cached-download auto-detect (tests may toggle it).
    settings.autodetect_downloads = True

    # Reset HPC ingestion sharding so per-test mutations don't leak.
    settings.ingest_shard_index = None
    settings.ingest_shard_count = None
    settings.ledger_suffix = ""

    # Reset index-build knobs (tests mutate these on the singleton).
    settings.index_memory_limit = None
    settings.index_threads = None
    settings.index_predictor = 2
    settings.index_write_chunk_rows = 4000
    settings.dictionary_row_group_size = 20000
    settings.max_plausible_depth = None

    # Reset index mode/URL so a test enabling remote doesn't leak. Tests default to
    # "local" (no baked default URL, deterministic offline) rather than "auto".
    settings.index_mode = "local"
    settings.index_base_url = None

    # Ensure directories exist via the method
    settings.ensure_dirs()

    yield settings

    # Cleanup is handled automatically by pytest's tmp_path fixture


@pytest.fixture
def sample_tif_path(tmp_path):
    """Generates a valid, small GeoTIFF file for testing.

    Creates a 10x10 raster with some valid data points and CRS information.
    """
    path = tmp_path / "test_flood.tif"

    # Define a small transform (top-left at 10E, 50N, pixel size 0.01 deg)
    transform = from_origin(10.0, 50.0, 0.01, 0.01)

    # Create fake data: mostly 0, diagonal is 1 (flooded)
    data = np.zeros((10, 10), dtype=np.uint8)
    np.fill_diagonal(data, 1)

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs="EPSG:4326",
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(data, 1)

    return path


@pytest.fixture
def sample_inventory_data():
    """Returns a list of sample dictionaries for the Inventory."""
    return [
        {
            "global_id": 1,
            "filename": "WD_MERGE_2020-01-01---2020-01-10_cluster_1.tif",
            "year": "2020",
            "start_date": "2020-01-01",
            "end_date": "2020-01-10",
            "cluster_id": "1",
            "download_url": "http://mock/2020/file.tif",
        }
    ]


@pytest.fixture
def hazard_tile_index_path(tmp_path):
    """A tile_extents.geojson of 4 tiles mirroring GLOFAS (id, name, bbox polygon).

    Layout (all 1-degree, test-sized): T_A and T_B abut east-west (mosaic pair),
    T_C sits north of T_A, and T_D is far away (the no-overlap case).
    """
    feats = [
        (1, "T_A", box(10.0, 50.0, 11.0, 51.0)),
        (2, "T_B", box(11.0, 50.0, 12.0, 51.0)),
        (3, "T_C", box(10.0, 51.0, 11.0, 52.0)),
        (4, "T_D", box(30.0, 30.0, 31.0, 31.0)),
    ]
    gdf = gpd.GeoDataFrame(
        {"id": [f[0] for f in feats], "name": [f[1] for f in feats]},
        geometry=[f[2] for f in feats],
        crs="EPSG:4326",
    )
    path = tmp_path / "tile_extents.geojson"
    gdf.to_file(path, driver="GeoJSON")
    return path


@pytest.fixture
def synth_rp_tile(tmp_path):
    """Factory for a tiny GLOFAS-shaped depth tile (float32, nodata -9999, EPSG:4326)."""

    def _make(name, minx, miny, maxx, maxy, *, value=1.5, res=0.1):
        path = tmp_path / f"{name}.tif"
        width = round((maxx - minx) / res)
        height = round((maxy - miny) / res)
        data = np.full((height, width), value, dtype=np.float32)
        data[0, 0] = -9999.0  # one nodata pixel (top-left)
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=height,
            width=width,
            count=1,
            dtype="float32",
            crs="EPSG:4326",
            transform=from_origin(minx, maxy, res, res),
            nodata=-9999.0,
        ) as dst:
            dst.write(data, 1)
        return path

    return _make


@pytest.fixture
def built_index(mock_settings, mocker):
    """Build a real (tiny) index bundle: COG + Parquet dictionary + manifest.

    Patches the GlobalGrid to 600x600 (> the 512 COG block, so overviews are
    built) and runs the real ExportPipeline over a 2-cell Parquet lake. Returns
    the settings plus the flooded cell's lon/lat for geocoding-based consumers.
    """
    import pandas as pd

    from euroflood.core.grid import GlobalGrid
    from euroflood.pipelines.export import ExportPipeline

    mocker.patch.object(GlobalGrid, "WIDTH_PX", 600)
    mocker.patch.object(GlobalGrid, "HEIGHT_PX", 600)

    pd.DataFrame(
        [
            {
                "global_id": 1,
                "filename": "WD_MERGE_2020_a.tif",
                "year": "2020",
                "start_date": "2020-05-01",
                "end_date": "2020-05-09",
                "cluster_id": "3",
                "download_url": "http://mock/2020/WD_MERGE_2020_a.tif",
            }
        ]
    ).to_csv(mock_settings.get_inventory_path(), index=False)

    year_dir = mock_settings.cache_dir / "parquet" / "2020"
    year_dir.mkdir(parents=True)
    pd.DataFrame({"col": [100, 101], "row": [100, 101], "flood_id": [1, 1]}).to_parquet(
        year_dir / "data.parquet"
    )

    ExportPipeline().run()

    col, row = 100.5, 100.5
    lon = GlobalGrid.ORIGIN_X + col / 1200
    lat = GlobalGrid.ORIGIN_Y - row / 1200
    return SimpleNamespace(settings=mock_settings, lon=lon, lat=lat)


@pytest.fixture
def mock_requests_get(mocker):
    """Mocks requests.Session.get globally to prevent accidental internet access.

    We patch Session.request because requests.get eventually calls that too,
    and ScraperService uses self.session.get().
    """
    mock = mocker.patch("requests.Session.request")
    mock.return_value.status_code = 200
    mock.return_value.text = "<html></html>"
    mock.return_value.headers = {}  # no Content-Length -> integrity check skipped
    # Ensure iter_content returns an empty iterator by default to avoid issues
    mock.return_value.iter_content = lambda chunk_size=1: []
    # Support context manager
    mock.return_value.__enter__ = mocker.Mock(return_value=mock.return_value)
    mock.return_value.__exit__ = mocker.Mock(return_value=None)

    # Also patch requests.get just in case someone uses it directly
    mocker.patch("requests.get", side_effect=mock)

    return mock


@pytest.fixture
def depth_tif_3035(tmp_path):
    """A float depth raster in EPSG:3035 (metres) with a NoData border + wet patch.

    Mirrors a real downloaded depth raster (non-4326 metric CRS) so the viz
    reprojection path is exercised — the synthetic 4326 fixtures never were.
    """
    data = np.zeros((20, 20), dtype="float32")  # 0 = dry (NoData)
    data[8:12, 8:12] = 2.5  # a small wet patch, depth 2.5 m
    transform = from_origin(3_200_000.0, 2_000_000.0, 100.0, 100.0)  # 100 m pixels
    path = tmp_path / "depth_3035.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=1,
        dtype="float32",
        crs="EPSG:3035",
        transform=transform,
        nodata=0.0,
    ) as dst:
        dst.write(data, 1)
    return path


# --- Real (CC-BY-4.0) data fixtures ---------------------------------------
# Small clipped samples committed under tests/fixtures/realdata/, so tests exercise
# the true code paths (real CRS reprojection, real cm depth values, the real NUTS
# schema, an end-to-end query over a real index) rather than only synthetic data.


@pytest.fixture
def realdata_depth_path():
    """Path to a real EFAS depth clip (native metric CRS, uint16 cm, nodata 0)."""
    return REALDATA_DIR / "efas_depth_clip.tif"


@pytest.fixture
def realdata_nuts_path():
    """Path to a real Eurostat NUTS subset (real schema + accented names)."""
    return REALDATA_DIR / "nuts_subset.geojson"


@pytest.fixture
def realdata_index_env(mock_settings):
    """Point ``settings`` at the committed real index bundle (read-only queries).

    The bundle uses the default cache filenames, so ``floods()`` reads it directly;
    ``output_dir`` stays in the tmp sandbox so any downloads land there, not in the
    committed fixtures. Returns the settings object.
    """
    mock_settings.cache_dir = REALDATA_DIR / "index"
    mock_settings.index_mode = "local"
    return mock_settings
