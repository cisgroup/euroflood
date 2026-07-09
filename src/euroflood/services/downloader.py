"""Service for downloading files with reliability features (retry, resume).

This module provides a robust downloading mechanism using the `tenacity` library
for retries and streaming for memory efficiency.
"""

import contextlib
import os
from collections.abc import Callable
from pathlib import Path

import requests
import structlog
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Settings, get_settings

logger = structlog.get_logger(__name__)


class DownloadService:
    """Manages file downloads with caching and retry logic.

    This service ensures files are downloaded reliably to the local cache.
    It checks if files already exist to avoid redundant downloads and uses
    atomic file writing (download to .tmp -> rename) to prevent partial files.

    Attributes:
        download_dir (Path): The directory where downloaded files are stored.
    """

    def __init__(
        self, download_dir: Path | None = None, settings: Settings | None = None
    ) -> None:
        """Initialize the DownloadService.

        Args:
            download_dir (Optional[Path]): Target directory for downloads.
                Defaults to `settings.cache_dir / 'downloads'`.
            settings: Optional configuration. Defaults to `get_settings`.
        """
        self.settings = settings or get_settings()
        self.download_dir = download_dir or (self.settings.cache_dir / "downloads")
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def _download_stream(
        self,
        url: str,
        local_path: Path,
        on_bytes: Callable[[int], None] | None = None,
    ) -> None:
        """Stream download with retry.

        Wraps `_download_stream_once` in a tenacity ``Retrying`` built at
        call time, so ``settings.retries`` is read at runtime (not frozen at
        import time as a decorator argument would be).

        Args:
            url (str): The URL to download from.
            local_path (Path): The final destination path.
            on_bytes: Optional per-chunk callback ``(n_bytes) -> None`` for
                byte-level progress; called with each written chunk's size.

        Raises:
            requests.RequestException: If the download fails after all retries.
        """
        retryer = Retrying(
            stop=stop_after_attempt(self.settings.retries),
            wait=wait_exponential(multiplier=1, min=4, max=10),
            retry=retry_if_exception_type(requests.RequestException),
            reraise=True,
        )
        retryer(self._download_stream_once, url, local_path, on_bytes)

    def _download_stream_once(
        self,
        url: str,
        local_path: Path,
        on_bytes: Callable[[int], None] | None = None,
    ) -> None:
        """Stream one attempt to a unique temp file, validate, then atomically rename.

        Validates the byte count against ``Content-Length`` (when the server
        provides it for an uncompressed body) so a truncated transfer is rejected
        and retried rather than atomically committed as a "complete" cache file.
        The temp file uses an appended ``.part`` suffix (so it never collides with
        another target's name) and is removed on any failure.

        Args:
            url (str): The URL to download from.
            local_path (Path): The final destination path.
            on_bytes: Optional per-chunk callback ``(n_bytes) -> None``.

        Raises:
            requests.RequestException: If this attempt fails or is truncated.
        """
        temp_path = local_path.with_name(local_path.name + ".part")
        try:
            with requests.get(
                url, stream=True, timeout=self.settings.timeout_seconds
            ) as r:
                r.raise_for_status()
                written = 0
                with open(temp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
                        written += len(chunk)
                        if on_bytes is not None:
                            on_bytes(len(chunk))
                expected = r.headers.get("Content-Length")
                encoding = r.headers.get("Content-Encoding", "").lower()
                if (
                    expected is not None
                    and encoding in ("", "identity")
                    and written != int(expected)
                ):
                    raise requests.RequestException(
                        f"truncated download: {written} of {expected} bytes for {url}"
                    )
            os.replace(temp_path, local_path)  # atomic within one filesystem
        finally:
            if temp_path.exists():
                with contextlib.suppress(OSError):
                    temp_path.unlink()

    def download_file(
        self,
        url: str,
        filename: str,
        expected_size: int | None = None,
        *,
        on_bytes: Callable[[int], None] | None = None,
    ) -> Path | None:
        """Download a file if it doesn't exist locally (or is the wrong size).

        Checks the cache first. If the file is missing, empty, or — when
        ``expected_size`` is given — a different size than expected, it is
        (re-)downloaded. A size mismatch means a stale/corrupt cache file (e.g.
        from an older interrupted run), so it is re-fetched.

        Args:
            url (str): Remote URL.
            filename (str): Target filename to save as in the download directory.
            expected_size (Optional[int]): Expected file size in bytes. When set, a
                cached file whose size differs is treated as corrupt and re-downloaded.
            on_bytes: Optional per-chunk progress callback ``(n_bytes) -> None``.
                Intended for single, large, foreground downloads (a cache hit
                never calls it).

        Returns:
            Optional[Path]: Path to the downloaded file if successful, or None if failed.
        """
        target_path = self.download_dir / filename

        # Cache hit (optionally validated against the expected size).
        if target_path.exists() and target_path.stat().st_size > 0:
            on_disk = target_path.stat().st_size
            if expected_size is None or on_disk == expected_size:
                logger.debug("download_cache_hit", file=filename)
                return target_path
            logger.warning(
                "cache_size_mismatch",
                file=filename,
                on_disk=on_disk,
                expected=expected_size,
            )
            # Fall through: re-download (the atomic rename overwrites the bad file).

        try:
            logger.debug("download_start", file=filename)
            self._download_stream(url, target_path, on_bytes)
            logger.debug("download_success", file=filename)
            return target_path
        except Exception as e:
            logger.error("download_failed", file=filename, error=str(e))
            if target_path.exists():
                # Cleanup partial file if it exists (though temp file handles this mostly)
                with contextlib.suppress(OSError):
                    os.remove(target_path)
            return None
