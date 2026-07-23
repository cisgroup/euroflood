"""Look up per-event metadata from the normalized ``events.parquet`` table.

With the normalized dictionary (v4) the combo dictionary carries only integer
``flood_ids``; the event metadata (dates, filename, download URL, …) lives once in
a small ``events.parquet`` (one row per flood event, ~1 MB for the full archive).
This repository resolves a set of ``global_id``s to their metadata with a single
keyed query. It degrades gracefully: an absent table or a metadata-less table
(only ``global_id``) returns whatever is available.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings, get_settings
from ._duckdb import connection

# The fields the discovery consumer expects per event (mirrors the old embedded
# ``events`` struct so row assembly is unchanged).
_EVENT_FIELDS = (
    "start_date",
    "end_date",
    "year",
    "cluster_id",
    "filename",
    "download_url",
)


class EventsRepository:
    """Keyed lookup of flood-event metadata by ``global_id`` (from events.parquet)."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Bind to a settings object; the table is read lazily per lookup."""
        self.settings = settings or get_settings()

    @property
    def available(self) -> bool:
        """Whether the normalized events table exists in the cache."""
        return self.settings.get_events_path().exists()

    def lookup_events(self, global_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Return ``{global_id: {metadata…}}`` for the given ids (present ones only).

        Reads only the matching rows. Missing table / ids resolve to ``{}`` /
        omission rather than raising, matching the old "skip unresolved" behaviour.
        """
        ids = [int(g) for g in global_ids]
        if not ids or not self.available:
            return {}
        id_list = ",".join(str(i) for i in ids)
        path = str(self.settings.get_events_path())
        # SELECT * (not a fixed column list) so a metadata-less table (only
        # global_id) still resolves. The connection is reused across queries.
        res = connection().execute(
            f"SELECT * FROM read_parquet('{path}') WHERE global_id IN ({id_list})"
        )
        cols = [d[0] for d in res.description]
        rows = res.fetchall()
        gid_at = cols.index("global_id")
        out: dict[int, dict[str, Any]] = {}
        for row in rows:
            record = {c: row[i] for i, c in enumerate(cols) if c in _EVENT_FIELDS}
            gid = int(row[gid_at])
            record["global_id"] = gid
            out[gid] = record
        return out
