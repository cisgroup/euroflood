#!/usr/bin/env python
"""Archive a released euroflood version on Zenodo (called from the release workflow).

Attaches the built sdist + wheel -- the same artifacts that go to PyPI -- to the
library's Zenodo record, so a release is citable by DOI as well as installable.

    python scripts/zenodo_software_release.py --version 0.3.0 --dist dist/ --dry-run

Target selection mirrors `publish_software_to_zenodo`: pass ``--draft-id`` for a
record's first release (honouring a pre-reserved DOI) or ``--concept-recid`` for every
release after it. Creating a fresh record is deliberately not offered, because it would
mint a new concept DOI and orphan the existing one.

Credentials come from ``ZENODO_TOKEN`` (or ``ZENODO_SANDBOX_TOKEN`` with ``--sandbox``)
in the environment or a local ``.env``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from euroflood.pipelines.publish import (
    SOFTWARE_CONCEPT_DOI,
    publish_software_to_zenodo,
)


def collect_artifacts(dist: Path) -> list[Path]:
    """Return the sdist and wheel in ``dist``, newest build only.

    Raises:
        SystemExit: If either artifact is missing, since a half-archived release is
            worse than none.
    """
    sdists = sorted(dist.glob("*.tar.gz"))
    wheels = sorted(dist.glob("*.whl"))
    if not sdists or not wheels:
        raise SystemExit(
            f"expected one sdist and one wheel in {dist}; "
            f"found {len(sdists)} sdist(s), {len(wheels)} wheel(s)"
        )
    return [*sdists, *wheels]


def main() -> None:
    """Parse arguments and archive the release."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", required=True, help="Released library version")
    ap.add_argument(
        "--dist",
        type=Path,
        default=Path("dist"),
        help="Directory holding the artifacts",
    )
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--draft-id",
        type=int,
        help="Reserved, unpublished draft to fill (a record's FIRST release)",
    )
    group.add_argument(
        "--concept-recid",
        type=int,
        help=f"Concept record id for later releases (euroflood: {SOFTWARE_CONCEPT_DOI})",
    )
    ap.add_argument("--sandbox", action="store_true", help="Use sandbox.zenodo.org")
    ap.add_argument(
        "--dry-run", action="store_true", help="Report the plan, upload nothing"
    )
    args = ap.parse_args()

    files = collect_artifacts(args.dist)
    result = publish_software_to_zenodo(
        files,
        version=args.version,
        sandbox=args.sandbox,
        draft_id=args.draft_id,
        concept_recid=args.concept_recid,
        dry_run=args.dry_run,
    )
    if result.get("dry_run"):
        print(
            f"[dry-run] archive {result['version']} -> Zenodo {result['target']} "
            f"[{result['mode']}] ({len(result['files'])} files: "
            f"{', '.join(result['files'])})"
        )
        return
    print(
        f"Archived {args.version} on Zenodo: DOI {result['doi']} "
        f"(concept {result['concept_doi']}) -> {result['record_url']}"
    )


if __name__ == "__main__":
    main()
