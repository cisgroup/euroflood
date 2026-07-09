"""Source Cooperative S3 client for publishing the index as the live ``/vsicurl`` host.

Source Cooperative is the *live query* endpoint (unlike Zenodo, the archival DOI host): it
serves HTTP Range reads so the COG streams a window at a time via GDAL ``/vsicurl``. This
uploads the bundle to the account's S3-compatible bucket behind the data proxy, using
**path-style** addressing against the ``data.source.coop`` endpoint.

Temporary STS credentials come from the environment (``AWS_ACCESS_KEY_ID`` /
``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN``); `load_dotenv`
populates them from a ``.env`` so ``euroflood publish`` needs no secrets in code.

Importing this module requires the optional ``publish`` extra
(``pip install "euroflood[publish]"``) for ``boto3``; callers guard the import and raise a
`SourceCoopError` with that hint when it is absent.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

import boto3
import structlog
from botocore.config import Config

from ..exceptions import SourceCoopError

logger = structlog.get_logger(__name__)

# Explicit content types so the data proxy serves the files sensibly (rasterio/GDAL and
# browsers both key off these); fall back to a guess, then octet-stream.
_CONTENT_TYPES = {
    ".tif": "image/tiff",
    ".parquet": "application/octet-stream",
    ".json": "application/json",
    ".md": "text/markdown",
    ".png": "image/png",
}
_CREDENTIAL_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower()) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )


class SourceCoopPublisher:
    """Minimal S3 uploader for a Source Cooperative repository (path-style, STS creds)."""

    def __init__(self, *, endpoint: str, region: str) -> None:
        """Build an S3 client for the data-proxy endpoint (creds from the environment)."""
        missing = [k for k in _CREDENTIAL_ENV if not os.environ.get(k)]
        if missing:
            raise SourceCoopError(
                "Missing AWS credentials for Source Cooperative: "
                f"{', '.join(missing)}. Add them to .env or export them (product page "
                "-> View Credentials -> Environment Variables), then retry."
            )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}),
        )

    def upload_files(
        self, files: list[Path], *, bucket: str, key_prefix: str
    ) -> list[str]:
        """Upload each file to ``s3://{bucket}/{key_prefix}/{name}``; return the keys."""
        keys = []
        for path in files:
            key = f"{key_prefix.strip('/')}/{path.name}"
            self._put(path, bucket=bucket, key=key)
            keys.append(key)
        return keys

    def upload_readme(self, path: Path, *, bucket: str, repository: str) -> str:
        """Upload a README to the repository root (Source Coop's product landing page)."""
        key = f"{repository.strip('/')}/README.md"
        self._put(path, bucket=bucket, key=key)
        return key

    def _put(self, path: Path, *, bucket: str, key: str) -> None:
        logger.info("source_coop_upload", key=key, bytes=path.stat().st_size)
        try:
            self._client.upload_file(
                str(path),
                bucket,
                key,
                ExtraArgs={"ContentType": _content_type(path)},
            )
        except Exception as exc:  # boto3/botocore raise many error types; unify them
            raise SourceCoopError(
                f"upload failed for s3://{bucket}/{key}: {exc}"
            ) from exc
