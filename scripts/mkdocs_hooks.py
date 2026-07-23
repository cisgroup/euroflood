"""MkDocs build hooks for the EuroFlood documentation.

Injects the installed ``euroflood`` version into the site at build time, so the docs
always show the version they were built from without hardcoding it anywhere. On the
deployed site this is the released version (docs are built and deployed from the
release tag); locally it is the working-tree version.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    EUROFLOOD_VERSION = version("euroflood")
except PackageNotFoundError:  # pragma: no cover - docs are always built in-env
    EUROFLOOD_VERSION = "dev"


def on_config(config: Any, **kwargs: Any) -> Any:
    """Show the built euroflood version in the site footer."""
    config.copyright = (
        f"EuroFlood v{EUROFLOOD_VERSION} · MIT-licensed · "
        "index & flood-depth maps © JRC / Copernicus (CC-BY-4.0)"
    )
    return config


def on_page_markdown(markdown: str, **kwargs: Any) -> str:
    """Replace the ``{{ euroflood_version }}`` token with the built version."""
    return markdown.replace("{{ euroflood_version }}", EUROFLOOD_VERSION)
