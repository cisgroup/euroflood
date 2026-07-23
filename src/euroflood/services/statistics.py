"""Flood statistics derived from downloaded depth rasters.

Turns a downloaded flood-depth GeoTIFF (or a whole catalogue's worth) into
headline numbers: how deep, how large an area, how much water. Depth is read in
a **metric** CRS so that a pixel's ground area is known in metres:

- If the raster is already projected in metres (the EFAS depth case, a per-tile
  azimuthal-equidistant grid) it is used **as-is** (no resampling), so the depth
  values are exact.
- If it is stored in geographic (degree) coordinates it is reprojected to an
  equal-area CRS (EPSG:6933) with **nearest** resampling (values preserved).

"Wet" pixels are those with a positive, non-NoData depth, optionally bounded by
``settings.max_plausible_depth`` (raster units) to drop suspect spikes. EFAS depth
is centimetres, so a ``scale`` of ``0.01`` converts to metres.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog
from rasterio.warp import Resampling

from ..config import get_settings
from ..exceptions import ProcessingError
from ..services.raster_ops import RasterOps, is_hazard

logger = structlog.get_logger(__name__)

# Equal-area metric CRS (EASE-Grid 2.0 global); used only when a raster is stored
# in geographic (degree) coordinates so pixel area comes out in metres.
_EQUAL_AREA = "EPSG:6933"

# Raster-value -> metre conversion by catalogue kind: EFAS historic depth is
# centimetres (x0.01), GLOFAS hazard depth is already metres (x1.0).
_HISTORIC_SCALE = 0.01
_HAZARD_SCALE = 1.0

_NO_DOWNLOADS_MSG = (
    "No downloaded rasters. Call .download() first (e.g. ef.floods(...).download())."
)


def depth_scale_for(frame: Any) -> float:
    """Depth value->metre scale for a catalogue: 1.0 for hazard, 0.01 for historic.

    EFAS historic depth is centimetres; GLOFAS hazard depth is already metres. A
    hazard frame is identified by its ``return_period`` column (via `is_hazard`).
    """
    return _HAZARD_SCALE if is_hazard(frame) else _HISTORIC_SCALE


def depth_raster_stats(
    path: str | Path, *, scale: float = 0.01, max_depth: float | None = None
) -> dict[str, float]:
    """Summarize one downloaded depth GeoTIFF.

    Args:
        path: A downloaded depth GeoTIFF.
        scale: Multiplier applied to raster values (EFAS depth is centimetres, so
            the default ``0.01`` yields metres; pass ``1.0`` for a metres raster).
        max_depth: Optional upper bound (in raster units) above which pixels are
            treated as suspect and excluded.

    Returns:
        A dict with ``wet_pixels``, ``max_depth_m``, ``mean_depth_m``,
        ``p95_depth_m``, ``flooded_area_km2``, ``volume_m3`` and ``volume_Mm3``
        (million m³). An all-dry raster yields zeros.
    """
    # Inspect the CRS from the header first (no band read) so the raster is read
    # exactly once, reprojected to a metric grid only if it is geographic.
    src_crs = RasterOps.crs_of(path)
    if src_crs is not None and src_crs.is_geographic:
        arr, transform, _crs, nodata = RasterOps.read_array(
            path, to_crs=_EQUAL_AREA, resampling=Resampling.nearest
        )
    else:
        arr, transform, _crs, nodata = RasterOps.read_array(path)

    data = np.asarray(arr, dtype="float64")
    sentinel = 0.0 if nodata is None else float(nodata)
    wet = (data > 0) & (data != sentinel)
    if max_depth is not None:
        wet &= data <= max_depth

    depth_m = data[wet] * scale
    px_area_m2 = abs(transform.a * transform.e)
    n = int(wet.sum())
    volume_m3 = float(depth_m.sum()) * px_area_m2
    return {
        "wet_pixels": n,
        "max_depth_m": round(float(depth_m.max()), 3) if n else 0.0,
        "mean_depth_m": round(float(depth_m.mean()), 3) if n else 0.0,
        "p95_depth_m": round(float(np.percentile(depth_m, 95)), 3) if n else 0.0,
        "flooded_area_km2": round(n * px_area_m2 / 1e6, 4),
        "volume_m3": round(volume_m3, 1),
        "volume_Mm3": round(volume_m3 / 1e6, 4),
    }


def _has_path(value: Any) -> bool:
    """True for a real path cell (excludes ``None``, ``NaN`` and empty strings)."""
    return bool(pd.notna(value)) and bool(value)


def _downloaded_paths(frame: Any) -> list[Path]:
    """Downloaded raster paths recorded on ``frame`` (raises if there are none)."""
    if "path" not in getattr(frame, "columns", []):
        raise ProcessingError(_NO_DOWNLOADS_MSG)
    paths = [Path(p) for p in frame["path"].tolist() if _has_path(p)]
    if not paths:
        raise ProcessingError(_NO_DOWNLOADS_MSG)
    return paths


def _identity_fields(row: Any) -> dict[str, Any]:
    """Identifying columns for a stats row: event_id/date (historic) or return_period."""
    fields: dict[str, Any] = {}
    for col in ("event_id", "date", "return_period"):
        if col in getattr(row, "index", []):
            fields[col] = row.get(col)
    return fields


def catalogue_stats(frame: Any, *, scale: float | None = None) -> pd.DataFrame:
    """Per-event depth statistics for a catalogue's downloaded rasters.

    One row per downloaded event, carrying the identifying columns (``event_id`` /
    ``date`` for historic, ``return_period`` for hazard) alongside the
    `depth_raster_stats` fields. Reads only already-downloaded rasters.

    Args:
        frame: A catalogue (a downloaded ``FloodFrame`` or equivalent) with a
            ``path`` column.
        scale: Raster-value->metre multiplier. ``None`` (default) picks it from the
            catalogue kind via `depth_scale_for` (0.01 for EFAS cm, 1.0 for
            GLOFAS m).

    Raises:
        ProcessingError: If nothing has been downloaded.
    """
    _downloaded_paths(frame)  # guard: friendly error if nothing is on disk
    if scale is None:
        scale = depth_scale_for(frame)
    max_depth = get_settings().max_plausible_depth
    records: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        path = row.get("path")
        if not _has_path(path):
            continue
        try:
            stats = depth_raster_stats(path, scale=scale, max_depth=max_depth)
        except Exception as exc:  # one unreadable raster shouldn't sink the frame
            logger.warning("depth_stats_skip", file=str(path), error=str(exc))
            continue
        records.append({**_identity_fields(row), **stats})
    return pd.DataFrame(records)


def catalogue_summary(frame: Any, *, scale: float | None = None) -> dict[str, float]:
    """Aggregate (envelope) depth statistics across a catalogue's downloaded rasters.

    Combines the rasters into a per-pixel MAX composite and summarizes that, so
    overlapping events are counted once: the returned ``flooded_area_km2`` is the
    union footprint and ``volume_m3`` the envelope volume. Adds ``n_events`` (how
    many rasters went in). Reads only already-downloaded rasters.

    Args:
        frame: A catalogue (a downloaded ``FloodFrame`` or equivalent) with a
            ``path`` column.
        scale: Raster-value->metre multiplier. ``None`` (default) picks it from the
            catalogue kind via `depth_scale_for`.

    Raises:
        ProcessingError: If nothing has been downloaded.
    """
    paths = _downloaded_paths(frame)
    if scale is None:
        scale = depth_scale_for(frame)
    max_depth = get_settings().max_plausible_depth
    if len(paths) == 1:
        target = paths[0]
    else:
        from ..viz._raster import max_composite

        target = max_composite(paths)
    summary = depth_raster_stats(target, scale=scale, max_depth=max_depth)
    summary["n_events"] = len(paths)
    return summary
