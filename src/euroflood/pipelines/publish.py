"""Publish the built index bundle to Zenodo (citable DOI + full-mirror archive).

Zenodo is the archival/citation endpoint. The live query host (Source Cooperative) is
wired once its beta credentials + upload mechanism are confirmed; this module covers the
account-ready Zenodo half. Always test with ``sandbox=True`` (``sandbox.zenodo.org``,
throwaway DOIs) before the real ``zenodo.org``.

Credentials come from the environment (``ZENODO_TOKEN`` / ``ZENODO_SANDBOX_TOKEN``);
`load_dotenv` auto-loads a ``.env`` so ``euroflood publish`` picks them up with no
secrets in the code or git.
"""

from __future__ import annotations

import json
import os
from importlib import resources
from pathlib import Path
from typing import Any

import structlog

from ..config import Settings, get_settings
from ..core.manifest import stamp_manifest, validate_publish_manifest
from ..exceptions import SourceCoopError
from ..services.zenodo import ZenodoPublisher

logger = structlog.get_logger(__name__)

# The source dataset this index is derived from: attribution required (CC-BY-4.0).
JRC_SOURCE_URL = (
    "https://data.jrc.ec.europa.eu/dataset/0bc96690-b89c-4909-9166-c2c322a20130"
)
_ORCID = "0000-0002-8849-5751"


def load_dotenv(path: Path) -> None:
    """Load ``KEY=VALUE`` lines from a ``.env`` into ``os.environ`` (never overriding).

    A tiny, dependency-free parser: skips blanks/comments, tolerates a leading
    ``export``, strips surrounding quotes, and uses `setdefault` so an
    already-exported value always wins.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep:
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


SOFTWARE_CONCEPT_DOI = "10.5281/zenodo.22837458"
INDEX_CONCEPT_DOI = "10.5281/zenodo.21284459"
_REPO_URL = "https://github.com/cisgroup/euroflood"
_DOCS_URL = "https://cisgroup.github.io/euroflood/"


def build_software_metadata(version: str) -> dict[str, Any]:
    """Zenodo record metadata for the euroflood **library** (MIT).

    Deliberately separate from `build_zenodo_metadata`, which describes the index
    *dataset*: that one is ``upload_type: dataset`` under CC-BY-4.0 and attributes JRC,
    none of which is true of the software. Conflating them is the mistake this split
    exists to prevent.

    Args:
        version: The released library version (e.g. ``"0.3.0"``).

    Returns:
        Zenodo deposition metadata.
    """
    return {
        "title": (
            "EuroFlood: query Europe's satellite flood-depth maps by place and time"
        ),
        "upload_type": "software",
        "description": (
            "<p>EuroFlood is a lightweight, cloud-native Python library that turns the "
            "JRC / Copernicus <em>CEMS-EFAS Satellite-Derived Flood Depth Maps for "
            "Europe</em> into a queryable index: discover which flood events touched a "
            "region (and when), then download only the depth rasters you need. It "
            "follows a <strong>Discover &rarr; Extract</strong> model, streaming a "
            "compact index over HTTP range reads rather than the multi-gigabyte source "
            "archive.</p><p>This record archives the released source distribution and "
            "wheel, byte-identical to the artifacts published on PyPI "
            "(<code>pip install euroflood</code>). The flood index the library reads is "
            "archived separately under its own DOI.</p>"
        ),
        "creators": [
            {
                "name": "Hackl, Jürgen",
                "affiliation": "Princeton University",
                "orcid": _ORCID,
            }
        ],
        "access_right": "open",
        "license": "mit",
        "version": version,
        "keywords": [
            "flood",
            "flood depth",
            "CEMS-EFAS",
            "GLOFAS",
            "Copernicus",
            "Sentinel-1",
            "geospatial",
            "remote sensing",
            "Python",
        ],
        "related_identifiers": [
            {
                "relation": "isSupplementedBy",
                "identifier": INDEX_CONCEPT_DOI,
                "resource_type": "dataset",
            },
            {
                "relation": "isDocumentedBy",
                "identifier": _DOCS_URL,
                "resource_type": "publication-softwaredocumentation",
            },
            {
                "relation": "isSupplementTo",
                "identifier": _REPO_URL,
                "resource_type": "software",
            },
        ],
    }


def build_zenodo_metadata(version: str) -> dict[str, Any]:
    """Zenodo record metadata for the EuroFlood index (CC-BY-4.0; attributes JRC)."""
    return {
        "title": (
            "EuroFlood: a queryable cloud-native index for the CEMS-EFAS "
            "Satellite-Derived Flood Depth Maps"
        ),
        "upload_type": "dataset",
        "description": (
            "<p>EuroFlood is an open, cloud-native index over the JRC/Copernicus "
            "<em>CEMS-EFAS Satellite-Derived Flood Depth Maps for Europe</em> "
            "(Betterle &amp; Salamon, 2025; CC-BY-4.0) &mdash; ~3,610 satellite-derived "
            "<em>observed</em> flood-depth maps across Europe, 2015&ndash;2025. The "
            "bundle is a sparse Cloud-Optimized GeoTIFF encoding, per pixel, the set of "
            "flood events that inundated it, plus a <code>combo_id</code>-sorted "
            "GeoParquet dictionary and a small events table. Query by region and time "
            "via HTTP range reads (GDAL <code>/vsicurl</code> + DuckDB) to retrieve "
            "matching events, then fetch only the source depth rasters needed. Built "
            "with the open-source EuroFlood Python package "
            "(<code>pip install euroflood</code>).</p>"
        ),
        "creators": [
            {
                "name": "Hackl, Jürgen",
                "affiliation": "Princeton University",
                "orcid": _ORCID,
            }
        ],
        "access_right": "open",
        "license": "cc-by-4.0",
        "version": version,
        "keywords": [
            "flood",
            "flood depth",
            "CEMS-EFAS",
            "Copernicus",
            "Sentinel-1",
            "cloud-optimized geotiff",
            "geoparquet",
            "Europe",
            "flood risk",
        ],
        "related_identifiers": [
            {
                "relation": "isDerivedFrom",
                "identifier": JRC_SOURCE_URL,
                "resource_type": "dataset",
            }
        ],
        "notes": (
            "Derived index over the JRC/Copernicus CEMS-EFAS Satellite-Derived Flood "
            "Depth Maps (Betterle & Salamon, 2025). Attribution to the original authors "
            "is required under CC-BY-4.0."
        ),
    }


def build_readme(
    version: str, *, base_url: str, zenodo_concept_doi: str | None = None
) -> str:
    """The dataset/product card (``README.md``) shipped with the bundle.

    One maintained source (``product_readme.md``), used by both the Source Cooperative
    product landing page and the Zenodo record. ``base_url`` is the version-pinned
    live-host prefix so the direct-access examples are copy-pasteable.
    ``zenodo_concept_doi`` (once the index is on Zenodo) adds a "cite" line pointing at
    the concept DOI; omitted before the DOI exists.
    """
    template = (
        resources.files("euroflood.pipelines")
        .joinpath("product_readme.md")
        .read_text(encoding="utf-8")
    )
    doi_line = (
        f"- **Cite:** [{zenodo_concept_doi}](https://doi.org/{zenodo_concept_doi})"
        ", the citable Zenodo archive (all versions)"
        if zenodo_concept_doi
        else ""
    )
    return (
        template.replace("__VERSION__", version.lstrip("v"))
        .replace("__BASE_URL__", base_url)
        .replace("__ZENODO_DOI_LINE__", doi_line)
    )


def _zenodo_concept_doi(manifest_path: Path) -> str | None:
    """The concept DOI stamped in the manifest (once the index is on Zenodo), else None."""
    try:
        data = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return (data.get("source_urls") or {}).get("zenodo_concept_doi")


def gather_bundle_files(settings: Settings) -> list[Path]:
    """The files to upload: the manifest plus every existing file it lists."""
    manifest_path = settings.get_manifest_path()
    files = [manifest_path]
    data = json.loads(manifest_path.read_text())
    for rel in data.get("files", {}):
        path = settings.cache_dir / rel
        if path.exists() and path not in files:
            files.append(path)
    return files


def publish_software_to_zenodo(
    files: list[Path],
    *,
    version: str,
    sandbox: bool = False,
    concept_recid: int | None = None,
    draft_id: int | None = None,
    dry_run: bool = False,
    dotenv: Path = Path(".env"),
) -> dict[str, Any]:
    """Archive the released library artifacts on Zenodo.

    Deliberately shares nothing with `publish_to_zenodo` beyond the token convention.
    That function is wired to the index bundle: it validates and *stamps the index
    manifest*, and stamping a software DOI there would leak into the dataset product
    card and corrupt the index's provenance. This path never touches `Settings` or any
    manifest.

    Exactly one of ``draft_id`` or ``concept_recid`` must be given:

    - ``draft_id`` publishes a **reserved, unpublished draft**, honouring its
      pre-reserved DOI. Use it for a record's first release.
    - ``concept_recid`` resolves the concept record to its latest published version and
      adds a new version to it. Use it for every release after the first.

    Creating a brand-new record is intentionally not offered: it would mint a fresh
    concept DOI and silently orphan the existing one.

    Args:
        files: Artifacts to attach (the sdist and wheel that went to PyPI).
        version: The released library version.
        sandbox: Publish to sandbox.zenodo.org instead of the real thing.
        concept_recid: Concept record id, for releases after the first.
        draft_id: Reserved draft id, for the first release.
        dry_run: Validate and report the plan; upload nothing.
        dotenv: A ``.env`` to load credentials from.

    Returns:
        ``{doi, concept_doi, record_url, deposition_id}``, or a dry-run plan.

    Raises:
        ValueError: If the target is ambiguous or unspecified, or a file is missing.
        RuntimeError: If the required token is not set.
    """
    if (draft_id is None) == (concept_recid is None):
        raise ValueError(
            "give exactly one of draft_id (first release) or concept_recid"
        )
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        raise ValueError(f"artifact(s) not found: {', '.join(missing)}")

    load_dotenv(dotenv)
    env_key = "ZENODO_SANDBOX_TOKEN" if sandbox else "ZENODO_TOKEN"
    metadata = build_software_metadata(version)
    mode = "reserved-draft" if draft_id else "new-version"

    if dry_run:  # read-only: report the plan, upload nothing
        return {
            "dry_run": True,
            "version": version,
            "target": "sandbox" if sandbox else "production",
            "token_env": env_key,
            "mode": mode,
            "draft_id": draft_id,
            "concept_recid": concept_recid,
            "files": [f.name for f in files],
        }

    token = os.environ.get(env_key)
    if not token:
        raise RuntimeError(f"{env_key} is not set (add it to .env or the environment).")
    publisher = ZenodoPublisher(token, sandbox=sandbox)
    if draft_id is not None:
        result = publisher.publish_reserved_draft(draft_id, files, metadata)
    else:
        # narrowed by the exactly-one guard at the top of this function
        assert concept_recid is not None
        latest = publisher.latest_version_id(concept_recid)
        result = publisher.publish_new_version(latest, files, metadata)
    logger.info(
        "zenodo_software_published",
        version=version,
        doi=result["doi"],
        concept_doi=result["concept_doi"],
        mode=mode,
        files=len(files),
    )
    return result


def publish_to_zenodo(
    settings: Settings | None = None,
    *,
    version: str,
    sandbox: bool,
    record_id: int | None = None,
    dry_run: bool = False,
    dotenv: Path = Path(".env"),
) -> dict[str, Any]:
    """Validate + publish the bundle to Zenodo; stamp the manifest with version + DOI.

    With ``record_id``, publishes a **new version** of that existing record, so its
    concept DOI keeps resolving to the latest release and only a fresh version DOI is
    minted. Without it, creates a brand-new record (and therefore a new concept DOI),
    which is correct only for a dataset's first publication.
    """
    settings = settings or get_settings()
    load_dotenv(dotenv)
    manifest_path = settings.get_manifest_path()
    validate_publish_manifest(manifest_path, base_dir=settings.cache_dir)
    env_key = "ZENODO_SANDBOX_TOKEN" if sandbox else "ZENODO_TOKEN"

    if dry_run:  # read-only: validate + report, mutate/upload nothing
        names = [f.name for f in gather_bundle_files(settings)] + ["README.md"]
        return {
            "dry_run": True,
            "version": version,
            "target": "sandbox" if sandbox else "production",
            "token_env": env_key,
            "files": names,
            "mode": "new-version" if record_id else "new-record",
            "record_id": record_id,
        }

    token = os.environ.get(env_key)
    if not token:
        raise RuntimeError(f"{env_key} is not set (add it to .env or the environment).")
    # Stamp the real version into the manifest + write the dataset card before upload.
    stamp_manifest(manifest_path, index_version=version)
    base_url = settings.source_coop_base_url(version)
    (settings.cache_dir / "README.md").write_text(
        build_readme(
            version,
            base_url=base_url,
            zenodo_concept_doi=_zenodo_concept_doi(manifest_path),
        )
    )
    files = [*gather_bundle_files(settings), settings.cache_dir / "README.md"]
    metadata = build_zenodo_metadata(version)
    publisher = ZenodoPublisher(token, sandbox=sandbox)
    if record_id:
        result = publisher.publish_new_version(record_id, files, metadata)
    else:
        result = publisher.create_and_publish(files, metadata)
    # Record the minted DOI back into the local manifest (and any future re-upload).
    # stamp_manifest merges source_urls, so a prior source_coop URL is preserved.
    stamp_manifest(
        manifest_path,
        source_urls={
            "zenodo_doi": result["doi"],
            "zenodo_concept_doi": result["concept_doi"],
        },
    )
    logger.info(
        "zenodo_published",
        version=version,
        doi=result["doi"],
        concept_doi=result["concept_doi"],
        mode="new-version" if record_id else "new-record",
    )
    return result


def publish_to_source_coop(
    settings: Settings | None = None,
    *,
    version: str,
    dry_run: bool = False,
    verify: bool = True,
    readme_only: bool = False,
    dotenv: Path = Path(".env"),
) -> dict[str, Any]:
    """Validate + upload the bundle to Source Cooperative (the live ``/vsicurl`` host).

    Stamps the manifest with the version + the public base URL, uploads the bundle to the
    immutable ``vX.Y.Z/`` prefix (plus a product README to the repo root), then
    self-verifies the hosted index end-to-end. AWS STS credentials come from the
    environment / a ``.env`` (never from code). Requires the ``publish`` extra (boto3).

    With ``readme_only``, re-renders and uploads **just the product card** at the
    repository root and touches nothing else: no bundle upload, no hero, and no manifest
    stamping, so the immutable ``vX.Y.Z/`` prefix keeps the exact bytes it was published
    with. Use it when the card needs to pick up something the manifest gained after the
    bundle went up, such as a Zenodo DOI minted afterwards.
    """
    settings = settings or get_settings()
    load_dotenv(dotenv)
    manifest_path = settings.get_manifest_path()
    validate_publish_manifest(
        manifest_path, base_dir=settings.cache_dir, verify_checksums=not readme_only
    )
    base_url = settings.source_coop_base_url(version)
    bucket = settings.source_coop_account
    key_prefix = f"{settings.source_coop_repository}/v{version.lstrip('v')}"

    if dry_run:  # read-only: validate + report, upload nothing
        names = (
            ["README.md"]
            if readme_only
            else [f.name for f in gather_bundle_files(settings)]
            + ["README.md", "hero.png"]
        )
        return {
            "dry_run": True,
            "version": version,
            "base_url": base_url,
            "bucket": bucket,
            "key_prefix": key_prefix,
            "files": names,
            "readme_only": readme_only,
        }

    try:
        from ..services.source_coop import SourceCoopPublisher
    except ImportError as exc:  # pragma: no cover - guarded optional publish extra
        raise SourceCoopError(
            "boto3 is required to publish to Source Cooperative. "
            'Install it with: pip install "euroflood[publish]".'
        ) from exc

    # Stamp version + the public URL into the manifest, write the product README, upload.
    # A readme-only refresh leaves the manifest alone: it is already published, and the
    # card is rendered from whatever the local manifest happens to say.
    if not readme_only:
        stamp_manifest(
            manifest_path, index_version=version, source_urls={"source_coop": base_url}
        )
    readme = settings.cache_dir / "README.md"
    readme.write_text(
        build_readme(
            version,
            base_url=base_url,
            zenodo_concept_doi=_zenodo_concept_doi(manifest_path),
        )
    )
    publisher = SourceCoopPublisher(
        endpoint=settings.source_coop_endpoint, region=settings.source_coop_region
    )
    keys: list[str] = []
    if not readme_only:
        # The product-card hero is shipped as package data; stage + upload it into the
        # version prefix so the card (served at the repo root) can reference a public URL
        # that doesn't depend on the GitHub repo being public: {base_url}/hero.png.
        hero = settings.cache_dir / "hero.png"
        hero.write_bytes(
            resources.files("euroflood.pipelines")
            .joinpath("product_hero.png")
            .read_bytes()
        )
        keys = publisher.upload_files(
            [*gather_bundle_files(settings), hero], bucket=bucket, key_prefix=key_prefix
        )
    readme_key = publisher.upload_readme(
        readme, bucket=bucket, repository=settings.source_coop_repository
    )
    logger.info(
        "source_coop_published",
        version=version,
        base_url=base_url,
        files=len(keys),
        readme_only=readme_only,
    )

    report = None
    if verify and not readme_only:
        from .verify import verify_published

        report = verify_published(base_url, settings=settings)

    return {
        "version": version,
        "base_url": base_url,
        "bucket": bucket,
        "keys": keys,
        "readme_key": readme_key,
        "readme_only": readme_only,
        "verified": None if report is None else report.ok,
        "verify_summary": None if report is None else report.summary(),
    }
