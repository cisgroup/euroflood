"""EuroFlood: tooling for European Satellite-Derived Flood Depth Maps.

Importing this package installs a **quiet-by-default** logging setup: the library is
silent (structlog is routed through a `NullHandler` and pooch is
quieted) unless you call `setup_logging` (the CLI does this
automatically). It never hijacks the root logger, so it is safe to import inside host
applications and notebooks.

The visualization helpers (``plot``, ``explore``, ``plot_depth``,
``explore_depth``, ``open_depth``) are exposed lazily via :pep:`562`
``__getattr__`` so that ``import euroflood`` never pulls in matplotlib/folium;
they require the ``viz`` extra (``pip install "euroflood[viz]"``). The producer
pipelines (``ExportPipeline``, ``IngestionPipeline``) are likewise lazy so the
consumer import never drags in the scraper/BeautifulSoup stack.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version
from typing import TYPE_CHECKING, Any

from .api import download, floods, hazard, mirror_hazard
from .config import settings
from .logging import _install_quiet_default, setup_logging
from .pipelines.discovery import FloodFrame

# Quiet by default (structlog + pooch); call setup_logging() to opt into logs.
_install_quiet_default()

try:
    __version__ = _version("euroflood")
except PackageNotFoundError:  # pragma: no cover - source checkout without an install
    __version__ = "0.0.0"

if TYPE_CHECKING:
    from .pipelines.export import ExportPipeline
    from .pipelines.ingestion import IngestionPipeline
    from .viz import (
        DepthRaster,
        explore,
        explore_depth,
        footprints,
        open_depth,
        plot,
        plot_depth,
    )

# Names served lazily from euroflood.viz (heavy optional deps loaded on demand).
_VIZ_EXPORTS = frozenset(
    {
        "plot",
        "explore",
        "plot_depth",
        "explore_depth",
        "footprints",
        "open_depth",
        "DepthRaster",
    }
)

# Producer pipelines served lazily so ``import euroflood`` skips the scraper/bs4 stack.
_PRODUCER_EXPORTS = frozenset({"ExportPipeline", "IngestionPipeline"})

__all__ = [
    "DepthRaster",
    "ExportPipeline",
    "FloodFrame",
    "IngestionPipeline",
    "__version__",
    "download",
    "explore",
    "explore_depth",
    "floods",
    "footprints",
    "hazard",
    "mirror_hazard",
    "open_depth",
    "plot",
    "plot_depth",
    "settings",
    "setup_logging",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve viz and producer-pipeline exports on first use (PEP 562)."""
    if name in _VIZ_EXPORTS:
        from . import viz

        return getattr(viz, name)
    if name in _PRODUCER_EXPORTS:
        if name == "ExportPipeline":
            from .pipelines.export import ExportPipeline

            return ExportPipeline
        from .pipelines.ingestion import IngestionPipeline

        return IngestionPipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
