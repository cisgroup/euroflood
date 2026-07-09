"""Tests for the DuckDB Export Pipeline (single COG + Parquet dictionary)."""

import json

import duckdb
import pandas as pd
import pytest
import rasterio

from euroflood.core.grid import GlobalGrid
from euroflood.pipelines.export import ExportPipeline
from euroflood.schemas import DICTIONARY_SCHEMA_VERSION
from euroflood.services.dictionary_repository import DictionaryRepository
from euroflood.services.events_repository import EventsRepository


@pytest.fixture
def mock_parquet_data(mock_settings, sample_inventory_data):
    """Create a tiny inventory + Parquet lake for DuckDB to aggregate."""
    pd.DataFrame(sample_inventory_data).to_csv(
        mock_settings.get_inventory_path(), index=False
    )
    year_dir = mock_settings.cache_dir / "parquet" / "2020"
    year_dir.mkdir(parents=True)
    pd.DataFrame({"col": [10, 11], "row": [20, 21], "flood_id": [1, 1]}).to_parquet(
        year_dir / "data.parquet"
    )


def test_export_builds_cog_dictionary_manifest(mock_settings, mock_parquet_data):
    """A run produces a tiled COG, a Parquet dictionary + meta, and a manifest."""
    ExportPipeline().run()

    # COG index (tiled, uint32, nodata 0).
    cog = mock_settings.get_index_tif_path()
    assert cog.exists()
    with rasterio.open(cog) as src:
        assert src.profile["tiled"] is True
        assert src.dtypes[0] == "uint32"
        assert src.nodata == 0

    # Single Parquet dictionary file + meta.
    assert mock_settings.get_dictionary_parquet_path().is_file()
    meta = json.loads(mock_settings.get_dictionary_meta_path().read_text())
    assert meta["schema_version"] == DICTIONARY_SCHEMA_VERSION
    assert meta["format"] == "parquet-single"
    assert meta["n_combos"] >= 1
    assert meta["events"] == mock_settings.events_filename

    # The dictionary maps the combo to integer flood_ids only (no embedded events);
    # metadata lives once in events.parquet, resolved separately.
    combos = DictionaryRepository(settings=mock_settings).lookup_combos([1])
    assert combos["1"]["flood_ids"] == [1]
    assert "events" not in combos["1"]
    gid = combos["1"]["flood_ids"][0]
    event = EventsRepository(settings=mock_settings).lookup_events([gid])[gid]
    assert event["filename"].endswith(".tif")
    assert event["start_date"] == "2020-01-01"  # string, not a date object
    assert isinstance(event["global_id"], int)

    # Publish manifest with provenance, COG attestation, and per-file checksums.
    manifest = json.loads(mock_settings.get_manifest_path().read_text())
    assert manifest["cog"]["sparse"] is True
    assert manifest["provenance"]["n_source_files"] == 1
    assert manifest["index_version"] == mock_settings.index_version
    assert any(name.endswith(".tif") for name in manifest["files"])


def test_dictionary_is_normalized_no_events_column(mock_settings, mock_parquet_data):
    """The dictionary carries only combo_id+flood_ids; metadata lives in events.parquet.

    Guards the ~40x size win: the old bulky denormalized ``events`` struct column
    must not come back into the single-file dictionary.
    """
    ExportPipeline().run()
    path = str(mock_settings.get_dictionary_parquet_path())
    con = duckdb.connect()
    cols = [
        d[0]
        for d in con.execute(
            f"SELECT * FROM read_parquet('{path}') LIMIT 0"
        ).description
    ]
    con.close()
    assert "events" not in cols
    assert {"combo_id", "flood_ids"} <= set(cols)
    assert mock_settings.get_events_path().exists()


def test_export_overviews_built_above_block_size(built_index):
    """A >512px index is COG-ified with internal overviews."""
    with rasterio.open(built_index.settings.get_index_tif_path()) as src:
        assert src.overviews(1)  # non-empty => true COG overviews


def test_export_plan(mock_settings, mock_parquet_data):
    """export_plan() reports parquet/cell counts + outputs without building."""
    from euroflood.pipelines.export import export_plan

    p = export_plan(mock_settings)
    assert p["parquet_files"] == 1
    assert p["populated_cells"] == 2  # the 2 cells in mock_parquet_data
    assert p["outputs"]["index"].endswith(".tif")
    # No build happened.
    assert not mock_settings.get_index_tif_path().exists()


def test_export_plan_empty_cache(mock_settings):
    from euroflood.pipelines.export import export_plan

    p = export_plan(mock_settings)
    assert p["parquet_files"] == 0
    assert p["populated_cells"] == 0


def test_export_empty_band_chunk_skipped(mocker, mock_settings, mock_parquet_data):
    """A row-band with no pixels hits the 'continue' branch without error."""
    mocker.patch.object(GlobalGrid, "HEIGHT_PX", 8000)
    mock_settings.index_write_chunk_rows = 4000  # 2 bands; data only in the first
    ExportPipeline().run()
    assert mock_settings.get_index_tif_path().exists()


def test_export_without_inventory(mock_settings):
    """With no inventory, events carry only the global_id (no metadata)."""
    year_dir = mock_settings.cache_dir / "parquet" / "2020"
    year_dir.mkdir(parents=True)
    pd.DataFrame({"col": [5], "row": [6], "flood_id": [42]}).to_parquet(
        year_dir / "d.parquet"
    )
    ExportPipeline().run()

    combos = DictionaryRepository(settings=mock_settings).lookup_combos([1])
    assert combos["1"]["flood_ids"] == [42]
    # No inventory -> events.parquet carries only the global_id (no metadata).
    events = EventsRepository(settings=mock_settings).lookup_events([42])
    assert 42 in events
    assert not events[42].get("filename")


def test_dictionary_is_single_sorted_file(mock_settings):
    """The dictionary is one Parquet FILE, sorted by combo_id (not partition dirs)."""
    year_dir = mock_settings.cache_dir / "parquet" / "2020"
    year_dir.mkdir(parents=True)
    # two distinct combos -> combo_ids 1 and 2
    pd.DataFrame({"col": [1, 2], "row": [1, 2], "flood_id": [10, 20]}).to_parquet(
        year_dir / "d.parquet"
    )
    ExportPipeline().run()

    dict_path = mock_settings.get_dictionary_parquet_path()
    assert dict_path.is_file()  # a single file, not a partitioned directory
    con = duckdb.connect()
    ids = [
        r[0]
        for r in con.execute(
            f"SELECT combo_id FROM read_parquet('{dict_path}') ORDER BY combo_id"
        ).fetchall()
    ]
    con.close()
    assert ids == sorted(ids)  # written in combo_id order (row-group pruning)
    assert len(ids) == 2


@pytest.fixture(autouse=True)
def _restore_index_build_settings(mock_settings):
    """Snapshot/restore the index-build settings the shared conftest doesn't reset.

    Tests here mutate ``index_memory_limit``/``index_threads``/``index_predictor``/
    ``dictionary_row_group_size`` on the process-wide settings singleton; without this
    a mutation (e.g. an intentionally-invalid memory_limit) would leak into later
    tests in the same session.
    """
    snapshot = {
        k: getattr(mock_settings, k)
        for k in (
            "index_memory_limit",
            "index_threads",
            "index_predictor",
            "dictionary_row_group_size",
        )
    }
    yield
    for k, v in snapshot.items():
        setattr(mock_settings, k, v)


def test_export_applies_memory_limit_and_threads(mock_settings, mock_parquet_data):
    """Configured DuckDB memory_limit + threads are SET on the connection (108, 110).

    HPC: a fat Della node should be allowed to use its RAM/cores for the build.
    """
    mock_settings.index_memory_limit = "512MB"
    mock_settings.index_threads = 2
    pipe = ExportPipeline()
    pipe.run()

    assert mock_settings.get_index_tif_path().exists()
    with rasterio.open(mock_settings.get_index_tif_path()) as src:
        assert src.dtypes[0] == "uint32"
    assert mock_settings.get_dictionary_meta_path().exists()


def test_export_memory_limit_value_flows_to_duckdb(mock_settings, mock_parquet_data):
    """The configured memory_limit really reaches DuckDB's SET.

    An unparseable value makes DuckDB raise at SET time, proving the statement is
    issued with the configured string rather than silently ignored.
    """
    mock_settings.index_memory_limit = "not-a-real-size"
    with pytest.raises(duckdb.Error, match="Memory"):
        ExportPipeline().run()


def test_export_thread_count_flows_to_duckdb(mock_settings, mock_parquet_data):
    """The configured thread count reaches DuckDB's SET.

    A negative thread count is rejected by DuckDB at SET time, proving the value
    is applied.
    """
    mock_settings.index_threads = -1
    with pytest.raises(duckdb.Error):
        ExportPipeline().run()


def test_export_predictor_no_when_not_two(mock_settings, mock_parquet_data):
    """index_predictor=1 cogifies with PREDICTOR=NO and still builds a valid COG."""
    mock_settings.index_predictor = 1
    ExportPipeline().run()

    cog = mock_settings.get_index_tif_path()
    assert cog.exists()
    with rasterio.open(cog) as src:
        assert src.dtypes[0] == "uint32"
        assert src.profile["tiled"] is True
    manifest = json.loads(mock_settings.get_manifest_path().read_text())
    assert manifest["cog"]["predictor"] == 1
    combos = DictionaryRepository(settings=mock_settings).lookup_combos([1])
    gid = combos["1"]["flood_ids"][0]
    event = EventsRepository(settings=mock_settings).lookup_events([gid])[gid]
    assert event["filename"].endswith(".tif")


def test_write_manifest_without_dictionary(mock_settings):
    """_write_manifest tolerates a missing dictionary file.

    Defensive: if no dictionary exists (degenerate/partial build), the manifest is
    still authored, just without the dictionary file entry.
    """
    pipe = ExportPipeline()
    # n_source_files is counted from the (small) combinations table.
    pipe.con.execute(
        "CREATE TABLE combinations AS SELECT [10, 11] AS flood_ids, 1 AS combo_id"
    )
    assert not mock_settings.get_dictionary_parquet_path().exists()  # branch under test

    pipe._write_manifest()
    pipe.con.close()

    manifest = json.loads(mock_settings.get_manifest_path().read_text())
    assert manifest["provenance"]["n_source_files"] == 2
    assert not any(name.endswith(".parquet") for name in manifest["files"])


def test_export_replaces_stale_partitioned_dir(mock_settings, mock_parquet_data):
    """A prior partitioned-DIR dictionary is removed and replaced by a single FILE.

    Covers the in-place upgrade path (``_remove_path`` dir branch): a v4 build left a
    directory at the dictionary path; the v5 build must clear it and write one file.
    """
    dict_path = mock_settings.get_dictionary_parquet_path()
    dict_path.mkdir(parents=True)  # simulate the old partitioned directory
    (dict_path / "combo_bucket=999").mkdir()
    (dict_path / "combo_bucket=999" / "orphan.parquet").write_bytes(b"stale")

    ExportPipeline().run()

    assert dict_path.is_file()  # replaced the dir with a single file
    # And the fresh dictionary resolves normally.
    combos = DictionaryRepository(settings=mock_settings).lookup_combos([1])
    gid = combos["1"]["flood_ids"][0]
    event = EventsRepository(settings=mock_settings).lookup_events([gid])[gid]
    assert event["filename"].endswith(".tif")
