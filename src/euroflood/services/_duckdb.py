"""A per-thread cached DuckDB connection for the keyed Parquet lookups.

The dictionary and events repositories do a keyed ``read_parquet(...)`` per query.
Opening a fresh ``duckdb.connect()`` each time costs ~5 ms (measured) and re-reads
the Parquet footer, with zero reuse across the many queries a notebook session
makes, the same "re-initialize per call" pattern the index-COG cache fixed.

This module hands out **one connection per thread** (a DuckDB ``Connection`` is not
safe to share across threads), reused for the process lifetime. Call
`close_cached_connections` to release it (tests / explicit cleanup).
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import duckdb

_local = threading.local()


def connection() -> Any:
    """Return this thread's cached in-memory DuckDB connection (opened on first use)."""
    con = getattr(_local, "con", None)
    if con is None:
        con = duckdb.connect()
        _local.con = con
    return con


def close_cached_connections() -> None:
    """Close and drop this thread's cached DuckDB connection (cleanup / tests)."""
    con = getattr(_local, "con", None)
    if con is not None:
        with contextlib.suppress(Exception):  # best-effort cleanup
            con.close()
        _local.con = None
