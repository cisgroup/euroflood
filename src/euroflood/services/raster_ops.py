"""Generic Raster Operations (Crop, Reproject).

This module provides static utility methods for spatial operations on raster files,
wrapping `rasterio` and `shapely`.
"""

import contextlib
import hashlib
import os
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import rasterio.mask
import rasterio.merge
import structlog
from pyproj import CRS, Transformer
from rasterio.crs import CRS as RioCRS
from rasterio.features import geometry_mask
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import from_bounds
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from .index_repository import _GDAL_ENV

logger = structlog.get_logger(__name__)

# Module-level constants, so they are not constructed in a default argument.
WGS84 = CRS("EPSG:4326")
# Equal-area CRS (EASE-Grid 2.0 global) for honest km² areas.
EQUAL_AREA_CRS = CRS("EPSG:6933")


def nonempty_file(path: Path) -> bool:
    """True if ``path`` exists and is non-empty (the download/crop cache-hit test)."""
    return path.exists() and path.stat().st_size > 0


def is_hazard(frame: Any) -> bool:
    """True for a hazard catalogue (identified by its ``return_period`` column)."""
    return "return_period" in getattr(frame, "columns", [])


def area_km2(geom: BaseGeometry, *, from_crs: CRS = WGS84) -> float:
    """Area of ``geom`` in km², via an equal-area projection (default source WGS84)."""
    projected = RasterOps.project_geometry(geom, EQUAL_AREA_CRS, from_crs=from_crs)
    return float(abs(projected.area)) / 1e6


def roi_key(geometry: BaseGeometry, *, length: int = 8) -> str:
    """A short, stable hash of a geometry, used to keep crop filenames ROI-safe.

    Two queries that share an ``output_dir`` but cover different areas would
    otherwise write to the same ``flood_<date>_id<id>.tif`` and collide; embedding
    this key in the name keeps each ROI's crop distinct (and cache-reusable).

    Args:
        geometry: The ROI geometry (any CRS; only its shape matters here).
        length: Number of leading hex characters to keep (default 8).

    Returns:
        A lowercase hex string of ``length`` characters.
    """
    return hashlib.sha1(geometry.wkb, usedforsecurity=False).hexdigest()[:length]


@lru_cache(maxsize=32)
def _transformer(from_crs: Any, to_crs: Any) -> Transformer:
    """Return a cached pyproj ``Transformer`` for a ``(from_crs, to_crs)`` pair.

    Building a ``Transformer`` is not free and `project_geometry`
    is called per event on download; the set of distinct CRS pairs is tiny, so
    memoizing on the (hashable) CRS objects avoids rebuilding it each time.
    """
    return Transformer.from_crs(from_crs, to_crs, always_xy=True)


def _write_raster(
    path: Path,
    array: Any,
    profile: dict[str, Any],
    *,
    tags: dict[str, str] | None = None,
) -> None:
    """Write ``array`` to ``path`` as a GeoTIFF, atomically, with optional tags.

    Writes to a sibling ``.part`` file and renames it into place with
    ``os.replace``, so an interrupted crop/mosaic can never leave a partial,
    valid-looking raster that a later ``nonempty_file`` cache check would trust and
    reuse. ``tags`` are stamped as dataset-level GeoTIFF metadata (embedded in the
    file, no sidecar), e.g. the set of source tiles a hazard mosaic was built from,
    so the output's provenance is auditable.

    The temp name is per-process-unique (``.<pid>.part``) so two processes cropping
    the same cache key concurrently each rename their own file (the last writer
    wins with a complete raster) instead of colliding on one shared temp.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        with rasterio.open(tmp, "w", **profile) as dest:
            dest.write(array)
            if tags:
                dest.update_tags(**tags)
        os.replace(tmp, path)  # atomic within one filesystem
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


class RasterOps:
    """Utilities for manipulating raster files."""

    @staticmethod
    def project_geometry(
        geom: BaseGeometry, to_crs: CRS, from_crs: CRS = WGS84
    ) -> BaseGeometry:
        """Reproject a shapely geometry to a different Coordinate Reference System.

        Args:
            geom (BaseGeometry): The input shapely geometry.
            to_crs (CRS): The target CRS (e.g., CRS("EPSG:3857")).
            from_crs (CRS, optional): The source CRS. Defaults to CRS("EPSG:4326").

        Returns:
            BaseGeometry: The reprojected geometry.
        """
        return transform(_transformer(from_crs, to_crs).transform, geom)

    @staticmethod
    def crs_of(source_path: str | Path) -> Any:
        """Return a raster's CRS without materializing a band (a cheap header read).

        Lets callers branch on the CRS (e.g. reproject-if-geographic) before deciding
        how to read, so the band is read exactly once instead of once to inspect the
        CRS and again to actually use it.
        """
        with rasterio.Env(**_GDAL_ENV), rasterio.open(source_path) as src:
            return src.crs

    @staticmethod
    def read_array(
        source_path: str | Path,
        *,
        band: int = 1,
        to_crs: Any = None,
        resampling: Resampling = Resampling.bilinear,
    ) -> tuple[Any, Any, Any, float | None]:
        """Read one band of a raster into memory with its georeferencing.

        Returns the ``(array, transform, crs, nodata)`` triple that plotting needs,
        the same data `crop_raster` / `mosaic_and_crop` compute but
        write to disk. The raster is assumed to already be on disk (e.g. a
        ``.download()`` output).

        Args:
            source_path: Path (or ``/vsicurl/`` URL) of the raster.
            band: 1-based band index to read (default 1).
            to_crs: If given (e.g. ``"EPSG:4326"``) and the source is in a different
                CRS, reproject the band to it in-memory. Downloaded depth GeoTIFFs
                keep their source CRS (a metric projection), so plotting/overlaying
                them needs this to land in lat/lon.
            resampling: Resampling method used when ``to_crs`` triggers a reproject.
                Defaults to ``bilinear`` (smooth, for display); pass
                ``Resampling.nearest`` to preserve exact pixel values (for stats).

        Returns:
            The 2-D array, the affine ``transform``, the ``crs``, and the
            ``nodata`` sentinel (``None`` if unset).
        """
        with rasterio.Env(**_GDAL_ENV), rasterio.open(source_path) as src:
            if to_crs is None:
                return src.read(band), src.transform, src.crs, src.nodata
            dst = RioCRS.from_user_input(to_crs)
            if src.crs is not None and src.crs == dst:
                return src.read(band), src.transform, src.crs, src.nodata
            transform, width, height = calculate_default_transform(
                src.crs, dst, src.width, src.height, *src.bounds
            )
            nodata = src.nodata if src.nodata is not None else 0.0
            destination = np.full((height, width), nodata, dtype=src.dtypes[band - 1])
            reproject(
                source=src.read(band),
                destination=destination,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=transform,
                dst_crs=dst,
                src_nodata=src.nodata,
                dst_nodata=nodata,
                resampling=resampling,
            )
            return destination, transform, dst, nodata

    @staticmethod
    def crop_raster(
        source_path: Path,
        output_path: Path,
        geometry: BaseGeometry,
        crop_to_poly: bool = True,
        *,
        tags: dict[str, str] | None = None,
    ) -> bool:
        """Crop a raster to a given geometry.

        This method reads a source raster, masks it using the provided geometry,
        and writes the result to a new file. It handles CRS mismatches by
        reprojecting the geometry to match the raster's CRS. The write is atomic
        (temp file + rename), so an interrupted crop cannot leave a partial output.

        Args:
            source_path (Path): Path to the input TIF file.
            output_path (Path): Path where the cropped TIF will be saved.
            geometry (BaseGeometry): The shapely geometry defining the crop area (usually in WGS84).
            crop_to_poly (bool, optional):
                If True, sets pixels outside the polygon to NoData.
                If False, crops to the bounding box of the geometry.
                Defaults to True.
            tags: Optional dataset-level GeoTIFF metadata to embed (provenance).

        Returns:
            bool: True if successful and data was written, False otherwise (e.g., no overlap or empty result).
        """
        try:
            with rasterio.open(source_path) as src:
                if src.crs:
                    geom_local = RasterOps.project_geometry(geometry, src.crs)
                else:
                    geom_local = geometry

                if crop_to_poly:
                    out_image, out_transform = rasterio.mask.mask(
                        src, [geom_local], crop=True, nodata=src.nodata
                    )
                else:
                    minx, miny, maxx, maxy = geom_local.bounds
                    window = from_bounds(minx, miny, maxx, maxy, src.transform)
                    out_image = src.read(window=window)
                    out_transform = src.window_transform(window)

                if out_image.size == 0 or (
                    src.nodata is not None and (out_image == src.nodata).all()
                ):
                    return False

                profile = src.profile.copy()
                profile.update(
                    {
                        "height": out_image.shape[1],
                        "width": out_image.shape[2],
                        "transform": out_transform,
                        "compress": "lzw",
                        "tiled": False,  # Small crops shouldn't be tiled
                    }
                )
                profile.pop("blockxsize", None)
                profile.pop("blockysize", None)

                _write_raster(output_path, out_image, profile, tags=tags)
                return True
        except ValueError:
            # Usually means geometry doesn't overlap raster
            return False

    @staticmethod
    def mosaic_and_crop(
        sources: Sequence[str | Path],
        output_path: Path,
        geometry: BaseGeometry,
        *,
        nodata: float | None = None,
        crop_to_poly: bool = True,
        tags: dict[str, str] | None = None,
    ) -> bool:
        """Mosaic one or more rasters over an ROI window, then crop to a geometry.

        Opens each source (a local path **or** a ``/vsicurl/`` URL), merges them
        with ``rasterio.merge.merge(bounds=geometry.bounds)`` so only the ROI
        window of each source is read (bounded memory regardless of full tile
        size), optionally masks pixels outside the polygon to ``nodata``, and
        writes an LZW GeoTIFF. Handles 1..N sources uniformly (a single source is
        a no-op mosaic).

        Args:
            sources: Local paths or ``/vsicurl/`` URLs, all in the same CRS.
            output_path: Where to write the cropped mosaic.
            geometry: ROI geometry (usually WGS84); reprojected to the source CRS.
            nodata: NoData value for merge/masking and the emptiness check.
            crop_to_poly: If True, set pixels outside the polygon to ``nodata``
                (requires ``nodata``); otherwise keep the bbox-cropped mosaic.
            tags: Optional dataset-level GeoTIFF metadata to embed (e.g. the source
                tiles used), so a mosaic's provenance is auditable on disk.

        Returns:
            bool: True if non-empty data was written, False otherwise (no overlap
            or an entirely-NoData result).
        """
        srcs = []
        try:
            # /vsicurl hazard tiles are opened in stream mode; the GDAL env keeps the
            # remote read cheap (no directory listing, extension-restricted, cached).
            with rasterio.Env(**_GDAL_ENV):
                srcs = [rasterio.open(s) for s in sources]
                if not srcs:
                    return False

                crs = srcs[0].crs
                geom_local = (
                    RasterOps.project_geometry(geometry, crs) if crs else geometry
                )

                mosaic, out_transform = rasterio.merge.merge(
                    srcs, bounds=geom_local.bounds, nodata=nodata
                )

                if crop_to_poly and nodata is not None:
                    outside = geometry_mask(
                        [geom_local],
                        out_shape=(mosaic.shape[1], mosaic.shape[2]),
                        transform=out_transform,
                        invert=False,  # True where a pixel is OUTSIDE the polygon
                    )
                    mosaic[:, outside] = nodata

                if mosaic.size == 0 or (
                    nodata is not None and bool((mosaic == nodata).all())
                ):
                    return False

                profile = srcs[0].profile.copy()
                profile.update(
                    {
                        "height": mosaic.shape[1],
                        "width": mosaic.shape[2],
                        "transform": out_transform,
                        "compress": "lzw",
                        "tiled": False,
                        "nodata": nodata,
                    }
                )
                profile.pop("blockxsize", None)
                profile.pop("blockysize", None)

                _write_raster(output_path, mosaic, profile, tags=tags)
                return True
        except ValueError:
            return False
        finally:
            for s in srcs:
                s.close()
