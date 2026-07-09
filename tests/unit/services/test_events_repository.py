"""Tests for EventsRepository (normalized events.parquet lookup)."""

import pandas as pd

from euroflood.services.events_repository import EventsRepository

_ROWS = [
    {
        "global_id": 7,
        "start_date": "2020-03-04",
        "end_date": "2020-03-09",
        "year": "2020",
        "cluster_id": "2",
        "filename": "WD_x.tif",
        "download_url": "http://m/x.tif",
    },
    {
        "global_id": 8,
        "start_date": "2021-01-01",
        "end_date": None,
        "year": "2021",
        "cluster_id": "3",
        "filename": "WD_y.tif",
        "download_url": "http://m/y.tif",
    },
]


def test_lookup_returns_metadata(mock_settings):
    """Present ids resolve to full metadata; absent ids are omitted."""
    pd.DataFrame(_ROWS).to_parquet(mock_settings.get_events_path())
    repo = EventsRepository(settings=mock_settings)
    assert repo.available

    out = repo.lookup_events([7, 8, 999])
    assert set(out) == {7, 8}
    assert out[7]["filename"] == "WD_x.tif"
    assert out[7]["global_id"] == 7
    assert out[7]["start_date"] == "2020-03-04"
    assert out[8]["year"] == "2021"


def test_missing_table_and_empty_ids(mock_settings):
    """No table, or no ids, resolves to an empty dict (never raises)."""
    repo = EventsRepository(settings=mock_settings)
    assert not repo.available
    assert repo.lookup_events([1]) == {}  # table absent

    pd.DataFrame(_ROWS).to_parquet(mock_settings.get_events_path())
    assert EventsRepository(settings=mock_settings).lookup_events([]) == {}


def test_metadata_less_table(mock_settings):
    """A no-inventory build writes only global_id; ids still resolve (no metadata)."""
    pd.DataFrame({"global_id": [42]}).to_parquet(mock_settings.get_events_path())
    out = EventsRepository(settings=mock_settings).lookup_events([42])
    assert out[42]["global_id"] == 42
    assert "filename" not in out[42]
