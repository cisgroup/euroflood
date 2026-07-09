"""Pipeline: Ingests raw data from JRC into the local Parquet data lake.

This pipeline acts as the primary ETL (Extract, Transform, Load) entry point for
the EuroFlood system. It manages the full lifecycle of bringing remote flood maps
into the local system, transforming them from heavy TIF rasters into optimized
sparse Parquet files.

**Architecture & Concurrency**
To handle the large volume of data (thousands of high-res rasters), this pipeline
implements a two-phase concurrent architecture:
1.  **I/O Phase (Downloading)**: Uses a `ThreadPoolExecutor`. Network operations
    release the Global Interpreter Lock (GIL), allowing multiple files to be
    downloaded simultaneously to saturate network bandwidth.
2.  **CPU Phase (Processing)**: Uses a `ProcessPoolExecutor`. Raster processing
    is CPU-intensive. Python processes bypass the GIL, allowing the system to
    utilize all available CPU cores for coordinate transformation and sparse
    matrix generation.

**Data Flow**
1.  **Scrape**: The `ScraperService` crawls the JRC website to build/update `inventory.csv`.
2.  **Filter**: The inventory is filtered by user arguments (Year, Month).
3.  **Download**: Filtered records are downloaded to `cache_dir/downloads/`.
4.  **Process**: Valid downloads are converted to `cache_dir/parquet/YYYY/...`.

Example:
    >>> from euroflood.pipelines.ingestion import IngestionPipeline
    >>> pipe = IngestionPipeline()
    >>> # Download and process all data for May 2020
    >>> pipe.run(year=2020, month="05")
"""

import json
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import structlog

from .._progress import progress_bar
from ..config import Settings, get_settings
from ..services.downloader import DownloadService
from ..services.inventory import InventoryRepository
from ..services.processor import RasterProcessor
from ..services.scraper import ScraperService
from .ledger import StateLedger

logger = structlog.get_logger(__name__)


def _filter_key(year: int | None, month: str | None) -> str:
    """A filesystem-safe key identifying the (year, month) ingest filter."""
    if year and month:
        return f"{year}-{str(month).zfill(2)}"
    if year:
        return str(year)
    return "all"


class IngestionPipeline:
    """Orchestrates the massive Download -> Process workflow.

    This class serves as the controller, instantiating necessary services and
    managing the flow of data between them. It ensures that the `cache` directories
    are prepared and handles the parallel execution logic.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initiate the Ingestion Pipeline.

        Initializes the following internal services:
        -   **Scraper**: For finding files on the web.
        -   **Inventory**: For tracking what is available.
        -   **Downloader**: For reliable file transfer.
        -   **Processor**: For converting TIFs to Parquet.

        It also ensures that `settings.cache_dir` and subdirectories exist.
        """
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()

        self.scraper = ScraperService(settings=self.settings)
        self.inventory = InventoryRepository(settings=self.settings)
        self.downloader = DownloadService(settings=self.settings)
        self.processor = RasterProcessor(settings=self.settings)

    def update_inventory(self) -> None:
        """Scrapes the remote website and updates the local inventory CSV.

        This is a blocking network operation. It fetches the directory listing
        for every year available on the JRC server and saves the metadata
        (filenames, dates, URLs) to `inventory.csv`.

        Logs:
            - Info: When scraping starts and finishes.
            - Error: If specific year directories cannot be accessed.
        """
        logger.info("pipeline_step_start", step="scrape_inventory")
        records = self.scraper.fetch_all_records()
        self.inventory.save(records)

    def _candidates(
        self, df: Any, year: int | None, month: str | None, limit: int | None
    ) -> Any:
        """Apply the year/month/shard/limit filters to the inventory (shared)."""
        if year:
            df = df[df["year"] == int(year)]
        if month and year:
            df = df[df["start_date"].str.startswith(f"{year}-{str(month).zfill(2)}")]
        sc, si = self.settings.ingest_shard_count, self.settings.ingest_shard_index
        if sc and si is not None:
            df = df[df["global_id"] % sc == si]
        return df.sort_values("global_id").head(limit) if limit else df

    def _ledger_suffix(self) -> str:
        """The ledger filename suffix for the current shard (sole-writer per shard)."""
        suffix = self.settings.ledger_suffix
        sc, si = self.settings.ingest_shard_count, self.settings.ingest_shard_index
        if sc and si is not None and not suffix:
            suffix = f"_shard{si}of{sc}"
        return suffix

    def plan(
        self,
        year: int | None = None,
        month: str | None = None,
        *,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Side-effect-free summary of what `run()` would process (for --dry-run)."""
        dest = str(self.settings.cache_dir / "parquet")
        if not self.inventory.exists():
            return {
                "total": 0,
                "pending": 0,
                "done": 0,
                "dest": dest,
                "note": "no inventory yet (run mirror/ingest --update first)",
            }
        df = self._candidates(self.inventory.load(), year, month, limit)
        ledger = StateLedger(
            self.settings.cache_dir
            / "state"
            / f"ingest_{_filter_key(year, month)}{self._ledger_suffix()}.jsonl"
        )
        pending = df[~df["global_id"].apply(ledger.is_done)]
        return {
            "total": len(df),
            "pending": len(pending),
            "done": len(df) - len(pending),
            "dest": dest,
        }

    def run(
        self,
        year: int | None = None,
        month: str | None = None,
        update: bool = False,
        limit: int | None = None,
    ) -> None:
        """Execute the ingestion pipeline with optional filters.

        This is the main entry point. It checks the local inventory, filters it
        based on the provided criteria, and queues up the work.

        Args:
            year (Optional[int]): The year to process (e.g., 2020). If None, ALL years
                in the inventory are processed (use with caution).
            month (Optional[str]): The month to filter by (e.g., "05" or "5").
                Effective only if `year` is also provided.
            update (bool): If True, forces a fresh scrape of the JRC website to update
                `inventory.csv` before processing starts. Defaults to False.
            limit (Optional[int]): If set, only the first N tiles (stable order) are
                handled — for small test runs. Defaults to None (all).

        Process:
            1.  **Inventory Check**: Loads the CSV. If empty or `update=True`, runs scraping.
            2.  **Filtering**: Narrows down the list of files to process.
            3.  **Downloading**: Runs `DownloadService.download_file` in threads.
            4.  **Processing**: Runs `RasterProcessor.process` in separate processes.

        Notes:
            -   The pipeline is robust to individual file failures. If a single TIF is corrupt,
                it logs the error and continues processing the rest of the batch.
            -   Intermediate files (downloads) are kept in the cache to avoid re-downloading
                if the pipeline is restarted.
        """
        if update or not self.inventory.exists():
            self.update_inventory()

        df = self.inventory.load()
        if df.empty:
            logger.warning("inventory_empty")
            return

        # Filter by year/month, the optional HPC shard (stable modulo on the
        # SHA1-derived global_id), and the optional --limit (small test runs).
        df = self._candidates(df, year, month, limit)
        if df.empty:
            logger.warning("no_files_found_filter", year=year, month=month)
            return
        suffix = self._ledger_suffix()

        logger.info("processing_batch_start", count=len(df))

        ledger = StateLedger(
            self.settings.cache_dir
            / "state"
            / f"ingest_{_filter_key(year, month)}{suffix}.jsonl"
        )

        # Resume: skip files already downloaded + processed in a previous run.
        pending = df[~df["global_id"].apply(ledger.is_done)]
        if len(pending) < len(df):
            logger.info("resume_skip", already_done=len(df) - len(pending))
        if pending.empty:
            logger.info("nothing_to_do")
            return

        # 1. Download Phase -> list of (local_path, global_id, year)
        downloaded_files: list[tuple[str, int, str]] = []
        with ThreadPoolExecutor(max_workers=self.settings.max_workers_dl) as io_pool:
            futures = {
                io_pool.submit(
                    self.downloader.download_file, row["download_url"], row["filename"]
                ): (row["global_id"], row["year"], row["filename"])
                for _, row in pending.iterrows()
            }

            with progress_bar("Downloading source tiles", total=len(futures)) as step:
                for fut in as_completed(futures):
                    step(1)
                    gid, year_str, filename = futures[fut]
                    try:
                        result_path = fut.result()
                    except Exception as e:
                        ledger.record(
                            gid,
                            filename=filename,
                            download_status="failed",
                            last_error=str(e),
                        )
                        continue
                    if result_path:
                        ledger.record(
                            gid, filename=filename, download_status="complete"
                        )
                        downloaded_files.append((str(result_path), gid, str(year_str)))
                    else:
                        ledger.record(
                            gid,
                            filename=filename,
                            download_status="failed",
                            last_error="download returned no file",
                        )

        # 2. Processing Phase. Futures are mapped to their global_id so a failure
        #    is attributable to a specific file (and recorded in the ledger).
        total_points = 0
        with ProcessPoolExecutor(max_workers=self.settings.max_workers_cpu) as cpu_pool:
            futures_proc = {
                cpu_pool.submit(self.processor.process, Path(p), gid, y): gid
                for p, gid, y in downloaded_files
            }

            with progress_bar("Processing tiles", total=len(futures_proc)) as step:
                for proc_fut in as_completed(futures_proc):
                    step(1)
                    gid = futures_proc[proc_fut]
                    try:
                        points = proc_fut.result()
                    except Exception as e:
                        # One bad file must not crash a multi-hour run; record + skip.
                        logger.error("process_job_error", global_id=gid, error=str(e))
                        ledger.record(gid, process_status="failed", last_error=str(e))
                        continue
                    total_points += points
                    ledger.record(
                        gid,
                        process_status="empty" if points == 0 else "complete",
                        points=points,
                    )

        # 3. Dead-letter report of any non-success files.
        failures = ledger.failures()
        if failures:
            report = (
                self.settings.cache_dir
                / "state"
                / f"failed_{_filter_key(year, month)}{suffix}.json"
            )
            report.write_text(json.dumps(failures, indent=2))
            logger.warning("ingest_failures", count=len(failures), report=str(report))

        logger.info(
            "pipeline_completed",
            total_points=total_points,
            processed=len(downloaded_files),
        )
