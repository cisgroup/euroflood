"""Tests for Inventory Repository and Pydantic validation."""

import pandas as pd
import pytest
from pydantic import ValidationError

from euroflood.services.inventory import InventoryRepository


def test_inventory_save_load(mock_settings, sample_inventory_data):
    """Test round trip save and load."""
    repo = InventoryRepository()

    # Save
    repo.save(sample_inventory_data)
    assert repo.exists()

    # Load
    df = repo.load()
    assert len(df) == 1
    assert df.iloc[0]["filename"] == sample_inventory_data[0]["filename"]


def test_inventory_save_invalid_schema():
    """Test that saving invalid data raises ValidationError."""
    repo = InventoryRepository()
    invalid_data = [{"global_id": 1, "filename": "oops"}]  # Missing fields

    with pytest.raises(ValidationError):
        repo.save(invalid_data)


def test_inventory_empty_load(mock_settings):
    """Test loading non-existent file returns empty DataFrame."""
    repo = InventoryRepository()
    df = repo.load()
    assert df.empty
    assert isinstance(df, pd.DataFrame)


def test_save_empty_list(mock_settings, caplog):
    """Test saving an empty list logs a warning and does not create file."""
    repo = InventoryRepository()
    repo.save([])

    assert "inventory_save_empty" in caplog.text
    assert not repo.exists()
