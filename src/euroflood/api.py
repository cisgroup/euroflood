"""Top-level functional API for EuroFlood.

The ergonomic surface researchers use::

    import euroflood as ef

    cat = ef.floods("Zutphen", start="2018", end="2021")  # cheap query
    cat["year"].value_counts()  # it's a GeoDataFrame
    big = cat[cat.area_km2 > 1]  # filter with pandas
    paths = big.download("out/")  # or ef.download(big, "out/")

    haz = ef.hazard("Zutphen", return_period=[100, 500])  # global hazard
    haz.download("out/hazard/")

`floods()` and `hazard()` both return a
`FloodFrame` (a thin GeoDataFrame subclass).
`download()` is the only step that fetches rasters and works for both (it routes
by the catalogue's ``collection``).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Settings
from .pipelines.discovery import (
    DiscoveryPipeline,
    FloodFrame,
    _dispatch_download,
)
from .pipelines.hazard import HazardPipeline, mirror_hazard

__all__ = ["download", "floods", "hazard", "mirror_hazard"]


def floods(
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
    settings: Settings | None = None,
) -> FloodFrame:
    """Query historic flood events for a region (cheap; no rasters downloaded).

    Args:
        region: Place name, shapely geometry, GeoDataFrame, or bbox tuple.
        point: A (lat, lon) point; combine with `radius_m`.
        radius_m: Radius in metres around `point`.
        bbox: A (minx, miny, maxx, maxy) bounding box (WGS84).
        shapefile: Path to a vector file used as the ROI.
        buffer_m: Optional extra metric buffer around the ROI.
        year: Keep only events in this year.
        start: Keep events on/after this date (``"YYYY"`` or ``"YYYY-MM-DD"``).
        end: Keep events on/before this date (``"YYYY"`` or ``"YYYY-MM-DD"``).
        level: Optional NUTS level filter for place-name resolution.
        shape: ROI shape derived from the resolved region — ``"exact"`` (default,
            the raw boundary), ``"bbox"`` (its bounding rectangle), or ``"hull"``
            (its convex hull). Useful when an admin boundary follows a river or is
            oddly shaped; ``buffer_m`` still applies on top of the chosen shape.
        output_dir: Directory the cached-download auto-detect scans (defaults to
            ``settings.output_dir``). Pass the same dir you ``.download()`` to so a
            later query of the same area already carries the files.
        settings: Optional configuration (e.g. a different cache/index version).

    Returns:
        FloodFrame: One row per flood event; ``.download("out/")`` fetches rasters.

    Raises:
        GeocodingError: If a place name cannot be resolved.
        FileNotFoundError: If no index is available locally and no remote index
            is configured (run ``build-index``/``mirror-index`` or set
            ``index_mode="remote"``).

    Examples:
        >>> import euroflood as ef
        >>> cat = ef.floods("Zutphen")  # doctest: +SKIP
        >>> recent = cat[cat["date"] >= "2021-01-01"]  # doctest: +SKIP
        >>> recent.download("out/")  # doctest: +SKIP
    """
    pipeline = DiscoveryPipeline(settings=settings)
    return pipeline.query(
        region,
        point=point,
        radius_m=radius_m,
        bbox=bbox,
        shapefile=shapefile,
        buffer_m=buffer_m,
        year=year,
        start=start,
        end=end,
        level=level,
        shape=shape,
        output_dir=output_dir,
    )


def hazard(
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
    settings: Settings | None = None,
) -> FloodFrame:
    """Query global flood-hazard maps by return period (CEMS-GLOFAS).

    Mirrors `floods` exactly but takes ``return_period`` instead of a time
    filter. Returns a downloadable FloodFrame with one row per return period;
    ``.download("out/")`` writes one mosaicked, ROI-cropped ``hazard_RP{rp}.tif``
    per row.

    Args:
        region: Place name, shapely geometry, GeoDataFrame, or bbox tuple.
        point: A (lat, lon) point; combine with `radius_m`.
        radius_m: Radius in metres around `point`.
        bbox: A (minx, miny, maxx, maxy) bounding box (WGS84).
        shapefile: Path to a vector file used as the ROI.
        buffer_m: Optional extra metric buffer around the ROI.
        return_period: One or more of 10/20/50/75/100/200/500. ``None`` returns
            all available return periods.
        level: Optional NUTS level filter for place-name resolution.
        shape: ROI shape derived from the resolved region — ``"exact"`` (default,
            the raw boundary), ``"bbox"`` (its bounding rectangle), or ``"hull"``
            (its convex hull); ``buffer_m`` still applies on top.
        output_dir: Directory the cached-download auto-detect scans (defaults to
            ``settings.output_dir``).
        settings: Optional configuration.

    Returns:
        FloodFrame: One row per return period; ``.download("out/")`` fetches maps.

    Raises:
        GeocodingError: If a place name cannot be resolved.
        HazardError: If the hazard tile index cannot be located or read.

    Examples:
        >>> import euroflood as ef
        >>> ef.hazard("Zutphen", return_period=100).download(
        ...     "hazard/"
        ... )  # doctest: +SKIP
        >>> ef.hazard("Zutphen", return_period=[100, 500])  # doctest: +SKIP
    """
    pipeline = HazardPipeline(settings=settings)
    return pipeline.query(
        region,
        point=point,
        radius_m=radius_m,
        bbox=bbox,
        shapefile=shapefile,
        buffer_m=buffer_m,
        return_period=return_period,
        level=level,
        shape=shape,
        output_dir=output_dir,
    )


def download(
    catalogue: FloodFrame,
    output_dir: str | Path | None = None,
    *,
    crop: bool = True,
    force: bool = False,
    settings: Settings | None = None,
    on_bytes: Callable[[int], None] | None = None,
) -> list[Path]:
    """Download + crop the rasters for a (possibly filtered) catalogue.

    The functional equivalent of ``catalogue.download(...)``; works on any
    GeoDataFrame produced by `floods` or `hazard` (it routes by the
    ``collection`` column), even after heavy reshaping. Returns the file paths;
    prefer the `download` method (returns the
    catalogue itself) for the fluent, actionable workflow.

    Args:
        catalogue: A catalogue from `floods` or `hazard`, or a filtered
            subset of one.
        output_dir: Directory for the written GeoTIFFs. Defaults to
            ``settings.output_dir``.
        crop: If True (default), crop each raster to the ROI polygon.
        force: Re-download + re-crop even if the output already exists (outputs are
            otherwise cached and reused).
        settings: Optional configuration.
        on_bytes: Optional progress callback ``(n_bytes) -> None`` per downloaded
            chunk (the CLI uses it to drive a progress bar).

    Returns:
        The paths of the written GeoTIFFs — one per historic event, or one
        ``hazard_RP{rp}_{roi}.tif`` per return period.

    Raises:
        HazardError: If a hazard mosaic/crop fails.

    Examples:
        >>> import euroflood as ef
        >>> cat = ef.floods("Zutphen")  # doctest: +SKIP
        >>> ef.download(cat[cat["date"] >= "2021-01-01"], "out/")  # doctest: +SKIP
    """
    return _dispatch_download(
        catalogue,
        output_dir,
        crop=crop,
        force=force,
        settings=settings,
        on_bytes=on_bytes,
    )
