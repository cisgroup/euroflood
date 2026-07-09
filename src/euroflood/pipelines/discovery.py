"""Discovery pipeline: query the index for a region -> a FloodFrame catalogue.

The cheap half of the historic flood workflow. Resolves a region to an ROI,
masks the global index raster to that ROI, and assembles a GeoDataFrame
(`FloodFrame`) with one row per flood event — without downloading any
flood rasters. ``FloodFrame.download()`` (or the functional `download_catalogue`)
materializes the cropped GeoTIFFs for the selected rows.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, ClassVar

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rasterio.mask
import structlog

from .._progress import file_progress
from ..config import Settings, get_settings
from ..core.grid import GlobalGrid
from ..core.manifest import validate_manifest
from ..services.dictionary_repository import DictionaryRepository
from ..services.downloader import DownloadService
from ..services.events_repository import EventsRepository
from ..services.index_repository import _GDAL_ENV, IndexRepository
from ..services.location import LocationResolver
from ..services.raster_ops import RasterOps, is_hazard, nonempty_file, roi_key

logger = structlog.get_logger(__name__)

# Public catalogue schema (EPSG:4326). One row per flood event.
CATALOGUE_COLUMNS = [
    "collection",
    "event_id",
    "date",
    "year",
    "end_date",
    "cluster_id",
    "filename",
    "download_url",
    "area_km2",
    "geometry",
]


class FloodFrame(gpd.GeoDataFrame):  # type: ignore[misc]
    """A GeoDataFrame of flood events with a convenience ``.download()`` method.

    It *is* a plain ``geopandas.GeoDataFrame`` — filtering, stats, plotting and
    ``to_parquet`` all work as usual. The only addition is `download`, so
    ``floods(...).download("out/")`` and ``cat[cat.year == 2021].download("out/")``
    both work. Use the functional `download_catalogue` if a heavy reshape
    ever degrades the object back to a plain GeoDataFrame.
    """

    # Propagate the injected settings through filtering/slicing operations.
    _metadata: ClassVar[list[str]] = ["_settings"]

    @property
    def _constructor(self) -> type[FloodFrame]:
        return FloodFrame

    def _summary(self) -> str:
        """A one-line human summary (collection, count, span, total area)."""
        try:
            n = len(self)
            cols = list(self.columns)
            if "return_period" in cols:
                kind = "hazard layer"
                rps = (
                    sorted({int(r) for r in self["return_period"].tolist()})
                    if n
                    else []
                )
                detail = f"RP {', '.join(str(r) for r in rps)} yr" if rps else ""
            else:
                kind = "flood event"
                dates = (
                    [d for d in self["date"].tolist() if d]
                    if n and "date" in cols
                    else []
                )
                detail = f"{min(dates)} … {max(dates)}" if dates else ""
            parts = [f"EuroFlood catalogue: {n} {kind}{'' if n == 1 else 's'}"]
            if detail:
                parts.append(detail)
            if "area_km2" in cols and n:
                parts.append(f"{float(self['area_km2'].sum()):,.1f} km² total")
            if "path" in cols and n:
                n_dl = int(self["path"].notna().sum())
                parts.append(f"⬇ {n_dl}/{n} downloaded")
            return " · ".join(parts)
        except Exception:  # never let a repr crash a notebook/session
            return f"EuroFlood catalogue: {len(self)} row(s)"

    def __repr__(self) -> str:
        """Prepend a one-line summary above the standard GeoDataFrame repr."""
        return f"{self._summary()}\n{super().__repr__()}"

    def _repr_html_(self) -> str:
        """Prepend the summary above the standard GeoDataFrame HTML (notebooks)."""
        base = super()._repr_html_()
        return (
            f'<div style="font-weight:600;margin-bottom:4px">{self._summary()}</div>'
            f"{base}"
        )

    def plot(self, *, geo: bool = False, **kwargs: Any) -> Any:
        """Plot this catalogue (static, matplotlib). Requires ``euroflood[viz]``.

        The default is the flood-recurrence heatmap over the query ROI — cheap, no
        download.

        Args:
            geo: If True, fall back to the raw geopandas plot of the catalogue
                geometry instead of the recurrence view.
            **kwargs: Passed through to the renderer. Notably ``depth=True``
                (download + render the depth raster, guarded by a row cap),
                ``footprints=True`` (per-event extents), ``event_id=`` (a single
                event's footprint), and ``boundary=False``.

        Returns:
            A ``matplotlib.axes.Axes``.

        Raises:
            ImportError: If the ``viz`` extra is not installed.
            VisualizationError: For an empty catalogue, or the recurrence view on a
                hazard catalogue (no combo index).

        Examples:
            >>> import euroflood as ef
            >>> ef.floods("Zutphen").plot()  # recurrence heatmap  # doctest: +SKIP
            >>> ef.floods("Zutphen").plot(footprints=True)  # doctest: +SKIP
        """
        if geo:
            return gpd.GeoDataFrame(self).plot(**kwargs)
        from ..viz import plot_frame

        return plot_frame(self, **kwargs)

    def explore(self, *, geo: bool = False, **kwargs: Any) -> Any:
        """Interactive map of this catalogue (folium). See `plot`.

        Recurrence regions carry hover tooltips listing which floods hit each area
        and when. Requires ``euroflood[viz]``.

        Args:
            geo: If True, fall back to the raw geopandas ``.explore()`` of the
                catalogue geometry.
            **kwargs: Passed through to the renderer (``depth=``, ``footprints=``,
                ``event_id=``, ``boundary=``, ``backend=``).

        Returns:
            A ``folium.Map``.

        Raises:
            ImportError: If the ``viz`` extra is not installed.
            VisualizationError: For an empty/hazard catalogue on the recurrence view.
        """
        if geo:
            return gpd.GeoDataFrame(self).explore(**kwargs)
        from ..viz import explore_frame

        return explore_frame(self, **kwargs)

    def footprints(self) -> Any:
        """Per-event flood extents from the index (one geometry per event).

        Replaces the catalogue's shared ROI geometry with each event's actual
        footprint (derived from the index — no download) and adds an
        ``extent_km2`` column, so the result is a normal GeoDataFrame you can plot,
        ``explore``, ``to_file``, or measure. Requires ``euroflood[viz]``.

        Returns:
            A plain ``geopandas.GeoDataFrame`` (EPSG:4326) with the catalogue's
            metadata, a per-event ``geometry``, and ``extent_km2``.

        Raises:
            VisualizationError: For an empty or hazard catalogue (no combo index).

        Examples:
            >>> import euroflood as ef
            >>> ext = ef.floods("Zutphen").footprints()  # doctest: +SKIP
            >>> ext.to_file("extents.geojson")  # doctest: +SKIP
        """
        from ..viz._raster import event_footprints

        return event_footprints(self)

    def download(
        self,
        output_dir: str | Path | None = None,
        *,
        crop: bool = True,
        force: bool = False,
        settings: Settings | None = None,
        on_bytes: Callable[[int], None] | None = None,
    ) -> FloodFrame:
        """Download + crop the rasters for these rows, and remember them.

        Routes by ``collection`` (see `_dispatch_download`), so a hazard
        catalogue fetches+mosaics+crops while a historic one downloads one file
        per event — both via the same ``.download()``. Cropped outputs are cached:
        a re-run reuses existing files (nothing re-downloaded) unless ``force=True``.

        Returns the catalogue itself, now carrying a ``path`` column, so the result
        is *actionable* — ``cat = ef.floods("Zutphen").download()`` then
        ``cat.plot(depth=True)`` / ``cat.stats()`` all reuse the downloaded rasters.
        The written paths are available via `files`.

        Args:
            output_dir: Directory for the written GeoTIFFs. Defaults to
                ``settings.output_dir``.
            crop: If True (default), crop each raster to the ROI polygon.
            force: Re-download + re-crop even if the output already exists.
            settings: Optional configuration (defaults to the frame's own settings).
            on_bytes: Optional progress callback ``(n_bytes) -> None`` per downloaded
                chunk (the CLI uses it to drive a progress bar; library callers can
                pass their own).

        Returns:
            This `FloodFrame`, with a ``path`` column populated for the rows
            whose rasters were written (``None`` where a row produced no raster).

        Examples:
            >>> import euroflood as ef
            >>> cat = ef.floods("Zutphen").download("out/")  # doctest: +SKIP
            >>> cat.files  # the downloaded GeoTIFFs  # doctest: +SKIP
        """
        settings = settings or getattr(self, "_settings", None) or get_settings()
        out_dir = Path(output_dir) if output_dir is not None else settings.output_dir
        _dispatch_download(
            self, out_dir, crop=crop, force=force, settings=settings, on_bytes=on_bytes
        )
        _attach_cached_paths(self, out_dir)
        return self

    @property
    def files(self) -> list[Path]:
        """Paths of the downloaded rasters for the rows in this frame (may be empty)."""
        if "path" not in self.columns:
            return []
        return [Path(p) for p in self["path"].tolist() if p]

    def depths(self) -> list[Any]:
        """Downloaded rasters as `DepthRaster` objects.

        So ``ef.floods("Zutphen").download().depths()[0].plot()`` works. Requires
        ``euroflood[viz]``. Returns an empty list if nothing has been downloaded.
        """
        from ..viz import open_depth

        return [open_depth(p) for p in self.files]

    def stats(self, *, scale: float | None = None) -> Any:
        """Per-event depth statistics for the downloaded rasters.

        One row per downloaded event: max/mean/p95 depth (m), flooded area (km²),
        water volume (m³ and million-m³), and wet-pixel count. Reads only rasters a
        prior `download` fetched — no network I/O.

        Args:
            scale: Raster-value→metre multiplier. ``None`` (default) picks it from
                the catalogue kind — EFAS historic depth is centimetres (``0.01``),
                GLOFAS hazard depth is already metres (``1.0``). Pass a value to override.

        Returns:
            A ``pandas.DataFrame`` of per-event statistics.

        Raises:
            ProcessingError: If nothing has been downloaded (call `download`).

        Examples:
            >>> import euroflood as ef
            >>> ef.floods("Zutphen").download().stats()  # doctest: +SKIP
        """
        from ..services.statistics import catalogue_stats

        return catalogue_stats(self, scale=scale)

    def summary(self, *, scale: float | None = None) -> dict[str, float]:
        """Aggregate (envelope) depth statistics across the downloaded rasters.

        Combines the downloaded rasters into a per-pixel maximum composite and
        summarizes it, so overlapping events are not double-counted: overall max
        depth, union flooded area (km²), and envelope water volume. Reads only
        already-downloaded rasters.

        Args:
            scale: Raster-value→metre multiplier; ``None`` (default) infers it from
                the catalogue kind (see `stats`).

        Returns:
            A dict of headline numbers (``max_depth_m``, ``flooded_area_km2``,
            ``volume_m3``, ``volume_Mm3``, ...).

        Raises:
            ProcessingError: If nothing has been downloaded (call `download`).
        """
        from ..services.statistics import catalogue_summary

        return catalogue_summary(self, scale=scale)


def _cell_area_km2(roi: Any) -> float:
    """Approximate the area of one grid cell (km²) at the ROI's latitude."""
    lat = roi.centroid.y
    res = GlobalGrid.RESOLUTION
    width_m = res * 111_320.0 * math.cos(math.radians(lat))
    height_m = res * 111_320.0
    return abs(width_m * height_m) / 1e6


def _norm_start(value: str | int) -> str:
    """Normalize a lower time bound; a bare year becomes Jan 1st."""
    text = str(value)
    return f"{text}-01-01" if len(text) == 4 else text


def _norm_end(value: str | int) -> str:
    """Normalize an upper time bound; a bare year becomes Dec 31st."""
    text = str(value)
    return f"{text}-12-31" if len(text) == 4 else text


def _make_frame(
    rows: list[dict[str, Any]],
    settings: Settings,
    *,
    columns: list[str] = CATALOGUE_COLUMNS,
) -> FloodFrame:
    """Build a FloodFrame (EPSG:4326) from rows, carrying `settings`.

    ``columns`` names the empty-frame schema (historic by default; the hazard
    pipeline passes ``HAZARD_CATALOGUE_COLUMNS``).
    """
    if rows:
        gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    else:
        empty: dict[str, list[Any]] = {c: [] for c in columns}
        gdf = gpd.GeoDataFrame(empty, geometry="geometry", crs="EPSG:4326")
    frame = FloodFrame(gdf)
    frame._settings = settings
    return frame


class DiscoveryPipeline:
    """Resolve a region and query the index into a FloodFrame catalogue (cheap)."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Build the resolver + dictionary repository; validate the cache manifest."""
        self.settings = settings or get_settings()
        self.resolver = LocationResolver(settings=self.settings)
        # Remote mode: mirror the small tables (dict/events/meta/manifest) into the
        # cache once so the repositories below read them locally; the COG stays
        # remote and is streamed per-ROI via /vsicurl. No-op in local mode.
        self.index = IndexRepository(settings=self.settings)
        self.index.ensure_tables()
        # Keyed combo lookup (partitioned Parquet, with a JSON fallback); raises
        # FileNotFoundError if no dictionary has been built/pulled yet.
        self.dictionary = DictionaryRepository(settings=self.settings)
        # Normalized event metadata (events.parquet); unused when a legacy JSON
        # dictionary embeds its own events.
        self.events = EventsRepository(settings=self.settings)

        # Reject a cache built by incompatible code (grid/dtype/schema changes).
        validate_manifest(self.settings.get_manifest_path())

    def query(
        self,
        region: Any = None,
        *,
        point: tuple[float, float] | None = None,
        radius_m: float = 0.0,
        bbox: tuple[float, float, float, float] | None = None,
        shapefile: str | Path | None = None,
        buffer_m: float = 0.0,
        year: int | None = None,
        start: str | int | None = None,
        end: str | int | None = None,
        level: int | None = None,
        shape: str = "exact",
        output_dir: str | Path | None = None,
    ) -> FloodFrame:
        """Query the index for a region/time -> a FloodFrame (no rasters fetched).

        Backs `floods` — see it for the argument reference.
        ``output_dir`` is the directory the cached-download auto-detect scans
        (defaults to ``settings.output_dir``); pass the same dir you download to so a
        re-query of a previously-downloaded area is immediately actionable.

        Returns:
            A historic `FloodFrame` (one row per flood event); empty if the
            ROI is outside the index or contains no floods.

        Raises:
            FileNotFoundError: If no local index exists and no remote index is
                configured.
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

        if self.index.local_index_missing():
            raise FileNotFoundError(
                "Index raster not found. Run 'export'/'build-index', pull a published "
                "index, or set index_mode='remote' with index_base_url."
            )

        # A per-process cached open handle (opening a remote COG re-reads its tile index
        # each time); the GDAL env keeps the windowed read block-cached in-process, so a
        # second query is near-instant. Local file or a /vsicurl URL — do not close it.
        src = self.index.open_index()
        with rasterio.Env(**_GDAL_ENV):
            try:
                out_image, _ = rasterio.mask.mask(src, [roi], crop=True)
            except ValueError:
                logger.warning("roi_outside_bounds")
                return _make_frame([], self.settings)

        flat = out_image[out_image != 0]
        if flat.size == 0:
            logger.info("no_floods_found_in_area")
            return _make_frame([], self.settings)

        # Accumulate flooded-pixel counts per event across the ROI's combos.
        combo_ids, counts = np.unique(flat, return_counts=True)
        combos = self.dictionary.lookup_combos([int(c) for c in combo_ids])
        pixels: dict[int, int] = {}
        embedded: dict[int, dict[str, Any]] = {}  # metadata a legacy JSON dict carries
        for combo_id, count in zip(combo_ids, counts, strict=False):
            combo = combos.get(str(int(combo_id)))
            if combo is None:
                continue
            if combo.get("events"):  # legacy JSON dictionary embeds event metadata
                for event in combo["events"]:
                    gid = event.get("global_id")
                    if gid is None:
                        continue
                    embedded.setdefault(gid, event)
                    pixels[gid] = pixels.get(gid, 0) + int(count)
            else:  # normalized dict: integer flood_ids -> metadata from events.parquet
                for gid in combo.get("flood_ids", []):
                    pixels[gid] = pixels.get(gid, 0) + int(count)

        # Resolve event metadata from the embedded JSON, else the events table.
        events = embedded or self.events.lookup_events(list(pixels.keys()))

        cell_area = _cell_area_km2(roi)
        rows = [
            {
                "collection": "historic",
                "event_id": gid,
                "date": event.get("start_date"),
                "year": int(event["year"]) if event.get("year") else None,
                "end_date": event.get("end_date"),
                "cluster_id": event.get("cluster_id"),
                "filename": event.get("filename"),
                "download_url": event.get("download_url"),
                "area_km2": round(pixels[gid] * cell_area, 3),
                "geometry": roi,
            }
            for gid, event in events.items()
        ]
        logger.info("events_identified", count=len(rows))

        frame = _make_frame(rows, self.settings)
        if year is not None:
            frame = frame[frame["year"] == int(year)]
        if start is not None:
            frame = frame[frame["date"] >= _norm_start(start)]
        if end is not None:
            frame = frame[frame["date"] <= _norm_end(end)]
        frame._settings = self.settings
        if self.settings.autodetect_downloads:
            scan_dir = (
                output_dir if output_dir is not None else self.settings.output_dir
            )
            _attach_cached_paths(frame, scan_dir, only_if_any=True)
        return frame


def _frame_roi_key(frame: Any) -> str | None:
    """The shared ROI hash for a catalogue, computed once.

    Every row of a catalogue shares one ROI geometry, so hashing it per row (once
    per event, over a possibly large WKB) is wasteful. Returns the key of the first
    non-empty geometry, or ``None`` for an empty frame.
    """
    for geom in getattr(frame, "geometry", []):
        if geom is not None:
            return roi_key(geom)
    return None


def _historic_crop_name(row: Any, *, key: str | None = None) -> str:
    """Deterministic, ROI-safe output filename for one historic event row.

    Embeds a short hash of the row geometry so the same event cropped to two
    different query areas lands in two different files (no collision / stale reuse).
    ``key`` is the precomputed shared ROI hash (see `_frame_roi_key`); when
    omitted it is derived from the row geometry.
    """
    date = row.get("date") or "unknown"
    if key is None:
        key = roi_key(row.geometry) if row.geometry is not None else "noroi"
    return f"flood_{date}_id{row.get('event_id')}_{key}.tif"


def _crop_namer(frame: Any) -> Any:
    """The ROI-safe crop-filename builder for this frame's kind (hazard vs historic).

    The returned callable takes ``(row, *, key=None)`` where ``key`` is the shared
    ROI hash from `_frame_roi_key`.
    """
    if is_hazard(frame):
        from .hazard import _hazard_crop_name

        return _hazard_crop_name
    return _historic_crop_name


def _attach_cached_paths(
    frame: Any, output_dir: str | Path, *, only_if_any: bool = False
) -> None:
    """Set a ``path`` column from crops already present in ``output_dir``.

    For each row, the expected ROI-safe crop name is looked up in ``output_dir``;
    the cell is the path if that file exists and is non-empty, else ``None``. Used
    both after `download` (records what was written) and at query
    time (auto-detects a previously downloaded region). With ``only_if_any=True``
    the column is written only when at least one crop is found, so a fresh query
    over an un-downloaded region stays clean (no all-``None`` ``path`` column).
    """
    out_dir = Path(output_dir)
    namer = _crop_namer(frame)
    key = _frame_roi_key(frame)  # every row shares one ROI; hash it once
    column: list[str | None] = []
    for _, row in frame.iterrows():
        candidate = out_dir / namer(row, key=key)
        column.append(str(candidate) if nonempty_file(candidate) else None)
    if only_if_any and not any(column):
        return
    # `frame` is often a slice (cat.head(1)/cat[mask]) we intend to mutate + return,
    # so silence the spurious chained-assignment warning.
    with pd.option_context("mode.chained_assignment", None):
        frame["path"] = column


def download_catalogue(
    catalogue: gpd.GeoDataFrame,
    output_dir: str | Path | None = None,
    *,
    crop: bool = True,
    force: bool = False,
    settings: Settings | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> list[Path]:
    """Download + crop the source rasters for the rows in `catalogue`.

    Cropped outputs are cached: a row whose GeoTIFF already exists is reused
    (nothing is re-downloaded or re-cropped) unless ``force=True``. The raw source
    tiles are cached separately by `DownloadService`.

    Args:
        catalogue: A (possibly filtered) FloodFrame / GeoDataFrame of events.
        output_dir: Where to write cropped GeoTIFFs. Defaults to `settings.output_dir`.
        crop: Crop each raster to its row geometry (the ROI). If False, copy whole.
        force: Re-download + re-crop even if the output already exists.
        settings: Optional configuration; defaults to the catalogue's settings or
            `get_settings`.
        on_bytes: Optional progress callback ``(n_bytes) -> None`` invoked per
            downloaded chunk (used by the CLI to drive a progress bar).

    Returns:
        list[Path]: Paths of the written (or reused) GeoTIFFs.
    """
    settings = settings or getattr(catalogue, "_settings", None) or get_settings()
    out_dir = Path(output_dir) if output_dir is not None else settings.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    downloader = DownloadService(settings=settings)
    key = _frame_roi_key(catalogue)  # every row shares one ROI; hash it once

    # Partition rows into cache hits (recorded as-is) and download tasks, keeping the
    # row order of any path we return.
    ordered: list[int] = []  # positions that should yield a path, in row order
    results: dict[int, Path] = {}  # position -> written/reused path
    tasks: list[tuple[int, str, str, Path, Any]] = []
    for pos, (_, row) in enumerate(catalogue.iterrows()):
        url = row.get("download_url")
        filename = row.get("filename")
        if not url or not filename:
            continue
        out_path = out_dir / _historic_crop_name(row, key=key)
        ordered.append(pos)
        if not force and nonempty_file(out_path):
            logger.debug("crop_cache_hit", file=out_path.name)
            results[pos] = out_path
            continue
        tasks.append((pos, url, filename, out_path, row.geometry))

    def _commit(pos: int, src_path: Path | None, out_path: Path, geom: Any) -> None:
        """Crop (or copy) a just-downloaded source into ``out_path`` — main thread."""
        if src_path is None:
            return
        if crop and geom is not None:
            if RasterOps.crop_raster(src_path, out_path, geom):
                results[pos] = out_path
                logger.info("map_extracted", file=out_path.name)
        else:
            shutil.copyfile(src_path, out_path)
            results[pos] = out_path

    # Download the source tiles concurrently (the bottleneck); crop each as it lands
    # in the main thread (crops stay serialized — cheap and thread-safe).
    if tasks:
        workers = min(len(tasks), settings.max_workers_dl)
        with (
            file_progress(
                len(tasks),
                enabled=on_bytes is None,
                description="Downloading flood maps",
            ) as advance,
            ThreadPoolExecutor(max_workers=workers) as pool,
        ):
            futures = {
                pool.submit(
                    downloader.download_file, url, filename, on_bytes=on_bytes
                ): (pos, out_path, geom)
                for pos, url, filename, out_path, geom in tasks
            }
            for future in as_completed(futures):
                pos, out_path, geom = futures[future]
                _commit(pos, future.result(), out_path, geom)
                advance(1)

    return [results[pos] for pos in ordered if pos in results]


def _dispatch_download(
    catalogue: gpd.GeoDataFrame,
    output_dir: str | Path | None = None,
    *,
    crop: bool = True,
    force: bool = False,
    settings: Settings | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> list[Path]:
    """Route a catalogue to the right download path by its ``collection``.

    Hazard catalogues need fetch + mosaic + crop; historic catalogues are one
    file per row. Inspecting the ``collection`` column lets a single
    ``.download()`` / `download` serve both. The hazard import is
    lazy to avoid a discovery <-> hazard import cycle.

    ``on_bytes`` is threaded straight through: the CLI passes it to drive its own
    byte bar, while the library default (``None``) lets each downloader show a
    per-file step bar over its parallel transfers.
    """
    if (
        "collection" in catalogue.columns
        and (catalogue["collection"] == "hazard").any()
    ):
        from .hazard import download_hazard_catalogue

        return download_hazard_catalogue(
            catalogue,
            output_dir,
            crop=crop,
            force=force,
            settings=settings,
            on_bytes=on_bytes,
        )
    return download_catalogue(
        catalogue,
        output_dir,
        crop=crop,
        force=force,
        settings=settings,
        on_bytes=on_bytes,
    )
