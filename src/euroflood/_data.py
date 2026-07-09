"""Where a fresh ``pip install euroflood`` finds the published index.

Zero-config consumers need a default place to read the index from. This module holds
the **version-pinned base URL** of the hosted bundle and fetches its small tables with
integrity checking + caching via `pooch`. The big COG is *not* fetched here — it is
streamed a window at a time via GDAL ``/vsicurl`` (see
`IndexRepository`).

``DEFAULT_INDEX_BASE_URL`` is baked to the published Source Cooperative bundle, pinned to
an immutable ``vX.Y.Z/`` prefix so a released EuroFlood always reads the exact index it was
built against; users can still override it with ``EUROFLOOD_INDEX_BASE_URL``.
"""

from __future__ import annotations

from pathlib import Path

import pooch

from .config import Settings

# Version-pinned to the immutable Source Cooperative prefix so a library release maps to
# one immutable published bundle (reproducibility). Bump this when a new index version is
# published; users override it with ``EUROFLOOD_INDEX_BASE_URL``.
DEFAULT_INDEX_BASE_URL: str | None = (
    "https://data.source.coop/hackl/euroflood-index/v1.0.0"
)


def resolve_base_url(settings: Settings) -> str | None:
    """Return the configured index base URL, else the baked default (may be ``None``)."""
    return settings.index_base_url or DEFAULT_INDEX_BASE_URL


def fetch_file(
    base_url: str, rel: str, dest_dir: Path, *, known_hash: str | None
) -> Path:
    """Download ``rel`` from ``base_url`` into ``dest_dir``, hash-verified and cached.

    Delegates to `retrieve`: it skips the download when a cached copy already
    matches ``known_hash``, verifies the SHA-256 on download, and writes atomically.
    ``known_hash`` is a bare hex digest (from the manifest) or ``None`` to skip
    verification (used only for the manifest itself).
    """
    url = f"{base_url.rstrip('/')}/{rel}"
    path = pooch.retrieve(
        url=url,
        known_hash=f"sha256:{known_hash}" if known_hash else None,
        fname=rel,
        path=dest_dir,
    )
    return Path(path)
