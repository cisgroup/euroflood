"""Resolve where the index artifacts live: local cache or a remote published index.

Three deployment modes (``settings.index_mode``):

- ``auto`` (default): use a complete local bundle if one is present, otherwise read the
  hosted index (the zero-config ``pip install`` path).
- ``local``: only ever read ``cache_dir``: the HPC / full-mirror / offline path.
- ``remote``: always use the hosted index (still preferring any file already cached).

When the hosted index is used, the small tables (the ~19 MB dictionary + ``events.parquet``
+ metadata + manifest) are **mirrored to the cache once, on first use** (hash-verified via
`pooch`), and the big COG is **streamed** via GDAL ``/vsicurl``: only a query's ROI
tiles are fetched, never the whole file. The base URL is the configured
``index_base_url`` or the baked `DEFAULT_INDEX_BASE_URL`; the
manifest's ``files`` map (with per-file SHA-256) is the authoritative artifact list.
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from typing import Any

import structlog

from .._data import fetch_file, http_get, resolve_base_url
from ..config import Settings, get_settings

logger = structlog.get_logger(__name__)

# GDAL tuning for the /vsicurl COG: skip per-open directory listing and keep an
# in-process block cache so a windowed read is fetched once and reused.
_GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "VSI_CACHE": "TRUE",
}

# Per-thread cache of open index datasets, keyed by source path/URL. Opening a
# remote COG re-reads its tile index over /vsicurl (hundreds of ms); reusing the
# open handle makes a repeat query in the same process near-instant. Thread-local
# so concurrent callers never share a (non-thread-safe) rasterio handle.
_local = threading.local()


def _open_cache() -> dict[str, Any]:
    cache: dict[str, Any] | None = getattr(_local, "index_datasets", None)
    if cache is None:
        cache = {}
        _local.index_datasets = cache
    return cache


def close_cached_datasets() -> None:
    """Close and clear this thread's cached index datasets (cleanup / tests)."""
    cache = getattr(_local, "index_datasets", None)
    if not cache:
        return
    for src in cache.values():
        with contextlib.suppress(Exception):  # best-effort cleanup
            src.close()
    cache.clear()


class IndexRepository:
    """Locate index artifacts locally or from a remote published index."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Bind to a settings object (defaults to `get_settings`)."""
        self.settings = settings or get_settings()

    @property
    def is_remote(self) -> bool:
        """True when the hosted index may be used (mode allows it + a base URL exists).

        Always False under the master ``offline`` switch, so a query on an offline node
        never streams the COG or mirrors tables over the network.
        """
        if self.settings.offline:
            return False
        return self.settings.index_mode in {"auto", "remote"} and bool(
            resolve_base_url(self.settings)
        )

    def index_source(self) -> str:
        """Return the path/URI to open the index COG with rasterio.

        A local file if present; otherwise a ``/vsicurl`` URL so the COG is read
        remotely, one ROI window at a time, without downloading it.
        """
        local = self.settings.get_index_tif_path()
        if local.exists() or not self.is_remote:
            return str(local)
        return f"/vsicurl/{self._url(self.settings.index_filename)}"

    def open_index(self) -> Any:
        """Return an open rasterio dataset for the index COG, cached per thread.

        Opening a remote COG re-reads its tile index over ``/vsicurl`` each time; caching
        the open handle lets a repeat ``floods()`` in one process reuse it (and GDAL's
        in-process block cache), so the second query is near-instant instead of paying the
        re-open. Local COGs are cached too (cheap, harmless). Callers must **not** close the
        returned dataset. Use `close_cached_datasets` to release the cache.
        """
        import rasterio

        source = self.index_source()
        cache = _open_cache()
        src = cache.get(source)
        if src is None or src.closed:
            with rasterio.Env(**_GDAL_ENV):
                src = rasterio.open(source)
            cache[source] = src
        return src

    def local_index_missing(self) -> bool:
        """True when the COG is neither local nor available remotely (hard error)."""
        return not self.settings.get_index_tif_path().exists() and not self.is_remote

    def ensure_tables(self) -> None:
        """Mirror the small tables (dict/events/meta/manifest) once, if hosted.

        No-op when reading a local bundle, or once the dictionary + events table are
        cached. Leaves the big COG remote (streamed via ``/vsicurl``).
        """
        if not self.is_remote:
            return
        if (
            self.settings.get_dictionary_parquet_path().exists()
            and self.settings.get_events_path().exists()
        ):
            return
        logger.info("index_mirror_tables", base_url=resolve_base_url(self.settings))
        self._mirror(include_cog=False)

    def mirror(self, *, include_cog: bool = True) -> None:
        """Download the full published bundle to the cache (offline / HPC mirror)."""
        self._mirror(include_cog=include_cog)

    # ---------------------------------------------------------------- internals
    def _url(self, rel: str) -> str:
        base = (resolve_base_url(self.settings) or "").rstrip("/")
        return f"{base}/{rel}"

    def _mirror(self, *, include_cog: bool) -> None:
        from .._progress import mirror_bar

        manifest = self._fetch_json(self.settings.manifest_filename)
        files: dict[str, Any] = manifest.get("files", {})
        todo = [
            (rel, rec)
            for rel, rec in files.items()
            if include_cog or rel != self.settings.index_filename
        ]
        # First-query cold-cache mirror: show activity in a notebook/TTY (no-op otherwise).
        with mirror_bar("Fetching index tables", total=len(todo)) as advance:
            for rel, rec in todo:
                self._download(rel, known_hash=rec.get("sha256"))
                advance(1)

    def _fetch_json(self, rel: str) -> dict[str, Any]:
        """Fetch a small JSON file, cache it locally, and return it parsed.

        Uses `http_get`, which retries a transient host failure (the
        published host intermittently 500s) instead of killing the caller's first query.
        """
        resp = http_get(self._url(rel), self.settings)
        dest = self.settings.cache_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return json.loads(resp.content)  # type: ignore[no-any-return]

    def _download(self, rel: str, *, known_hash: str | None = None) -> Path:
        """Fetch one artifact into the cache, hash-verified + cached (pooch, retried)."""
        base = resolve_base_url(self.settings)
        if base is None:  # pragma: no cover - guarded by is_remote before we get here
            raise RuntimeError("No index base URL configured.")
        return fetch_file(
            base,
            rel,
            self.settings.cache_dir,
            known_hash=known_hash,
            settings=self.settings,
        )
