"""Tile lookup over the CEMS-GLOFAS tile-extents GeoJSON.

GLOFAS ships no global index raster — only ``tile_extents.geojson`` (271 ~10x10
degree tiles) plus a deterministic per-tile filename pattern. This service caches
that GeoJSON (download-on-first-use, like the NUTS boundary dataset) and answers
two questions: which tiles intersect an ROI, and what is the remote URL / cache
filename for a given tile at a given return period.

It is the GLOFAS analogue of the historic flood index raster + dictionary,
collapsed into one small vector file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd
import structlog
from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..exceptions import HazardError
from .downloader import DownloadService

logger = structlog.get_logger(__name__)

# Confirmed live against the JRC FTP: the RP{n} folders that exist (note 75/200).
SUPPORTED_RETURN_PERIODS: tuple[int, ...] = (10, 20, 50, 75, 100, 200, 500)


@dataclass(frozen=True)
class HazardTile:
    """One GLOFAS tile resolved for a return period (URL + cache filename)."""

    tile_id: int  # GeoJSON ``id`` (1..271)
    name: str  # GeoJSON ``name``, e.g. "N70_W180"
    return_period: int
    download_url: str  # full https URL to the depth GeoTIFF
    filename: str  # cache basename (== URL basename)
    geometry: BaseGeometry  # tile bbox polygon (EPSG:4326)


class HazardTileIndex:
    """Cache and query the GLOFAS tile-extents GeoJSON."""

    def __init__(
        self,
        settings: Settings | None = None,
        downloader: DownloadService | None = None,
        *,
        allow_download: bool = True,
    ) -> None:
        """Build the index.

        Args:
            settings: Optional configuration. Defaults to `get_settings`.
            downloader: Optional injected DownloadService. Defaults to one writing
                into ``settings.get_hazard_dir()`` so the index lands at
                ``settings.get_hazard_index_path()``.
            allow_download: If False (an offline consumer), a missing tile index is a
                clear error instead of a network fetch. The mirror always passes True
                (it is the populate action); consumers pass ``not offline_hazard``.
        """
        self.settings = settings or get_settings()
        self.downloader = downloader or DownloadService(
            download_dir=self.settings.get_hazard_dir(), settings=self.settings
        )
        self.allow_download = allow_download
        self._tiles: gpd.GeoDataFrame | None = None  # lazy

    # --- index loading -----------------------------------------------------
    def _ensure_index(self) -> Path:
        """Return the cached tile-extents path, downloading it on first use."""
        target = self.settings.get_hazard_index_path()
        if target.exists() and target.stat().st_size > 0:
            return target
        if self.settings.hazard_index_path is not None:
            raise HazardError(
                f"hazard_index_path {target} does not exist; point it at a valid "
                "tile_extents.geojson or unset it to download the default."
            )
        if not self.allow_download:
            switch = (
                "EUROFLOOD_OFFLINE" if self.settings.offline else "hazard_mode='local'"
            )
            remedy = (
                "unset offline (EUROFLOOD_OFFLINE=0)"
                if self.settings.offline
                else "set hazard_mode='auto'"
            )
            raise HazardError(
                f"hazard is offline ({switch}) but the GLOFAS tile index is not cached "
                f"at {target}. Populate it on a networked node with "
                f"`euroflood mirror hazard ...`, or {remedy} to allow the one-time JRC "
                "download."
            )
        url = self.settings.hazard_base_url + self.settings.hazard_index_filename
        fetched = self.downloader.download_file(
            url, self.settings.hazard_index_filename
        )
        if fetched is None:
            raise HazardError(f"Could not fetch the GLOFAS tile index from {url}")
        return fetched

    def _load(self) -> gpd.GeoDataFrame:
        """Download (if needed) and load the tile-extents GeoJSON in EPSG:4326."""
        if self._tiles is not None:
            return self._tiles
        gdf = gpd.read_file(self._ensure_index())
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        self._tiles = gdf
        return gdf

    # --- queries -----------------------------------------------------------
    def tiles_for(self, roi: BaseGeometry, return_period: int) -> list[HazardTile]:
        """Return the tiles whose extent intersects ``roi`` for ``return_period``."""
        self._validate_rp(return_period)
        gdf = self._load()
        hits = gdf[gdf.intersects(roi)]
        return [self._to_tile(row, return_period) for _, row in hits.iterrows()]

    def all_tiles(self, return_period: int) -> list[HazardTile]:
        """Return every tile for ``return_period`` (used by the bulk mirror)."""
        self._validate_rp(return_period)
        gdf = self._load()
        return [self._to_tile(row, return_period) for _, row in gdf.iterrows()]

    def _to_tile(self, row: Any, return_period: int) -> HazardTile:
        tile_id = int(row["id"])
        name = str(row["name"])
        filename = f"ID{tile_id}_{name}_RP{return_period}_depth.tif"
        url = f"{self.settings.hazard_base_url}RP{return_period}/{filename}"
        return HazardTile(tile_id, name, return_period, url, filename, row.geometry)

    @staticmethod
    def _validate_rp(rp: int) -> None:
        if rp not in SUPPORTED_RETURN_PERIODS:
            raise HazardError(
                f"Unsupported return period {rp!r}; "
                f"choose from {list(SUPPORTED_RETURN_PERIODS)}."
            )
