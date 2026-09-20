"""Resolve flexible location inputs to a single WGS84 geometry.

Turns the various ways a user can name a region (place name, Eurostat NUTS
identifier, point + radius, bounding box, a user-supplied shapefile, a shapely
geometry, or a GeoDataFrame) into one shapely geometry in EPSG:4326, reusing
`GeocodingService` for names and `NutsRepository` for NUTS identifiers. Coordinates given in another coordinate reference system are declared
with ``crs`` and reprojected here, so everything downstream (the index grid,
tile lookups, crops, cache keys, plots) sees WGS84 only. Metric buffers
(``radius_m`` / ``buffer_m``) are computed in the local UTM zone, so they are
ground metres at any latitude.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import geopandas as gpd
import shapely
from pyproj import CRS
from pyproj.exceptions import CRSError as _PyprojCRSError
from shapely.geometry import Point, box
from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..exceptions import CRSError, GeocodingError
from .geocoding import GeocodingService
from .nuts import NutsRepository
from .raster_ops import RasterOps

_WGS84 = CRS("EPSG:4326")
# Universal Polar Stereographic: the metric fallback where no UTM zone is defined
# (north of 84 N / south of 80 S). Hazard is global, so the poles are reachable.
_UPS_NORTH = CRS("EPSG:32661")
_UPS_SOUTH = CRS("EPSG:32761")
# Longest segment (degrees, ~2 km) a geometry keeps when densified before a metric
# buffer. A lon/lat edge is a curve in UTM; buffering only its chord would offset the
# north and south edges of a country-sized box by several percent.
_BUFFER_DENSIFY_DEG = 0.02
# Segments per longest edge a bbox given in a projected CRS is cut into before it is
# reprojected, so its edges follow the true rectangle: transforming only the four
# corners sags ~250 m (three index pixels) mid-edge on a 100 km box.
_BBOX_DENSIFY_SEGMENTS = 64
# Warnings are attributed to the caller's frame, not to euroflood internals.
_PACKAGE_DIR = str(Path(__file__).resolve().parent.parent)

# ROI shape modes: regularize the resolved geometry before buffering. "exact" keeps
# the raw (often river-following, concave, or multipart) admin boundary; "bbox" and
# "hull" replace it with a clean rectangle / convex hull that sidesteps those quirks
# and collapse MultiPolygon exclaves into one shape.
_SHAPE_TRANSFORMS: dict[str, Callable[[BaseGeometry], BaseGeometry]] = {
    "exact": lambda g: g,
    "bbox": lambda g: g.envelope,
    "hull": lambda g: g.convex_hull,
}


def _metric_crs_for(geom: BaseGeometry) -> CRS:
    """Local UTM zone of a WGS84 geometry (the geopandas / OSMnx convention).

    One UTM unit is one ground metre to within 0.1 % across the zone, which is what
    a metric buffer needs. Falls back to Universal Polar Stereographic where no UTM
    zone exists (``estimate_utm_crs`` raises above 84 N / below 80 S).
    """
    try:
        utm: CRS = gpd.GeoSeries([geom], crs=_WGS84).estimate_utm_crs()
    except RuntimeError:  # "Unable to determine UTM CRS"
        return _UPS_NORTH if geom.centroid.y > 0 else _UPS_SOUTH
    return utm


def _crs_label(crs: CRS) -> str:
    """A short human name for a CRS in messages, e.g. ``EPSG:28992 (Amersfoort / RD New)``."""
    authority = crs.to_authority()
    if authority is not None:
        return f"{authority[0]}:{authority[1]} ({crs.name})"
    return str(crs.name)


def _parse_crs(crs: Any) -> CRS | None:
    """Turn the user's ``crs`` argument into a horizontal ``pyproj.CRS`` (or ``None``).

    Accepts anything ``pyproj.CRS.from_user_input`` does. Rejects, with a
    `CRSError`, values pyproj cannot parse and CRSs that are neither geographic nor
    projected (geocentric or vertical ones would otherwise pass every later check
    and yield a finite but wrong region).
    """
    if crs is None:
        return None
    try:
        parsed = CRS.from_user_input(crs)
    except _PyprojCRSError as err:
        raise CRSError(
            f"Unrecognised crs {crs!r}: {err}. Pass anything pyproj accepts, e.g. an "
            "authority string ('EPSG:28992'), an EPSG integer (3035), a WKT/PROJ "
            "string or a pyproj.CRS."
        ) from err
    if not (parsed.is_geographic or parsed.is_projected):
        raise CRSError(
            f"{_crs_label(parsed)} is not a horizontal (geographic or projected) CRS; "
            "give the 2-D CRS of the coordinates, e.g. 'EPSG:28992' or 'EPSG:4326'."
        )
    return parsed


def _is_wgs84(crs: CRS) -> bool:
    """True for WGS 84 lon/lat under any name (EPSG:4326, OGC:CRS84, "WGS84", ...)."""
    return bool(crs.equals(_WGS84, ignore_axis_order=True))


def _check_coordinates(
    xs: Sequence[float],
    ys: Sequence[float],
    *,
    crs: CRS | None,
    what: str,
    value: Any,
) -> None:
    """Catch the two common slips before reprojecting.

    Degrees out of range in a geographic CRS (including projected metres passed with
    no ``crs``), and degrees typed into a projected CRS whose unit is the metre. Both
    would otherwise resolve to a finite, plausible-looking, wrong region.
    """
    if crs is None or crs.is_geographic:
        if crs is not None and crs.axis_info and crs.axis_info[0].unit_name != "degree":
            return  # e.g. grads (EPSG:4807): the degree range does not apply
        if any(abs(y) > 90 for y in ys) or any(abs(x) > 180 for x in xs):
            name = "WGS 84" if crs is None else _crs_label(crs)
            order = (
                "(lat, lon) pair"
                if what == "point"
                else "(min lon, min lat, max lon, max lat) box"
            )
            raise CRSError(
                f"{what}={value!r} is not a valid {order} in {name}: latitude must be "
                "within [-90, 90] and longitude within [-180, 180]. Note that point is "
                "(lat, lon) while bbox is (min lon, min lat, max lon, max lat); pass "
                "crs= if the coordinates are in a projected system."
            )
        return
    if all(abs(v) <= 360 for v in (*xs, *ys)):
        unit = crs.axis_info[0].unit_name if crs.axis_info else "metre"
        raise CRSError(
            f"{what}={value!r} looks like degrees, but crs={_crs_label(crs)} is a "
            f"projected CRS in {unit}. Pass (x, y) = (easting, northing) in {unit}, "
            "or drop crs= to give WGS 84 (lat, lon)."
        )


def _warn_outside_area_of_use(geom: BaseGeometry, crs: CRS) -> None:
    """Advisory: the region landed outside the CRS's declared area of use.

    Lat/lon typed into a national metre grid transforms to finite coordinates far
    away (RD New puts Zutphen's lat/lon in France), which no range check can catch;
    the registry's area of use can.
    """
    area = crs.area_of_use
    if area is None or area.west > area.east:  # unknown, or crosses the antimeridian
        return
    if box(*area.bounds).intersects(geom):
        return
    centre = geom.centroid
    warnings.warn(
        f"The region resolves to lon {centre.x:.2f}, lat {centre.y:.2f}, outside the "
        f"declared area of use of {_crs_label(crs)} ({area.name}). Check the "
        "coordinates and the CRS.",
        UserWarning,
        skip_file_prefixes=(_PACKAGE_DIR,),
    )


def _to_wgs84(geom: BaseGeometry, crs: CRS | None, *, densify: bool) -> BaseGeometry:
    """Reproject ``geom`` from ``crs`` to WGS84; the identity (same object) for WGS84.

    With ``densify`` (bbox inputs) a box in a projected CRS is segmentized first so
    its reprojected edges follow the true rectangle instead of four chords. User-drawn
    geometries are transformed vertex-wise, as ``GeoDataFrame.to_crs`` does.
    """
    if crs is None or _is_wgs84(crs) or geom.is_empty:
        return geom
    if densify and crs.is_projected and geom.length > 0:
        minx, miny, maxx, maxy = geom.bounds
        geom = shapely.segmentize(
            geom, max(maxx - minx, maxy - miny) / _BBOX_DENSIFY_SEGMENTS
        )
    out = RasterOps.project_geometry(geom, _WGS84, from_crs=crs)
    minx, miny, maxx, maxy = out.bounds
    if (
        not all(math.isfinite(v) for v in (minx, miny, maxx, maxy))
        or minx < -180
        or maxx > 180
        or miny < -90
        or maxy > 90
    ):
        raise CRSError(
            f"The coordinates fall outside the valid domain of {_crs_label(crs)}; "
            "check the CRS and the coordinate order."
        )
    _warn_outside_area_of_use(out, crs)
    return out


def _reconcile_gdf_crs(
    gdf: gpd.GeoDataFrame, crs: CRS | None, *, source: str
) -> gpd.GeoDataFrame:
    """Settle which CRS a GeoDataFrame is in (geopandas constructor semantics).

    Its own CRS wins and must agree with ``crs`` when both are given; ``crs`` fills in
    a missing one (a ``.shp`` without its ``.prj``); with neither, WGS84 is assumed
    with a warning.
    """
    own = gdf.crs
    if own is not None:
        if crs is not None and not own.equals(crs, ignore_axis_order=True):
            raise CRSError(
                f"CRS mismatch: the {source} is in {_crs_label(own)} but "
                f"crs={_crs_label(crs)} was given. Drop crs= to use the {source}'s "
                "own CRS, or reproject it first with .to_crs()."
            )
        return gdf
    if crs is not None:
        return gdf.set_crs(crs)
    warnings.warn(
        f"The {source} has no CRS and none was given via crs=; assuming WGS 84 "
        "lon/lat (EPSG:4326). Pass crs= to declare it.",
        UserWarning,
        skip_file_prefixes=(_PACKAGE_DIR,),
    )
    return gdf.set_crs(_WGS84)


class LocationResolver:
    """Resolve a region specification to a single WGS84 shapely geometry."""

    def __init__(
        self,
        settings: Settings | None = None,
        geocoder: GeocodingService | None = None,
        nuts: NutsRepository | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            settings: Optional configuration. Defaults to `get_settings`.
            geocoder: Optional injected GeocodingService (for place names).
            nuts: Optional injected NutsRepository (for NUTS identifiers).
        """
        self.settings = settings or get_settings()
        self.geocoder = geocoder or GeocodingService(settings=self.settings)
        self.nuts = nuts or NutsRepository(settings=self.settings)

    def resolve(
        self,
        region: str
        | BaseGeometry
        | gpd.GeoDataFrame
        | tuple[float, float, float, float]
        | None = None,
        *,
        point: tuple[float, float] | None = None,
        radius_m: float = 0.0,
        bbox: tuple[float, float, float, float] | None = None,
        shapefile: str | Path | None = None,
        nuts: str | Sequence[str] | None = None,
        buffer_m: float = 0.0,
        crs: Any = None,
        level: int | None = None,
        shape: str = "exact",
    ) -> BaseGeometry:
        """Resolve exactly one location input to a WGS84 geometry.

        Args:
            region: A place name, shapely geometry, GeoDataFrame, or bbox tuple.
            point: A ``(lat, lon)`` point in WGS 84, or ``(x, y)`` in a projected
                ``crs``; combine with `radius_m`.
            radius_m: Radius in ground metres around `point`.
            bbox: A ``(minx, miny, maxx, maxy)`` bounding box in WGS 84 lon/lat, or
                in ``crs``.
            shapefile: Path to a vector file (read via geopandas, unioned).
            nuts: One or more Eurostat NUTS identifiers (``"NL22"``, or a list for
                their union), resolved to their GISCO boundaries by `NutsRepository`.
            buffer_m: Optional extra buffer, in ground metres, applied to the result.
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
            level: Optional NUTS level filter for place-name resolution.
            shape: How to regularize the resolved geometry before buffering:
                ``"exact"`` (default, the raw boundary), ``"bbox"`` (its bounding
                rectangle), or ``"hull"`` (its convex hull). Applied to every input
                mode, so ``buffer_m`` then grows the chosen shape.

        Returns:
            BaseGeometry: The resolved geometry in EPSG:4326.

        Raises:
            GeocodingError: If zero or more than one input is given, the input type
                is unsupported, or ``shape`` is not a known mode.
            NutsError: (a `GeocodingError`) if a NUTS identifier is malformed, unknown
                or has no boundary.
            CRSError: (a `GeocodingError`) if ``crs`` is invalid or not horizontal,
                conflicts with the CRS of a GeoDataFrame/file, is combined with a
                place name, or the coordinates do not fit it (degrees out of range,
                degrees typed into a metre CRS, or outside the CRS's domain).
        """
        parsed = _parse_crs(crs)
        geom = self._base(
            region, point, radius_m, bbox, shapefile, level, crs=parsed, nuts=nuts
        )
        geom = self._apply_shape(geom, shape)
        if buffer_m:
            # Keep a bbox/hull sharp-cornered when buffered: a box grows into a
            # bigger box, not a rounded one; organic "exact" shapes stay round.
            join = "round" if shape == "exact" else "mitre"
            geom = self.buffer_metric(geom, buffer_m, join_style=join)
        return geom

    @staticmethod
    def _apply_shape(geom: BaseGeometry, shape: str) -> BaseGeometry:
        """Regularize an ROI geometry per ``shape`` (see `resolve`).

        Runs before the metric buffer, so ``buffer_m`` grows the chosen shape. For
        ``"bbox"``/``"hull"`` this also collapses a MultiPolygon (exclaves) into one
        clean shape. Raises `GeocodingError` on an unknown mode.
        """
        try:
            transform = _SHAPE_TRANSFORMS[shape]
        except KeyError:
            raise GeocodingError(
                f"Unknown shape {shape!r}; use one of {sorted(_SHAPE_TRANSFORMS)}."
            ) from None
        return transform(geom)

    def _base(
        self,
        region: Any,
        point: tuple[float, float] | None,
        radius_m: float,
        bbox: tuple[float, float, float, float] | None,
        shapefile: str | Path | None,
        level: int | None,
        *,
        crs: CRS | None = None,
        nuts: str | Sequence[str] | None = None,
    ) -> BaseGeometry:
        if sum(x is not None for x in (region, point, bbox, shapefile, nuts)) != 1:
            raise GeocodingError(
                "Provide exactly one of: region, point, bbox, shapefile, nuts."
            )
        if nuts is not None:
            if crs is not None:
                raise CRSError(
                    "crs= does not apply to a NUTS identifier: NUTS boundaries are "
                    "looked up in WGS 84. Drop crs=, or pass coordinates (bbox=, point=, "
                    "a geometry) in that CRS instead."
                )
            return self.nuts.geometry(nuts)
        if region is not None:
            return self._from_region(region, level, crs=crs)
        if bbox is not None:
            return self._from_bbox(bbox, crs, what="bbox")
        if shapefile is not None:
            return self._from_vector_file(shapefile, crs=crs)
        if point is None:  # defensive; the exactly-one check above guarantees it
            raise GeocodingError("Provide exactly one location input.")
        return self._from_point(point, radius_m, crs)

    def _from_point(
        self, point: tuple[float, float], radius_m: float, crs: CRS | None
    ) -> BaseGeometry:
        first, second = point
        if crs is None or crs.is_geographic:
            lat, lon = first, second  # the documented (lat, lon) order for lon/lat CRSs
            x, y = lon, lat
        else:
            x, y = first, second  # (easting, northing) for any projected CRS
        _check_coordinates([x], [y], crs=crs, what="point", value=point)
        geom = _to_wgs84(Point(x, y), crs, densify=False)
        return self.buffer_metric(geom, radius_m) if radius_m else geom

    @staticmethod
    def _from_bbox(
        bounds: Sequence[float], crs: CRS | None, *, what: str
    ) -> BaseGeometry:
        minx, miny, maxx, maxy = bounds
        _check_coordinates(
            [minx, maxx], [miny, maxy], crs=crs, what=what, value=tuple(bounds)
        )
        return _to_wgs84(box(minx, miny, maxx, maxy), crs, densify=True)

    def _from_region(
        self, region: Any, level: int | None, *, crs: CRS | None
    ) -> BaseGeometry:
        if isinstance(region, str):
            if crs is not None:
                raise CRSError(
                    "crs= does not apply to a place name: place names are geocoded in "
                    "WGS 84. Drop crs=, or pass coordinates (bbox=, point=, a geometry) "
                    "in that CRS. To get results in another CRS call .to_crs() on the "
                    "returned catalogue."
                )
            return self.geocoder.get_geometry(region, level=level)
        if isinstance(region, BaseGeometry):
            return _to_wgs84(region, crs, densify=False)
        if isinstance(region, gpd.GeoDataFrame):
            return self._union_wgs84(region, crs=crs, source="GeoDataFrame")
        if isinstance(region, tuple | list) and len(region) == 4:
            return self._from_bbox(region, crs, what="region")
        raise GeocodingError(f"Unsupported region type: {type(region)!r}")

    def _from_vector_file(self, path: str | Path, *, crs: CRS | None) -> BaseGeometry:
        return self._union_wgs84(
            gpd.read_file(path), crs=crs, source=f"vector file {Path(path).name!r}"
        )

    @staticmethod
    def _union_wgs84(
        gdf: gpd.GeoDataFrame, *, crs: CRS | None, source: str
    ) -> BaseGeometry:
        gdf = _reconcile_gdf_crs(gdf, crs, source=source)
        if not _is_wgs84(gdf.crs):
            gdf = gdf.to_crs(_WGS84)
        return gdf.geometry.union_all()

    @staticmethod
    def buffer_metric(
        geom: BaseGeometry, meters: float, *, join_style: str = "round"
    ) -> BaseGeometry:
        """Buffer a WGS84 geometry by ground metres (project → buffer → project back).

        The buffer is computed in the local UTM zone (see `_metric_crs_for`), where
        one unit is one metre on the ground, so a 5 km radius is 5 km at Tromsø as at
        Valencia. Web Mercator, the usual shortcut, is conformal but not equidistant:
        its scale is 1/cos(latitude), so a "5000 m" buffer there is ~3.1 km on the
        ground at 52 N and ~2.5 km at 60 N. The geometry is densified first so a
        lon/lat rectangle is buffered along its true (curved, in UTM) edges rather
        than along their chords.

        ``join_style`` controls corners: ``"round"`` (default) for organic shapes,
        ``"mitre"`` to keep a bounding box / convex hull sharp-cornered so it grows
        into a larger polygon of the same kind rather than a rounded one.
        """
        if not meters or geom.is_empty:
            return geom
        metric_crs = _metric_crs_for(geom)
        # Points and degenerate (zero-length) boxes have nothing to densify, and
        # GEOS refuses to segmentize them.
        dense = (
            shapely.segmentize(geom, _BUFFER_DENSIFY_DEG) if geom.length > 0 else geom
        )
        metric = RasterOps.project_geometry(dense, metric_crs)
        buffered = metric.buffer(meters, join_style=join_style)
        return RasterOps.project_geometry(buffered, _WGS84, from_crs=metric_crs)
