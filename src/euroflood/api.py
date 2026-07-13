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
from .config import settings as _settings
from .pipelines.discovery import (
    DiscoveryPipeline,
    FloodFrame,
    _dispatch_download,
    mirror_floods,
    mirror_index,
    verify_floods_mirror,
    verify_index_mirror,
)
from .pipelines.hazard import HazardPipeline, mirror_hazard, verify_hazard_mirror
from .services.mirror_ledger import MirrorReport, MirrorResult

__all__ = [
    "download",
    "floods",
    "hazard",
    "mirror",
    "offline",
    "verify",
]

_MIRROR_TARGETS = ("index", "floods", "hazard", "all")


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


def offline(enabled: bool = True) -> None:
    """Flip EuroFlood into (or out of) fully-offline mode.

    Sets the master ``offline`` switch on the global settings, forcing both
    collections cache-only (the index COG is not streamed, hazard tiles are not
    fetched) and the geocoder to the local NUTS backend — the programmatic
    equivalent of ``EUROFLOOD_OFFLINE=1``. Mirror your data first (see `mirror`).

    Examples:
        >>> import euroflood as ef
        >>> ef.offline()  # this node has no internet  # doctest: +SKIP
        >>> ef.hazard(
        ...     bbox=(6.1, 52.0, 6.3, 52.2), return_period=100
        ... ).download()  # doctest: +SKIP
    """
    _settings.offline = enabled


def mirror(
    target: str,
    region: Any = None,
    *,
    point: tuple[float, float] | None = None,
    radius_m: float = 0.0,
    bbox: tuple[float, float, float, float] | None = None,
    shapefile: str | Path | None = None,
    buffer_m: float = 0.0,
    return_period: int | list[int] | None = None,
    year: int | None = None,
    start: str | int | None = None,
    end: str | int | None = None,
    level: int | None = None,
    shape: str = "exact",
    dry_run: bool = False,
    settings: Settings | None = None,
) -> MirrorResult | dict[str, MirrorResult]:
    """Stage a data layer for offline/HPC use.

    ``target`` selects what to mirror into the cache:

    - ``"index"`` — the flood **catalogue** (global; enables offline ``floods()`` queries).
    - ``"floods"`` — historic flood **depth maps** for the region (ensures the index).
    - ``"hazard"`` — GLOFAS **hazard tiles** for the region.
    - ``"all"`` — index + flood depths + hazard tiles for the region.

    ``region``/``bbox``/``point``/… scope the region for ``floods``/``hazard``/``all``.
    ``return_period`` applies to hazard, ``year``/``start``/``end`` to floods. Pass
    ``dry_run=True`` to plan without downloading.

    Returns:
        A `MirrorResult` (single target) or a ``{layer: MirrorResult}`` dict (``"all"``).

    Examples:
        >>> import euroflood as ef
        >>> ef.mirror(
        ...     "hazard", bbox=(6.1, 52.0, 6.3, 52.2), return_period=100
        ... )  # doctest: +SKIP
        >>> ef.mirror("all", "Zutphen")  # doctest: +SKIP
    """
    key = target.lower()
    if key == "index":
        return mirror_index(dry_run=dry_run, settings=settings)
    if key == "floods":
        return mirror_floods(
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
            dry_run=dry_run,
            settings=settings,
        )
    if key == "hazard":
        return mirror_hazard(
            region,
            point=point,
            radius_m=radius_m,
            bbox=bbox,
            shapefile=shapefile,
            buffer_m=buffer_m,
            return_period=return_period,
            level=level,
            shape=shape,
            dry_run=dry_run,
            settings=settings,
        )
    if key == "all":
        return {
            "index": mirror_index(dry_run=dry_run, settings=settings),
            "floods": mirror_floods(
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
                dry_run=dry_run,
                settings=settings,
            ),
            "hazard": mirror_hazard(
                region,
                point=point,
                radius_m=radius_m,
                bbox=bbox,
                shapefile=shapefile,
                buffer_m=buffer_m,
                return_period=return_period,
                level=level,
                shape=shape,
                dry_run=dry_run,
                settings=settings,
            ),
        }
    raise ValueError(
        f"Unknown mirror target {target!r}; choose from {list(_MIRROR_TARGETS)}."
    )


def verify(
    target: str,
    region: Any = None,
    *,
    point: tuple[float, float] | None = None,
    radius_m: float = 0.0,
    bbox: tuple[float, float, float, float] | None = None,
    shapefile: str | Path | None = None,
    buffer_m: float = 0.0,
    return_period: int | list[int] | None = None,
    year: int | None = None,
    start: str | int | None = None,
    end: str | int | None = None,
    level: int | None = None,
    shape: str = "exact",
    deep: bool = False,
    settings: Settings | None = None,
) -> MirrorReport | dict[str, MirrorReport]:
    """Report local-mirror readiness (present/missing/corrupt) for a data layer.

    ``target`` mirrors `mirror` (``index``/``floods``/``hazard``/``all``). ``deep``
    re-hashes each file's sha256 against the ledger (slower, catches silent corruption).
    Returns a `MirrorReport` (or a ``{layer: MirrorReport}`` dict for ``"all"``).
    """
    key = target.lower()
    if key == "index":
        return verify_index_mirror(deep=deep, settings=settings)
    if key == "floods":
        return verify_floods_mirror(
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
            deep=deep,
            settings=settings,
        )
    if key == "hazard":
        return verify_hazard_mirror(
            region,
            point=point,
            radius_m=radius_m,
            bbox=bbox,
            shapefile=shapefile,
            buffer_m=buffer_m,
            return_period=return_period,
            level=level,
            shape=shape,
            deep=deep,
            settings=settings,
        )
    if key == "all":
        return {
            "index": verify_index_mirror(deep=deep, settings=settings),
            "floods": verify_floods_mirror(
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
                deep=deep,
                settings=settings,
            ),
            "hazard": verify_hazard_mirror(
                region,
                point=point,
                radius_m=radius_m,
                bbox=bbox,
                shapefile=shapefile,
                buffer_m=buffer_m,
                return_period=return_period,
                level=level,
                shape=shape,
                deep=deep,
                settings=settings,
            ),
        }
    raise ValueError(
        f"Unknown verify target {target!r}; choose from {list(_MIRROR_TARGETS)}."
    )
