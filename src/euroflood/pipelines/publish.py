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

# The source dataset this index is derived from — attribution required (CC-BY-4.0).
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
            "(Betterle &amp; Salamon, 2025; CC-BY-4.0) &mdash; ~3,280 satellite-derived "
            "<em>observed</em> flood-depth maps across Europe, 2015&ndash;2024. The "
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


def build_readme(version: str, *, base_url: str) -> str:
    """The dataset/product card (``README.md``) shipped with the bundle.

    One maintained source (``product_readme.md``), used by both the Source Cooperative
    product landing page and the Zenodo record. ``base_url`` is the version-pinned
    live-host prefix so the direct-access examples are copy-pasteable.
    """
    template = (
        resources.files("euroflood.pipelines")
        .joinpath("product_readme.md")
        .read_text(encoding="utf-8")
    )
    return template.replace("__VERSION__", version.lstrip("v")).replace(
        "__BASE_URL__", base_url
    )


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


def publish_to_zenodo(
    settings: Settings | None = None,
    *,
    version: str,
    sandbox: bool,
    dry_run: bool = False,
    dotenv: Path = Path(".env"),
) -> dict[str, Any]:
    """Validate + publish the bundle to Zenodo; stamp the manifest with version + DOI."""
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
        }

    token = os.environ.get(env_key)
    if not token:
        raise RuntimeError(f"{env_key} is not set (add it to .env or the environment).")
    # Stamp the real version into the manifest + write the dataset card before upload.
    stamp_manifest(manifest_path, index_version=version)
    base_url = settings.source_coop_base_url(version)
    (settings.cache_dir / "README.md").write_text(
        build_readme(version, base_url=base_url)
    )
    files = [*gather_bundle_files(settings), settings.cache_dir / "README.md"]
    metadata = build_zenodo_metadata(version)
    result = ZenodoPublisher(token, sandbox=sandbox).create_and_publish(files, metadata)
    # Record the minted DOI back into the local manifest (and any future re-upload).
    stamp_manifest(
        manifest_path,
        source_urls={
            "zenodo_doi": result["doi"],
            "zenodo_concept_doi": result["concept_doi"],
            "source_coop": None,  # filled when the live host is wired
        },
    )
    logger.info("zenodo_published", version=version, doi=result["doi"])
    return result


def publish_to_source_coop(
    settings: Settings | None = None,
    *,
    version: str,
    dry_run: bool = False,
    verify: bool = True,
    dotenv: Path = Path(".env"),
) -> dict[str, Any]:
    """Validate + upload the bundle to Source Cooperative (the live ``/vsicurl`` host).

    Stamps the manifest with the version + the public base URL, uploads the bundle to the
    immutable ``vX.Y.Z/`` prefix (plus a product README to the repo root), then
    self-verifies the hosted index end-to-end. AWS STS credentials come from the
    environment / a ``.env`` (never from code). Requires the ``publish`` extra (boto3).
    """
    settings = settings or get_settings()
    load_dotenv(dotenv)
    manifest_path = settings.get_manifest_path()
    validate_publish_manifest(
        manifest_path, base_dir=settings.cache_dir, verify_checksums=True
    )
    base_url = settings.source_coop_base_url(version)
    bucket = settings.source_coop_account
    key_prefix = f"{settings.source_coop_repository}/v{version.lstrip('v')}"

    if dry_run:  # read-only: validate + report, upload nothing
        names = [f.name for f in gather_bundle_files(settings)] + [
            "README.md",
            "hero.png",
        ]
        return {
            "dry_run": True,
            "version": version,
            "base_url": base_url,
            "bucket": bucket,
            "key_prefix": key_prefix,
            "files": names,
        }

    try:
        from ..services.source_coop import SourceCoopPublisher
    except ImportError as exc:  # pragma: no cover - guarded optional publish extra
        raise SourceCoopError(
            "boto3 is required to publish to Source Cooperative. "
            'Install it with: pip install "euroflood[publish]".'
        ) from exc

    # Stamp version + the public URL into the manifest, write the product README, upload.
    stamp_manifest(
        manifest_path, index_version=version, source_urls={"source_coop": base_url}
    )
    readme = settings.cache_dir / "README.md"
    readme.write_text(build_readme(version, base_url=base_url))
    # The product-card hero is shipped as package data; stage + upload it into the version
    # prefix so the card (served at the repo root) can reference a public URL that doesn't
    # depend on the GitHub repo being public: {base_url}/hero.png.
    hero = settings.cache_dir / "hero.png"
    hero.write_bytes(
        resources.files("euroflood.pipelines").joinpath("product_hero.png").read_bytes()
    )

    publisher = SourceCoopPublisher(
        endpoint=settings.source_coop_endpoint, region=settings.source_coop_region
    )
    keys = publisher.upload_files(
        [*gather_bundle_files(settings), hero], bucket=bucket, key_prefix=key_prefix
    )
    readme_key = publisher.upload_readme(
        readme, bucket=bucket, repository=settings.source_coop_repository
    )
    logger.info(
        "source_coop_published", version=version, base_url=base_url, files=len(keys)
    )

    report = None
    if verify:
        from .verify import verify_published

        report = verify_published(base_url, settings=settings)

    return {
        "version": version,
        "base_url": base_url,
        "bucket": bucket,
        "keys": keys,
        "readme_key": readme_key,
        "verified": None if report is None else report.ok,
        "verify_summary": None if report is None else report.summary(),
    }
