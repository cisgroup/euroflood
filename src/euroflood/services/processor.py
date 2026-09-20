"""Service for processing Raster TIFs into sparse Parquet files.

This module converts heavy TIF raster data into an efficient sparse point representation
stored in Parquet format, aligned with the `GlobalGrid`.
"""

import os
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
import structlog
from rasterio.warp import transform
from rasterio.windows import Window

from ..config import Settings, get_settings
from ..core.grid import GlobalGrid
from ..exceptions import ProcessingError

logger = structlog.get_logger(__name__)

# Target pixels per read stripe. process() streams each raster in full-width row
# stripes of ~this many pixels so peak memory is bounded by the stripe, not the
# whole tile: these uint16 masks are mostly nodata and compress ~1000x on disk,
# but a whole-band src.read(1) inflates one tile to its full size (up to
# 60000x105000 ~ 12 GB) plus several temporaries. At ~8M pixels a stripe's arrays
# stay well under ~0.5 GB even if fully wet, so a full worker pool fits comfortably
# on a standard node.
_STRIPE_TARGET_PIXELS = 8_000_000


class ProcessOutcome(NamedTuple):
    """What `RasterProcessor.process` did with one source raster.

    Naming the outcome keeps four unrelated situations apart -- an absent source
    file, a cache hit, an untrusted CRS and a genuinely empty raster -- so the
    ledger records each one correctly: a cache hit keeps its real point count (its
    cells are in the index), and the two failures are retried by a later run.

    Attributes:
        status: One of ``complete`` (parquet written by this call), ``cached``
            (parquet already present from an earlier run), ``empty`` (raster read
            but no valid wet pixels), ``missing_file`` (source tif absent) or
            ``missing_crs`` (georeferencing absent, so unsafe to place).
        points: Grid cells stored in the parquet file. Non-zero for ``complete``
            and ``cached``; zero for every other status.
    """

    status: str
    points: int


def _cached_points(parquet_path: Path) -> int | None:
    """Row count of an already-written parquet, or None if it cannot be read.

    Reads only the Parquet footer, so a resumed run reports each cached file's
    true point count without re-reading any raster.

    Args:
        parquet_path (Path): An existing parquet written by a previous run.

    Returns:
        int | None: The stored row count, or None if the file is unreadable
            (e.g. truncated by a crash), signalling the caller to rebuild it.
    """
    try:
        return int(pq.ParquetFile(parquet_path).metadata.num_rows)
    except Exception as e:
        logger.warning(
            "cached_parquet_unreadable", file=parquet_path.name, error=str(e)
        )
        return None


class RasterProcessor:
    """Converts raw Flood TIFs into standardized sparse point data (Parquet).

    The processor performs the following steps:
    1. Reads the raster TIF in full-width row stripes (bounded memory).
    2. Filters for valid data (wet pixels > 0).
    3. Transforms pixel coordinates to WGS84 Lat/Lon.
    4. Maps these Lat/Lon coordinates to the `GlobalGrid` indices (col, row).
    5. Saves the result as a Parquet file for efficient aggregation later.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize the RasterProcessor.

        Args:
            settings: Optional configuration. Defaults to `get_settings`.
                Stored on the instance so the configuration travels with the
                processor when it is pickled to ``ProcessPoolExecutor`` workers.
        """
        self.settings = settings or get_settings()

    def process(self, tif_path: Path, global_id: int, year: str) -> ProcessOutcome:
        """Process a single TIF file.

        Args:
            tif_path (Path): Path to source TIF file.
            global_id (int): Unique ID assigned to this flood event (from inventory).
            year (str): Year string for directory organization.

        Returns:
            ProcessOutcome: Both *what happened* and the point count, so the caller
                can tell a cache hit from an empty raster from a skipped one. See
                `ProcessOutcome`.

        Raises:
            ProcessingError: If an error occurs during raster reading or processing.
        """
        if not tif_path.exists():
            logger.warning("missing_source_file", file=tif_path.name)
            return ProcessOutcome("missing_file", 0)
        if global_id > np.iinfo("uint32").max:
            raise ProcessingError(
                f"global_id {global_id} exceeds the uint32 range used for flood_id."
            )

        parquet_filename = tif_path.name.replace(".tif", f"_id{global_id}.parquet")
        year_dir = self.settings.cache_dir / "parquet" / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = year_dir / parquet_filename

        # Cache check. Report the stored row count: a cache hit is a success and its
        # cells are in the index. An unreadable parquet (torn by a crash mid-write) is
        # discarded and rebuilt below.
        if parquet_path.exists():
            cached = _cached_points(parquet_path)
            if cached is not None:
                return ProcessOutcome("cached", cached)
            parquet_path.unlink(missing_ok=True)

        try:
            with rasterio.open(tif_path) as src:
                # A missing CRS cannot be trusted
                # (treating the coordinates as WGS84 would silently mis-place every
                # pixel), so skip and flag the file. Checked up front so we never
                # read a multi-GB raster only to discard it.
                if src.crs is None:
                    logger.warning("missing_crs_skip", file=tif_path.name)
                    return ProcessOutcome("missing_crs", 0)
                needs_reproject = src.crs.to_epsg() != 4326

                # Stream the band in full-width row stripes instead of one
                # src.read(1): peak memory is then bounded by the stripe, not the
                # whole (up to ~12 GB) tile. See _STRIPE_TARGET_PIXELS.
                stripe_rows = max(1, _STRIPE_TARGET_PIXELS // src.width)
                nodata = src.nodata
                max_depth = self.settings.max_plausible_depth
                cell_parts: list[np.ndarray] = []

                for row_off in range(0, src.height, stripe_rows):
                    h = min(stripe_rows, src.height - row_off)
                    data = src.read(1, window=Window(0, row_off, src.width, h))

                    # Filter wet pixels. Use the declared NoData when present, never a
                    # fabricated sentinel (it would keep or drop real values).
                    if nodata is not None:
                        wet = (data > 0) & (data != nodata)
                    else:
                        wet = data > 0
                    if max_depth is not None:
                        wet &= data <= max_depth

                    local_rows, cols = np.where(wet)
                    if len(local_rows) == 0:
                        continue
                    rows = local_rows + row_off  # window-local -> full-raster rows

                    # Pixel centroids -> WGS84 lat/lon.
                    xs, ys = rasterio.transform.xy(
                        src.transform, rows, cols, offset="center"
                    )
                    if needs_reproject:
                        lons, lats = transform(src.crs, "EPSG:4326", xs, ys)
                        lons = np.asarray(lons)
                        lats = np.asarray(lats)
                    else:
                        lons = np.asarray(xs)
                        lats = np.asarray(ys)

                    g_cols, g_rows = GlobalGrid.latlon_to_grid(lats, lons)
                    valid = GlobalGrid.is_valid(g_cols, g_rows)
                    if not valid.any():
                        continue

                    # Collapse duplicates: the source (~20 m) is finer than the
                    # GlobalGrid (~90 m), so many source pixels (even across
                    # stripes) land in one cell. Pack (col, row) into one uint64
                    # so de-dup is a 1-D np.unique; de-dup per stripe first to keep
                    # the cross-stripe accumulator small. flood_id is constant per
                    # file, so a single (col, row) per cell suffices (the export
                    # de-duplicates again; this is purely size/speed).
                    packed = (g_cols[valid].astype(np.uint64) << np.uint64(32)) | (
                        g_rows[valid].astype(np.uint64)
                    )
                    cell_parts.append(np.unique(packed))

                if not cell_parts:
                    return ProcessOutcome("empty", 0)
                cells = np.unique(np.concatenate(cell_parts))
                final_cols = (cells >> np.uint64(32)).astype("uint32")
                final_rows = (cells & np.uint64(0xFFFFFFFF)).astype("uint32")

                # Build DataFrame. flood_id is uint32: global_id (a filename hash)
                # spans the whole archive, so uint16 (<=65535) would silently wrap.
                df = pd.DataFrame(
                    {
                        "col": final_cols,
                        "row": final_rows,
                        "flood_id": np.full(len(cells), global_id, dtype="uint32"),
                    }
                )

                table = pa.Table.from_pandas(df)
                tmp_path = parquet_path.with_suffix(".tmp")
                pq.write_table(table, tmp_path, compression="snappy")
                os.rename(tmp_path, parquet_path)

                return ProcessOutcome("complete", len(df))

        except Exception as e:
            logger.error("processing_failed", file=tif_path.name, error=str(e))
            raise ProcessingError(f"Failed to process {tif_path.name}") from e
