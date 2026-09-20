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

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

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
from .services.nuts import NutsRepository

__all__ = [
    "download",
    "floods",
    "hazard",
    "mirror",
    "nuts",
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
    nuts: str | Sequence[str] | None = None,
    buffer_m: float = 0.0,
    crs: Any = None,
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
        point: A ``(lat, lon)`` point in WGS 84, or ``(x, y)`` in a projected
            ``crs``; combine with `radius_m`.
        radius_m: Radius in ground metres around `point`.
        bbox: A ``(minx, miny, maxx, maxy)`` bounding box in WGS 84 lon/lat, or in
            ``crs``.
        shapefile: Path to a vector file used as the ROI.
        nuts: One or more Eurostat NUTS identifiers, e.g. ``"NL22"`` (Gelderland)
            or ``["NL22", "NL21"]`` (their union). Case-insensitive; the level follows
            from the identifier (``NL`` country, ``NL2``, ``NL22``, ``NL225``).
            Boundaries come from the Eurostat GISCO 1:1M files for
            ``settings.nuts_year`` (default 2024), downloaded once per level and
            cached. Find identifiers with `nuts`.
        buffer_m: Optional extra buffer around the ROI, in ground metres.
        crs: Coordinate reference system of ``point``, ``bbox`` and a bare shapely
            or 4-tuple ``region``: any horizontal (geographic or projected) CRS
            accepted by ``pyproj.CRS.from_user_input``, e.g. an authority string
            (``"EPSG:28992"``), an EPSG integer (``3035``), a WKT/PROJ string or a
            ``pyproj.CRS``. Default ``None`` means WGS 84 lon/lat (``EPSG:4326``),
            i.e. today's behaviour. ``point`` is ``(lat, lon)`` in any geographic
            CRS and ``(x, y)`` = (easting, northing) in any projected CRS,
            regardless of the axis order the CRS authority declares (EPSG:3035 is
            northing-first in the registry but is still given as (easting,
            northing) here); ``bbox`` is always ``(minx, miny, maxx, maxy)``. A
            GeoDataFrame or vector file carries its own CRS: ``crs`` then only
            fills in a missing one and must agree with it otherwise. Not valid
            with a place name. The result is always EPSG:4326.
        year: Keep only events in this year.
        start: Keep events on/after this date (``"YYYY"`` or ``"YYYY-MM-DD"``).
        end: Keep events on/before this date (``"YYYY"`` or ``"YYYY-MM-DD"``).
        level: Optional NUTS level filter for place-name resolution.
        shape: ROI shape derived from the resolved region: ``"exact"`` (default,
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
        CRSError: (a `GeocodingError`) if ``crs`` is invalid, conflicts with the
            CRS of a GeoDataFrame/file, is combined with a place name, or the
            coordinates do not fit it.
        NutsError: (a `GeocodingError`) if a NUTS identifier is malformed or
            unknown, has no published boundary, or its boundary file is unavailable
            offline.
        FileNotFoundError: If no index is available locally and no remote index
            is configured (run ``build-index``/``mirror-index`` or set
            ``index_mode="remote"``).

    Examples:
        >>> import euroflood as ef
        >>> cat = ef.floods("Zutphen")  # doctest: +SKIP
        >>> recent = cat[cat["date"] >= "2021-01-01"]  # doctest: +SKIP
        >>> recent.download("out/")  # doctest: +SKIP
        >>> ef.floods(
        ...     bbox=(200000, 455000, 220000, 475000), crs="EPSG:28992"
        ... )  # Dutch RD New metres  # doctest: +SKIP
        >>> ef.floods(
        ...     nuts="NL22"
        ... )  # a Eurostat NUTS region (Gelderland)  # doctest: +SKIP
    """
    pipeline = DiscoveryPipeline(settings=settings)
    return pipeline.query(
        region,
        point=point,
        radius_m=radius_m,
        bbox=bbox,
        shapefile=shapefile,
        nuts=nuts,
        buffer_m=buffer_m,
        crs=crs,
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
    nuts: str | Sequence[str] | None = None,
    buffer_m: float = 0.0,
    crs: Any = None,
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
        point: A ``(lat, lon)`` point in WGS 84, or ``(x, y)`` in a projected
            ``crs``; combine with `radius_m`.
        radius_m: Radius in ground metres around `point`.
        bbox: A ``(minx, miny, maxx, maxy)`` bounding box in WGS 84 lon/lat, or in
            ``crs``.
        shapefile: Path to a vector file used as the ROI.
        nuts: One or more Eurostat NUTS identifiers, e.g. ``"NL22"`` (Gelderland)
            or ``["NL22", "NL21"]`` (their union). Case-insensitive; the level follows
            from the identifier (``NL`` country, ``NL2``, ``NL22``, ``NL225``).
            Boundaries come from the Eurostat GISCO 1:1M files for
            ``settings.nuts_year`` (default 2024), downloaded once per level and
            cached. Find identifiers with `nuts`.
        buffer_m: Optional extra buffer around the ROI, in ground metres.
        crs: Coordinate reference system of ``point``, ``bbox`` and a bare shapely
            or 4-tuple ``region``: any horizontal (geographic or projected) CRS
            accepted by ``pyproj.CRS.from_user_input``, e.g. an authority string
            (``"EPSG:28992"``), an EPSG integer (``3035``), a WKT/PROJ string or a
            ``pyproj.CRS``. Default ``None`` means WGS 84 lon/lat (``EPSG:4326``),
            i.e. today's behaviour. ``point`` is ``(lat, lon)`` in any geographic
            CRS and ``(x, y)`` = (easting, northing) in any projected CRS,
            regardless of the axis order the CRS authority declares (EPSG:3035 is
            northing-first in the registry but is still given as (easting,
            northing) here); ``bbox`` is always ``(minx, miny, maxx, maxy)``. A
            GeoDataFrame or vector file carries its own CRS: ``crs`` then only
            fills in a missing one and must agree with it otherwise. Not valid
            with a place name. The result is always EPSG:4326.
        return_period: One or more of 10/20/50/75/100/200/500. ``None`` returns
            all available return periods.
        level: Optional NUTS level filter for place-name resolution.
        shape: ROI shape derived from the resolved region: ``"exact"`` (default,
            the raw boundary), ``"bbox"`` (its bounding rectangle), or ``"hull"``
            (its convex hull); ``buffer_m`` still applies on top.
        output_dir: Directory the cached-download auto-detect scans (defaults to
            ``settings.output_dir``).
        settings: Optional configuration.

    Returns:
        FloodFrame: One row per return period; ``.download("out/")`` fetches maps.

    Raises:
        GeocodingError: If a place name cannot be resolved.
        CRSError: (a `GeocodingError`) if ``crs`` is invalid, conflicts with the
            CRS of a GeoDataFrame/file, is combined with a place name, or the
            coordinates do not fit it.
        NutsError: (a `GeocodingError`) if a NUTS identifier is malformed or
            unknown, has no published boundary, or its boundary file is unavailable
            offline.
        HazardError: If the hazard tile index cannot be located or read.

    Examples:
        >>> import euroflood as ef
        >>> ef.hazard("Zutphen", return_period=100).download(
        ...     "hazard/"
        ... )  # doctest: +SKIP
        >>> ef.hazard("Zutphen", return_period=[100, 500])  # doctest: +SKIP
        >>> ef.hazard(
        ...     point=(308400, 5780300),
        ...     radius_m=5000,
        ...     crs="EPSG:32632",
        ...     return_period=100,
        ... )  # a UTM 32N point: (x, y)  # doctest: +SKIP
        >>> ef.hazard(nuts=["NL22", "NL21"], return_period=100)  # doctest: +SKIP
    """
    pipeline = HazardPipeline(settings=settings)
    return pipeline.query(
        region,
        point=point,
        radius_m=radius_m,
        bbox=bbox,
        shapefile=shapefile,
        nuts=nuts,
        buffer_m=buffer_m,
        crs=crs,
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
        The paths of the written GeoTIFFs: one per historic event, or one
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


def nuts(
    query: str | None = None,
    *,
    level: int | None = None,
    country: str | None = None,
    geometry: bool = False,
    settings: Settings | None = None,
) -> pd.DataFrame | gpd.GeoDataFrame:
    """Find Eurostat NUTS regions: search by name, or list by country and level.

    Use it to find the identifier for `floods` / `hazard` ``nuts=`` (``"Gelderland"``
    -> ``NL22``) or to enumerate regions to loop over (every Dutch NUTS-2 region).
    NUTS levels: 0 country (``NL``), 1 major regions (``NL2``), 2 basic regions
    (``NL22``), 3 small regions (``NL225``); an identifier's length gives its level.

    Args:
        query: A region name (accent/case-insensitive; bilingual and suffixed names
            and common exonyms such as ``"Cologne"`` match, and every name containing
            the text is listed), or a NUTS identifier, which lists that region and
            its descendants (``"NL2"`` -> ``NL2``, ``NL22``, ``NL225``, ...).
        level: Keep only this NUTS level (0-3).
        country: Keep only this two-letter country code (``"NL"``).
        geometry: Attach the boundary polygons (downloads the GISCO per-level files
            the result needs, once). Without it only the 90 KB attribute table is
            read, so a search never downloads a boundary file.
        settings: Optional configuration (``nuts_year``, ``nuts_scale``, ...).

    Returns:
        A ``DataFrame`` with ``NUTS_ID``, ``LEVL_CODE``, ``CNTR_CODE``, ``NAME_LATN``
        and ``NUTS_NAME`` sorted by identifier; with ``geometry=True`` a
        ``GeoDataFrame`` in EPSG:4326. No match is an empty frame, not an error.

    Raises:
        NutsError: If the attribute table or a boundary file is unavailable offline
            or fails to download.

    Examples:
        >>> import euroflood as ef
        >>> ef.nuts("Gelderland")  # -> NL22 (level 2), NL224  # doctest: +SKIP
        >>> ef.nuts(
        ...     country="NL", level=2
        ... )  # every Dutch NUTS-2 region  # doctest: +SKIP
        >>> for nuts_id in ef.nuts(country="NL", level=2)["NUTS_ID"]:  # doctest: +SKIP
        ...     ef.floods(nuts=nuts_id)
        >>> ef.nuts("NL2", level=3, geometry=True).explore()  # doctest: +SKIP
    """
    return NutsRepository(settings=settings).regions(
        query, level=level, country=country, geometry=geometry
    )


def offline(enabled: bool = True) -> None:
    """Flip EuroFlood into (or out of) fully-offline mode.

    Sets the master ``offline`` switch on the global settings, forcing both
    collections cache-only (the index COG is not streamed, hazard tiles are not
    fetched) and the geocoder to the local NUTS backend, the programmatic
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
    nuts: str | Sequence[str] | None = None,
    buffer_m: float = 0.0,
    crs: Any = None,
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

    - ``"index"``: the flood **catalogue** (global; enables offline ``floods()`` queries).
    - ``"floods"``: historic flood **depth maps** for the region (ensures the index).
    - ``"hazard"``: GLOFAS **hazard tiles** for the region.
    - ``"all"``: index + flood depths + hazard tiles for the region.

    ``region``/``bbox``/``point``/… scope the region for ``floods``/``hazard``/``all``;
    ``crs`` declares the coordinate reference system of ``bbox``/``point``/a bare
    geometry, and ``nuts`` selects Eurostat NUTS regions by identifier, both as in
    `floods`. Resolving a NUTS region caches its GISCO boundary file, so a later
    offline run finds it.
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
            nuts=nuts,
            buffer_m=buffer_m,
            crs=crs,
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
            nuts=nuts,
            buffer_m=buffer_m,
            crs=crs,
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
                nuts=nuts,
                buffer_m=buffer_m,
                crs=crs,
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
                nuts=nuts,
                buffer_m=buffer_m,
                crs=crs,
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
    nuts: str | Sequence[str] | None = None,
    buffer_m: float = 0.0,
    crs: Any = None,
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

    ``target`` mirrors `mirror` (``index``/``floods``/``hazard``/``all``), and so do
    the ROI arguments including ``crs`` and ``nuts``. ``deep``
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
            nuts=nuts,
            buffer_m=buffer_m,
            crs=crs,
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
            nuts=nuts,
            buffer_m=buffer_m,
            crs=crs,
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
                nuts=nuts,
                buffer_m=buffer_m,
                crs=crs,
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
                nuts=nuts,
                buffer_m=buffer_m,
                crs=crs,
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
