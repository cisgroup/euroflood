"""Hazard pipeline: query GLOFAS return-period tiles for a region -> a FloodFrame.

Mirrors the historic-flood discovery pipeline. A cheap vector query (ROI vs the
``tile_extents.geojson`` tile index) yields a downloadable catalogue with one row
per return period; `download_hazard_catalogue` then fetches the intersecting
tiles, mosaics them, and crops to the ROI. `mirror_hazard` bulk-downloads all
tiles for offline/local use. Reuses ``LocationResolver``, ``DownloadService``,
``RasterOps``, and ``FloodFrame`` unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import geopandas as gpd
import structlog

from .._progress import file_progress
from ..config import Settings, get_settings
from ..services.downloader import DownloadService
from ..services.hazard_tiles import (
    SUPPORTED_RETURN_PERIODS,
    HazardTile,
    HazardTileIndex,
)
from ..services.location import LocationResolver
from ..services.raster_ops import RasterOps, area_km2, nonempty_file, roi_key
from .discovery import FloodFrame, _frame_roi_key, _make_frame

logger = structlog.get_logger(__name__)

# Declared NoData of the GLOFAS depth tiles (verified live).
HAZARD_NODATA = -9999.0

# Public hazard catalogue schema (EPSG:4326). One row per return period.
HAZARD_CATALOGUE_COLUMNS = [
    "collection",  # const "hazard" (mirrors floods' "historic")
    "return_period",  # int: 10/20/50/75/100/200/500
    "model_version",  # e.g. "v2.1.2"
    "n_tiles",  # int: tiles intersecting the ROI (informational)
    "filename",  # output hint "hazard_RP{rp}.tif"
    "area_km2",  # ROI area (not flooded area, unlike floods)
    "geometry",  # the ROI polygon (EPSG:4326)
]


def _normalize_return_periods(
    return_period: int | list[int] | None,
) -> list[int]:
    """Coerce the ``return_period`` argument to a list (``None`` -> all supported)."""
    if return_period is None:
        return list(SUPPORTED_RETURN_PERIODS)
    if isinstance(return_period, int):
        return [return_period]
    return list(return_period)


def _make_hazard_frame(rows: list[dict[str, Any]], settings: Settings) -> FloodFrame:
    """Build a hazard FloodFrame (EPSG:4326) from rows, carrying `settings`."""
    return _make_frame(rows, settings, columns=HAZARD_CATALOGUE_COLUMNS)


def _tile_sources(
    tiles: list[HazardTile],
    downloader: DownloadService,
    settings: Settings,
    *,
    on_bytes: Callable[[int], None] | None = None,
) -> list[str]:
    """Resolve tiles to raster sources: cached local paths, or /vsicurl URLs.

    When caching, the intersecting tiles are fetched concurrently (each is a
    distinct file, so the atomic-rename downloader is thread-safe) and reassembled
    in tile order.
    """
    if not settings.hazard_cache_tiles:
        return [f"/vsicurl/{t.download_url}" for t in tiles]
    if not tiles:
        return []
    fetched: list[Path | None] = [None] * len(tiles)
    workers = min(len(tiles), settings.max_workers_dl)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                downloader.download_file, t.download_url, t.filename, on_bytes=on_bytes
            ): i
            for i, t in enumerate(tiles)
        }
        for future in as_completed(futures):
            fetched[futures[future]] = future.result()
    return [str(p) for p in fetched if p is not None]


class HazardPipeline:
    """Resolve a region and query the GLOFAS tile index into a FloodFrame (cheap)."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Build the resolver and tile index."""
        self.settings = settings or get_settings()
        self.resolver = LocationResolver(settings=self.settings)
        self.tile_index = HazardTileIndex(settings=self.settings)

    def query(
        self,
        region: Any = None,
        *,
        point: tuple[float, float] | None = None,
        radius_m: float = 0.0,
        bbox: tuple[float, float, float, float] | None = None,
        shapefile: str | Path | None = None,
        buffer_m: float = 0.0,
        return_period: int | list[int] | None = None,
        level: int | None = None,
        shape: str = "exact",
        output_dir: str | Path | None = None,
    ) -> FloodFrame:
        """Query GLOFAS hazard tiles for a region -> a FloodFrame (no rasters fetched).

        Backs `hazard` — see it for the argument reference.
        ``output_dir`` is the directory the cached-download auto-detect scans
        (defaults to ``settings.output_dir``).

        Returns:
            A hazard `FloodFrame` (one row per
            return period).

        Raises:
            GeocodingError: If a place name cannot be resolved.
            HazardError: If the hazard tile index cannot be located or read.
        """
        roi = self.resolver.resolve(
            region,
            point=point,
            radius_m=radius_m,
            bbox=bbox,
            shapefile=shapefile,
            buffer_m=buffer_m,
            level=level,
            shape=shape,
        )
        rps = _normalize_return_periods(return_period)
        area = area_km2(roi)

        rows: list[dict[str, Any]] = []
        for rp in rps:
            tiles = self.tile_index.tiles_for(roi, rp)  # validates rp, may load index
            if not tiles:
                logger.info("no_hazard_tiles_for_roi", return_period=rp)
                continue
            rows.append(
                {
                    "collection": "hazard",
                    "return_period": rp,
                    "model_version": self.settings.hazard_model_version,
                    "n_tiles": len(tiles),
                    "filename": f"hazard_RP{rp}.tif",
                    "area_km2": round(area, 3),
                    "geometry": roi,
                }
            )
        logger.info("hazard_query_complete", rows=len(rows))
        frame = _make_hazard_frame(rows, self.settings)
        if self.settings.autodetect_downloads:
            from .discovery import _attach_cached_paths

            scan_dir = (
                output_dir if output_dir is not None else self.settings.output_dir
            )
            _attach_cached_paths(frame, scan_dir, only_if_any=True)
        return frame


def _hazard_crop_name(row: Any, *, key: str | None = None) -> str:
    """Deterministic, ROI-safe output filename for one hazard (return-period) row.

    Embeds a short hash of the ROI geometry so the same return period cropped to
    two different query areas lands in two different files (no collision). ``key``
    is the precomputed shared ROI hash (see
    `_frame_roi_key`); when omitted it is
    derived from the row geometry.
    """
    rp = int(row["return_period"])
    if key is None:
        key = roi_key(row.geometry) if row.geometry is not None else "noroi"
    return f"hazard_RP{rp}_{key}.tif"


def download_hazard_catalogue(
    catalogue: gpd.GeoDataFrame,
    output_dir: str | Path | None = None,
    *,
    crop: bool = True,
    force: bool = False,
    settings: Settings | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> list[Path]:
    """Fetch + mosaic + crop the GLOFAS tiles for each row of `catalogue`.

    One output GeoTIFF per row (per return period): the intersecting tiles are
    resolved from the row's ROI, read (from the cache or via ``/vsicurl`` per
    ``settings.hazard_cache_tiles``), merged over the ROI window, and cropped to
    the ROI polygon. Outputs are cached — a row whose GeoTIFF already exists is
    reused unless ``force=True``.

    Args:
        catalogue: A (possibly filtered) hazard FloodFrame / GeoDataFrame.
        output_dir: Where to write outputs. Defaults to ``settings.output_dir``.
        crop: Crop each mosaic to its ROI polygon. If False, keep the bbox window.
        force: Re-fetch + re-crop even if the output already exists.
        settings: Optional configuration; defaults to the catalogue's settings or
            `get_settings`.
        on_bytes: Optional progress callback ``(n_bytes) -> None`` per downloaded
            tile chunk (used by the CLI to drive a progress bar).

    Returns:
        list[Path]: Paths of the written (or reused) ``hazard_RP{rp}_{roi}.tif`` files.
    """
    settings = settings or getattr(catalogue, "_settings", None) or get_settings()
    out_dir = Path(output_dir) if output_dir is not None else settings.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    downloader = DownloadService(
        download_dir=settings.get_hazard_tiles_dir(), settings=settings
    )
    index = HazardTileIndex(settings=settings, downloader=downloader)
    key = _frame_roi_key(catalogue)  # every row shares one ROI; hash it once

    # Partition rows into cache hits (recorded as-is) and per-return-period work,
    # keeping the row order of any path we return.
    ordered: list[int] = []
    results: dict[int, Path] = {}
    work: list[tuple[int, int, Any, Path]] = []  # (pos, rp, roi, out_path)
    for pos, (_, row) in enumerate(catalogue.iterrows()):
        rp = int(row["return_period"])
        out_path = out_dir / _hazard_crop_name(row, key=key)
        ordered.append(pos)
        if not force and nonempty_file(out_path):
            logger.debug("crop_cache_hit", file=out_path.name)
            results[pos] = out_path
            continue
        work.append((pos, rp, row.geometry, out_path))

    # Rows stay sequential (each mosaic is a barrier), but the intersecting tiles of
    # each row are fetched concurrently inside ``_tile_sources`` (the download cost).
    with file_progress(
        len(work), enabled=on_bytes is None, description="Downloading hazard maps"
    ) as advance:
        for pos, rp, roi, out_path in work:
            tiles = index.tiles_for(roi, rp)  # re-resolve URLs/filenames from the ROI
            sources = _tile_sources(tiles, downloader, settings, on_bytes=on_bytes)
            if not sources:
                logger.warning("hazard_no_tiles_available", return_period=rp)
                advance(1)
                continue
            if RasterOps.mosaic_and_crop(
                sources, out_path, roi, nodata=HAZARD_NODATA, crop_to_poly=crop
            ):
                results[pos] = out_path
                logger.info(
                    "hazard_extracted", file=out_path.name, n_tiles=len(sources)
                )
            advance(1)

    return [results[pos] for pos in ordered if pos in results]


def mirror_hazard(
    return_period: int | list[int] | None = None,
    *,
    settings: Settings | None = None,
) -> int:
    """Download every GLOFAS tile for the given return period(s) into the cache.

    A bulk "download everything once" for fast local/offline access: it
    pre-populates ``settings.get_hazard_tiles_dir()`` so subsequent
    ``hazard(...).download()`` runs fully offline (cache hits skip re-downloads).
    Roughly 1.3 MB x 271 tiles x number of return periods (~350 MB per RP).

    Args:
        return_period: Return period(s) to mirror. ``None`` mirrors all supported.
        settings: Optional configuration. Defaults to `get_settings`.

    Returns:
        int: The number of tiles available locally after the run.
    """
    settings = settings or get_settings()
    rps = _normalize_return_periods(return_period)
    index = HazardTileIndex(settings=settings)
    downloader = DownloadService(
        download_dir=settings.get_hazard_tiles_dir(), settings=settings
    )

    total = 0
    for rp in rps:
        tiles = index.all_tiles(rp)  # validates rp
        logger.info("mirror_hazard_start", return_period=rp, tiles=len(tiles))
        count = 0
        with ThreadPoolExecutor(max_workers=settings.max_workers_dl) as pool:
            futures = [
                pool.submit(downloader.download_file, t.download_url, t.filename)
                for t in tiles
            ]
            for future in as_completed(futures):
                if future.result() is not None:
                    count += 1
        logger.info(
            "mirror_hazard_rp_done", return_period=rp, downloaded=count, of=len(tiles)
        )
        total += count
    logger.info("mirror_hazard_complete", total=total)
    return total


def build_hazard_manifest(settings: Settings | None = None) -> Path:
    """Author a thin hazard reference manifest (provenance; no tile copies).

    Makes the GLOFAS hazard layer citable/version-pinned without re-hosting the
    ~2.5 GB of tiles: it pins the JRC base URL, the model version, the return
    periods, the deterministic filename/URL templates, and a frozen copy +
    checksum of ``tile_extents.geojson`` (downloaded on first use). The consumer
    keeps reading JRC tiles via ``/vsicurl``. Re-hosting is an opt-in for 5b.
    """
    from ..core.manifest import INDEX_SCHEMA_VERSION, file_records

    settings = settings or get_settings()
    index = HazardTileIndex(settings=settings)
    gdf = index._load()  # downloads + caches tile_extents.geojson on first use
    tile_path = settings.get_hazard_index_path()

    doc = {
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "collection": "hazard",
        "model_version": settings.hazard_model_version,
        "provenance": {
            "hazard_base_url": settings.hazard_base_url,
            "return_periods": list(SUPPORTED_RETURN_PERIODS),
            "n_tiles": len(gdf),
            "tile_filename_template": "ID{id}_{name}_RP{rp}_depth.tif",
            "tile_url_template": settings.hazard_base_url + "RP{rp}/{filename}",
        },
        "files": file_records({settings.hazard_index_filename: tile_path}),
        "source_urls": {
            "jrc": settings.hazard_base_url,
            "huggingface": None,
            "zenodo_doi": None,
        },
    }
    out = settings.get_hazard_manifest_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2))
    logger.info("hazard_manifest_written", path=str(out), n_tiles=len(gdf))
    return out


__all__ = [
    "HAZARD_CATALOGUE_COLUMNS",
    "HazardPipeline",
    "build_hazard_manifest",
    "download_hazard_catalogue",
    "mirror_hazard",
]
