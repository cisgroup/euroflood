"""Service for processing Raster TIFs into sparse Parquet files.

This module converts heavy TIF raster data into an efficient sparse point representation
stored in Parquet format, aligned with the `GlobalGrid`.
"""

import os
from pathlib import Path

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
# 60000x105000 ~ 12 GB) plus several temporaries — which OOM-killed the 32-worker
# ingest. At ~8M pixels a stripe's arrays stay well under ~0.5 GB even if fully
# wet, so a full worker pool fits comfortably on a standard node.
_STRIPE_TARGET_PIXELS = 8_000_000


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

    def process(self, tif_path: Path, global_id: int, year: str) -> int:
        """Process a single TIF file.

        Args:
            tif_path (Path): Path to source TIF file.
            global_id (int): Unique ID assigned to this flood event (from inventory).
            year (str): Year string for directory organization.

        Returns:
            int: Number of points stored in the parquet file. Returns 0 if the file
                 was empty, skipped, or had no valid pixels.

        Raises:
            ProcessingError: If an error occurs during raster reading or processing.
        """
        if not tif_path.exists():
            return 0
        if global_id > np.iinfo("uint32").max:
            raise ProcessingError(
                f"global_id {global_id} exceeds the uint32 range used for flood_id."
            )

        # Construct output path
        parquet_filename = tif_path.name.replace(".tif", f"_id{global_id}.parquet")
        year_dir = self.settings.cache_dir / "parquet" / str(year)
        year_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = year_dir / parquet_filename

        # Cache check
        if parquet_path.exists():
            return 0  # Already processed

        try:
            with rasterio.open(tif_path) as src:
                # Reproject policy (unchanged): a missing CRS cannot be trusted —
                # treating the coordinates as WGS84 would silently mis-place every
                # pixel — so skip and flag the file. Checked up front so we never
                # read a multi-GB raster only to discard it.
                if src.crs is None:
                    logger.warning("missing_crs_skip", file=tif_path.name)
                    return 0
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

                    # Filter wet pixels. Use the declared NoData when present; do
                    # NOT fabricate a 9999 sentinel (the old fallback silently kept
                    # or dropped real values for a different/absent NoData).
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
                    # GlobalGrid (~90 m), so many source pixels — even across
                    # stripes — land in one cell. Pack (col, row) into one uint64
                    # so de-dup is a 1-D np.unique; de-dup per stripe first to keep
                    # the cross-stripe accumulator small. flood_id is constant per
                    # file, so a single (col, row) per cell suffices (the export
                    # de-duplicates again; this is purely size/speed).
                    packed = (g_cols[valid].astype(np.uint64) << np.uint64(32)) | (
                        g_rows[valid].astype(np.uint64)
                    )
                    cell_parts.append(np.unique(packed))

                if not cell_parts:
                    return 0
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

                # Atomic Write
                table = pa.Table.from_pandas(df)
                tmp_path = parquet_path.with_suffix(".tmp")
                pq.write_table(table, tmp_path, compression="snappy")
                os.rename(tmp_path, parquet_path)

                return len(df)

        except Exception as e:
            logger.error("processing_failed", file=tif_path.name, error=str(e))
            raise ProcessingError(f"Failed to process {tif_path.name}") from e
