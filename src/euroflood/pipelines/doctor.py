"""Validate a built index bundle before it is published.

Read-only checks that the COG + Parquet dictionary + publish manifest are
internally consistent and `/vsicurl`-ready: a valid tiled COG with overviews and
`nodata=0`, a grid fingerprint matching the running code, per-file checksums that
match the manifest, and sampled combo_ids that resolve in the dictionary. It also
**reports** the populated-cell count N, the COG size, and the size an equivalent
sparse-Parquet pixel table would be, for sizing comparisons.
"""

from __future__ import annotations

import json
from typing import Any

import duckdb
import rasterio
import structlog

from ..config import Settings, get_settings
from ..core.manifest import validate_publish_manifest
from ..exceptions import CacheSchemaError
from ..services.dictionary_repository import DictionaryRepository

logger = structlog.get_logger(__name__)


class IndexDoctor:
    """Validate + report on a built index bundle."""

    def __init__(self, settings: Settings | None = None) -> None:
        """Bind to a settings object (defaults to `get_settings`)."""
        self.settings = settings or get_settings()

    def run(self) -> dict[str, Any]:
        """Run all checks; raise on failure, else return a report dict."""
        s = self.settings
        report: dict[str, Any] = {}

        # 1. Manifest: schema versions + grid fingerprint + per-file checksums.
        validate_publish_manifest(
            s.get_manifest_path(), base_dir=s.cache_dir, verify_checksums=True
        )

        # 2. The COG: tiled, nodata=0, overviews (when larger than one block).
        cog = s.get_index_tif_path()
        if not cog.exists():
            raise CacheSchemaError("Index COG is missing.")
        with rasterio.open(cog) as src:
            if not src.profile.get("tiled", False):
                raise CacheSchemaError("Index raster is not tiled (not a COG).")
            if src.nodata != 0:
                raise CacheSchemaError(f"Index nodata is {src.nodata!r}, expected 0.")
            overviews = src.overviews(1)
            if (src.width > 512 or src.height > 512) and not overviews:
                raise CacheSchemaError("Index COG has no overviews.")
            report["width"] = src.width
            report["height"] = src.height
            report["overviews"] = overviews
        report["cog_size_bytes"] = cog.stat().st_size

        # 3. Dictionary: sampled combo_ids must resolve.
        meta = json.loads(s.get_dictionary_meta_path().read_text())
        n_combos = int(meta.get("n_combos", 0))
        report["n_combos"] = n_combos
        repo = DictionaryRepository(settings=s)
        sample = list(range(1, min(n_combos, 5) + 1))
        found = repo.lookup_combos(sample)
        missing = [c for c in sample if str(c) not in found]
        if missing:
            raise CacheSchemaError(f"combo_ids missing from dictionary: {missing}")

        # 4. Report N (populated cells) + the equivalent sparse-Parquet size, for
        #    sizing comparisons between the COG and a table.
        n_cells = self._populated_cells()
        report["populated_cells"] = n_cells
        report["sparse_parquet_estimate_bytes"] = n_cells * 12  # 3x uint32 / cell

        logger.info(
            "doctor_ok",
            populated_cells=n_cells,
            n_combos=n_combos,
            cog_size_mb=round(report["cog_size_bytes"] / 1e6, 2),
            sparse_parquet_estimate_mb=round(
                report["sparse_parquet_estimate_bytes"] / 1e6, 2
            ),
        )
        return report

    def _populated_cells(self) -> int:
        """Count distinct flooded (col, row) cells across the Parquet lake."""
        glob = str(self.settings.cache_dir / "parquet" / "*" / "*.parquet")
        con = duckdb.connect()
        try:
            result = con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT col, row FROM '{glob}')"
            ).fetchone()
        except duckdb.IOException:
            return 0  # no parquet present (e.g. validating a pulled bundle)
        finally:
            con.close()
        return int(result[0]) if result else 0
