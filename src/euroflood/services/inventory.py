"""Repository for managing the Inventory CSV.

This module abstracts the reading and writing of the inventory CSV file,
which acts as the local database of available flood maps.
"""

from typing import Any

import pandas as pd
import structlog

from ..config import Settings, get_settings
from ..schemas import FloodRecord

logger = structlog.get_logger(__name__)


class InventoryRepository:
    """Handles loading, saving, and querying the flood map inventory.

    The inventory is stored as a CSV file defined in `settings.inventory_filename`.
    It uses Pydantic for validation before saving to ensure data integrity.

    Attributes:
        csv_path (Path): Path to the inventory CSV file.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initiate Inventory Repository.

        Args:
            settings: Optional configuration. Defaults to `get_settings`.
        """
        self.settings = settings or get_settings()
        self.csv_path = self.settings.get_inventory_path()

    def save(self, records: list[dict[str, Any]]) -> None:
        """Validate and save records to CSV.

        Converts raw dictionaries into `FloodRecord` Pydantic models to validate them,
        then serializes them to a CSV file.

        Args:
            records (List[Dict[str, Any]]): List of raw dictionaries obtained from the scraper.

        Note:
            If `records` is empty, no file is written and a warning is logged.
        """
        if not records:
            logger.warning("inventory_save_empty")
            return

        # Validate via Pydantic (fail fast if scraper broke)
        validated_data = [FloodRecord(**r).model_dump() for r in records]

        df = pd.DataFrame(validated_data)
        df.to_csv(self.csv_path, index=False)
        logger.info("inventory_saved", path=str(self.csv_path), count=len(df))

    def load(self) -> pd.DataFrame:
        """Load the inventory into a pandas DataFrame.

        Returns:
            pd.DataFrame: DataFrame containing the inventory.
                          Returns an empty DataFrame if the file does not exist.
        """
        if not self.csv_path.exists():
            return pd.DataFrame()
        return pd.read_csv(self.csv_path)

    def exists(self) -> bool:
        """Check if inventory file exists.

        Returns:
            bool: True if the inventory CSV exists, False otherwise.
        """
        return self.csv_path.exists()
