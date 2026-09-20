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
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import geopandas as gpd
import structlog

from .._progress import file_progress
from ..config import Settings, get_settings
from ..core.manifest import file_record
from ..exceptions import CRSError, HazardError
from ..services.downloader import DownloadService
from ..services.hazard_tiles import (
    SUPPORTED_RETURN_PERIODS,
    HazardTile,
    HazardTileIndex,
)
from ..services.location import LocationResolver
from ..services.mirror_ledger import (
    MirrorReport,
    MirrorResult,
    ledger_size,
    load_ledger,
    update_ledger,
    verify_against_ledger,
)
from ..services.raster_ops import RasterOps, area_km2, nonempty_file, roi_key
from .discovery import FloodFrame, _frame_roi_key, _make_frame

# Rough per-tile size for a dry-run estimate when the ledger has no record yet.
_HAZARD_TILE_BYTES_EST = 1_350_000

logger = structlog.get_logger(__name__)

# Declared NoData of the GLOFAS depth tiles.
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


def _tiles_bounds(
    tiles: list[HazardTile],
) -> tuple[float, float, float, float] | None:
    """Union bbox of a tile list (for an offline remediation hint)."""
    if not tiles:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for t in tiles:
        minx, miny, maxx, maxy = t.geometry.bounds
        xs += [minx, maxx]
        ys += [miny, maxy]
    return (min(xs), min(ys), max(xs), max(ys))


def _offline_switch(settings: Settings) -> tuple[str, str]:
    """Name the active offline switch and the remedy to disable it (for messages)."""
    if settings.offline:
        return "EUROFLOOD_OFFLINE", "unset offline (EUROFLOOD_OFFLINE=0)"
    return "hazard_mode='local'", "set hazard_mode='auto'"


def _offline_tiles_message(
    missing: list[str], tiles: list[HazardTile], roi: Any, settings: Settings
) -> str:
    """A remediable error for missing tiles when hazard is offline."""
    bounds = roi.bounds if roi is not None else _tiles_bounds(tiles)
    bbox = " ".join(f"{v:.4g}" for v in bounds) if bounds else "<lon0 lat0 lon1 lat1>"
    rp_flags = " ".join(f"-r {rp}" for rp in sorted({t.return_period for t in tiles}))
    shown = ", ".join(missing[:4]) + (", ..." if len(missing) > 4 else "")
    switch, remedy = _offline_switch(settings)
    return (
        f"hazard is offline ({switch}) but {len(missing)} required GLOFAS tile(s) are "
        f"not cached ({shown}). Pre-download on a networked node:\n"
        f"    euroflood mirror hazard --bbox {bbox} {rp_flags}\n"
        f"then re-run offline, or {remedy} to allow JRC fetches."
    )


def _tile_sources(
    tiles: list[HazardTile],
    downloader: DownloadService,
    settings: Settings,
    *,
    roi: Any = None,
    on_bytes: Callable[[int], None] | None = None,
) -> list[str]:
    """Resolve tiles to raster sources: cached local paths, or /vsicurl URLs.

    When caching, the intersecting tiles are fetched concurrently (each is a
    distinct file, so the atomic-rename downloader is thread-safe) and reassembled
    in tile order.

    Offline (``hazard_mode='local'`` or ``offline``): reads only the local tile
    cache and never touches the network; a missing tile is a remediable
    ``HazardError`` pointing at ``euroflood mirror hazard`` (``hazard_cache_tiles``
    is ignored: ``/vsicurl`` would be a network read).

    Otherwise fails closed: if any required tile cannot be fetched, a ``HazardError``
    is raised rather than silently dropping it. Mosaicking only the tiles that
    happened to succeed would otherwise yield a hazard raster covering just part of
    the ROI, returned with no error and a valid-looking GeoTIFF, which silently
    corrupts any ROI spanning more than one GLOFAS tile whenever a single (often
    transient) tile fetch fails. ``DownloadService`` already retries each tile with
    exponential backoff, so a raise here means the tile is persistently unavailable.
    """
    if settings.offline_hazard:
        tiles_dir = settings.get_hazard_tiles_dir()
        resolved: list[str] = []
        missing_local: list[str] = []
        for t in tiles:
            p = tiles_dir / t.filename
            if nonempty_file(p):
                resolved.append(str(p))
            else:
                missing_local.append(t.filename)
        if missing_local:
            raise HazardError(
                _offline_tiles_message(missing_local, tiles, roi, settings)
            )
        return resolved
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
    missing = [t.filename for t, p in zip(tiles, fetched, strict=False) if p is None]
    if missing:
        raise HazardError(
            f"Incomplete hazard tile set: {len(missing)} of {len(tiles)} tiles "
            f"failed to download ({', '.join(missing)}). Refusing to build a "
            "truncated hazard raster for the ROI, retry (JRC tile fetches can fail "
            "transiently) or set hazard_cache_tiles=False to stream via /vsicurl."
        )
    return [str(p) for p in fetched if p is not None]


class HazardPipeline:
    """Resolve a region and query the GLOFAS tile index into a FloodFrame (cheap)."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Build the resolver and tile index."""
        self.settings = settings or get_settings()
        self.resolver = LocationResolver(settings=self.settings)
        self.tile_index = HazardTileIndex(
            settings=self.settings,
            allow_download=not self.settings.offline_hazard,
        )

    def query(
        self,
        region: Any = None,
        *,
        point: tuple[float, float] | None = None,
        radius_m: float = 0.0,
        bbox: tuple[float, float, float, float] | None = None,
        shapefile: str | Path | None = None,
        nuts: str | Sequence[str] | None = None,
        buffer_m: float = 0.0,
        crs: Any = None,
        return_period: int | list[int] | None = None,
        level: int | None = None,
        shape: str = "exact",
        output_dir: str | Path | None = None,
    ) -> FloodFrame:
        """Query GLOFAS hazard tiles for a region -> a FloodFrame (no rasters fetched).

        Backs `hazard`. See it for the argument reference.
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
            nuts=nuts,
            buffer_m=buffer_m,
            crs=crs,
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
    the ROI polygon. Outputs are cached: a row whose GeoTIFF already exists is
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
    index = HazardTileIndex(
        settings=settings,
        downloader=downloader,
        allow_download=not settings.offline_hazard,
    )
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
    incomplete: list[int] = []  # return periods whose tile set could not be completed
    with file_progress(
        len(work), enabled=on_bytes is None, description="Downloading hazard maps"
    ) as advance:
        for pos, rp, roi, out_path in work:
            tiles = index.tiles_for(roi, rp)  # re-resolve URLs/filenames from the ROI
            try:
                # Fails closed on a partial tile set (see `_tile_sources`), so a
                # written mosaic is always built from the full set of tiles.
                sources = _tile_sources(
                    tiles, downloader, settings, roi=roi, on_bytes=on_bytes
                )
            except HazardError:
                # Offline: the missing-tile error is definitive (not a transient
                # fetch failure), so propagate its `mirror hazard` remediation as-is
                # instead of treating it as a skippable partial-tile set.
                if settings.offline_hazard:
                    raise
                # Online per-return-period fail-closed: skip this RP (write nothing: an
                # absent raster is honest, a partial one would be a silent truncation)
                # rather than aborting the whole sweep, so a transient failure on one
                # RP does not discard the complete sibling RPs. A total failure is
                # still surfaced loudly after the loop.
                logger.warning("hazard_incomplete_tiles", return_period=rp)
                incomplete.append(rp)
                advance(1)
                continue
            if not sources:
                logger.warning("hazard_no_tiles_available", return_period=rp)
                advance(1)
                continue
            tile_names = sorted(t.filename for t in tiles)
            if RasterOps.mosaic_and_crop(
                sources,
                out_path,
                roi,
                nodata=HAZARD_NODATA,
                crop_to_poly=crop,
                tags={
                    "EUROFLOOD_RETURN_PERIOD": str(rp),
                    "EUROFLOOD_N_SOURCE_TILES": str(len(tile_names)),
                    "EUROFLOOD_SOURCE_TILES": ",".join(tile_names),
                },
            ):
                results[pos] = out_path
                logger.info(
                    "hazard_extracted", file=out_path.name, n_tiles=len(sources)
                )
            advance(1)

    # Fail loudly only when tile failures left nothing usable at all (e.g. a single
    # return period whose tiles were unavailable), so a wholly-failed request can
    # never be mistaken for "no hazard here". A partial success returns what
    # completed; the skipped return periods were warned above.
    if incomplete and not results:
        raise HazardError(
            "No hazard rasters could be produced: every requested return period had "
            f"an incomplete tile set ({', '.join(f'RP{rp}' for rp in incomplete)}). "
            "Retry (JRC tile fetches can fail transiently) or set "
            "hazard_cache_tiles=False to stream tiles via /vsicurl."
        )
    return [results[pos] for pos in ordered if pos in results]


def _resolve_hazard_roi(
    resolver: LocationResolver,
    region: Any,
    *,
    point: tuple[float, float] | None,
    radius_m: float,
    bbox: tuple[float, float, float, float] | None,
    shapefile: str | Path | None,
    nuts: str | Sequence[str] | None = None,
    buffer_m: float,
    crs: Any = None,
    level: int | None,
    shape: str,
) -> Any:
    """Resolve the ROI args to a geometry, or ``None`` when none were given (all tiles).

    A bare ``crs`` with no coordinates is an error rather than "all tiles": a
    forgotten ``bbox`` must not start a global multi-GB tile download.
    """
    if (
        region is None
        and point is None
        and bbox is None
        and shapefile is None
        and nuts is None
    ):
        if crs is not None:
            raise CRSError(
                "crs= was given but no coordinates: pass bbox=, point= or a geometry "
                "in that CRS, or drop crs= to mirror every tile."
            )
        return None
    return resolver.resolve(
        region,
        point=point,
        radius_m=radius_m,
        bbox=bbox,
        shapefile=shapefile,
        nuts=nuts,
        buffer_m=buffer_m,
        crs=crs,
        level=level,
        shape=shape,
    )


def _expected_hazard_tiles(
    index: HazardTileIndex, roi: Any, rps: list[int]
) -> dict[str, HazardTile]:
    """The set of tiles (deduped by filename) needed for ``rps`` over ``roi``.

    Region-scoped (``tiles_for``) when an ROI is given, else every tile (``all_tiles``).
    """
    expected: dict[str, HazardTile] = {}
    for rp in rps:
        tiles = index.tiles_for(roi, rp) if roi is not None else index.all_tiles(rp)
        for t in tiles:
            expected[t.filename] = t
    return expected


def mirror_hazard(
    region: Any = None,
    *,
    point: tuple[float, float] | None = None,
    radius_m: float = 0.0,
    bbox: tuple[float, float, float, float] | None = None,
    shapefile: str | Path | None = None,
    nuts: str | Sequence[str] | None = None,
    buffer_m: float = 0.0,
    crs: Any = None,
    return_period: int | list[int] | None = None,
    level: int | None = None,
    shape: str = "exact",
    dry_run: bool = False,
    settings: Settings | None = None,
) -> MirrorResult:
    """Mirror GLOFAS hazard tiles into the cache for offline/HPC use.

    Region-scoped when an ROI is given (only the intersecting tiles, the
    HPC-friendly footprint), else every tile globally (~350 MB per return period).
    Records a per-tile sha256+size ledger in ``hazard_manifest.json`` (accumulating
    across incremental region mirrors), passes each tile's ledgered size as
    ``expected_size`` so a corrupt cached tile is re-fetched, and leaves the cache so
    a later ``hazard(...).download()`` runs fully offline.

    This is the *populate* action, so it is network-permitted regardless of
    ``hazard_mode``. It does **not** fail closed on a partial download (a bulk mirror
    is idempotent/resumable). Failures are surfaced via ``result.missing``.

    Args:
        region: ROI selection (place / geometry / bbox tuple), as `hazard`. Omit
            every ROI argument to mirror all tiles globally.
        point: A ``(lat, lon)`` point in WGS 84, or ``(x, y)`` in a projected
            ``crs``; combine with ``radius_m``.
        radius_m: Radius in ground metres around ``point``.
        bbox: A ``(minx, miny, maxx, maxy)`` bounding box in WGS 84 lon/lat, or in
            ``crs``.
        shapefile: Path to a vector file used as the ROI.
        nuts: One or more Eurostat NUTS identifiers (``"NL22"``, or a list for
            their union), as in `hazard`.
        buffer_m: Optional extra buffer around the ROI, in ground metres.
        crs: Coordinate reference system of ``bbox``/``point``/a bare geometry, as
            in `hazard`. A bare ``crs`` with no coordinates raises `CRSError`
            instead of mirroring every tile.
        level: Optional NUTS level filter for place-name resolution.
        shape: ROI shape, ``"exact"``/``"bbox"``/``"hull"`` (see `hazard`).
        return_period: Return period(s) to mirror. ``None`` mirrors all supported.
        dry_run: Resolve the tile set and report the plan without downloading.
        settings: Optional configuration. Defaults to `get_settings`.

    Returns:
        MirrorResult: an ``int`` (tiles downloaded) carrying ``.n_expected``,
        ``.bytes_total``, ``.missing``, and ``.ledger_path``.
    """
    settings = settings or get_settings()
    rps = _normalize_return_periods(return_period)
    resolver = LocationResolver(settings=settings)
    roi = _resolve_hazard_roi(
        resolver,
        region,
        point=point,
        radius_m=radius_m,
        bbox=bbox,
        shapefile=shapefile,
        nuts=nuts,
        buffer_m=buffer_m,
        crs=crs,
        level=level,
        shape=shape,
    )
    # The mirror populates the cache, so it is always network-permitted.
    index = HazardTileIndex(settings=settings, allow_download=True)
    downloader = DownloadService(
        download_dir=settings.get_hazard_tiles_dir(), settings=settings
    )
    ledger_path = settings.get_hazard_manifest_path()
    ledger = load_ledger(ledger_path)
    expected = _expected_hazard_tiles(index, roi, rps)
    tiles_dir = settings.get_hazard_tiles_dir()
    n_expected = len(expected)

    if dry_run:
        missing = sorted(fn for fn in expected if not nonempty_file(tiles_dir / fn))
        est = sum(ledger_size(ledger, fn) or _HAZARD_TILE_BYTES_EST for fn in missing)
        logger.info(
            "hazard_mirror_plan", tiles=n_expected, to_download=len(missing), bytes=est
        )
        return MirrorResult(
            0,
            n_expected=n_expected,
            bytes_total=est,
            missing=missing,
            ledger_path=ledger_path,
        )

    if not expected:
        logger.info("hazard_mirror_empty_roi")
        return MirrorResult(0, n_expected=0, ledger_path=ledger_path)

    tiles_dir.mkdir(parents=True, exist_ok=True)
    # A model_version bump silently changes tile content at the same URL: evict the
    # stale cached tiles so download_file re-fetches (a cache hit would otherwise serve
    # old content, and re-hashing it would falsely certify it as current).
    if ledger.get("model_version") not in (None, settings.hazard_model_version):
        for fn in expected:
            (tiles_dir / fn).unlink(missing_ok=True)
    items = list(expected.items())
    results: dict[str, Path | None] = {}
    with (
        file_progress(
            len(items), enabled=True, description="Mirroring hazard tiles"
        ) as advance,
        ThreadPoolExecutor(
            max_workers=min(len(items), settings.max_workers_dl)
        ) as pool,
    ):
        futures = {
            pool.submit(
                downloader.download_file,
                t.download_url,
                t.filename,
                expected_size=ledger_size(ledger, fn),
            ): fn
            for fn, t in items
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            advance(1)

    records: dict[str, dict[str, Any]] = {}
    for fn, p in results.items():
        if (
            p is not None and p.exists()
        ):  # a download that reported success but wrote nothing
            records[fn] = {
                **file_record(p),
                "return_period": expected[fn].return_period,
                "tile_id": expected[fn].tile_id,
            }
    downloaded = sum(1 for p in results.values() if p is not None)
    missing = sorted(fn for fn, p in results.items() if p is None)
    region_meta = {
        "bbox": list(roi.bounds) if roi is not None else None,
        "return_periods": rps,
        "n_tiles": n_expected,
    }
    update_ledger(
        ledger_path,
        records,
        region_meta=region_meta,
        model_version=settings.hazard_model_version,
    )
    logger.info(
        "mirror_hazard_complete",
        downloaded=downloaded,
        of=n_expected,
        missing=len(missing),
    )
    return MirrorResult(
        downloaded,
        n_expected=n_expected,
        bytes_total=sum(r["size_bytes"] for r in records.values()),
        missing=missing,
        ledger_path=ledger_path,
    )


def verify_hazard_mirror(
    region: Any = None,
    *,
    point: tuple[float, float] | None = None,
    radius_m: float = 0.0,
    bbox: tuple[float, float, float, float] | None = None,
    shapefile: str | Path | None = None,
    nuts: str | Sequence[str] | None = None,
    buffer_m: float = 0.0,
    crs: Any = None,
    return_period: int | list[int] | None = None,
    level: int | None = None,
    shape: str = "exact",
    deep: bool = False,
    settings: Settings | None = None,
) -> MirrorReport:
    """Report local hazard-mirror readiness for a region: present/missing/corrupt.

    Resolves the expected tile set (region-scoped, or all), then checks each tile on
    disk against the ``hazard_manifest.json`` ledger (``deep`` re-hashes sha256).
    """
    settings = settings or get_settings()
    rps = _normalize_return_periods(return_period)
    resolver = LocationResolver(settings=settings)
    roi = _resolve_hazard_roi(
        resolver,
        region,
        point=point,
        radius_m=radius_m,
        bbox=bbox,
        shapefile=shapefile,
        nuts=nuts,
        buffer_m=buffer_m,
        crs=crs,
        level=level,
        shape=shape,
    )
    # Verify may run offline; the tile index must already be cached.
    index = HazardTileIndex(
        settings=settings, allow_download=not settings.offline_hazard
    )
    expected = _expected_hazard_tiles(index, roi, rps)
    ledger_path = settings.get_hazard_manifest_path()
    tiles = list(expected.values())
    bounds = roi.bounds if roi is not None else _tiles_bounds(tiles)
    bbox_str = " ".join(f"{v:.4g}" for v in bounds) if bounds else ""
    rp_flags = " ".join(f"-r {rp}" for rp in rps)
    report = verify_against_ledger(
        sorted(expected),
        settings.get_hazard_tiles_dir(),
        load_ledger(ledger_path),
        collection="hazard",
        deep=deep,
        remediation_cmd=f"euroflood mirror hazard --bbox {bbox_str} {rp_flags}".strip(),
    )
    report.ledger_path = ledger_path
    return report


def build_hazard_manifest(settings: Settings | None = None) -> Path:
    """Author a thin hazard reference manifest (provenance; no tile copies).

    Makes the GLOFAS hazard layer citable/version-pinned without re-hosting the
    ~2.5 GB of tiles: it pins the JRC base URL, the model version, the return
    periods, the deterministic filename/URL templates, and a frozen copy +
    checksum of ``tile_extents.geojson`` (downloaded on first use). The consumer
    keeps reading JRC tiles via ``/vsicurl``.
    """
    from ..core.manifest import INDEX_SCHEMA_VERSION, file_records

    settings = settings or get_settings()
    index = HazardTileIndex(settings=settings)
    gdf = index._load()  # downloads + caches tile_extents.geojson on first use
    tile_path = settings.get_hazard_index_path()

    out = settings.get_hazard_manifest_path()
    # Preserve any local mirror ledger already in the file: authoring the citable
    # provenance must never wipe the record of mirrored tiles.
    existing = json.loads(out.read_text()) if out.exists() else {}
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
    if "mirror" in existing:
        doc["mirror"] = existing["mirror"]
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
    "verify_hazard_mirror",
]
