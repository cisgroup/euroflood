#!/usr/bin/env python
"""Fail if the declared versions have drifted apart.

`pyproject.toml` is the single source of truth for the library version, and the release
workflow already checks the git tag against it. `CITATION.cff` was not checked by
anything -- RELEASING.md called it out as "bump by hand" -- and it has been stale before.
That matters more now that it is the citation record backing the Zenodo software archive:
a stale version there mislabels a permanently published DOI.

    python scripts/check_versions.py
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pyproject_version() -> str:
    """Return ``project.version`` from pyproject.toml."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    version: str = data["project"]["version"]
    return version


def citation_version() -> str:
    """Return the top-level ``version:`` from CITATION.cff.

    Parsed with a regex rather than a YAML library so this stays dependency-free and
    runnable in a bare release environment.

    Raises:
        SystemExit: If no top-level version field is present.
    """
    text = (ROOT / "CITATION.cff").read_text()
    match = re.search(r'(?m)^version:\s*"?([^"\s]+)"?\s*$', text)
    if not match:
        raise SystemExit("CITATION.cff has no top-level 'version:' field")
    return match.group(1)


def main() -> None:
    """Compare the two and exit non-zero on a mismatch."""
    pp, cff = pyproject_version(), citation_version()
    if pp != cff:
        raise SystemExit(
            f"version drift: pyproject.toml is {pp!r} but CITATION.cff is {cff!r}. "
            "Bump CITATION.cff (version + date-released) to match."
        )
    print(f"versions agree: {pp}")
    sys.exit(0)


if __name__ == "__main__":
    main()
