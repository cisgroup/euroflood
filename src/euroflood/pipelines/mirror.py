"""Pipeline: download-only mirror of the JRC source flood-map tiles.

Downloads every source tile to the local cache **without** processing them: the
stable "download first, then process" workflow for large bulk fetches (the full
EFAS archive is ~39 GB across ~3,610 tiles). It is resumable (already-present
files are skipped via the download cache), tracks state in a ledger (with a
dead-letter report + ``--retry-failed``), and ``--verify`` re-downloads any cached
file whose on-disk size doesn't match the size JRC lists for it.

A subsequent ``ingest`` then cache-hits every downloaded file and only processes
them into Parquet.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd
import structlog

from .._progress import progress_bar
from ..config import Settings, get_settings
from ..services.downloader import DownloadService
from ..services.inventory import InventoryRepository
from ..services.scraper import ScraperService
from .ingestion import _filter_key
from .ledger import StateLedger

logger = structlog.get_logger(__name__)


class MirrorPipeline:
    """Download all source tiles to the cache (download-only, resumable)."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Build the scraper, inventory, and downloader; ensure cache dirs exist."""
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self.scraper = ScraperService(settings=self.settings)
        self.inventory = InventoryRepository(settings=self.settings)
        self.downloader = DownloadService(settings=self.settings)

    def _limited(self, df: Any, limit: int | None) -> Any:
        """Stably take the first `limit` rows (by global_id) for small test runs."""
        return df.sort_values("global_id").head(limit) if limit else df

    def plan(
        self, year: int | None = None, *, limit: int | None = None
    ) -> dict[str, Any]:
        """Return a side-effect-free summary of what `run()` would do (for --dry-run)."""
        dest = str(self.downloader.download_dir)
        if not self.inventory.exists():
            return {
                "tiles": 0,
                "present": 0,
                "to_download": 0,
                "total_gb": 0.0,
                "dest": dest,
                "note": "no inventory yet (run without --dry-run)",
            }
        df = self.inventory.load()
        if year:
            df = df[df["year"] == int(year)]
        df = self._limited(df, limit)
        present, total_bytes = 0, 0
        for _, row in df.iterrows():
            size = row.get("size_bytes")
            if size is not None and not pd.isna(size):
                total_bytes += int(size)
            path = self.downloader.download_dir / row["filename"]
            if path.exists() and path.stat().st_size > 0:
                present += 1
        return {
            "tiles": len(df),
            "present": present,
            "to_download": len(df) - present,
            "total_gb": round(total_bytes / 1e9, 2),
            "dest": dest,
        }

    def run(
        self,
        year: int | None = None,
        *,
        update: bool = False,
        verify: bool = False,
        retry_failed: bool = False,
        limit: int | None = None,
    ) -> int:
        """Download all (optionally year-filtered) source tiles to the cache.

        Args:
            year: Mirror only this year; ``None`` mirrors all years.
            update: Force a fresh inventory scrape first.
            verify: Re-download any cached file whose on-disk size differs from the
                size JRC lists for it (catches truncated/corrupt cache files).
            retry_failed: Only re-attempt files in the dead-letter ledger.
            limit: Only handle the first `limit` tiles (stable; for small test runs).

        Returns:
            int: The number of tiles available in the cache after the run.
        """
        if update or not self.inventory.exists():
            self.inventory.save(self.scraper.fetch_all_records())

        df = self.inventory.load()
        if df.empty:
            logger.warning("inventory_empty")
            return 0
        if year:
            df = df[df["year"] == int(year)]
        df = self._limited(df, limit)

        key = _filter_key(year, None)
        ledger = StateLedger(self.settings.cache_dir / "state" / f"mirror_{key}.jsonl")

        if retry_failed:
            failed = {r["global_id"] for r in ledger.failures()}
            df = df[df["global_id"].isin(failed)]
            if df.empty:
                logger.info("nothing_to_retry")
                return 0

        logger.info("mirror_start", count=len(df), verify=verify)
        done = 0
        with ThreadPoolExecutor(max_workers=self.settings.max_workers_dl) as pool:
            futures = {
                pool.submit(self._download_one, row, verify): row["global_id"]
                for _, row in df.iterrows()
            }
            with progress_bar("Mirroring source tiles", total=len(futures)) as step:
                for fut in as_completed(futures):
                    step(1)
                    gid = futures[fut]
                    ok, err = fut.result()
                    if ok:
                        done += 1
                        ledger.record(gid, download_status="complete")
                    else:
                        ledger.record(gid, download_status="failed", last_error=err)

        failures = ledger.failures()
        if failures:
            report = self.settings.cache_dir / "state" / f"failed_mirror_{key}.json"
            report.write_text(json.dumps(failures, indent=2))
            logger.warning("mirror_failures", count=len(failures), report=str(report))

        logger.info("mirror_complete", downloaded=done, total=len(df))
        return done

    def _download_one(self, row: Any, verify: bool) -> tuple[bool, str | None]:
        """Download one tile; in verify mode pass the listed size for the cache check."""
        size = row.get("size_bytes")
        expected = (
            int(size) if (verify and size is not None and not pd.isna(size)) else None
        )
        try:
            path = self.downloader.download_file(
                row["download_url"], row["filename"], expected_size=expected
            )
            return (path is not None, None if path is not None else "no file returned")
        except Exception as exc:  # pragma: no cover - download_file catches most
            return (False, str(exc))
