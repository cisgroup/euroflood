"""Minimal support shim for the case-study notebooks under ``case_studies/``.

These notebooks are reproduced verbatim from the EuroFlood paper. In the docs they
are rendered from their committed outputs (``mkdocs-jupyter`` with
``execute: false``) and they are deliberately *not* executed in CI: their external
inputs (OSM networks, HANZE, GISCO NUTS; ~80 MB) and the full reproducible pipeline
live in the paper repository, not in this package.

This module only provides what a notebook cannot do on its own: anchor
repo-relative paths and display the paper's published figure for comparison. It
imports no ``euroflood``. It is a self-contained copy of the paper's
``notebooks/_support.py`` (which anchors on the paper repo layout) so the notebooks
can stay byte-identical to their published versions.
"""

from __future__ import annotations

from pathlib import Path

# This file is case_studies/_support.py.
ROOT = Path(__file__).resolve().parent          # the case_studies/ directory
DATA = ROOT / "data"                            # external inputs (not vendored; see the paper repo)
RESULTS = ROOT / "results"                      # (unused here; kept for signature parity)
OUT = ROOT / "out"                              # scratch, if a reader re-runs (gitignored)
IMAGES = ROOT.parent / "docs" / "images" / "paper"  # the vendored published figures

__all__ = ["ROOT", "DATA", "RESULTS", "OUT", "IMAGES", "published_figure"]


def published_figure(name: str):
    """Display the paper's published figure ``docs/images/paper/fig-<name>.png`` inline."""
    from IPython.display import Image

    return Image(filename=str(IMAGES / f"fig-{name}.png"))
