"""Zenodo REST client for publishing the index as a citable DOI + full-mirror archive.

Zenodo is the *archival* endpoint, not the live query endpoint (it does not serve HTTP
Range + CORS on one URL). This wraps the deposition workflow: create a deposition ->
upload the bundle files to its bucket -> set metadata -> publish -> obtain the DOI.
Always exercise ``sandbox=True`` (``sandbox.zenodo.org``, throwaway DOIs) before the real
``zenodo.org``.

Two entry points, and the difference matters for citation:

- `create_and_publish` starts a **brand-new record**, which mints a brand-new *concept*
  DOI. Correct only for the very first publication of a dataset.
- `publish_new_version` adds a version to an **existing** record. Zenodo mints a fresh
  *version* DOI while the *concept* DOI keeps resolving to the latest, so every index
  release stays reachable under one citable identifier. This is the right call for every
  release after the first; using `create_and_publish` instead would strand the existing
  concept DOI on the old version forever.
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

    def new_version(self, record_id: int) -> dict[str, Any]:
        """Open a draft for a new version of an existing published record.

        Zenodo keeps the record's *concept* DOI pointing at the latest version and mints a
        fresh version DOI on publish, so the citable identifier survives across releases.
        The draft **inherits the previous version's files**; call `clear_files` before
        uploading replacements.

        Args:
            record_id: Deposition id of any published version of the record.

        Returns:
            The draft deposition JSON (including ``id`` and ``links.bucket``).

        Raises:
            ZenodoError: If Zenodo does not hand back a draft link.
        """
        data: dict[str, Any] = self._request(
            "POST", f"{self.base}/deposit/depositions/{record_id}/actions/newversion"
        ).json()
        draft_url = data.get("links", {}).get("latest_draft")
        if not draft_url:
            raise ZenodoError(
                f"Zenodo newversion for {record_id} returned no links.latest_draft"
            )
        draft: dict[str, Any] = self._request("GET", draft_url).json()
        return draft

    def list_files(self, deposition_id: int) -> list[dict[str, Any]]:
        """Return the files currently attached to a deposition."""
        data: list[dict[str, Any]] = self._request(
            "GET", f"{self.base}/deposit/depositions/{deposition_id}/files"
        ).json()
        return data

    def delete_file(self, deposition_id: int, file_id: str) -> None:
        """Remove one file from a draft deposition."""
        self._request(
            "DELETE",
            f"{self.base}/deposit/depositions/{deposition_id}/files/{file_id}",
        )

    def clear_files(self, deposition_id: int) -> int:
        """Delete every file inherited by a draft; returns how many were removed.

        A new-version draft starts as a copy of the previous version, so the stale bundle
        must go before the current one is uploaded, otherwise the record would carry both.
        """
        files = self.list_files(deposition_id)
        for entry in files:
            self.delete_file(deposition_id, entry["id"])
        if files:
            logger.info("zenodo_cleared_inherited_files", count=len(files))
        return len(files)

    def _finalize(
        self,
        deposition_id: int,
        bucket: str,
        files: list[Path],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Upload, set metadata, publish; return the common result mapping."""
        for path in files:
            self.upload_file(bucket, path)
        self.set_metadata(deposition_id, metadata)
        record = self.publish(deposition_id)
        links = record.get("links", {})
        return {
            "doi": record.get("doi"),
            "concept_doi": record.get("conceptdoi"),
            "record_url": links.get("record_html") or links.get("html"),
            "deposition_id": deposition_id,
        }

    def create_and_publish(
        self, files: list[Path], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """Publish a **brand-new** record; returns ``{doi, concept_doi, record_url, deposition_id}``.

        This mints a new concept DOI, so use it only for a dataset's first publication.
        To add a release to an existing record, use `publish_new_version`.
        """
        dep = self.create_deposition()
        return self._finalize(dep["id"], dep["links"]["bucket"], files, metadata)

    def publish_new_version(
        self, record_id: int, files: list[Path], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        """Publish a new version of an existing record, preserving its concept DOI.

        Opens a new-version draft, drops the files inherited from the previous version,
        uploads the current bundle, then publishes.

        Args:
            record_id: Deposition id of any published version of the record.
            files: The bundle files to attach to this version.
            metadata: Record metadata, including the new ``version``.

        Returns:
            ``{doi, concept_doi, record_url, deposition_id}`` for the new version.
        """
        draft = self.new_version(record_id)
        deposition_id = int(draft["id"])
        self.clear_files(deposition_id)
        return self._finalize(deposition_id, draft["links"]["bucket"], files, metadata)
