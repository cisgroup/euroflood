"""Configuration Management for EuroFlood.

This module handles global application settings using Pydantic Settings.
It manages environment variables, default paths, and system-wide constants.
It implements a Hybrid Cache Strategy (Option C) allowing system defaults
to be overridden by environment variables.

Example:
    To use the settings in your code:

    >>> from euroflood.config import settings
    >>> print(settings.base_url)
    >>> settings.ensure_dirs()

Notes:
    Environment variables should be prefixed with `EUROFLOOD_` (e.g., `EUROFLOOD_OUTPUT_DIR`).
"""

import os
from functools import lru_cache
from pathlib import Path

import platformdirs
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Global application settings and configuration.

    Every field is set by an ``EUROFLOOD_<FIELD>`` environment variable (e.g.
    ``EUROFLOOD_OUTPUT_DIR``) or by mutating the `settings`
    singleton at runtime. See each field's description below (or
    ``help(euroflood.settings)`` / the Configuration reference) for what it does.

    Note:
        See https://docs.pydantic.dev/latest/concepts/pydantic_settings/ for
        environment-variable precedence.
    """

    # validate_assignment=True auto-converts assigned strings to Paths (so
    # ``settings.cache_dir = "..."`` works and never hits a 'str has no mkdir').
    model_config = SettingsConfigDict(env_prefix="EUROFLOOD_", validate_assignment=True)

    # --- Remote source ---
    base_url: str = Field(
        default=(
            "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-EFAS/"
            "European_Satellite-Derived_Flood_Depth_Maps/maps/"
        ),
        description="Base URL of the JRC CEMS-EFAS flood-depth map archive.",
    )

    # --- Paths (OS default, override via env var) ---
    cache_dir: Path = Field(
        default_factory=lambda: Path(platformdirs.user_cache_dir("euroflood")),
        description="Directory for the index, downloads, and intermediate files "
        "(defaults to the OS user cache, e.g. ~/.cache/euroflood).",
    )
    output_dir: Path = Field(
        default_factory=lambda: Path(os.getcwd()) / "outputs",
        description="Directory for downloaded/cropped result GeoTIFFs "
        "(defaults to ./outputs).",
    )
    autodetect_downloads: bool = Field(
        default=True,
        description="When building a catalogue with floods()/hazard(), auto-populate "
        "the `path` column from crops already present in output_dir (matched by the "
        "ROI-safe filename), so a re-query is immediately actionable without a new "
        "download. Best-effort: only output_dir is scanned. Set False to disable.",
    )

    # --- Cache filenames ---
    inventory_filename: str = Field(
        default="inventory.csv",
        description="Filename of the scraped source inventory CSV (in cache_dir).",
    )
    index_filename: str = Field(
        default="europe_flood_index.tif",
        description="Filename of the index COG (in cache_dir).",
    )
    dictionary_filename: str = Field(
        default="flood_dictionary.json",
        description="Filename of the legacy JSON dictionary (in cache_dir); "
        "current bundles use dictionary_parquet_filename instead.",
    )
    manifest_filename: str = Field(
        default="manifest.json",
        description="Filename of the cache/publish manifest (in cache_dir).",
    )

    # --- Geocoding (place name -> geometry) ---
    geocoder_backend: str = Field(
        default="online_first",
        description="Geocoder: 'online_first' (Nominatim then offline NUTS "
        "fallback; default), 'nominatim' (online only), or 'local' (offline NUTS "
        "only; Nominatim on a miss iff allow_remote_geocoding).",
    )
    boundary_dataset_url: str = Field(
        default=(
            "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
            "NUTS_RG_20M_2024_4326.geojson"
        ),
        description="URL of the Eurostat NUTS boundary dataset (offline geocoder).",
    )
    boundary_dataset_filename: str = Field(
        default="NUTS_RG_20M_2024_4326.geojson",
        description="Cached filename of the NUTS boundary dataset.",
    )
    boundary_dataset_path: Path | None = Field(
        default=None,
        description="Override path to a local NUTS boundary dataset (skips the "
        "download).",
    )
    nominatim_url: str = Field(
        default="https://nominatim.openstreetmap.org/search",
        description="Nominatim search endpoint for online geocoding.",
    )
    nominatim_email: str | None = Field(
        default=None,
        description="Contact email included in the Nominatim User-Agent (etiquette).",
    )
    allow_remote_geocoding: bool = Field(
        default=False,
        description="Allow a Nominatim fallback when using the 'local' backend.",
    )
    geocode_timeout_seconds: int = Field(
        default=10,
        description="Per-request Nominatim timeout, kept short so the "
        "'online_first' offline fallback stays snappy.",
    )
    geocode_cache: bool = Field(
        default=True,
        description="Cache resolved place-name geometries on disk "
        "(cache_dir/geocode/) so a repeat query skips the Nominatim round-trip.",
    )
    show_progress: bool = Field(
        default=True,
        description="Show progress bars for downloads + the first-query index "
        "mirror when running interactively (a Jupyter notebook or a TTY). "
        "Set EUROFLOOD_SHOW_PROGRESS=0 to disable (e.g. for clean CI logs).",
    )

    # --- GLOFAS global flood hazard ---
    hazard_base_url: str = Field(
        default=(
            "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-GLOFAS/flood_hazard/"
        ),
        description="Base URL of the JRC CEMS-GLOFAS flood-hazard tiles.",
    )
    hazard_index_filename: str = Field(
        default="tile_extents.geojson",
        description="Filename of the GLOFAS tile-extents index (in the hazard cache).",
    )
    hazard_index_path: Path | None = Field(
        default=None,
        description="Override path to a local GLOFAS tile index (skips the download).",
    )
    hazard_model_version: str = Field(
        default="v2.1.2", description="GLOFAS model version tag."
    )
    hazard_cache_tiles: bool = Field(
        default=True,
        description="True: download whole hazard tiles into the cache "
        "(offline-friendly); False: stream them via /vsicurl (nothing cached).",
    )
    hazard_manifest_filename: str = Field(
        default="hazard_manifest.json",
        description="Filename of the hazard reference manifest.",
    )

    # --- Index build (producer; full-scale HPC export) ---
    dictionary_parquet_filename: str = Field(
        default="flood_dictionary.parquet",
        description="Filename of the single combo_id-sorted Parquet dictionary.",
    )
    dictionary_meta_filename: str = Field(
        default="dictionary_meta.json",
        description="Filename of the dictionary metadata sidecar.",
    )
    dictionary_row_group_size: int = Field(
        default=20000,
        description="Rows per Parquet row group (keyed-lookup pruning granularity).",
    )
    index_memory_limit: str | None = Field(
        default=None, description="DuckDB memory_limit for the export (e.g. '32GB')."
    )
    index_threads: int | None = Field(
        default=None, description="DuckDB threads for the export (None = default)."
    )
    index_write_chunk_rows: int = Field(
        default=4000, description="Rows per raster write band during export."
    )
    index_compress: str = Field(
        default="deflate",
        description="COG compression codec (DEFLATE = universal readers).",
    )
    index_predictor: int = Field(
        default=2, description="COG compression predictor (1 or 2)."
    )
    index_version: str = Field(
        default="0.0.0-dev", description="Published index version tag."
    )

    # --- Normalized dictionary + index source ---
    events_filename: str = Field(
        default="events.parquet",
        description="Filename of the normalized per-event metadata table "
        "(flood_id -> metadata, joined at query time).",
    )
    index_mode: str = Field(
        default="auto",
        description="Where the index is read from: 'auto' (local bundle if present, "
        "else hosted), 'local' (cache only; HPC/offline), or 'remote' (always "
        "hosted; COG streams via /vsicurl, small tables mirror on first use).",
    )
    index_base_url: str | None = Field(
        default=None,
        description="Base URL of the published index (falls back to the baked-in "
        "default, which is None until the index is published).",
    )

    # --- Source Cooperative (the live index host; producer/publish side) ---
    source_coop_account: str = Field(
        default="hackl",
        description="Source Cooperative account = the S3 bucket in the data proxy.",
    )
    source_coop_repository: str = Field(
        default="euroflood-index",
        description="Source Cooperative repository = the S3 key prefix.",
    )
    source_coop_endpoint: str = Field(
        default="https://data.source.coop",
        description="Source Cooperative S3-compatible endpoint URL.",
    )
    source_coop_region: str = Field(
        default="us-east-1",
        description="AWS region for the Source Cooperative endpoint.",
    )

    # --- HPC ingestion sharding (SLURM array jobs) ---
    ingest_shard_index: int | None = Field(
        default=None, description="This shard's 0-based index (SLURM array task)."
    )
    ingest_shard_count: int | None = Field(
        default=None, description="Total number of ingestion shards."
    )
    ledger_suffix: str = Field(
        default="", description="Suffix appended to state-ledger filenames (sharding)."
    )

    # --- Processing ---
    max_plausible_depth: float | None = Field(
        default=None,
        description="Upper bound (raster units) for plausible flood depth; pixels "
        "above it are dropped as suspect. None disables the guard.",
    )

    # --- Execution ---
    max_workers_dl: int = Field(
        default=8, description="Number of threads for downloading."
    )
    max_workers_cpu: int = Field(
        default_factory=lambda: os.cpu_count() or 4,
        description="Number of processes for CPU-bound work (defaults to CPU count).",
    )
    timeout_seconds: int = Field(
        default=60, description="HTTP request timeout, in seconds."
    )
    retries: int = Field(default=3, description="Retry attempts for network requests.")

    # --- Logging ---
    log_level: str = Field(
        default="INFO", description="Log level (DEBUG, INFO, WARNING, ERROR)."
    )

    def get_inventory_path(self) -> Path:
        """Return the full path to the inventory CSV file.

        Returns:
            Path: The absolute path to the inventory.csv file within the cache directory.
        """
        return self.cache_dir / self.inventory_filename

    def get_index_tif_path(self) -> Path:
        """Return the full path to the global index raster file.

        Returns:
            Path: The absolute path to the index TIF file within the cache directory.
        """
        return self.cache_dir / self.index_filename

    def get_dictionary_path(self) -> Path:
        """Return the full path to the flood event dictionary JSON file.

        Returns:
            Path: The absolute path to the dictionary JSON file within the cache directory.
        """
        return self.cache_dir / self.dictionary_filename

    def get_manifest_path(self) -> Path:
        """Return the full path to the cache manifest (version + grid fingerprint).

        Returns:
            Path: The absolute path to the manifest JSON within the cache directory.
        """
        return self.cache_dir / self.manifest_filename

    def get_hazard_dir(self) -> Path:
        """Return the cache directory for GLOFAS hazard artefacts (index + tiles)."""
        return self.cache_dir / "hazard"

    def get_hazard_index_path(self) -> Path:
        """Return the path to the cached GLOFAS tile-extents GeoJSON.

        Honours ``hazard_index_path`` if set (skips the download), otherwise
        places it under `get_hazard_dir`.
        """
        return self.hazard_index_path or (
            self.get_hazard_dir() / self.hazard_index_filename
        )

    def get_hazard_tiles_dir(self) -> Path:
        """Return the cache directory for downloaded GLOFAS hazard tiles."""
        return self.get_hazard_dir() / "tiles"

    def get_hazard_manifest_path(self) -> Path:
        """Return the path to the hazard reference manifest."""
        return self.get_hazard_dir() / self.hazard_manifest_filename

    def get_dictionary_parquet_path(self) -> Path:
        """Return the path to the single combo_id-sorted Parquet dictionary file."""
        return self.cache_dir / self.dictionary_parquet_filename

    def get_dictionary_meta_path(self) -> Path:
        """Return the path to the small dictionary metadata JSON sidecar."""
        return self.cache_dir / self.dictionary_meta_filename

    def get_events_path(self) -> Path:
        """Return the path to the normalized per-event metadata Parquet table."""
        return self.cache_dir / self.events_filename

    def source_coop_base_url(self, version: str) -> str:
        """Public HTTPS base URL of a published index version (immutable ``vX.Y.Z``)."""
        v = version.lstrip("v")
        return (
            f"{self.source_coop_endpoint.rstrip('/')}/"
            f"{self.source_coop_account}/{self.source_coop_repository}/v{v}"
        )

    def ensure_dirs(self) -> None:
        """Create necessary directories if they don't exist.

        This ensures that `cache_dir` and `output_dir` exist on the filesystem.
        Should be called before starting pipelines.

        Raises:
            OSError: If directories cannot be created due to permission issues.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached `Settings` instance.

    Using a cached factory rather than a bare module-level constant lets tests
    reset configuration via ``get_settings.cache_clear()`` and lets callers pass
    an explicit ``Settings`` into pipelines/services for multi-config workflows
    (e.g. pointing ``cache_dir`` at node-local scratch per HPC process).
    """
    return Settings()


# Backwards-compatible module-level alias (the same object as get_settings()).
settings = get_settings()
