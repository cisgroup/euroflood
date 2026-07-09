"""Tests for Data Schemas."""

import pytest
from pydantic import ValidationError

from euroflood.schemas import FloodRecord


def test_flood_record_rejects_impossible_date():
    """A shape-valid but impossible date (2020-13-40) is rejected."""
    with pytest.raises(ValidationError):
        FloodRecord(
            global_id=1,
            filename="x.tif",
            year="2020",
            start_date="2020-13-40",
            cluster_id="1",
            download_url="http://x",
        )


def test_flood_record_accepts_none_end_date():
    """end_date=None passes the ISO validator unchanged."""
    record = FloodRecord(
        global_id=1,
        filename="x.tif",
        year="2020",
        start_date="2020-01-01",
        end_date=None,
        cluster_id="1",
        download_url="http://x",
    )
    assert record.end_date is None
    assert record.start_date == "2020-01-01"


def test_flood_record_accepts_valid_iso_dates():
    """Real calendar dates for both bounds validate and round-trip."""
    record = FloodRecord(
        global_id=2,
        filename="y.tif",
        year="2021",
        start_date="2021-02-28",
        end_date="2021-03-01",
        cluster_id="9",
        download_url="http://y",
    )
    assert record.start_date == "2021-02-28"
    assert record.end_date == "2021-03-01"


def test_flood_record_rejects_impossible_end_date():
    """An impossible end_date (2020-02-30) is rejected, exercising the error path."""
    with pytest.raises(ValidationError):
        FloodRecord(
            global_id=3,
            filename="z.tif",
            year="2020",
            start_date="2020-01-01",
            end_date="2020-02-30",
            cluster_id="1",
            download_url="http://z",
        )
