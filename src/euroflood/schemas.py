"""Data schemas and the on-disk dictionary version.

Holds `FloodRecord` (one scraped source map, validated at ingest) and
``DICTIONARY_SCHEMA_VERSION``, the version stamp that guards the on-disk index
dictionary against incompatible code (see the version history below).
"""

from datetime import date

from pydantic import BaseModel, field_validator

# Version of the flood dictionary produced by the export pipeline and required by
# the discovery/extraction consumers. Bump when the on-disk shape changes.
# v3: partitioned Parquet dictionary (was a single JSON in v2).
# v4: normalized dictionary (combo_id -> flood_ids ints only); event metadata moved
#     out to a separate events.parquet, joined at query time (~23x smaller on disk).
# v5: dictionary is a SINGLE combo_id-sorted Parquet file (was ~1,574 Hive part-files);
#     one object streams/mirrors cleanly and row-group stats prune keyed lookups.
DICTIONARY_SCHEMA_VERSION = 5


class FloodRecord(BaseModel):
    """Represents a single flood event file found on the remote server."""

    global_id: int
    filename: str
    year: str
    start_date: str
    end_date: str | None = None
    cluster_id: str
    download_url: str
    # File size from the JRC listing, in bytes (used by `mirror --verify`).
    size_bytes: int | None = None

    @field_validator("start_date", "end_date")
    @classmethod
    def _valid_iso_date(cls, value: str | None) -> str | None:
        """Reject shapes that are not real ISO calendar dates (e.g. 2020-13-40).

        The previous shape-only regex matched impossible dates; this validates
        the actual calendar via ``date.fromisoformat``.
        """
        if value is None:
            return value
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{value!r} is not a valid YYYY-MM-DD date") from exc
        return value
