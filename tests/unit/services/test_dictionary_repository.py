"""Tests for DictionaryRepository (Parquet keyed lookup, with JSON fallback)."""

import json

import pandas as pd
import pytest

from euroflood.pipelines.export import ExportPipeline
from euroflood.schemas import DICTIONARY_SCHEMA_VERSION
from euroflood.services.dictionary_repository import DictionaryRepository
from euroflood.services.events_repository import EventsRepository


def _build_parquet_dict(mock_settings):
    """Run a tiny export so a partitioned Parquet dictionary exists."""
    pd.DataFrame(
        [
            {
                "global_id": 7,
                "filename": "WD_2020_x.tif",
                "year": "2020",
                "start_date": "2020-03-04",
                "end_date": "2020-03-09",
                "cluster_id": "2",
                "download_url": "http://m/x.tif",
            }
        ]
    ).to_csv(mock_settings.get_inventory_path(), index=False)
    year_dir = mock_settings.cache_dir / "parquet" / "2020"
    year_dir.mkdir(parents=True)
    pd.DataFrame({"col": [1], "row": [2], "flood_id": [7]}).to_parquet(
        year_dir / "d.parquet"
    )
    ExportPipeline().run()


def test_parquet_mode_lookup(mock_settings):
    _build_parquet_dict(mock_settings)
    repo = DictionaryRepository(settings=mock_settings)
    assert repo._mode == "parquet"

    combos = repo.lookup_combos([1])
    assert combos["1"]["flood_ids"] == [7]
    assert "events" not in combos["1"]  # normalized: no embedded metadata

    # Event metadata resolves separately from events.parquet.
    event = EventsRepository(settings=mock_settings).lookup_events([7])[7]
    assert event["global_id"] == 7
    assert event["start_date"] == "2020-03-04"
    assert event["filename"] == "WD_2020_x.tif"


def test_parquet_empty_and_missing(mock_settings):
    _build_parquet_dict(mock_settings)
    repo = DictionaryRepository(settings=mock_settings)
    assert repo.lookup_combos([]) == {}
    assert repo.lookup_combos([999]) == {}  # combo not present


def test_json_fallback_mode(mock_settings):
    """A legacy JSON dictionary is read when no Parquet dir is present."""
    doc = {
        "schema_version": DICTIONARY_SCHEMA_VERSION,
        "combos": {
            "5": {"flood_ids": [10], "events": [{"global_id": 10, "year": "2019"}]}
        },
    }
    mock_settings.get_dictionary_path().write_text(json.dumps(doc))

    repo = DictionaryRepository(settings=mock_settings)
    assert repo._mode == "json"
    assert repo.lookup_combos([5])["5"]["events"][0]["global_id"] == 10
    assert repo.lookup_combos([6]) == {}


def test_missing_dictionary_raises(mock_settings):
    with pytest.raises(FileNotFoundError, match="No flood dictionary"):
        DictionaryRepository(settings=mock_settings)
