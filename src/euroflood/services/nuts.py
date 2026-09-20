"""Eurostat NUTS regions: identifiers -> boundaries, plus a searchable region table.

NUTS (Nomenclature of Territorial Units for Statistics) divides Europe into a
hierarchy of statistical regions: level 0 is a country (``NL``), level 1 its major
socio-economic regions (``NL2``), level 2 its basic regions (``NL22``, Gelderland)
and level 3 its small regions (``NL225``). An identifier's length gives its level.

Boundaries come from the Eurostat GISCO distribution, one GeoJSON per level at the
configured generalisation scale (default 1:1M, the most detailed) and NUTS version
(default 2024), downloaded on first use into ``cache_dir/boundaries/`` and cached.
The much smaller attribute table (every identifier with its names, no geometry)
backs identifier search and the suggestions in error messages. A single local file
holding every level (``settings.nuts_dataset_path``) can stand in for both.

The offline place-name geocoder (`GeocodingService`) still reads its own 1:20M
file, whose boundaries sit kilometres off the real ones; moving it onto this
repository is a follow-up.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely
import structlog
from pyproj import CRS
from shapely.geometry.base import BaseGeometry

from ..config import Settings, get_settings
from ..exceptions import NutsError
from .downloader import DownloadService
from .geocoding import _EXONYMS, _name_variants, _normalize

logger = structlog.get_logger(__name__)

# A two-letter country code plus up to three letters/digits: NL, NL2, NL22, NL225.
NUTS_ID_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{0,3}$")
NUTS_SCALES = ("01M", "03M", "10M", "20M", "60M")
NUTS_LEVELS = (0, 1, 2, 3)
# Columns of a `regions()` result, in GISCO's own naming so they match Eurostat docs.
REGION_COLUMNS = ["NUTS_ID", "LEVL_CODE", "CNTR_CODE", "NAME_LATN", "NUTS_NAME"]

_WGS84 = CRS("EPSG:4326")


def normalize_nuts_id(value: Any) -> str:
    """Upper-case and validate a NUTS identifier (``"nl22"`` -> ``"NL22"``).

    Raises:
        NutsError: If the value is not shaped like a NUTS identifier.
    """
    text = str(value).strip().upper()
    if not NUTS_ID_PATTERN.match(text):
        raise NutsError(
            f"{value!r} is not a valid NUTS identifier: expected a two-letter country "
            "code followed by up to three letters or digits, e.g. 'NL', 'NL2', 'NL22' "
            "or 'NL225'. Find identifiers with euroflood.nuts('<region name>')."
        )
    return text


def nuts_level(nuts_id: str) -> int:
    """The NUTS level an identifier's length encodes (``NL`` = 0 ... ``NL225`` = 3)."""
    return len(nuts_id) - 2


def _is_extra_regio(nuts_id: str) -> bool:
    """True for GISCO's ``Z`` codes (``NLZ``, ``NLZZ``, ...): activity that has no region."""
    tail = nuts_id[2:]
    return bool(tail) and set(tail) == {"Z"}


def _complete_columns(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Normalise a GISCO table to `REGION_COLUMNS`, deriving what a file may lack."""
    if "NUTS_ID" not in df.columns:
        raise NutsError(
            f"{source} has no NUTS_ID column; is it a Eurostat GISCO NUTS file?"
        )
    df = df.copy()
    df["NUTS_ID"] = df["NUTS_ID"].astype(str).str.strip().str.upper()
    if "LEVL_CODE" not in df.columns:
        df["LEVL_CODE"] = df["NUTS_ID"].str.len() - 2
    df["LEVL_CODE"] = df["LEVL_CODE"].astype(int)
    if "CNTR_CODE" not in df.columns:
        df["CNTR_CODE"] = df["NUTS_ID"].str[:2]
    if "NAME_LATN" not in df.columns and "NUTS_NAME" in df.columns:
        df["NAME_LATN"] = df["NUTS_NAME"]
    if "NAME_LATN" not in df.columns:
        df["NAME_LATN"] = df["NUTS_ID"]
    if "NUTS_NAME" not in df.columns:
        df["NUTS_NAME"] = df["NAME_LATN"]
    return df


@lru_cache(maxsize=16)
def _load_regions(path: str) -> gpd.GeoDataFrame:
    """Load a NUTS boundary file (any levels) in EPSG:4326, cached once per process.

    The per-level 1:1M files are 10-28 MB of GeoJSON; a fresh `NutsRepository` is
    built per query, so caching by path amortises the read to once per process.
    """
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(_WGS84)
    elif not gdf.crs.equals(_WGS84, ignore_axis_order=True):
        gdf = gdf.to_crs(_WGS84)
    completed = _complete_columns(pd.DataFrame(gdf.drop(columns="geometry")), path)
    return gpd.GeoDataFrame(completed, geometry=gdf.geometry.values, crs=_WGS84)


@lru_cache(maxsize=4)
def _load_attributes(path: str) -> tuple[pd.DataFrame, dict[str, list[int]]]:
    """Load the NUTS attribute table + a normalised-name index, cached per process.

    Reads GISCO's ``NUTS_AT_<year>.csv`` or, for a single all-levels boundary file,
    derives the table from its properties. The index maps every normalised name
    variant (see `_name_variants`) to row positions, as the geocoder does.
    """
    file = Path(path)
    if file.suffix.lower() == ".csv":
        raw = pd.read_csv(file, dtype=str, keep_default_na=False)
    else:
        raw = pd.DataFrame(gpd.read_file(file).drop(columns="geometry"))
    table = _complete_columns(raw, path)[REGION_COLUMNS]
    # Eurostat's classification has extra-regio 'Z' codes (NLZ, NLZZ, ...):
    # statistical placeholders with no territory. Drop any a table carries; there is
    # nothing to search for or query.
    keep = ~table["NUTS_ID"].map(_is_extra_regio)
    table = table[keep].sort_values("NUTS_ID").reset_index(drop=True)
    index: dict[str, list[int]] = {}
    for pos, (latin, native) in enumerate(
        zip(table["NAME_LATN"], table["NUTS_NAME"], strict=True)
    ):
        for key in _name_variants(latin) | _name_variants(native):
            index.setdefault(key, []).append(pos)
    return table, index


class NutsRepository:
    """Look up Eurostat NUTS regions by identifier, and search them by name.

    Boundaries are read from the GISCO per-level files for ``settings.nuts_scale`` /
    ``settings.nuts_year`` (downloaded once, cached in ``cache_dir/boundaries``), or
    from the single file ``settings.nuts_dataset_path`` when set.

    Attributes:
        settings: The active configuration.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        downloader: DownloadService | None = None,
    ) -> None:
        """Initialize the repository.

        Args:
            settings: Optional configuration. Defaults to `get_settings`.
            downloader: Optional injected DownloadService for the GISCO files.
        """
        self.settings = settings or get_settings()
        self._downloader = downloader or DownloadService(
            download_dir=self.settings.cache_dir / "boundaries",
            settings=self.settings,
        )

    # -- files ------------------------------------------------------------------

    def _fetch(self, relative: str, filename: str, *, what: str) -> Path:
        """Return the cached copy of a GISCO file, downloading it on first use."""
        dest = self.settings.cache_dir / "boundaries" / filename
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        if self.settings.offline:
            raise NutsError(
                f"Offline (EUROFLOOD_OFFLINE) but the NUTS {what} ({filename}) is not "
                "cached. Resolve the region once while online to cache it, set "
                "EUROFLOOD_NUTS_DATASET_PATH to a local NUTS boundary file, or select "
                "the region by bbox/point instead."
            )
        url = self.settings.nuts_base_url + relative
        logger.info("nuts_download", url=url)
        path = self._downloader.download_file(url, filename)
        if path is None:
            raise NutsError(
                f"Failed to download the NUTS {what} from {url}. Check "
                "EUROFLOOD_NUTS_YEAR / EUROFLOOD_NUTS_SCALE, or set "
                "EUROFLOOD_NUTS_DATASET_PATH to a local NUTS boundary file."
            )
        return path

    def _level_filename(self, level: int) -> str:
        return (
            f"NUTS_RG_{self.settings.nuts_scale}_{self.settings.nuts_year}_4326"
            f"_LEVL_{level}.geojson"
        )

    def _level_path(self, level: int) -> Path:
        """The boundary file holding ``level`` (one all-levels file, or GISCO's per level)."""
        if self.settings.nuts_dataset_path is not None:
            return self.settings.nuts_dataset_path
        filename = self._level_filename(level)
        return self._fetch(
            f"geojson/{filename}", filename, what=f"level-{level} boundaries"
        )

    def _attributes_path(self) -> Path:
        if self.settings.nuts_dataset_path is not None:
            return self.settings.nuts_dataset_path
        filename = f"NUTS_AT_{self.settings.nuts_year}.csv"
        return self._fetch(f"csv/{filename}", filename, what="attribute table")

    def _regions_at(self, level: int) -> gpd.GeoDataFrame:
        """All regions of one level with their boundaries."""
        gdf = _load_regions(str(self._level_path(level)))
        return gdf[gdf["LEVL_CODE"] == level]

    def _attributes(self) -> tuple[pd.DataFrame, dict[str, list[int]]]:
        return _load_attributes(str(self._attributes_path()))

    # -- identifiers -> geometry ------------------------------------------------

    def geometry(self, ids: str | Sequence[str]) -> BaseGeometry:
        """The boundary of one NUTS region, or the union of several, in EPSG:4326.

        Args:
            ids: A NUTS identifier (``"NL22"``, case-insensitive) or several
                (``["NL22", "NL21"]``), possibly of different levels.

        Raises:
            NutsError: If an identifier is malformed or unknown, names an
                extra-regio territory (no boundary), or its boundary file cannot be
                obtained (offline and not cached, or the download failed).
        """
        raw = [ids] if isinstance(ids, str) else list(ids)
        if not raw:
            raise NutsError("Provide at least one NUTS identifier.")
        wanted = list(dict.fromkeys(self._normalize_or_explain(v) for v in raw))
        parts: list[BaseGeometry] = []
        for nuts_id in wanted:
            if _is_extra_regio(nuts_id):
                raise NutsError(
                    f"{nuts_id} is an extra-regio territory (activity that cannot be "
                    "attributed to a region) and has no boundary."
                )
            regions = self._regions_at(nuts_level(nuts_id))
            hit = regions[regions["NUTS_ID"] == nuts_id]
            if len(hit) == 0:
                raise NutsError(self._unknown_message(nuts_id))
            parts.append(hit.geometry.iloc[0])
        if len(parts) == 1:
            return parts[0]
        union: BaseGeometry = shapely.union_all(parts)
        return union

    def _normalize_or_explain(self, value: Any) -> str:
        """`normalize_nuts_id`, but tell a user who passed a region *name* its identifier."""
        try:
            return normalize_nuts_id(value)
        except NutsError as err:
            hint = self._name_hint(str(value))
            if hint:
                raise NutsError(hint) from None
            raise err from None

    def _name_hint(self, text: str) -> str | None:
        """``"'Gelderland' is a region name ... use nuts='NL22'"`` when ``text`` is a name."""
        try:
            table, index = self._attributes()
        except NutsError:  # no attribute table available (offline): no hint
            return None
        target = _normalize(text.split(",")[0])
        positions = index.get(target) or index.get(_EXONYMS.get(target, ""))
        if not positions:
            return None
        rows = table.iloc[positions]
        options = ", ".join(
            f"nuts={nid!r} ({name}, level {lvl})"
            for nid, name, lvl in zip(
                rows["NUTS_ID"], rows["NAME_LATN"], rows["LEVL_CODE"], strict=True
            )
        )
        return f"{text!r} is a region name, not a NUTS identifier; use {options}."

    def _unknown_message(self, nuts_id: str) -> str:
        """An actionable message for an identifier absent from the boundary file."""
        year = self.settings.nuts_year
        hint = self._name_hint(nuts_id)
        if hint:
            return hint
        try:
            table, _ = self._attributes()
        except NutsError:
            return (
                f"Unknown NUTS identifier {nuts_id!r} in the NUTS {year} classification "
                "(EUROFLOOD_NUTS_YEAR selects another version)."
            )
        ids = table["NUTS_ID"].tolist()
        if (
            nuts_id in ids
        ):  # classified, but GISCO ships no polygon (UA sub-regions in 2024)
            name = table.loc[table["NUTS_ID"] == nuts_id, "NAME_LATN"].iloc[0]
            return (
                f"{nuts_id} ({name}) is in the NUTS {year} classification, but GISCO "
                f"publishes no boundary for it at level {nuts_level(nuts_id)} (the "
                f"{self.settings.nuts_scale} files cover it only through its country "
                "outline). Select its country, or a bbox, instead."
            )
        close = set(difflib.get_close_matches(nuts_id, ids, n=5, cutoff=0.6))
        if len(nuts_id) > 2:
            # Its would-be siblings (same parent, same level); when the parent does
            # not exist either, every identifier of that country at that level.
            same_level = [
                i for i in ids if len(i) == len(nuts_id) and i[:2] == nuts_id[:2]
            ]
            siblings = [i for i in same_level if i.startswith(nuts_id[:-1])]
            close |= set(siblings or same_level)
        suggestion = (
            f" Close identifiers: {', '.join(sorted(close)[:8])}." if close else ""
        )
        return (
            f"Unknown NUTS identifier {nuts_id!r} in the NUTS {year} classification."
            f"{suggestion} Identifiers change between NUTS versions "
            "(EUROFLOOD_NUTS_YEAR); search by name with euroflood.nuts('<region name>')."
        )

    # -- search / listing -------------------------------------------------------

    def regions(
        self,
        query: str | None = None,
        *,
        level: int | None = None,
        country: str | None = None,
        geometry: bool = False,
    ) -> pd.DataFrame | gpd.GeoDataFrame:
        """Search or list NUTS regions.

        Args:
            query: A region name (accent/case-insensitive; bilingual and suffixed
                names and common exonyms such as ``"Cologne"`` match) or a NUTS
                identifier, which lists that region and its descendants
                (``"NL2"`` -> ``NL2``, ``NL22``, ``NL225``, ...).
            level: Keep only this NUTS level (0 country ... 3 small region).
            country: Keep only this two-letter country code (``"NL"``).
            geometry: Attach the boundary polygons (loads the per-level boundary
                files the result needs). Without it only the small attribute table
                is read, so a search never downloads a boundary file.

        Returns:
            A ``DataFrame`` with `REGION_COLUMNS` sorted by ``NUTS_ID``, or with
            ``geometry=True`` a ``GeoDataFrame`` (EPSG:4326) adding ``geometry``.
            No match yields an empty frame, not an error. Extra-regio ``Z`` codes
            (no territory) are never listed.
        """
        table, index = self._attributes()
        rows = table
        if query is not None:
            text = query.strip()
            upper = text.upper()
            if NUTS_ID_PATTERN.match(upper) and (table["NUTS_ID"] == upper).any():
                rows = rows[rows["NUTS_ID"].str.startswith(upper)]
            else:
                rows = rows.iloc[self._match_names(text, table, index)]
        if level is not None:
            rows = rows[rows["LEVL_CODE"] == level]
        if country is not None:
            rows = rows[rows["CNTR_CODE"] == country.strip().upper()]
        rows = rows.sort_values("NUTS_ID").reset_index(drop=True)
        return self._with_geometry(rows) if geometry else rows

    @staticmethod
    def _match_names(
        text: str, table: pd.DataFrame, index: dict[str, list[int]]
    ) -> list[int]:
        """Row positions whose name matches ``text``.

        Exact name variants, the exonym's endonym, and every name *containing* the
        query all count (``"Nederland"`` lists the country and ``Oost-Nederland``);
        only when nothing matches are close typos accepted.
        """
        target = _normalize(text.split(",")[0])
        hits: set[int] = set()
        for key in (target, _EXONYMS.get(target)):
            if key and key in index:
                hits.update(index[key])
        if len(target) >= 3:
            names = table["NAME_LATN"].map(_normalize)
            natives = table["NUTS_NAME"].map(_normalize)
            mask = names.str.contains(target, regex=False) | natives.str.contains(
                target, regex=False
            )
            hits.update(int(i) for i in mask.to_numpy().nonzero()[0])
        if not hits:
            close = difflib.get_close_matches(target, list(index), n=10, cutoff=0.8)
            hits = {pos for key in close for pos in index[key]}
        return sorted(hits)

    def _with_geometry(self, rows: pd.DataFrame) -> gpd.GeoDataFrame:
        """Join boundary polygons onto attribute rows, reading only the levels present."""
        frames: list[Any] = []
        for lvl in sorted(int(v) for v in rows["LEVL_CODE"].unique()):
            regions = self._regions_at(lvl)
            wanted = rows.loc[rows["LEVL_CODE"] == lvl, "NUTS_ID"]
            frames.append(
                regions.loc[regions["NUTS_ID"].isin(wanted), ["NUTS_ID", "geometry"]]
            )
        if frames:
            geoms = pd.concat(frames, ignore_index=True)
            merged = rows.merge(geoms, on="NUTS_ID", how="left")
        else:
            merged = rows.assign(geometry=gpd.GeoSeries([], dtype="geometry"))
        return gpd.GeoDataFrame(merged, geometry="geometry", crs=_WGS84)


__all__ = [
    "NUTS_ID_PATTERN",
    "NUTS_LEVELS",
    "NUTS_SCALES",
    "REGION_COLUMNS",
    "NutsRepository",
    "normalize_nuts_id",
    "nuts_level",
]
