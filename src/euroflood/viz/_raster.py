"""Data core for the visualization module (no plotting libraries here).

Everything needed to build the **flood-recurrence** view is derived from the
already-built index: masking the index COG to the catalogue's ROI gives a
``combo_id`` per pixel, and each ``combo_id`` maps to the *set* of flood events
that touched it, so ``len(flood_ids)`` is exactly "how many times this pixel
flooded", computed with **no source-raster download**. The event dates behind
each region feed the interactive hover tooltip.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.mask
from rasterio.features import shapes
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from ..config import Settings, get_settings
from ..exceptions import VisualizationError
from ..services.dictionary_repository import DictionaryRepository
from ..services.events_repository import EventsRepository
from ..services.index_repository import _GDAL_ENV, IndexRepository
from ..services.raster_ops import RasterOps, is_hazard


def _settings_of(frame: Any) -> Settings:
    """Return the settings carried by a FloodFrame (or the global default)."""
    settings = getattr(frame, "_settings", None)
    return settings if settings is not None else get_settings()


def _mask_index_to_roi(
    frame: Any,
) -> tuple[Any, Any, Any, BaseGeometry, dict[str, Any], Settings]:
    """Mask the index COG to the catalogue's ROI (historic frames only).

    Returns ``(combo_array, transform, crs, roi, combos, settings)`` where
    ``combos`` maps ``str(combo_id) -> {"flood_ids": [...]}``. Raises
    `VisualizationError` for an empty or hazard catalogue (no combo index).
    """
    settings = _settings_of(frame)
    if len(frame) == 0:
        raise VisualizationError("Empty catalogue: nothing to visualize.")
    if is_hazard(frame):
        raise VisualizationError(
            "The recurrence/footprint view is historic-only (hazard has no combo "
            "index). Use the depth view or the context view for hazard layers."
        )
    roi = frame.geometry.iloc[0]
    index = IndexRepository(settings=settings)
    index.ensure_tables()
    # Reuse the per-process cached open COG (same handle floods() used), so the
    # recurrence/footprint views don't re-open the remote COG on every plot/explore.
    src = index.open_index()
    try:
        with rasterio.Env(**_GDAL_ENV):
            out_image, transform = rasterio.mask.mask(src, [roi], crop=True)
            crs = src.crs
    except ValueError as exc:  # ROI outside the index bounds
        raise VisualizationError(f"ROI does not overlap the index: {exc}") from exc
    combo = np.asarray(out_image[0])
    combo_ids = [int(c) for c in np.unique(combo) if c != 0]
    combos = DictionaryRepository(settings=settings).lookup_combos(combo_ids)
    return combo, transform, crs, roi, combos, settings


def _counts_by_combo(combos: dict[str, Any], event_id: int | None) -> dict[int, int]:
    """Map ``combo_id -> recorded-flood count`` (or 0/1 membership for one event)."""
    result: dict[int, int] = {}
    for cid_str, record in combos.items():
        flood_ids = record.get("flood_ids", [])
        if event_id is None:
            result[int(cid_str)] = len(flood_ids)
        else:
            result[int(cid_str)] = 1 if int(event_id) in flood_ids else 0
    return result


def _remap(combo: Any, count_by_combo: dict[int, int]) -> Any:
    """Vectorized remap of a ``combo_id`` raster to a per-pixel count raster."""
    counts = np.zeros(combo.shape, dtype="float64")
    if not count_by_combo:
        return counts
    keys = np.array(sorted(count_by_combo), dtype=combo.dtype)
    vals = np.array([count_by_combo[int(k)] for k in keys], dtype="float64")
    idx = np.clip(np.searchsorted(keys, combo), 0, len(keys) - 1)
    match = keys[idx] == combo
    return np.where(match, vals[idx], 0.0)


def recurrence_grid(
    frame: Any, *, event_id: int | None = None
) -> tuple[Any, Any, Any, BaseGeometry]:
    """Build the per-pixel flood-recurrence raster for a catalogue's ROI.

    Args:
        frame: A historic ``FloodFrame``.
        event_id: If given, render that single event's footprint (0/1) instead of
            the recurrence count.

    Returns:
        ``(counts, transform, crs, roi)``: ``counts`` is a float raster where 0
        means "never flooded" (masked out when plotted).
    """
    combo, transform, crs, roi, combos, _ = _mask_index_to_roi(frame)
    counts = _remap(combo, _counts_by_combo(combos, event_id))
    return counts, transform, crs, roi


def recurrence_regions(frame: Any, *, event_id: int | None = None) -> Any:
    """Vectorize the recurrence raster into per-region polygons with hover data.

    Each polygon carries ``count`` (recorded floods), a concise ``when`` (single
    date or a first-last year range), and an ``events`` field (the deduplicated
    dates joined by ``<br>`` for a click popup). Returned in EPSG:4326.

    Args:
        frame: A historic ``FloodFrame``.
        event_id: If given, keep only regions touched by that event.

    Returns:
        A ``geopandas.GeoDataFrame`` with ``count``, ``when``, ``events``, ``geometry``.
    """
    combo, transform, crs, _roi, combos, settings = _mask_index_to_roi(frame)
    all_ids = sorted(
        {fid for rec in combos.values() for fid in rec.get("flood_ids", [])}
    )
    meta = EventsRepository(settings=settings).lookup_events(all_ids)

    records: list[dict[str, Any]] = []
    combo32 = combo.astype("int32")
    for geom, value in shapes(combo32, mask=(combo != 0), transform=transform):
        cid = int(value)
        flood_ids = combos.get(str(cid), {}).get("flood_ids", [])
        if event_id is not None and int(event_id) not in flood_ids:
            continue
        dates = sorted(
            {str(meta.get(fid, {}).get("start_date") or fid) for fid in flood_ids}
        )
        if len(dates) <= 1:
            when = dates[0] if dates else ""
        else:
            when = f"{dates[0][:4]}-{dates[-1][:4]}"  # first-last year
        records.append(
            {
                "count": 1 if event_id is not None else len(flood_ids),
                "when": when,
                "events": "<br>".join(dates),
                "geometry": shape(geom),
            }
        )
    gdf = gpd.GeoDataFrame(
        records or {"count": [], "when": [], "events": [], "geometry": []},
        geometry="geometry",
        crs=crs,
    )
    if len(gdf) and str(gdf.crs) != "EPSG:4326":
        gdf = gdf.to_crs(4326)
    return gdf


def event_footprints(frame: Any) -> Any:
    """Per-event flood extents from the index (one dissolved polygon per event).

    Replaces the catalogue's shared ROI geometry with each event's actual footprint
    (the union of ROI pixels whose ``combo_id`` includes that event) and adds an
    ``extent_km2`` column. No rasters are downloaded; the extent is the ~90 m index
    presence mask, i.e. an approximate footprint.

    Args:
        frame: A historic ``FloodFrame``.

    Returns:
        A plain ``geopandas.GeoDataFrame`` (EPSG:4326), date-sorted, with the
        catalogue's metadata, a per-event ``geometry``, ``extent_km2`` (the area
        **within the queried view**: the footprint is clipped to the ROI), and
        ``duration_days`` (event length, when ``end_date`` is available).

    Raises:
        VisualizationError: For an empty or hazard catalogue (no combo index).
    """
    combo, transform, crs, _roi, combos, _settings = _mask_index_to_roi(frame)
    regions = [
        (shape(geom), int(value))
        for geom, value in shapes(
            combo.astype("int32"), mask=(combo != 0), transform=transform
        )
    ]
    geoms_by_event: dict[int, list[Any]] = {}
    for geom, cid in regions:
        for fid in combos.get(str(cid), {}).get("flood_ids", []):
            geoms_by_event.setdefault(int(fid), []).append(geom)

    ids = [int(e) for e in frame["event_id"].tolist()]
    footprints = [unary_union(geoms_by_event.get(i, [])) for i in ids]
    metadata = {c: frame[c].to_numpy() for c in frame.columns if c != "geometry"}
    gdf = gpd.GeoDataFrame(metadata, geometry=footprints, crs=crs)
    gdf["extent_km2"] = (gdf.to_crs(6933).area / 1e6).round(3)
    if "date" in gdf.columns and "end_date" in gdf.columns:
        start = pd.to_datetime(gdf["date"], errors="coerce")
        end = pd.to_datetime(gdf["end_date"], errors="coerce")
        gdf["duration_days"] = (end - start).dt.days
    gdf = gdf[~gdf.geometry.is_empty].copy()  # events with no ROI pixels
    if "date" in gdf.columns:
        gdf = gdf.sort_values("date", kind="stable").reset_index(drop=True)
    if str(gdf.crs) != "EPSG:4326":
        gdf = gdf.to_crs(4326)
    return gdf


def valid_bounds(
    masked: Any, transform: Any
) -> tuple[float, float, float, float] | None:
    """Return ``(west, south, east, north)`` of the non-masked pixels, or ``None``.

    Used to zoom a depth view to the wet area instead of the mostly-NoData tile.
    """
    valid = ~np.ma.getmaskarray(np.ma.asarray(masked))
    if valid.ndim > 2:
        valid = valid.any(axis=0)
    row_any = np.any(valid, axis=1)
    col_any = np.any(valid, axis=0)
    if not row_any.any():
        return None
    r0 = int(np.argmax(row_any))
    r1 = len(row_any) - int(np.argmax(row_any[::-1]))
    c0 = int(np.argmax(col_any))
    c1 = len(col_any) - int(np.argmax(col_any[::-1]))
    west, north = transform * (c0, r0)
    east, south = transform * (c1, r1)
    return west, south, east, north


def depth_array(path: str | Path) -> tuple[Any, Any, Any, float | None]:
    """Read a downloaded depth GeoTIFF, reprojected to EPSG:4326 (lat/lon).

    Downloaded depth rasters keep their source CRS (a metric projection); the viz
    needs lat/lon so static axes and folium overlays are georeferenced correctly.
    """
    return RasterOps.read_array(path, to_crs="EPSG:4326")


def depth_composite(paths: list[Path], out_path: Path, *, agg: str = "max") -> Path:
    """Aggregate several depth rasters into one EPSG:4326 GeoTIFF (per-pixel max).

    Each event's depth raster may be in its own per-tile projection, so they are
    reprojected to EPSG:4326 (via ``WarpedVRT``) and merged with ``agg`` (default
    ``"max"``: the deepest value seen at each pixel). Written to ``out_path``.

    Args:
        paths: The downloaded depth GeoTIFFs to combine.
        out_path: Where to write the composite GeoTIFF.
        agg: Merge method (``"max"``/``"min"``/``"first"``/``"last"``).

    Returns:
        ``out_path``.
    """
    from contextlib import ExitStack

    from rasterio.merge import merge as rio_merge
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import Resampling

    with ExitStack() as stack:
        vrts = []
        for p in paths:
            src = stack.enter_context(rasterio.open(p))
            vrts.append(
                stack.enter_context(
                    WarpedVRT(
                        src,
                        crs="EPSG:4326",
                        resampling=Resampling.bilinear,
                        src_nodata=src.nodata if src.nodata is not None else 0.0,
                        nodata=0.0,
                    )
                )
            )
        mosaic, transform = rio_merge(vrts, method=agg, nodata=0.0)

    with rasterio.open(
        out_path,
        "w",
        driver="GTiff",
        height=mosaic.shape[1],
        width=mosaic.shape[2],
        count=1,
        dtype=mosaic.dtype,
        crs="EPSG:4326",
        transform=transform,
        nodata=0.0,
        compress="lzw",
    ) as dst:
        dst.write(mosaic[0], 1)
    return out_path


def max_composite(paths: list[Path]) -> Path:
    """Return the cached per-pixel MAX composite of ``paths`` (building it if absent).

    The composite is named from a hash of the source filenames and written next to
    them, so distinct event selections never collide on one fixed name and a re-run
    (or a later stats call over the same set) reuses the file rather than rebuilding.
    """
    import hashlib

    ordered = [Path(p) for p in paths]
    digest = hashlib.sha1(
        "|".join(sorted(p.name for p in ordered)).encode(), usedforsecurity=False
    ).hexdigest()[:8]
    out = ordered[0].parent / f"depth_composite_max_{digest}.tif"
    if not out.exists():
        depth_composite(ordered, out)
    return out
