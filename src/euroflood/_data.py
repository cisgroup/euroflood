"""Where a fresh ``pip install euroflood`` finds the published index.

Zero-config consumers need a default place to read the index from. This module holds
the **version-pinned base URL** of the hosted bundle and fetches its small tables with
integrity checking + caching via `pooch`. The big COG is *not* fetched here. It is
streamed a window at a time via GDAL ``/vsicurl`` (see
`IndexRepository`).

``DEFAULT_INDEX_BASE_URL`` is baked to the published Source Cooperative bundle, pinned to
an immutable ``vX.Y.Z/`` prefix so a released EuroFlood always reads the exact index it was
built against; users can still override it with ``EUROFLOOD_INDEX_BASE_URL``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pooch
import requests
import structlog
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings, get_settings

logger = structlog.get_logger(__name__)

# Version-pinned to the immutable Source Cooperative prefix so a library release maps to
# one immutable published bundle (reproducibility). Bump this when a new index version is
# published; users override it with ``EUROFLOOD_INDEX_BASE_URL``.
DEFAULT_INDEX_BASE_URL: str | None = (
    "https://data.source.coop/hackl/euroflood-index/v1.0.0"
)

# Statuses worth another attempt. The published host intermittently returns 5xx, and a
# single hiccup must not kill a first query (it previously raised straight out of
# ``ensure_tables``). Permanent client errors (404, 403, ...) are NOT retried.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class TransientHTTPError(requests.RequestException):
    """A retryable failure talking to the index host (5xx / 429 / connect / timeout)."""


def resolve_base_url(settings: Settings) -> str | None:
    """Return the configured index base URL, else the baked default (may be ``None``)."""
    return settings.index_base_url or DEFAULT_INDEX_BASE_URL


def _retrying(settings: Settings) -> Retrying:
    """Exponential-backoff retryer for transient index-host failures.

    Mirrors `DownloadService`'s policy (``settings.retries``) so every
    HTTP read of the published bundle (the manifest and the small tables) survives an
    intermittent 5xx instead of failing the caller's first query.
    """
    return Retrying(
        stop=stop_after_attempt(max(1, settings.retries)),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(TransientHTTPError),
        reraise=True,
    )


def _as_transient(exc: requests.RequestException) -> TransientHTTPError | None:
    """Return a retryable error for a transient failure, else ``None`` (permanent)."""
    if isinstance(exc, requests.ConnectionError | requests.Timeout):
        return TransientHTTPError(str(exc))
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in RETRYABLE_STATUS:
        return TransientHTTPError(str(exc))
    return None


def http_get(url: str, settings: Settings | None = None) -> requests.Response:
    """GET ``url``, retrying transient host failures with exponential backoff.

    A 5xx/429/timeout is retried up to ``settings.retries`` times; a permanent client
    error (404, 403, ...) raises immediately. Retrying it would only add latency.
    """
    settings = settings or get_settings()

    def _once() -> requests.Response:
        try:
            resp = requests.get(url, timeout=settings.timeout_seconds)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise TransientHTTPError(str(exc)) from exc
        if resp.status_code in RETRYABLE_STATUS:
            logger.warning("index_host_transient", url=url, status=resp.status_code)
            raise TransientHTTPError(f"{resp.status_code} from {url}")
        resp.raise_for_status()  # permanent 4xx: raise, do not retry
        return resp

    return _run(_retrying(settings), _once)


def fetch_file(
    base_url: str,
    rel: str,
    dest_dir: Path,
    *,
    known_hash: str | None,
    settings: Settings | None = None,
) -> Path:
    """Download ``rel`` from ``base_url`` into ``dest_dir``, hash-verified and cached.

    Delegates to `retrieve`: it skips the download when a cached copy already
    matches ``known_hash``, verifies the SHA-256 on download, and writes atomically.
    ``known_hash`` is a bare hex digest (from the manifest) or ``None`` to skip
    verification (used only for the manifest itself). Transient host failures
    (5xx / 429 / timeouts) are retried with backoff; permanent ones raise.
    """
    settings = settings or get_settings()
    url = f"{base_url.rstrip('/')}/{rel}"

    def _once() -> Path:
        try:
            path = pooch.retrieve(
                url=url,
                known_hash=f"sha256:{known_hash}" if known_hash else None,
                fname=rel,
                path=dest_dir,
            )
        except requests.RequestException as exc:
            transient = _as_transient(exc)
            if transient is None:
                raise
            logger.warning("index_host_transient", url=url, error=str(exc))
            raise transient from exc
        return Path(path)

    return _run(_retrying(settings), _once)


def _run[T](retryer: Retrying, fn: Callable[[], T]) -> T:
    """Invoke ``fn`` through ``retryer`` (a typed seam for the callers above)."""
    return retryer(fn)
