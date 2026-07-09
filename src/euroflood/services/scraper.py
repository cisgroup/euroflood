"""Service for scraping the JRC website to build a file inventory.

This module handles the interaction with the external JRC FTP-like website
to discover available flood map files.
"""

import hashlib
import re
from typing import Any

import requests
import structlog
from bs4 import BeautifulSoup

from ..config import Settings, get_settings
from ..exceptions import ScrapingError

logger = structlog.get_logger(__name__)

_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _parse_size(text: str) -> int | None:
    """Parse an Apache-style size cell ('452M', '120K', '1.2G') to bytes.

    Returns None for non-size cells (filenames, dates, the empty description), so
    the first cell that parses in a row is the file size.
    """
    match = re.fullmatch(r"\s*([\d.]+)\s*([KMGT])?\s*", text)
    if not match:
        return None
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2) or ""])


def _stable_global_id(filename: str) -> int:
    """Deterministic 32-bit id derived from the filename.

    Hashing the filename (instead of using scrape order) keeps ``global_id`` —
    and therefore every Parquet ``flood_id``, dictionary entry and index value —
    stable across re-scrapes, so ``ingest --update`` does not silently renumber
    and invalidate the existing cache. Fits uint32 (the ``flood_id`` dtype).
    """
    digest = hashlib.sha1(filename.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


class ScraperService:
    """Handles navigation and parsing of the remote JRC FTP-like website.

    This service connects to the base URL defined in settings, recursively
    navigates year directories, and parses HTML links to identify valid
    flood map TIF files based on a regex pattern.

    Attributes:
        base_url (str): The root URL to scrape.
        session (requests.Session): Persistent HTTP session for efficiency.
        file_pattern (re.Pattern): Regex pattern to match valid filenames.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        """Initialize the ScraperService.

        Args:
            settings: Optional configuration. Defaults to `get_settings`.
        """
        self.settings = settings or get_settings()
        self.base_url: str = self.settings.base_url
        self.session: requests.Session = requests.Session()
        # Compile Regex once for performance
        # Matches files like: WD_MERGE_2020-02-15---2020-02-24_..._cluster_123.tif
        self.file_pattern = re.compile(
            r"WD_MERGE_(?P<start_date>\d{4}-\d{2}-\d{2})---(?P<end_date>\d{4}-\d{2}-\d{2}).*?_cluster_(?P<cluster_id>\d+).*?\.tif$"
        )

    def _get_year_links(self) -> list[str]:
        """Fetch the list of year directories (e.g. '2020/').

        Parses the base URL to find all links that look like 4-digit years followed by a slash.

        Returns:
            List[str]: A list of relative href strings (e.g., ['2016/', '2017/']).

        Raises:
            ScrapingError: If the base URL cannot be accessed or returns an error.
        """
        try:
            response = self.session.get(
                self.base_url, timeout=self.settings.timeout_seconds
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            return [
                href
                for link in soup.find_all("a")
                if isinstance(href := link.get("href"), str)
                and re.match(r"^\d{4}/$", href)
            ]
        except requests.RequestException as e:
            raise ScrapingError(f"Failed to access base URL: {e}") from e

    def fetch_all_records(self) -> list[dict[str, Any]]:
        """Crawls all year directories and returns raw record dictionaries.

        Iterates through every year folder found by `_get_year_links`, scrapes
        the TIF files within, and extracts metadata using the regex pattern.

        Returns:
            List[Dict[str, Any]]: List of dictionaries compatible with the FloodRecord schema.
                Each dict contains keys: global_id, filename, year, start_date, cluster_id, download_url.

        Note:
            Failures in specific year folders are logged as errors but do not stop
            the scraping of other years.
        """
        year_links = self._get_year_links()
        all_records: list[dict[str, Any]] = []

        logger.info("scraping_started", years_found=len(year_links))

        for year_folder in year_links:
            year_url = self.base_url + year_folder
            clean_year = year_folder.strip("/")

            try:
                r = self.session.get(year_url, timeout=self.settings.timeout_seconds)
                y_soup = BeautifulSoup(r.text, "html.parser")

                count_for_year = 0
                for link in y_soup.find_all("a"):
                    fname = link.get("href")
                    if not isinstance(fname, str) or not fname.endswith(".tif"):
                        continue

                    match = self.file_pattern.match(fname)
                    if match:
                        record = {
                            "global_id": _stable_global_id(fname),
                            "filename": fname,
                            "year": clean_year,
                            "start_date": match.group("start_date"),
                            "end_date": match.group("end_date"),
                            "cluster_id": match.group("cluster_id"),
                            "download_url": year_url + fname,
                            "size_bytes": self._parse_link_size(link),
                        }
                        all_records.append(record)
                        count_for_year += 1

                logger.debug("scraped_year", year=clean_year, count=count_for_year)

            except Exception as e:
                logger.error("scraping_failed_year", year=year_folder, error=str(e))

        # A sha1->uint32 collision between two distinct files is astronomically
        # unlikely for this archive, but fail fast rather than merge two events.
        seen: dict[int, str] = {}
        for record in all_records:
            gid, fname = record["global_id"], record["filename"]
            if seen.get(gid, fname) != fname:
                raise ScrapingError(
                    f"global_id collision: {seen[gid]!r} and {fname!r} hash to {gid}"
                )
            seen[gid] = fname

        logger.info("scraping_completed", total_records=len(all_records))
        return all_records

    @staticmethod
    def _parse_link_size(link: Any) -> int | None:
        """Extract the file size (bytes) from the link's table row, if present."""
        row = link.find_parent("tr")
        if row is None:
            return None
        for cell in row.find_all("td"):
            size = _parse_size(cell.get_text())
            if size:
                return size
        return None
