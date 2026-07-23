"""Service for resolving place names to administrative-area geometries.

Resolves place names (countries, regions, cities) to shapely geometries in WGS84
(EPSG:4326). Three backends:

- ``online_first`` (default): an OpenStreetMap Nominatim REST query first, falling
  back to the local dataset on any failure (no network or no result). Forgiving of
  city and exonym names (e.g. "Valencia", "Cologne") that the NUTS dataset only
  stores under bilingual/native names ("Valencia/València", "Köln").
- ``local``: a cached administrative-boundary dataset (Eurostat GISCO NUTS) read
  via geopandas. Fully offline and reproducible once the dataset is cached.
  Downloaded on first use into ``cache_dir/boundaries/``. Falls back to Nominatim
  on a miss only when ``settings.allow_remote_geocoding`` is set.
- ``nominatim``: a Nominatim query only, with no local fallback.

This replaces the previous heavy/slow OSMnx dependency: EuroFlood only needs
administrative-area polygons, not street networks.
"""

from __future__ import annotations

import difflib
import hashlib
import unicodedata
from functools import lru_cache
from pathlib import Path

import geopandas as gpd
import requests
import structlog
from shapely import from_wkb, to_wkb
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..exceptions import GeocodingError
from .downloader import DownloadService

logger = structlog.get_logger(__name__)

# Candidate name columns across boundary datasets (GISCO NUTS, GADM, ...).
_NAME_COLUMNS = ("NAME_LATN", "NUTS_NAME", "NAME", "name")

# Common exonyms -> the endonym the NUTS dataset actually stores (both normalized).
# Only the query side needs these: bilingual names ("Valencia/València") and
# suffixed ones ("München, Kreisfreie Stadt") are already resolved from the data by
# the variant index. Each endonym here is a real NUTS variant (a test guards the
# ones present in the committed fixture); no lightweight exonym library exists
# (pycountry/Babel cover country names only), so this small map is hand-curated.
_EXONYMS = {
    # country endonyms
    "germany": "deutschland",
    "spain": "espana",
    "italy": "italia",
    "greece": "ellada",
    "austria": "osterreich",
    "switzerland": "schweiz",
    "finland": "suomi",
    "poland": "polska",
    "czechia": "cesko",
    "czech republic": "cesko",
    "hungary": "magyarorszag",
    "belgium": "belgique",
    "netherlands": "nederland",
    "sweden": "sverige",
    "norway": "norge",
    "croatia": "hrvatska",
    # major-city exonyms
    "cologne": "koln",
    "munich": "munchen",
    "vienna": "wien",
    "rome": "roma",
    "milan": "milano",
    "florence": "firenze",
    "venice": "venezia",
    "naples": "napoli",
    "turin": "torino",
    "prague": "praha",
}

# Auto-accept a fuzzy typo only above this similarity (kept strict so a near-miss
# like "Kolnx" (~0.89 vs "koln") still fails rather than silently resolving wrong).
_FUZZY_CUTOFF = 0.9


def _normalize(name: str) -> str:
    """Lowercase and strip accents for tolerant name matching."""
    nfkd = unicodedata.normalize("NFKD", name)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).strip().lower()


def _name_variants(name: str) -> set[str]:
    """Normalized lookup keys for a boundary name.

    Beyond the full name, indexes each ``/``-separated segment (bilingual names like
    ``Valencia/València``) and each segment's pre-first-comma head (suffixed names
    like ``München, Kreisfreie Stadt`` -> ``münchen``). Only the head is taken, never
    the generic suffix (``kreisfreie stadt``), so this never over-matches.
    """
    keys: set[str] = set()
    for segment in str(name).split("/"):
        keys.add(_normalize(segment))
        keys.add(_normalize(segment.split(",")[0]))
    keys.discard("")
    return keys


def _name_column(gdf: gpd.GeoDataFrame) -> str:
    """Return the first recognizable place-name column in the dataset."""
    for col in _NAME_COLUMNS:
        if col in gdf.columns:
            return col
    raise GeocodingError(
        f"No recognizable name column ({_NAME_COLUMNS}) in the boundary "
        f"dataset; found columns: {list(gdf.columns)}"
    )


@lru_cache(maxsize=4)
def _load_nuts(path: str) -> tuple[gpd.GeoDataFrame, dict[str, list[int]]]:
    """Load + index a NUTS boundary dataset (EPSG:4326), cached once per path/process.

    The ~14 MB Eurostat GeoJSON is heavy to read and index. Because a fresh
    ``GeocodingService`` is built per query, the ``local`` backend would otherwise
    re-read and re-index it on every call; caching by resolved path amortizes that
    to once per process. The returned frame is read-only, so it is safe to share.
    """
    gdf = gpd.read_file(path)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    name_col = _name_column(gdf)
    gdf = gdf.assign(_norm_name=gdf[name_col].map(_normalize))
    # Map every normalized variant -> the row positions carrying it, so bilingual
    # and suffixed names resolve without an exact full-name match.
    index: dict[str, list[int]] = {}
    for pos, name in enumerate(gdf[name_col]):
        for key in _name_variants(name):
            index.setdefault(key, []).append(pos)
    return gdf, index


class GeocodingService:
    """Resolve administrative-area place names to WGS84 geometries.

    The backend is chosen by ``settings.geocoder_backend``:

    - ``"online_first"`` (default): query OpenStreetMap Nominatim, then fall back
      to the offline Eurostat NUTS dataset on any network failure or empty result.
    - ``"nominatim"``: online only (no offline fallback).
    - ``"local"``: the offline NUTS dataset only (fully offline). Tolerant of
      accents/case, bilingual and suffixed names, common exonyms, and close typos.

    Attributes:
        settings: The active configuration.
        backend: The resolved backend name (see above).
    """

    def __init__(
        self,
        settings: Settings | None = None,
        downloader: DownloadService | None = None,
    ) -> None:
        """Initialize the GeocodingService.

        Args:
            settings: Optional configuration. Defaults to `get_settings`.
            downloader: Optional injected DownloadService used to fetch the local
                boundary dataset on first use.
        """
        self.settings = settings or get_settings()
        self.backend = self.settings.effective_geocoder_backend
        self._downloader = downloader or DownloadService(
            download_dir=self.settings.cache_dir / "boundaries",
            settings=self.settings,
        )
        self._gdf: gpd.GeoDataFrame | None = None
        self._variant_index: dict[str, list[int]] | None = None

    def get_geometry(self, query: str, level: int | None = None) -> BaseGeometry:
        """Resolve a place name to a single shapely geometry in EPSG:4326.

        Args:
            query: Place name, e.g. "Cologne, Germany". Only the part before the
                first comma is matched against the local dataset.
            level: Optional NUTS level filter (0=country ... 3=small region).

        Returns:
            BaseGeometry: The (possibly multi-) polygon in EPSG:4326.

        Raises:
            GeocodingError: If the place cannot be resolved.
        """
        if not self.settings.geocode_cache:
            return self._resolve(query, level)
        cache_path = self._cache_path(query, level)
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached
        geom = self._resolve(query, level)
        self._write_cache(cache_path, geom)
        return geom

    def _resolve(self, query: str, level: int | None) -> BaseGeometry:
        """Resolve a place name to a geometry (uncached), per the active backend."""
        if self.backend == "nominatim":
            return self._resolve_nominatim(query)
        if self.backend == "online_first":
            return self._resolve_online_first(query, level)
        try:
            return self._resolve_local(query, level)
        except GeocodingError:
            if self.settings.allow_remote_geocoding:
                logger.info("geocode_fallback_nominatim", query=query)
                return self._resolve_nominatim(query)
            raise

    # -- disk cache -----------------------------------------------------------

    def _cache_path(self, query: str, level: int | None) -> Path:
        """Path of the on-disk WKB cache entry for a (backend, query, level) key."""
        key = f"{self.backend}:{_normalize(query)}:{level}"
        # sha1 is a cache key here, not a security primitive.
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.settings.cache_dir / "geocode" / f"{digest}.wkb"

    @staticmethod
    def _read_cache(path: Path) -> BaseGeometry | None:
        """Return the cached geometry, or None on a miss / unreadable entry."""
        if not path.exists():
            return None
        try:
            geom: BaseGeometry = from_wkb(path.read_bytes())
            return geom
        except Exception:  # a corrupt/partial cache entry just re-resolves
            return None

    @staticmethod
    def _write_cache(path: Path, geom: BaseGeometry) -> None:
        """Best-effort write of a resolved geometry to the disk cache."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(to_wkb(geom))
        except OSError:  # pragma: no cover - best-effort; a write failure is non-fatal
            pass

    def _resolve_online_first(self, query: str, level: int | None) -> BaseGeometry:
        """Resolve via Nominatim, falling back to the local dataset on any failure.

        Keeps the tool usable offline: a network error or an empty Nominatim
        result drops through to the cached NUTS dataset. Only when both fail is a
        combined ``GeocodingError`` raised.
        """
        try:
            return self._resolve_nominatim(query)
        except GeocodingError as remote_err:
            logger.info("geocode_fallback_local", query=query, error=str(remote_err))
            try:
                return self._resolve_local(query, level)
            except GeocodingError as local_err:
                raise GeocodingError(
                    f"Online geocoding failed ({remote_err}); local fallback also "
                    f"failed ({local_err})."
                ) from local_err

    # -- local backend --------------------------------------------------------

    def _dataset_path(self) -> Path:
        """Return the local boundary dataset path, downloading on first use."""
        if self.settings.boundary_dataset_path is not None:
            return self.settings.boundary_dataset_path
        filename = self.settings.boundary_dataset_filename
        dest = self.settings.cache_dir / "boundaries" / filename
        if dest.exists():
            return dest
        if self.settings.offline:
            raise GeocodingError(
                "Offline (EUROFLOOD_OFFLINE) but the NUTS boundary dataset needed for "
                "place-name lookup is not cached. Resolve a place name once while online "
                "to cache it, set EUROFLOOD_BOUNDARY_DATASET_PATH to a local file, or "
                "select the region by bbox/point instead of a name."
            )
        logger.info("boundary_dataset_download", url=self.settings.boundary_dataset_url)
        path = self._downloader.download_file(
            self.settings.boundary_dataset_url, filename
        )
        if path is None:
            raise GeocodingError(
                "Failed to download the boundary dataset from "
                f"{self.settings.boundary_dataset_url}. Set "
                "EUROFLOOD_BOUNDARY_DATASET_PATH to a local file, or enable "
                "EUROFLOOD_ALLOW_REMOTE_GEOCODING to use Nominatim."
            )
        return path

    def _load_dataset(self) -> gpd.GeoDataFrame:
        """Return the local boundary GeoDataFrame + variant index (cached per process)."""
        if self._gdf is None:
            self._gdf, self._variant_index = _load_nuts(str(self._dataset_path()))
        return self._gdf

    def _match_rows(self, target: str) -> list[int] | None:
        """Resolve a normalized query to row positions: exact, then exonym, then fuzzy.

        Returns ``None`` on a genuine miss (so the caller raises with a hint).
        """
        index = self._variant_index or {}
        for key in (target, _EXONYMS.get(target)):
            if key and key in index:
                return index[key]
        close = difflib.get_close_matches(
            target, list(index), n=1, cutoff=_FUZZY_CUTOFF
        )
        if close:
            logger.info("geocode_fuzzy_match", query=target, matched=close[0])
            return index[close[0]]
        return None

    def _resolve_local(self, query: str, level: int | None) -> BaseGeometry:
        """Match `query` against the local dataset and union the matching rows.

        Matching is tolerant of accents/case, bilingual and suffixed NUTS names
        (``Valencia/València``, ``München, Kreisfreie Stadt``), common exonyms
        (``Cologne``->``Köln``), and very close typos, but a real miss still raises.
        """
        gdf = self._load_dataset()
        target = _normalize(query.split(",")[0])
        rows = self._match_rows(target)
        matches = gdf.iloc[rows] if rows is not None else gdf.iloc[0:0]
        if level is not None and "LEVL_CODE" in gdf.columns:
            matches = matches[matches["LEVL_CODE"] == level]
        if len(matches) == 0:
            close = difflib.get_close_matches(
                target, list(self._variant_index or {}), n=5
            )
            hint = f" Did you mean: {', '.join(sorted(set(close)))}?" if close else ""
            raise GeocodingError(f"No local boundary match for '{query}'.{hint}")
        return matches.geometry.union_all()

    # -- nominatim backend ----------------------------------------------------

    def _resolve_nominatim(self, query: str) -> BaseGeometry:
        """Resolve `query` via a lightweight OSM Nominatim REST request."""
        headers = {
            "User-Agent": f"euroflood ({self.settings.nominatim_email or 'no-contact'})"
        }
        params = {"q": query, "format": "geojson", "polygon_geojson": "1", "limit": "1"}
        try:
            resp = requests.get(
                self.settings.nominatim_url,
                params=params,
                headers=headers,
                timeout=self.settings.geocode_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise GeocodingError(f"Nominatim request failed for '{query}': {e}") from e
        features = data.get("features", [])
        if not features:
            raise GeocodingError(f"Nominatim found no result for '{query}'.")
        return shape(features[0]["geometry"])
