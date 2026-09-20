"""Pipeline: Aggregates sparse Parquet data into a Global Index COG + dictionary.

This is the "Reduce" phase. It takes the thousands of sparse Parquet files from
ingestion and aggregates them into a single, highly compressed **Cloud-Optimized
GeoTIFF (COG)** index covering Europe, plus a queryable **partitioned-Parquet
dictionary** mapping each pixel's combination id to its flood events.

**The Index Raster.** A single ``uint32`` raster:
-   **Pixel 0**: no flood ever recorded (NoData; dropped from the file via SPARSE_OK).
-   **Pixel N**: a unique "combination id" -> a set of flood events (in the dictionary).

**Scaling.** The full grid is ~9.18 billion pixels (~37 GB uint32), but
sparse, so it compresses to a few hundred MB. Three changes make the build tractable:
1.  ``raw_pixels`` / ``pixel_combo`` are **materialized once** (not a VIEW re-aggregated
    per write chunk).
2.  the raster is written block-by-block as a **sparse** tiled BigTIFF, then COG-ified
    (overviews + NEAREST resampling) via the GDAL COG driver.
3.  the dictionary is streamed to **partitioned Parquet** (not one in-memory JSON), so it
    survives millions of combos and supports selective reads.

**Technology: DuckDB.** An embedded OLAP engine that queries thousands of Parquet files
directly from disk and spills to disk when aggregations exceed RAM.
"""

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import rasterio
import rasterio.shutil
import structlog

from .._progress import progress_bar
from ..config import Settings, get_settings
from ..core.grid import GlobalGrid
from ..core.manifest import write_publish_manifest
from ..schemas import DICTIONARY_SCHEMA_VERSION

logger = structlog.get_logger(__name__)


def export_plan(settings: Settings | None = None) -> dict[str, Any]:
    """Side-effect-free summary of what the export would build (for --dry-run).

    Reports the Parquet file count, the populated-cell count, and the output
    paths, without opening the pipeline's DuckDB file or building anything.
    """
    settings = settings or get_settings()
    pdir = settings.cache_dir / "parquet"
    files = list(pdir.rglob("*.parquet")) if pdir.exists() else []
    populated = 0
    if files:
        con = duckdb.connect()  # in-memory; no temp_aggregation.duckdb side effect
        try:
            glob = str(pdir / "*" / "*.parquet")
            row = con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT col, row FROM '{glob}')"
            ).fetchone()
            populated = int(row[0]) if row else 0
        finally:
            con.close()
    return {
        "parquet_files": len(files),
        "populated_cells": populated,
        "outputs": {
            "index": str(settings.get_index_tif_path()),
            "dictionary": str(settings.get_dictionary_parquet_path()),
            "manifest": str(settings.get_manifest_path()),
        },
    }


def _remove_path(path: Path) -> None:
    """Remove a path whether it is a file or a directory (idempotent).

    Handles upgrading in place: a prior build may have left the dictionary as an old
    partitioned *directory*; the new build writes a single *file* at the same path.
    """
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _git_commit() -> str | None:
    """Best-effort current git commit (provenance); None if unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout.strip() or None
    except Exception:  # pragma: no cover - git absent / not a repo
        return None


class ExportPipeline:
    """Aggregate flood occurrences into the Index COG + Parquet dictionary."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Set up the disk-backed DuckDB connection (spills if over RAM)."""
        self.settings = settings or get_settings()
        self.db_path = str(self.settings.cache_dir / "temp_aggregation.duckdb")
        self.con = duckdb.connect(self.db_path)

    def run(self) -> None:
        """Materialize the aggregation, write the COG, dictionary, and manifest."""
        parquet_glob = str(self.settings.cache_dir / "parquet" / "*" / "*.parquet")
        logger.info("export_start", source=parquet_glob)

        # Let a fat HPC node use its RAM/cores (DuckDB still spills to disk).
        if self.settings.index_memory_limit:
            self.con.execute(f"SET memory_limit='{self.settings.index_memory_limit}'")
        if self.settings.index_threads:
            self.con.execute(f"SET threads={self.settings.index_threads}")

        try:
            # 1. Materialize raw_pixels ONCE (TABLE, not VIEW): group every flood_id
            #    at a cell into a canonical sorted list. (A VIEW would re-run this
            #    GROUP BY for every raster write-band.)
            self.con.execute(
                f"""
                CREATE OR REPLACE TABLE raw_pixels AS
                SELECT col, row, list_sort(list_distinct(list(flood_id))) AS flood_ids
                FROM '{parquet_glob}'
                GROUP BY col, row
                """
            )

            # 2. Deterministic combo_id per unique flood-id list (0 reserved=NoData).
            logger.info("generating_combinations")
            self.con.execute(
                """
                CREATE OR REPLACE TABLE combinations AS
                SELECT
                    flood_ids,
                    CAST(row_number() OVER (ORDER BY flood_ids) AS UINTEGER) AS combo_id
                FROM (SELECT DISTINCT flood_ids FROM raw_pixels)
                """
            )

            # 3. Join combo_id back ONCE so the write loop is a pure range scan.
            self.con.execute(
                """
                CREATE OR REPLACE TABLE pixel_combo AS
                SELECT r.col, r.row, c.combo_id
                FROM raw_pixels r JOIN combinations c ON r.flood_ids = c.flood_ids
                """
            )

            # 4. Raster FIRST (so it exists before the dictionary indexes into it).
            self._write_raster()

            # 5. Dictionary as partitioned Parquet.
            self._export_dictionary()

            # 6. Publish manifest last: signals a complete, consistent bundle.
            self._write_manifest()
        finally:
            self.con.close()

        logger.info("export_complete")

    # ------------------------------------------------------------------ raster
    def _write_raster(self) -> None:
        """Write the index as a sparse tiled BigTIFF (block-by-block), then COG-ify."""
        out_path = self.settings.get_index_tif_path()
        intermediate = out_path.with_name(out_path.name + ".intermediate.tif")
        logger.info("writing_raster", path=str(out_path))

        profile = {
            "driver": "GTiff",
            "height": GlobalGrid.HEIGHT_PX,
            "width": GlobalGrid.WIDTH_PX,
            "count": 1,
            "dtype": rasterio.uint32,
            "crs": "EPSG:4326",
            "transform": rasterio.transform.from_origin(
                GlobalGrid.ORIGIN_X,
                GlobalGrid.ORIGIN_Y,
                GlobalGrid.RESOLUTION,
                GlobalGrid.RESOLUTION,
            ),
            "compress": self.settings.index_compress,
            "predictor": self.settings.index_predictor,
            "nodata": 0,
            "tiled": True,
            "blockxsize": 512,
            "blockysize": 512,
            "BIGTIFF": "YES",
            "SPARSE_OK": "TRUE",  # all-zero 512^2 tiles are never written
        }

        chunk_h = self.settings.index_write_chunk_rows
        total_rows = GlobalGrid.HEIGHT_PX
        chunks = range(0, total_rows, chunk_h)
        with (
            rasterio.open(intermediate, "w", **profile) as dst,
            progress_bar("Writing index raster", total=len(chunks)) as step,
        ):
            for start_row in chunks:
                step(1)
                end_row = min(start_row + chunk_h, total_rows)
                chunk_df = self.con.execute(
                    "SELECT col, row, combo_id FROM pixel_combo "
                    f"WHERE row >= {start_row} AND row < {end_row}"
                ).fetchdf()
                if chunk_df.empty:
                    continue
                h = end_row - start_row
                data = np.zeros((h, GlobalGrid.WIDTH_PX), dtype=np.uint32)
                data[
                    chunk_df["row"].to_numpy() - start_row,
                    chunk_df["col"].to_numpy(),
                ] = chunk_df["combo_id"].to_numpy()
                dst.write(
                    data,
                    1,
                    window=rasterio.windows.Window(
                        0, start_row, GlobalGrid.WIDTH_PX, h
                    ),
                )

        self._cogify(intermediate, out_path)
        os.remove(intermediate)

    def _cogify(self, src_path: Any, out_path: Any) -> None:
        """Reorder the tiled BigTIFF into a true COG (overviews + sparse) atomically.

        Uses the GDAL COG driver (via rasterio), NOT rio-cogeo, which unpacks the
        whole raster in memory at this scale. NEAREST resampling because combo_id is
        categorical (averaging ids is meaningless).
        """
        tmp_cog = out_path.with_name(out_path.name + ".part")
        predictor = "YES" if self.settings.index_predictor == 2 else "NO"
        rasterio.shutil.copy(
            str(src_path),
            str(tmp_cog),
            driver="COG",
            COMPRESS=self.settings.index_compress.upper(),
            PREDICTOR=predictor,
            BLOCKSIZE=512,
            OVERVIEW_RESAMPLING="NEAREST",
            BIGTIFF="YES",
            SPARSE_OK="TRUE",
            NUM_THREADS="ALL_CPUS",
        )
        os.replace(tmp_cog, out_path)

    # -------------------------------------------------------------- dictionary
    def _export_dictionary(self) -> None:
        """Write the NORMALIZED dictionary + a separate events table (+ meta JSON).

        The dictionary is ``combo_id -> flood_ids`` (integers only) as a SINGLE
        Parquet file **sorted by combo_id** with bounded row groups, so per-row-group
        min/max stats prune a keyed ``combo_id IN (...)`` lookup to a few groups (local
        or over HTTP range) and the whole ~20 MB is one clean object to mirror/stream.
        Event metadata is deliberately NOT denormalized into every combo (the repeated
        struct would dominate the file); it lives once in ``events.parquet`` and is
        joined at query time.
        """
        logger.info("saving_dictionary")
        dict_path = self.settings.get_dictionary_parquet_path()
        _remove_path(dict_path)  # tolerate a stale file OR an old partitioned dir
        rg = self.settings.dictionary_row_group_size

        tmp = dict_path.with_name(dict_path.name + ".part")
        self.con.execute(
            "COPY (SELECT combo_id, flood_ids FROM combinations ORDER BY combo_id) "
            f"TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {rg})"
        )
        os.replace(tmp, dict_path)

        self._export_events()

        meta = {
            "schema_version": DICTIONARY_SCHEMA_VERSION,
            "format": "parquet-single",
            "n_combos": self._scalar_int("SELECT count(*) FROM combinations"),
            "row_group_size": rg,
            "filename": self.settings.dictionary_parquet_filename,
            "events": self.settings.events_filename,
        }
        meta_path = self.settings.get_dictionary_meta_path()
        tmp = meta_path.with_name(meta_path.name + ".part")
        tmp.write_text(json.dumps(meta, indent=2))
        os.replace(tmp, meta_path)

    def _export_events(self) -> None:
        """Write the normalized per-event metadata table (one row per flood event).

        One row per ``global_id`` present in the index, with the same 7 fields the
        old denormalized ``events`` struct carried. Sourced from the inventory when
        present; otherwise just the distinct global_ids (metadata-less fallback).
        Tiny (~1 MB for the full archive), so it is downloaded whole and cached.
        """
        events_path = self.settings.get_events_path()
        # Distinct events actually present in the index (via the small combinations
        # table, NOT the 82.5 M-row raw_pixels, an order of magnitude cheaper).
        present = "SELECT DISTINCT fid FROM combinations, UNNEST(flood_ids) AS t(fid)"
        inv_path = self.settings.get_inventory_path()
        if inv_path.exists():
            self.con.execute(
                "CREATE OR REPLACE TABLE inventory AS "
                f"SELECT * FROM read_csv_auto('{inv_path}')"
            )
            select = f"""
                SELECT
                    CAST(i.global_id AS BIGINT) AS global_id,
                    CAST(i.start_date AS VARCHAR) AS start_date,
                    CAST(i.end_date AS VARCHAR) AS end_date,
                    CAST(i.year AS VARCHAR) AS year,
                    CAST(i.cluster_id AS VARCHAR) AS cluster_id,
                    CAST(i.filename AS VARCHAR) AS filename,
                    CAST(i.download_url AS VARCHAR) AS download_url
                FROM inventory i
                WHERE i.global_id IN ({present})
            """
        else:
            select = f"SELECT fid AS global_id FROM ({present})"

        tmp = events_path.with_name(events_path.name + ".part")
        self.con.execute(
            f"COPY ({select}) TO '{tmp}' (FORMAT PARQUET, OVERWRITE_OR_IGNORE)"
        )
        os.replace(tmp, events_path)

    def _scalar_int(self, query: str) -> int:
        """Run a scalar-count query and return it as an int (0 if empty)."""
        row = self.con.execute(query).fetchone()
        return int(row[0]) if row else 0

    # ---------------------------------------------------------------- manifest
    def _write_manifest(self) -> None:
        """Author the publish manifest (provenance, COG attestation, checksums)."""
        n_sources = self._scalar_int(
            "SELECT count(DISTINCT fid) FROM combinations, UNNEST(flood_ids) AS t(fid)"
        )

        files: dict[str, Any] = {
            self.settings.index_filename: self.settings.get_index_tif_path(),
            self.settings.dictionary_meta_filename: (
                self.settings.get_dictionary_meta_path()
            ),
        }
        events_path = self.settings.get_events_path()
        if events_path.exists():
            files[self.settings.events_filename] = events_path
        dict_path = self.settings.get_dictionary_parquet_path()
        if dict_path.exists():
            files[self.settings.dictionary_parquet_filename] = dict_path

        write_publish_manifest(
            self.settings.get_manifest_path(),
            index_version=self.settings.index_version,
            files=files,
            published_at=datetime.now(UTC).isoformat(),
            git_commit=_git_commit(),
            provenance={
                "jrc_base_url": self.settings.base_url,
                "n_source_files": int(n_sources),
            },
            cog={
                "tiled": True,
                "blocksize": 512,
                "compress": self.settings.index_compress,
                "predictor": self.settings.index_predictor,
                "sparse": True,
                "nodata": 0,
                "bigtiff": True,
                "overviews": True,
            },
            source_urls={"source_coop": None, "zenodo_doi": None},
        )
