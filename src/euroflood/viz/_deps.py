"""Lazy loading of the optional visualization dependencies.

Keeping these imports lazy (inside functions, via `require`) means the base
library imports without matplotlib/folium, and a missing ``viz`` extra produces a
friendly, actionable error instead of a bare ``ModuleNotFoundError``.
"""

from __future__ import annotations

import importlib
from typing import Any


def require(module: str) -> Any:
    """Import an optional viz dependency, or raise an install hint.

    Args:
        module: The importable module name (e.g. ``"matplotlib.pyplot"``).

    Returns:
        The imported module.

    Raises:
        ImportError: If the module (i.e. the ``viz`` extra) is not installed.
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        top = module.split(".")[0]
        raise ImportError(
            f"euroflood visualization requires '{top}'. "
            'Install the viz extra:  pip install "euroflood[viz]"'
        ) from exc
