"""Expand combo_ids into their flood-event id sets from the dictionary.

The export pipeline writes the NORMALIZED dictionary as partitioned Parquet (one
row per ``combo_id`` -> integer ``flood_ids``). This repository maps the combo_ids
found in an ROI to their ``flood_ids`` with a single keyed query that reads only
the needed Hive partitions. Event *metadata* is resolved separately from
``events.parquet`` (see `EventsRepository`)
— it is no longer denormalized into every combo. A legacy JSON fallback (which may
still embed ``events``) is kept for tiny/older caches.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import Settings, get_settings
from ._duckdb import connection


class DictionaryRepository:
    """Keyed lookup of flood events by combo_id (Parquet, with JSON fallback)."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Detect the dictionary format; raise if none is present."""
        self.settings = settings or get_settings()
        self._json_combos: dict[str, Any] | None = None
        if self.settings.get_dictionary_parquet_path().exists():
            self._mode = "parquet"
        elif self.settings.get_dictionary_path().exists():
            self._mode = "json"
        else:
            raise FileNotFoundError(
                "No flood dictionary found. Run 'build-index' / 'export' (or pull a "
                "published index) first."
            )

    def lookup_combos(self, combo_ids: list[int]) -> dict[str, dict[str, Any]]:
        """Return ``{str(combo_id): {flood_ids[, events]}}`` for the given ids.

        The normalized Parquet path returns ``flood_ids`` only; the legacy JSON
        path may also carry an embedded ``events`` list.
        """
        ids = [int(c) for c in combo_ids]
        if not ids:
            return {}
        return (
            self._lookup_json(ids)
            if self._mode == "json"
            else self._lookup_parquet(ids)
        )

    def _load_json(self) -> dict[str, Any]:
        if self._json_combos is None:
            with open(self.settings.get_dictionary_path()) as f:
                self._json_combos = json.load(f).get("combos", {})
        return self._json_combos

    def _lookup_json(self, ids: list[int]) -> dict[str, dict[str, Any]]:
        combos = self._load_json()
        return {str(i): combos[str(i)] for i in ids if str(i) in combos}

    def _lookup_parquet(self, ids: list[int]) -> dict[str, dict[str, Any]]:
        # Single combo_id-sorted file: DuckDB prunes to the row groups whose
        # min/max cover the requested ids (works locally and over HTTP range).
        path = str(self.settings.get_dictionary_parquet_path())
        id_list = ",".join(str(i) for i in ids)
        rows = (
            connection()
            .execute(
                "SELECT combo_id, flood_ids "
                f"FROM read_parquet('{path}') WHERE combo_id IN ({id_list})"
            )
            .fetchall()
        )
        result: dict[str, dict[str, Any]] = {}
        for combo_id, flood_ids in rows:
            result[str(int(combo_id))] = {"flood_ids": [int(x) for x in flood_ids]}
        return result
