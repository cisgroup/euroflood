"""Resolve flexible location inputs to a single WGS84 geometry.

Turns the various ways a user can name a region — place name, point + radius,
bounding box, a user-supplied shapefile, a shapely geometry, or a GeoDataFrame —
into one shapely geometry in EPSG:4326, reusing `GeocodingService` for
names and the metric-buffer trick from the extraction pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import Point, box
from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..exceptions import GeocodingError
from .geocoding import GeocodingService
from .raster_ops import RasterOps

_WGS84 = CRS("EPSG:4326")
_WEB_MERCATOR = CRS("EPSG:3857")

# ROI shape modes: regularize the resolved geometry before buffering. "exact" keeps
# the raw (often river-following, concave, or multipart) admin boundary; "bbox" and
# "hull" replace it with a clean rectangle / convex hull that sidesteps those quirks
# and collapse MultiPolygon exclaves into one shape.
_SHAPE_TRANSFORMS: dict[str, Callable[[BaseGeometry], BaseGeometry]] = {
    "exact": lambda g: g,
    "bbox": lambda g: g.envelope,
    "hull": lambda g: g.convex_hull,
}


class LocationResolver:
    """Resolve a region specification to a single WGS84 shapely geometry."""

    def __init__(
        self,
        settings: Settings | None = None,
        geocoder: GeocodingService | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            settings: Optional configuration. Defaults to `get_settings`.
            geocoder: Optional injected GeocodingService (for place names).
        """
        self.settings = settings or get_settings()
        self.geocoder = geocoder or GeocodingService(settings=self.settings)

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
        buffer_m: float = 0.0,
        level: int | None = None,
        shape: str = "exact",
    ) -> BaseGeometry:
        """Resolve exactly one location input to a WGS84 geometry.

        Args:
            region: A place name, shapely geometry, GeoDataFrame, or bbox tuple.
            point: A (lat, lon) point; combine with `radius_m`.
            radius_m: Radius in metres around `point`.
            bbox: A (minx, miny, maxx, maxy) bounding box in WGS84.
            shapefile: Path to a vector file (read via geopandas, unioned).
            buffer_m: Optional extra metric buffer applied to the result.
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
        """
        geom = self._base(region, point, radius_m, bbox, shapefile, level)
        geom = self._apply_shape(geom, shape)
        if buffer_m:
            # Keep a bbox/hull sharp-cornered when buffered — a box grows into a
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
    ) -> BaseGeometry:
        if sum(x is not None for x in (region, point, bbox, shapefile)) != 1:
            raise GeocodingError(
                "Provide exactly one of: region, point, bbox, shapefile."
            )
        if region is not None:
            return self._from_region(region, level)
        if bbox is not None:
            return box(*bbox)
        if shapefile is not None:
            return self._from_vector_file(shapefile)
        if point is None:  # defensive; the exactly-one check above guarantees it
            raise GeocodingError("Provide exactly one location input.")
        lat, lon = point
        geom: BaseGeometry = Point(lon, lat)
        return self.buffer_metric(geom, radius_m) if radius_m else geom

    def _from_region(self, region: Any, level: int | None) -> BaseGeometry:
        if isinstance(region, str):
            return self.geocoder.get_geometry(region, level=level)
        if isinstance(region, BaseGeometry):
            return region
        if isinstance(region, gpd.GeoDataFrame):
            return self._union_wgs84(region)
        if isinstance(region, tuple | list) and len(region) == 4:
            return box(*region)
        raise GeocodingError(f"Unsupported region type: {type(region)!r}")

    def _from_vector_file(self, path: str | Path) -> BaseGeometry:
        return self._union_wgs84(gpd.read_file(path))

    @staticmethod
    def _union_wgs84(gdf: gpd.GeoDataFrame) -> BaseGeometry:
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        return gdf.geometry.union_all()

    @staticmethod
    def buffer_metric(
        geom: BaseGeometry, meters: float, *, join_style: str = "round"
    ) -> BaseGeometry:
        """Buffer a WGS84 geometry by metres (project → buffer → project back).

        ``join_style`` controls corners: ``"round"`` (default) for organic shapes,
        ``"mitre"`` to keep a bounding box / convex hull sharp-cornered so it grows
        into a larger polygon of the same kind rather than a rounded one.
        """
        if not meters:
            return geom
        metric = RasterOps.project_geometry(geom, _WEB_MERCATOR)
        buffered = metric.buffer(meters, join_style=join_style)
        return RasterOps.project_geometry(buffered, _WGS84, from_crs=_WEB_MERCATOR)
