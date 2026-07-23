"""Command Line Interface using Click.

Consumer commands: ``floods`` (cheap query, prints/writes a catalogue) and
``download`` (search + download + crop). Producer commands: ``ingest`` and
``export``.

All user-facing output flows through `console` (a rich Console on
stdout); ``structlog`` keeps owning logs on stderr. Domain exceptions are caught
by `EuroFloodCLI` and rendered as a clean one-block message (no traceback)
with a per-type exit code; ``-v/--verbose`` restores the traceback for developers.
"""

from typing import Any

import click
import structlog

from . import __version__, _progress, console
from .api import download as api_download
from .api import floods as api_floods
from .api import hazard as api_hazard
from .api import mirror as api_mirror
from .api import verify as api_verify
from .config import settings
from .exceptions import (
    CacheSchemaError,
    ConfigurationError,
    EuroFloodError,
    GeocodingError,
    HazardError,
    NetworkError,
    ProcessingError,
    PublishError,
    ScrapingError,
    VerificationError,
)
from .logging import setup_logging
from .pipelines.doctor import IndexDoctor
from .pipelines.export import ExportPipeline, export_plan
from .pipelines.hazard import build_hazard_manifest
from .pipelines.ingestion import IngestionPipeline
from .pipelines.mirror import MirrorPipeline
from .services.mirror_ledger import MirrorResult

logger = structlog.get_logger(__name__)

# Ordered most-specific-first: the first matching type wins. ``EuroFloodError`` is
# last (base of the domain family); ``FileNotFoundError`` is independent (a missing
# index/dictionary that ships with a genuinely actionable message).
_EXIT_CODES: list[tuple[type[BaseException], int]] = [
    (ConfigurationError, 3),
    (GeocodingError, 4),
    (ScrapingError, 5),
    (HazardError, 6),
    (CacheSchemaError, 7),
    (ProcessingError, 8),
    (PublishError, 9),
    (VerificationError, 10),
    (FileNotFoundError, 2),
    (EuroFloodError, 1),
]

_NEXT_STEPS: list[tuple[type[BaseException], str]] = [
    (
        GeocodingError,
        "Try a more specific name, or set EUROFLOOD_ALLOW_REMOTE_GEOCODING=1 "
        "for online lookup.",
    ),
    (
        CacheSchemaError,
        "Rebuild with 'euroflood build-index', or pull a published one with "
        "'euroflood mirror index'.",
    ),
    (
        FileNotFoundError,
        "Run 'euroflood build-index' (or 'euroflood mirror index' for a "
        "published index).",
    ),
    (NetworkError, "Check your network connection and retry."),
    (HazardError, "Verify the return period; see 'euroflood mirror hazard'."),
]


def _exit_code_for(exc: BaseException) -> int:
    """Map a caught exception to its CLI exit code (default 1)."""
    for exc_type, code in _EXIT_CODES:
        if isinstance(exc, exc_type):
            return code
    return 1


def _next_step_for(exc: BaseException) -> str | None:
    """Return a suggested remedy line for a caught exception, if any."""
    for exc_type, hint in _NEXT_STEPS:
        if isinstance(exc, exc_type):
            return hint
    return None


class EuroFloodCLI(click.Group):
    """A click Group that renders EuroFlood errors cleanly (no traceback)."""

    def invoke(self, ctx: click.Context) -> Any:
        """Dispatch, turning known EuroFlood errors into clean CLI output.

        Unknown (non-EuroFlood) exceptions propagate untouched so real bugs stay
        loud. ``-v/--verbose`` re-raises the domain error too, restoring the
        traceback for developers. ``click`` exceptions keep click's own handling.
        """
        try:
            return super().invoke(ctx)
        except (EuroFloodError, FileNotFoundError) as exc:
            if console.verbosity() > 0:
                raise
            console.error_block(exc, next_step=_next_step_for(exc))
            ctx.exit(_exit_code_for(exc))


def _write_catalogue(catalogue: object, path: str) -> None:
    """Write a catalogue to CSV / GeoParquet / GeoJSON based on the extension."""
    if path.endswith(".parquet"):
        catalogue.to_parquet(path)  # type: ignore[attr-defined]
    elif path.endswith((".geojson", ".json")):
        catalogue.to_file(path, driver="GeoJSON")  # type: ignore[attr-defined]
    else:  # CSV: drop geometry so the table stays portable
        catalogue.drop(columns="geometry").to_csv(path, index=False)  # type: ignore[attr-defined]


@click.group(cls=EuroFloodCLI)
@click.version_option(version=__version__, prog_name="euroflood")
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase log detail (DEBUG); show tracebacks on error.",
)
@click.option(
    "-q", "--quiet", is_flag=True, help="Suppress secondary status and progress."
)
@click.option(
    "--json",
    "json_mode",
    is_flag=True,
    help="Emit machine-readable JSON for query results.",
)
@click.option("--no-color", "no_color", is_flag=True, help="Disable colored output.")
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: int,
    quiet: bool,
    json_mode: bool,
    no_color: bool,
) -> None:
    """EuroFlood: Satellite Flood Map Processing Tool.

    A toolset for downloading, indexing, and extracting European Satellite-Derived
    Flood Depth Maps.
    """
    level = "DEBUG" if verbose else ("WARNING" if quiet else settings.log_level)
    setup_logging(level=level)
    console.configure(
        quiet=quiet, json_mode=json_mode, no_color=no_color, verbose=verbose
    )
    # Route pipeline progress bars through the rich stdout Console (a no-op on a
    # non-TTY / under --quiet), keeping them off the stderr log stream.
    _progress.set_factory(console.progress)
    ctx.obj = {"verbose": verbose}


@cli.command()
@click.option("--year", type=int, help="Filter by Year (e.g., 2020).")
@click.option("--month", type=str, help="Filter by Month (e.g., '05').")
@click.option(
    "--update", is_flag=True, help="Force update of the inventory CSV from the web."
)
@click.option(
    "--shard-count",
    type=int,
    default=None,
    help="Total number of HPC shards (SLURM array size). Enables sharded ingest.",
)
@click.option(
    "--shard-index",
    type=int,
    default=None,
    help="This shard's 0-based index (e.g. $SLURM_ARRAY_TASK_ID).",
)
@click.option(
    "--limit", type=int, default=None, help="Only process the first N tiles (test run)."
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the plan and exit (no changes)."
)
def ingest(
    year: int | None,
    month: str | None,
    update: bool,
    shard_count: int | None,
    shard_index: int | None,
    limit: int | None,
    dry_run: bool,
) -> None:
    """Download and process flood maps.

    Runs the ingestion pipeline which:
    1. Scrapes the JRC website for available files.
    2. Downloads matching files to the cache.
    3. Processes them into optimized Parquet files.

    For HPC: a SLURM array maps ``$SLURM_ARRAY_TASK_ID`` -> ``--shard-index`` so
    each task ingests a deterministic, resumable slice of the inventory.
    """
    settings.ingest_shard_count = shard_count
    settings.ingest_shard_index = shard_index
    pipeline = IngestionPipeline()
    if dry_run:
        p = pipeline.plan(year=year, month=month, limit=limit)
        console.echo(
            f"[dry-run] ingest: {p['pending']} to process, {p['done']} done, "
            f"of {p['total']} in scope -> {p['dest']}"
        )
        if p.get("note"):
            console.echo(f"  note: {p['note']}")
        return
    pipeline.run(year=year, month=month, update=update, limit=limit)
    console.success("Ingest complete.")


@cli.command(name="fetch-sources")
@click.option("--year", type=int, help="Mirror only this year (default: all years).")
@click.option("--update", is_flag=True, help="Re-scrape the inventory first.")
@click.option(
    "--verify",
    is_flag=True,
    help="Re-download cached files whose on-disk size != the JRC-listed size.",
)
@click.option(
    "--retry-failed",
    "retry_failed",
    is_flag=True,
    help="Re-attempt only the files in the dead-letter ledger.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Only download the first N tiles (test run).",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the plan and exit (no changes)."
)
def fetch_sources(
    year: int | None,
    update: bool,
    verify: bool,
    retry_failed: bool,
    limit: int | None,
    dry_run: bool,
) -> None:
    """Download ALL raw source flood-map tiles to the cache (producer; ~35 GB).

    The stable "download first, then process" path for the full archive, used to
    *build* the index. Not a consumer offline command. For that use ``mirror``.
    Re-running skips files already present; ``--verify`` re-fetches any whose
    on-disk size doesn't match the JRC listing; a later ``ingest`` only processes
    the cached files.
    """
    pipeline = MirrorPipeline()
    if dry_run:
        p = pipeline.plan(year=year, limit=limit)
        console.echo(
            f"[dry-run] fetch-sources: {p['to_download']} to download of {p['tiles']} "
            f"({p['total_gb']} GB), {p['present']} present -> {p['dest']}"
        )
        if p.get("note"):
            console.echo(f"  note: {p['note']}")
        return
    count = pipeline.run(
        year=year, update=update, verify=verify, retry_failed=retry_failed, limit=limit
    )
    console.success(f"{count} source tile(s) available in the cache.")


def _echo_export_plan(label: str) -> None:
    """Print the export/build-index dry-run plan."""
    p = export_plan()
    out = p["outputs"]["index"]
    console.echo(
        f"[dry-run] {label}: {p['parquet_files']} parquet files, "
        f"{p['populated_cells']} populated cells -> {out}"
    )


@cli.command()
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the plan and exit (no changes)."
)
def export(dry_run: bool) -> None:
    """Generate the Global Index Raster.

    Aggregates all processed Parquet files into a single, high-resolution
    GeoTIFF index covering Europe. This index maps every pixel to a unique
    combination of historical flood events.
    """
    if dry_run:
        _echo_export_plan("export")
        return
    ExportPipeline().run()
    console.success("Export complete.")


def _echo_index_report(report: dict[str, object]) -> None:
    """Print a one-line summary of an IndexDoctor report."""
    size_mb = float(report["cog_size_bytes"]) / 1e6  # type: ignore[arg-type]
    console.success(
        f"Index OK: {report['populated_cells']} cells, {report['n_combos']} combos, "
        f"COG {size_mb:.1f} MB."
    )


@cli.command(name="build-index")
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the plan and exit (no changes)."
)
def build_index(dry_run: bool) -> None:
    """Build the index COG + Parquet dictionary + publish manifest, then validate.

    The Phase-5a producer entry point: runs the export aggregation and immediately
    validates the bundle with the doctor (the gate Phase 5b publishes through).
    """
    if dry_run:
        _echo_export_plan("build-index")
        return
    ExportPipeline().run()
    _echo_index_report(IndexDoctor().run())


@cli.command()
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the plan and exit (no changes)."
)
def doctor(dry_run: bool) -> None:
    """Validate a built index bundle (COG + Parquet dictionary + publish manifest)."""
    if dry_run:
        console.echo(
            f"[dry-run] doctor: would validate {settings.get_index_tif_path()}, "
            f"{settings.get_dictionary_parquet_path()}, {settings.get_manifest_path()}"
        )
        return
    _echo_index_report(IndexDoctor().run())


@cli.command()
@click.option(
    "--version",
    "version",
    default=None,
    help="Index version tag (e.g. v1.0.0); defaults to settings.index_version.",
)
@click.option(
    "--source-coop",
    "to_source_coop",
    is_flag=True,
    help="Upload to Source Cooperative (the live /vsicurl host). Default target.",
)
@click.option(
    "--zenodo",
    "to_zenodo",
    is_flag=True,
    help="Publish to Zenodo (citable DOI + full mirror).",
)
@click.option(
    "--sandbox",
    is_flag=True,
    help="With --zenodo: use sandbox.zenodo.org (throwaway DOIs) via "
    "ZENODO_SANDBOX_TOKEN.",
)
@click.option(
    "--no-verify",
    "no_verify",
    is_flag=True,
    help="With --source-coop: skip the post-upload live verification.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Validate + print the plan; upload nothing.",
)
def publish(
    version: str | None,
    to_source_coop: bool,
    to_zenodo: bool,
    sandbox: bool,
    no_verify: bool,
    dry_run: bool,
) -> None:
    """Publish the built index bundle to Source Cooperative and/or Zenodo.

    Source Cooperative is the live ``/vsicurl`` query host; Zenodo mints a citable DOI.
    With no target flag, defaults to ``--source-coop``. Credentials come from the
    environment / a local ``.env`` (AWS STS for Source Cooperative, ZENODO_TOKEN for
    Zenodo). ``--source-coop`` needs the ``publish`` extra (``pip install
    "euroflood[publish]"``).
    """
    from .pipelines.publish import publish_to_source_coop, publish_to_zenodo

    tag = version or settings.index_version
    if not to_source_coop and not to_zenodo:
        to_source_coop = True  # default target: the live host

    if to_source_coop:
        sc = publish_to_source_coop(
            settings, version=tag, dry_run=dry_run, verify=not no_verify
        )
        if sc.get("dry_run"):
            console.echo(
                f"[dry-run] publish {sc['version']} -> Source Cooperative "
                f"{sc['base_url']} ({len(sc['files'])} files: "
                f"{', '.join(sc['files'])})"
            )
        else:
            console.success(f"Published {tag} to Source Cooperative: {sc['base_url']}")
            if sc.get("verify_summary"):
                console.success(sc["verify_summary"])

    if to_zenodo:
        zn = publish_to_zenodo(settings, version=tag, sandbox=sandbox, dry_run=dry_run)
        if zn.get("dry_run"):
            console.echo(
                f"[dry-run] publish {zn['version']} -> Zenodo {zn['target']} "
                f"(token ${zn['token_env']}); {len(zn['files'])} files"
            )
        else:
            console.success(
                f"Published {tag} to Zenodo: DOI {zn['doi']} "
                f"(concept {zn['concept_doi']}) -> {zn['record_url']}"
            )


# --- offline mirror / verify family ----------------------------------------
def _roi_options(f: Any) -> Any:
    """Attach the standard ROI-selection options to a mirror/verify subcommand."""
    f = click.argument("place", required=False)(f)
    f = click.option(
        "--bbox",
        nargs=4,
        type=float,
        default=None,
        help="ROI box: MINX MINY MAXX MAXY (WGS84).",
    )(f)
    f = click.option(
        "--point",
        nargs=2,
        type=float,
        default=None,
        help="ROI point: LAT LON (with --radius).",
    )(f)
    f = click.option(
        "--radius",
        "radius_m",
        type=float,
        default=0.0,
        help="Radius (m) around --point.",
    )(f)
    f = click.option(
        "--shapefile",
        type=click.Path(exists=True, dir_okay=False),
        default=None,
        help="Vector file used as the ROI.",
    )(f)
    f = click.option(
        "--buffer", "buffer_m", type=float, default=0.0, help="Extra metric ROI buffer."
    )(f)
    f = click.option(
        "--shape",
        type=click.Choice(["exact", "bbox", "hull"]),
        default="exact",
        help="ROI shape derived from the region.",
    )(f)
    return f


def _roi_kwargs(
    place: str | None,
    bbox: tuple[float, ...] | None,
    point: tuple[float, ...] | None,
    radius_m: float,
    shapefile: str | None,
    buffer_m: float,
    shape: str,
) -> dict[str, Any]:
    return {
        "region": place or None,
        "bbox": tuple(bbox) if bbox else None,
        "point": tuple(point) if point else None,
        "radius_m": radius_m,
        "shapefile": shapefile,
        "buffer_m": buffer_m,
        "shape": shape,
    }


def _report_mirror(label: str, res: Any) -> None:
    line = f"  {label}: {res.downloaded}/{res.n_expected} available"
    if res.missing:
        line += f"; {len(res.missing)} missing (e.g. {', '.join(res.missing[:3])})"
    console.echo(line + ".")


def _report_verify(rep: Any) -> None:
    console.echo(f"  {rep.summary()}")
    if rep.missing:
        console.echo(f"    missing: {', '.join(rep.missing[:6])}")
    if rep.corrupt:
        console.echo(f"    corrupt: {', '.join(rep.corrupt[:6])}")


@cli.group()
def mirror() -> None:
    """Stage data for offline/HPC use: index | floods | hazard | all."""


@cli.group()
def verify() -> None:
    """Check local-mirror readiness: index | floods | hazard | all | remote."""


@mirror.command("index")
@click.option("--dry-run", "dry_run", is_flag=True, help="Print the plan and exit.")
def mirror_index_cmd(dry_run: bool) -> None:
    """Mirror the flood catalogue (index bundle) so floods() queries run offline."""
    if dry_run:
        res = api_mirror("index", dry_run=True)
        assert isinstance(res, MirrorResult)
        console.echo(
            f"[dry-run] mirror index: {res.n_expected - len(res.missing)}/"
            f"{res.n_expected} bundle files present -> {settings.cache_dir}"
        )
        return
    res = api_mirror("index")
    assert isinstance(res, MirrorResult)
    console.success(
        f"Index mirrored to {settings.cache_dir} "
        f"({res.downloaded}/{res.n_expected} files)."
    )


@mirror.command("floods")
@_roi_options
@click.option("--year", type=int, default=None, help="Only floods in this year.")
@click.option("--start", default=None, help="Floods on/after (YYYY[-MM-DD]).")
@click.option("--end", default=None, help="Floods on/before (YYYY[-MM-DD]).")
@click.option("--dry-run", "dry_run", is_flag=True, help="Print the plan and exit.")
def mirror_floods_cmd(
    place: str | None,
    bbox: Any,
    point: Any,
    radius_m: float,
    shapefile: str | None,
    buffer_m: float,
    shape: str,
    year: int | None,
    start: str | None,
    end: str | None,
    dry_run: bool,
) -> None:
    """Mirror flood DEPTH maps for a region so floods().download() runs offline."""
    kw = _roi_kwargs(place, bbox, point, radius_m, shapefile, buffer_m, shape)
    res = api_mirror("floods", year=year, start=start, end=end, dry_run=dry_run, **kw)
    assert isinstance(res, MirrorResult)
    if dry_run:
        console.echo(
            f"[dry-run] mirror floods: {len(res.missing)} of {res.n_expected} depth "
            f"map(s) to fetch (~{res.bytes_total / 1e6:.0f} MB) -> "
            f"{settings.cache_dir / 'downloads'}"
        )
        return
    _report_mirror("floods", res)
    console.success("Flood depth maps mirrored.")


@mirror.command("hazard")
@_roi_options
@click.option(
    "--return-period",
    "-r",
    "return_periods",
    type=int,
    multiple=True,
    help="Return period(s). Repeatable. Default: all.",
)
@click.option("--dry-run", "dry_run", is_flag=True, help="Print the plan and exit.")
def mirror_hazard_cmd(
    place: str | None,
    bbox: Any,
    point: Any,
    radius_m: float,
    shapefile: str | None,
    buffer_m: float,
    shape: str,
    return_periods: tuple[int, ...],
    dry_run: bool,
) -> None:
    """Mirror GLOFAS hazard tiles for a region so hazard().download() runs offline."""
    kw = _roi_kwargs(place, bbox, point, radius_m, shapefile, buffer_m, shape)
    rps: int | list[int] | None = list(return_periods) or None
    res = api_mirror("hazard", return_period=rps, dry_run=dry_run, **kw)
    assert isinstance(res, MirrorResult)
    if dry_run:
        console.echo(
            f"[dry-run] mirror hazard: {len(res.missing)} of {res.n_expected} tile(s) "
            f"to fetch (~{res.bytes_total / 1e6:.0f} MB) -> "
            f"{settings.get_hazard_tiles_dir()}"
        )
        return
    _report_mirror("hazard", res)
    console.success(f"{res.downloaded} hazard tile(s) available locally.")


@mirror.command("all")
@_roi_options
@click.option(
    "--return-period",
    "-r",
    "return_periods",
    type=int,
    multiple=True,
    help="Hazard return period(s). Repeatable. Default: all.",
)
@click.option("--year", type=int, default=None, help="Flood year filter.")
@click.option("--start", default=None, help="Flood start filter.")
@click.option("--end", default=None, help="Flood end filter.")
@click.option("--dry-run", "dry_run", is_flag=True, help="Print the plan and exit.")
def mirror_all_cmd(
    place: str | None,
    bbox: Any,
    point: Any,
    radius_m: float,
    shapefile: str | None,
    buffer_m: float,
    shape: str,
    return_periods: tuple[int, ...],
    year: int | None,
    start: str | None,
    end: str | None,
    dry_run: bool,
) -> None:
    """Mirror index + flood depths + hazard tiles for a region (one-shot prestage)."""
    kw = _roi_kwargs(place, bbox, point, radius_m, shapefile, buffer_m, shape)
    rps: int | list[int] | None = list(return_periods) or None
    out = api_mirror(
        "all", return_period=rps, year=year, start=start, end=end, dry_run=dry_run, **kw
    )
    assert isinstance(out, dict)  # 'all' returns one result per layer
    console.echo(f"{'[dry-run] ' if dry_run else ''}mirror all:")
    for layer in ("index", "floods", "hazard"):
        _report_mirror(layer, out[layer])
    if not dry_run:
        console.success("All layers mirrored for the region.")


@verify.command("remote")
@click.option(
    "--url",
    "url",
    default=None,
    help="Published index base URL to verify (defaults to the configured/baked one).",
)
@click.option(
    "--deep",
    is_flag=True,
    help="Also SHA-256 the full COG (a large download), not just the small tables.",
)
def verify_remote_cmd(url: str | None, deep: bool) -> None:
    """Verify a PUBLISHED index end-to-end over HTTP (manifest, /vsicurl COG, a query)."""
    from ._data import resolve_base_url
    from .pipelines.verify import verify_published

    base = url or resolve_base_url(settings)
    if not base:
        raise click.ClickException(
            "No index base URL. Pass --url or set EUROFLOOD_INDEX_BASE_URL."
        )
    report = verify_published(base, settings=settings, deep=deep, raise_on_error=False)
    for name, ok, detail in report.checks:
        console.echo(
            f"  {'OK ' if ok else 'XX '}{name}{f': {detail}' if detail else ''}"
        )
    if not report.ok:
        raise VerificationError(report.summary())
    console.success(report.summary())


def _emit_verify(rep: Any) -> None:
    """Print a local-mirror report and raise VerificationError if not ready."""
    _report_verify(rep)
    if not rep.ok:
        remedy = rep.remediation()
        raise VerificationError(
            rep.summary() + (f". Repair: {remedy}" if remedy else "")
        )
    console.success(rep.summary())


@verify.command("index")
@click.option("--deep", is_flag=True, help="Re-hash sha256 vs the manifest.")
def verify_index_cmd(deep: bool) -> None:
    """Report index-bundle mirror readiness."""
    _emit_verify(api_verify("index", deep=deep))


@verify.command("floods")
@_roi_options
@click.option("--deep", is_flag=True, help="Re-hash sha256 vs the ledger.")
def verify_floods_cmd(
    place: str | None,
    bbox: Any,
    point: Any,
    radius_m: float,
    shapefile: str | None,
    buffer_m: float,
    shape: str,
    deep: bool,
) -> None:
    """Report flood depth-map mirror readiness for a region."""
    kw = _roi_kwargs(place, bbox, point, radius_m, shapefile, buffer_m, shape)
    _emit_verify(api_verify("floods", deep=deep, **kw))


@verify.command("hazard")
@_roi_options
@click.option(
    "--return-period",
    "-r",
    "return_periods",
    type=int,
    multiple=True,
    help="Return period(s). Repeatable. Default: all.",
)
@click.option("--deep", is_flag=True, help="Re-hash sha256 vs the ledger.")
def verify_hazard_cmd(
    place: str | None,
    bbox: Any,
    point: Any,
    radius_m: float,
    shapefile: str | None,
    buffer_m: float,
    shape: str,
    return_periods: tuple[int, ...],
    deep: bool,
) -> None:
    """Report hazard-tile mirror readiness for a region."""
    kw = _roi_kwargs(place, bbox, point, radius_m, shapefile, buffer_m, shape)
    rps: int | list[int] | None = list(return_periods) or None
    _emit_verify(api_verify("hazard", return_period=rps, deep=deep, **kw))


@verify.command("all")
@_roi_options
@click.option(
    "--return-period",
    "-r",
    "return_periods",
    type=int,
    multiple=True,
    help="Hazard return period(s). Repeatable.",
)
@click.option("--deep", is_flag=True, help="Re-hash sha256 vs the ledgers.")
def verify_all_cmd(
    place: str | None,
    bbox: Any,
    point: Any,
    radius_m: float,
    shapefile: str | None,
    buffer_m: float,
    shape: str,
    return_periods: tuple[int, ...],
    deep: bool,
) -> None:
    """Report index + floods + hazard mirror readiness for a region."""
    kw = _roi_kwargs(place, bbox, point, radius_m, shapefile, buffer_m, shape)
    rps: int | list[int] | None = list(return_periods) or None
    out = api_verify("all", return_period=rps, deep=deep, **kw)
    assert isinstance(out, dict)  # 'all' returns one report per layer
    failed: list[str] = []
    for layer in ("index", "floods", "hazard"):
        console.echo(f"{layer}:")
        _report_verify(out[layer])
        if not out[layer].ok:
            failed.append(layer)
    if failed:
        raise VerificationError(f"Mirror incomplete for: {', '.join(failed)}.")
    console.success("All layers present and verified.")


@cli.command()
@click.argument("place")
@click.option("--year", type=int, help="Keep only events in this year.")
@click.option(
    "--start", type=str, help="Keep events on/after a date (YYYY or YYYY-MM-DD)."
)
@click.option(
    "--end", type=str, help="Keep events on/before a date (YYYY or YYYY-MM-DD)."
)
@click.option("--buffer", type=float, default=0.0, help="Buffer radius in metres.")
@click.option(
    "--shape",
    type=click.Choice(["exact", "bbox", "hull"]),
    default="exact",
    help="ROI shape: exact boundary (default), its bounding box, or convex hull.",
)
@click.option(
    "--query",
    "query_expr",
    type=str,
    help="pandas .query() filter on the catalogue, e.g. 'area_km2 > 5'.",
)
@click.option(
    "-o",
    "--out",
    "out_path",
    type=click.Path(),
    help="Write the catalogue to .csv / .parquet / .geojson.",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Query + print but don't write -o."
)
def floods(
    place: str,
    year: int | None,
    start: str | None,
    end: str | None,
    buffer: float,
    shape: str,
    query_expr: str | None,
    out_path: str | None,
    dry_run: bool,
) -> None:
    """Query historic flood events for PLACE (cheap; no rasters downloaded)."""
    catalogue = api_floods(
        place, year=year, start=start, end=end, buffer_m=buffer, shape=shape
    )
    if query_expr:
        catalogue = catalogue.query(query_expr)
    console.render_catalogue(catalogue, kind="floods", place=place)
    if out_path and dry_run:
        console.echo(f"[dry-run] would write catalogue to {out_path}")
    elif out_path:
        _write_catalogue(catalogue, out_path)
        console.success(f"Wrote catalogue to {out_path}")


@cli.command()
@click.argument("place")
@click.option("--year", type=int, help="Keep only events in this year.")
@click.option("--start", type=str, help="Keep events on/after a date.")
@click.option("--end", type=str, help="Keep events on/before a date.")
@click.option("--buffer", type=float, default=0.0, help="Buffer radius in metres.")
@click.option(
    "--shape",
    type=click.Choice(["exact", "bbox", "hull"]),
    default="exact",
    help="ROI shape: exact boundary (default), its bounding box, or convex hull.",
)
@click.option(
    "--query", "query_expr", type=str, help="pandas .query() filter on the catalogue."
)
@click.option(
    "-o",
    "--out",
    "output_dir",
    type=click.Path(),
    help="Output directory for cropped GeoTIFFs.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Print what would be fetched; no download.",
)
def download(
    place: str,
    year: int | None,
    start: str | None,
    end: str | None,
    buffer: float,
    shape: str,
    query_expr: str | None,
    output_dir: str | None,
    dry_run: bool,
) -> None:
    """Search PLACE and download + crop the matching flood maps."""
    catalogue = api_floods(
        place, year=year, start=start, end=end, buffer_m=buffer, shape=shape
    )
    if query_expr:
        catalogue = catalogue.query(query_expr)
    if dry_run:
        dest = output_dir or str(settings.output_dir)
        console.echo(
            f"[dry-run] download: would fetch + crop {len(catalogue)} flood map(s) "
            f"-> {dest}"
        )
        return
    with console.download_progress("Downloading flood maps") as advance:
        paths = api_download(catalogue, output_dir, on_bytes=advance)
    console.success(f"Downloaded {len(paths)} flood map(s).")


@cli.command()
@click.argument("place")
@click.option(
    "--return-period",
    "-r",
    "return_periods",
    type=int,
    multiple=True,
    help="Return period(s): 10/20/50/75/100/200/500. Repeatable. Default: all.",
)
@click.option("--buffer", type=float, default=0.0, help="Buffer radius in metres.")
@click.option(
    "--shape",
    type=click.Choice(["exact", "bbox", "hull"]),
    default="exact",
    help="ROI shape: exact boundary (default), its bounding box, or convex hull.",
)
@click.option(
    "--download",
    "do_download",
    is_flag=True,
    help="Also fetch + mosaic + crop the hazard rasters into --out.",
)
@click.option(
    "--query", "query_expr", type=str, help="pandas .query() filter on the catalogue."
)
@click.option(
    "-o",
    "--out",
    "out_path",
    type=click.Path(),
    help="Catalogue file (.csv/.parquet/.geojson), or with --download the output dir.",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Query + print but don't fetch/write."
)
def hazard(
    place: str,
    return_periods: tuple[int, ...],
    buffer: float,
    shape: str,
    do_download: bool,
    query_expr: str | None,
    out_path: str | None,
    dry_run: bool,
) -> None:
    """Query global GLOFAS flood-hazard maps for PLACE by return period."""
    rps: int | list[int] | None = list(return_periods) or None
    catalogue = api_hazard(place, return_period=rps, buffer_m=buffer, shape=shape)
    if query_expr:
        catalogue = catalogue.query(query_expr)
    console.render_catalogue(catalogue, kind="hazard", place=place)
    if dry_run:
        if do_download:
            dest = out_path or str(settings.output_dir)
            console.echo(
                f"[dry-run] hazard: would fetch + mosaic {len(catalogue)} map(s) "
                f"-> {dest}"
            )
        elif out_path:
            console.echo(f"[dry-run] would write catalogue to {out_path}")
        return
    if do_download:
        with console.download_progress("Downloading hazard maps") as advance:
            paths = api_download(catalogue, out_path, on_bytes=advance)
        console.success(f"Downloaded {len(paths)} hazard map(s).")
    elif out_path:
        _write_catalogue(catalogue, out_path)
        console.success(f"Wrote catalogue to {out_path}")


@cli.command(name="build-hazard-manifest")
@click.option(
    "--dry-run", "dry_run", is_flag=True, help="Print the target path and exit."
)
def build_hazard_manifest_cmd(dry_run: bool) -> None:
    """Author the hazard reference manifest (provenance; references JRC, no copies)."""
    if dry_run:
        console.echo(
            f"[dry-run] build-hazard-manifest: would write "
            f"{settings.get_hazard_manifest_path()}"
        )
        return
    path = build_hazard_manifest()
    console.success(f"Wrote hazard manifest to {path}")


if __name__ == "__main__":  # pragma: no cover
    cli()
