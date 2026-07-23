"""End-to-end verification of a PUBLISHED index bundle over HTTP (the live-host gate).

Confirms a hosted bundle at ``base_url`` is actually consumable the way a fresh
``pip install euroflood`` consumes it: the manifest is fetchable, the small tables'
SHA-256 match the manifest, the COG opens and streams a window via GDAL ``/vsicurl``
(without downloading it), and a real ``floods(bbox=...)`` query returns events.

The same helper powers three call sites: the post-upload check inside
``publish --source-coop``, the ``euroflood verify-remote`` CLI, and the opt-in ``online``
test, so what we publish is always checked the way users will read it.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
import structlog

from ..config import Settings, get_settings
from ..exceptions import VerificationError

logger = structlog.get_logger(__name__)

# A small ROI known to carry real events in the published index (Zutphen, IJssel).
_PROBE_BBOX = (6.14, 52.09, 6.27, 52.17)
_GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "VSI_CACHE": "TRUE",
}


@dataclass
class VerifyReport:
    """Structured result of `verify_published`."""

    base_url: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    n_events: int | None = None

    def record(self, name: str, ok: bool, detail: str = "") -> bool:
        """Append a check result and return ``ok`` (for inline branching)."""
        self.checks.append((name, bool(ok), detail))
        return bool(ok)

    @property
    def ok(self) -> bool:
        """True when every recorded check passed."""
        return all(ok for _, ok, _ in self.checks)

    @property
    def failures(self) -> list[str]:
        """Names (+ detail) of the checks that failed."""
        return [f"{n} ({d})" if d else n for n, ok, d in self.checks if not ok]

    def summary(self) -> str:
        """A one-line ``k/n checks passed`` summary (naming failures when any)."""
        n_pass = sum(1 for _, ok, _ in self.checks if ok)
        head = f"{n_pass}/{len(self.checks)} checks passed for {self.base_url}"
        return head if self.ok else f"{head}; failed: {', '.join(self.failures)}"


def _sha256_stream(url: str, timeout: float) -> tuple[str, int]:
    """Stream ``url`` and return ``(sha256_hexdigest, byte_count)`` without buffering it."""
    hasher = hashlib.sha256()
    n = 0
    with requests.get(url, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(1 << 20):
            hasher.update(chunk)
            n += len(chunk)
    return hasher.hexdigest(), n


def _probe_cog(vurl: str, manifest: dict[str, Any], report: VerifyReport) -> None:
    """Open the COG via ``/vsicurl`` and record structure + a windowed-read check."""
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.Env(**_GDAL_ENV), rasterio.open(vurl) as src:
        report.record(
            "cog_crs_4326",
            src.crs is not None and src.crs.to_epsg() == 4326,
            str(src.crs),
        )
        report.record("cog_uint32", str(src.dtypes[0]) == "uint32", str(src.dtypes[0]))
        report.record("cog_tiled", bool(src.profile.get("tiled", False)), "")
        report.record("cog_nodata_0", src.nodata == 0, str(src.nodata))
        report.record("cog_overviews", len(src.overviews(1)) > 0, str(src.overviews(1)))
        grid = manifest.get("grid", {})
        report.record(
            "cog_grid_matches_manifest",
            src.width == grid.get("width_px") and src.height == grid.get("height_px"),
            f"{src.width}x{src.height}",
        )
        arr = src.read(1, window=from_bounds(*_PROBE_BBOX, src.transform))
        wet = int((arr > 0).sum())
        report.record("cog_probe_window_has_data", wet > 0, f"{wet} wet px")


def _probe_query(
    base_url: str, settings: Settings | None, timeout: float, report: VerifyReport
) -> None:
    """Run a real remote ``floods(bbox=...)`` in an isolated temp cache; record the count."""
    from ..api import floods

    with tempfile.TemporaryDirectory(prefix="ef_verify_") as td:
        qs = (settings or get_settings()).model_copy(
            update={
                "cache_dir": Path(td),
                "output_dir": Path(td) / "out",
                "index_mode": "remote",
                "index_base_url": base_url,
                "timeout_seconds": max(30, int(timeout)),
            }
        )
        cat = floods(bbox=_PROBE_BBOX, settings=qs)
    report.n_events = len(cat)
    report.record("remote_query_returns_events", len(cat) > 0, f"{len(cat)} events")


def verify_published(
    base_url: str,
    *,
    settings: Settings | None = None,
    deep: bool = False,
    run_query: bool = True,
    timeout: float = 120.0,
    raise_on_error: bool = True,
) -> VerifyReport:
    """Verify a published index bundle end-to-end over HTTP.

    Args:
        base_url: The version-pinned bundle prefix, e.g.
            ``https://data.source.coop/hackl/euroflood-index/v1.0.0``.
        settings: Optional base settings to copy for the probe query (defaults to the
            global settings); the query always runs remote against ``base_url`` in an
            isolated temp cache, so the caller's cache is never touched.
        deep: Also SHA-256 the (large) COG, not just the small tables.
        run_query: Also run a real ``floods(bbox=...)`` query against the hosted index.
        timeout: Per-request timeout in seconds for the HTTP fetches.
        raise_on_error: Raise `VerificationError` if any
            check fails (default). Pass ``False`` to always return the report instead.

    Returns:
        A `VerifyReport` with one ``(name, ok, detail)`` entry per check.

    Raises:
        VerificationError: If any check failed and ``raise_on_error`` is True.
    """
    base = base_url.rstrip("/")
    s = settings or get_settings()
    report = VerifyReport(base_url=base)

    # 1. Manifest fetchable + well-formed.
    manifest: dict[str, Any] = requests.get(
        f"{base}/manifest.json", timeout=timeout
    ).json()
    files: dict[str, Any] = manifest.get("files", {})
    report.record("manifest_fetched", bool(files), f"{len(files)} files")
    report.record(
        "manifest_index_version",
        bool(manifest.get("index_version")),
        str(manifest.get("index_version")),
    )

    # 2. SHA-256 of each table (and the COG only when deep=True) vs the manifest.
    for rel, rec in files.items():
        if rel == s.index_filename and not deep:
            report.record(f"sha256:{rel}", True, "skipped (COG; pass deep=True)")
            continue
        digest, size = _sha256_stream(f"{base}/{rel}", timeout)
        report.record(
            f"sha256:{rel}",
            digest == rec.get("sha256") and size == rec.get("size_bytes"),
            f"{size} B",
        )

    # 3. The COG opens + streams a window via /vsicurl (no full download).
    _probe_cog(f"/vsicurl/{base}/{s.index_filename}", manifest, report)

    # 4. A real remote query returns events.
    if run_query:
        _probe_query(base, settings, timeout, report)

    logger.info(
        "verify_published", base_url=base, ok=report.ok, checks=len(report.checks)
    )
    if raise_on_error and not report.ok:
        raise VerificationError(report.summary())
    return report
