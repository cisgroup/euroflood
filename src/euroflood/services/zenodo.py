"""Zenodo REST client for publishing the index as a citable DOI + full-mirror archive.

Zenodo is the *archival* endpoint, not the live query endpoint (it does not serve HTTP
Range + CORS on one URL). This wraps the deposition workflow: create a deposition ->
upload the bundle files to its bucket -> set metadata -> publish -> obtain the DOI.
Always exercise ``sandbox=True`` (``sandbox.zenodo.org``, throwaway DOIs) before the real
``zenodo.org``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests
import structlog

logger = structlog.get_logger(__name__)

_PROD = "https://zenodo.org/api"
_SANDBOX = "https://sandbox.zenodo.org/api"


class ZenodoError(RuntimeError):
    """A Zenodo API request failed (non-2xx), carrying the response detail."""


class ZenodoPublisher:
    """Minimal Zenodo deposition client (create -> upload -> metadata -> publish)."""

    def __init__(
        self, token: str, *, sandbox: bool = False, timeout: int = 600
    ) -> None:
        """Bind to a token + endpoint (sandbox or production)."""
        self.base = _SANDBOX if sandbox else _PROD
        self.sandbox = sandbox
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"Bearer {token}"

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
        if not resp.ok:
            raise ZenodoError(
                f"Zenodo {method} {url} -> {resp.status_code}: {resp.text[:500]}"
            )
        return resp

    def create_deposition(self) -> dict[str, Any]:
        """Create an empty draft deposition; returns its JSON (incl. ``links.bucket``)."""
        data: dict[str, Any] = self._request(
            "POST", f"{self.base}/deposit/depositions", json={}
        ).json()
        return data

    def upload_file(self, bucket_url: str, path: Path) -> None:
        """Stream one file into the deposition's bucket (files API)."""
        logger.info("zenodo_upload", file=path.name, bytes=path.stat().st_size)
        with open(path, "rb") as fh:
            self._request("PUT", f"{bucket_url}/{path.name}", data=fh)

    def set_metadata(self, deposition_id: int, metadata: dict[str, Any]) -> None:
        """Attach the record metadata (title, creators, license, ...)."""
        self._request(
            "PUT",
            f"{self.base}/deposit/depositions/{deposition_id}",
            json={"metadata": metadata},
        )

    def publish(self, deposition_id: int) -> dict[str, Any]:
        """Publish the deposition -> mints the DOI. Returns the record JSON."""
        data: dict[str, Any] = self._request(
            "POST", f"{self.base}/deposit/depositions/{deposition_id}/actions/publish"
        ).json()
        return data

    def create_and_publish(
        self, files: list[Path], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the full flow and return ``{doi, concept_doi, record_url, deposition_id}``."""
        dep = self.create_deposition()
        bucket = dep["links"]["bucket"]
        for path in files:
            self.upload_file(bucket, path)
        self.set_metadata(dep["id"], metadata)
        record = self.publish(dep["id"])
        links = record.get("links", {})
        return {
            "doi": record.get("doi"),
            "concept_doi": record.get("conceptdoi"),
            "record_url": links.get("record_html") or links.get("html"),
            "deposition_id": dep["id"],
        }
