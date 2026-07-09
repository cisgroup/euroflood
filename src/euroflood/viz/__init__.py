"""Visualization for EuroFlood: static (matplotlib) + interactive (folium).

Public entry points (also re-exported lazily from the top-level ``euroflood``):

- `plot` / `explore` — dispatch on a ``FloodFrame``, a depth GeoTIFF
  path, a list of paths, or a `DepthRaster`.
- `plot_depth` / `explore_depth` — render an already-downloaded depth
  raster (no download).
- `open_depth` — wrap a depth GeoTIFF as a `DepthRaster`.

The **default** view for a historic ``FloodFrame`` is the cheap flood-recurrence
heatmap (no download). The depth raster — which requires downloading tiles — is
opt-in via ``depth=True`` and guarded by a row-count cap.

All heavy imports (matplotlib/folium) are lazy; without the ``viz`` extra any
entry point raises a friendly ``pip install "euroflood[viz]"`` error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..exceptions import VisualizationError
from ..services.raster_ops import is_hazard
from ._interactive import (
    _check_backend,
    explore_context,
    explore_depth,
    explore_footprints,
    explore_recurrence,
)
from ._raster import depth_array
from ._raster import event_footprints as footprints
from ._static import plot_context, plot_depth, plot_footprints, plot_recurrence

__all__ = [
    "DepthRaster",
    "explore",
    "explore_depth",
    "footprints",
    "open_depth",
    "plot",
    "plot_depth",
]

# Guardrail: the most depth rasters `depth=True` will auto-download before it
# insists on an explicit `limit=` (or a narrower filter / event_id=).
_DEFAULT_DEPTH_LIMIT = 4


@dataclass(frozen=True)
class DepthRaster:
    """A downloaded flood-depth GeoTIFF with plot/explore/read/save helpers."""

    path: Path

    def read(self) -> tuple[Any, Any, Any, float | None]:
        """Read the raster into ``(array, transform, crs, nodata)``."""
        return depth_array(self.path)

    def plot(self, **kwargs: Any) -> Any:
        """Static depth plot (matplotlib ``Axes``)."""
        return plot_depth(self.path, **kwargs)

    def explore(self, **kwargs: Any) -> Any:
        """Interactive depth map (``folium.Map``)."""
        return explore_depth(self.path, **kwargs)

    def save(self, out: str | Path) -> Path:
        """Render and save to ``.png`` (static) or ``.html`` (interactive)."""
        out = Path(out)
        if out.suffix.lower() in (".html", ".htm"):
            self.explore().save(str(out))
        else:
            self.plot().figure.savefig(out)
        return out


def open_depth(path: str | Path) -> DepthRaster:
    """Wrap an already-downloaded depth GeoTIFF as a `DepthRaster`."""
    return DepthRaster(Path(path))


def _download_for_depth(
    frame: Any, *, event_id: int | None, output_dir: Any, limit: int | None
) -> list[Path]:
    """Download depth rasters for the frame under a row-count guardrail."""
    sub = frame
    if event_id is not None and "event_id" in frame.columns:
        sub = frame[frame["event_id"] == int(event_id)]
    n = len(sub)
    if n == 0:
        raise VisualizationError("No matching events to download for a depth view.")
    # Already downloaded? Reuse the frame's own rasters (no re-download, and the
    # cap only guards *new* downloads, so it doesn't apply here).
    existing = list(getattr(sub, "files", []))
    if len(existing) == n:
        return [Path(p) for p in existing]
    cap = limit if limit is not None else _DEFAULT_DEPTH_LIMIT
    if n > cap:
        raise VisualizationError(
            f"depth=True would download {n} rasters (cap {cap}). Pass limit=N, "
            "filter the catalogue, or select one with event_id= to confirm."
        )
    # .download() returns the frame (cached crops reused); read its file paths.
    paths = sub.download(output_dir).files
    if not paths:
        raise VisualizationError("Depth download produced no rasters.")
    return [Path(p) for p in paths]


def _depth_target(
    frame: Any, *, event_id: int | None, output_dir: Any, limit: int | None
) -> tuple[Path, int | None]:
    """Resolve the depth raster to render: one event, or a max-composite of many.

    Returns ``(path, n)`` where ``n`` is the number of events aggregated (``None``
    for a single event).
    """
    paths = _download_for_depth(
        frame, event_id=event_id, output_dir=output_dir, limit=limit
    )
    if len(paths) == 1:
        return paths[0], None
    from ._raster import max_composite

    return max_composite(paths), len(paths)


def plot_frame(
    frame: Any,
    *,
    depth: bool = False,
    footprints: bool = False,
    event_id: int | None = None,
    boundary: bool = True,
    output_dir: Any = None,
    limit: int | None = None,
    **kwargs: Any,
) -> Any:
    """Static plot for a ``FloodFrame``: recurrence (default), footprints, depth, context."""
    if depth:
        from ..services.statistics import depth_scale_for

        target, n = _depth_target(
            frame, event_id=event_id, output_dir=output_dir, limit=limit
        )
        if n and "title" not in kwargs:
            kwargs["title"] = f"Flood depth — max of {n} events"
        kwargs.setdefault("scale", depth_scale_for(frame))  # cm (EFAS) vs m (hazard)
        return plot_depth(target, **kwargs)
    if footprints:
        return plot_footprints(frame, boundary=boundary, **kwargs)
    if is_hazard(frame):
        return plot_context(frame, boundary=boundary, **kwargs)
    return plot_recurrence(frame, event_id=event_id, boundary=boundary, **kwargs)


def explore_frame(
    frame: Any,
    *,
    depth: bool = False,
    footprints: bool = False,
    event_id: int | None = None,
    boundary: bool = True,
    backend: str = "folium",
    output_dir: Any = None,
    limit: int | None = None,
    **kwargs: Any,
) -> Any:
    """Interactive map for a ``FloodFrame``: recurrence (default), footprints, depth, context."""
    _check_backend(backend)
    if depth:
        from ..services.statistics import depth_scale_for

        target, _n = _depth_target(
            frame, event_id=event_id, output_dir=output_dir, limit=limit
        )
        geom = frame.geometry.iloc[0] if (boundary and len(frame)) else None
        kwargs.setdefault("scale", depth_scale_for(frame))  # cm (EFAS) vs m (hazard)
        return explore_depth(target, boundary_geom=geom, **kwargs)
    if footprints:
        return explore_footprints(frame, boundary=boundary, **kwargs)
    if is_hazard(frame):
        return explore_context(frame, boundary=boundary, **kwargs)
    return explore_recurrence(frame, event_id=event_id, boundary=boundary, **kwargs)


def plot(obj: Any, **kwargs: Any) -> Any:
    """Static plot dispatching on a frame, a depth path/list, or a DepthRaster."""
    if isinstance(obj, DepthRaster):
        return obj.plot(**kwargs)
    if isinstance(obj, str | Path):
        return plot_depth(obj, **kwargs)
    if isinstance(obj, list):
        if not obj:
            raise VisualizationError("Empty path list — nothing to plot.")
        return plot_depth(obj[0], **kwargs)
    return plot_frame(obj, **kwargs)


def explore(obj: Any, **kwargs: Any) -> Any:
    """Interactive map dispatching on a frame, a depth path/list, or a DepthRaster."""
    if isinstance(obj, DepthRaster):
        return obj.explore(**kwargs)
    if isinstance(obj, str | Path):
        return explore_depth(obj, **kwargs)
    if isinstance(obj, list):
        if not obj:
            raise VisualizationError("Empty path list — nothing to explore.")
        return explore_depth(obj[0], **kwargs)
    return explore_frame(obj, **kwargs)
