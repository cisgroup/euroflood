"""Static (matplotlib) plots: recurrence, depth, and context views."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd

from ..services.raster_ops import is_hazard
from ._colormaps import (
    DEPTH_CMAP,
    RECURRENCE_CMAP,
    mask_nodata,
    year_colors,
)
from ._deps import require
from ._raster import depth_array, event_footprints, recurrence_grid, valid_bounds


def _zoom_to(ax: Any, bounds: Any) -> None:
    """Set the axes view to ``(west, south, east, north)`` with a small margin."""
    if bounds is None:
        return
    west, south, east, north = bounds
    margin = 0.05 * max(east - west, north - south) or 0.001
    ax.set_xlim(west - margin, east + margin)
    ax.set_ylim(south - margin, north + margin)


def _new_ax(ax: Any) -> Any:
    """Return the given axes, or create a fresh figure/axes."""
    if ax is not None:
        return ax
    plt = require("matplotlib.pyplot")
    _, ax = plt.subplots()
    return ax


def _draw_boundary(ax: Any, roi: Any) -> None:
    """Outline the query ROI on the axes (handles Polygon/MultiPolygon)."""
    gpd.GeoSeries([roi], crs="EPSG:4326").boundary.plot(
        ax=ax, color="black", linewidth=1.0
    )


_STATIC_TILES = {
    "grayscale": "CartoDB.Positron",
    "greyscale": "CartoDB.Positron",
    "gray": "CartoDB.Positron",
    "grey": "CartoDB.Positron",
    "light": "CartoDB.Positron",
    "dark": "CartoDB.DarkMatter",
    "osm": "OpenStreetMap.Mapnik",
    "openstreetmap": "OpenStreetMap.Mapnik",
}


def _add_basemap(ax: Any, *, tiles: str = "OpenStreetMap") -> None:
    """Add a contextily XYZ basemap under a WGS84 axes (needs network).

    ``tiles`` accepts a preset (``"grayscale"``, ``"dark"``, ...) or a contextily
    provider path such as ``"CartoDB.Positron"``.
    """
    cx = require("contextily")
    key = _STATIC_TILES.get(str(tiles).lower(), tiles)
    provider = cx.providers
    for part in key.split("."):
        provider = getattr(provider, part)
    # zorder=-1 keeps the basemap *behind* the flood raster (an AxesImage at the
    # default zorder 0); contextily otherwise draws it on top and mutes the overlay.
    cx.add_basemap(ax, crs="EPSG:4326", source=provider, attribution_size=6, zorder=-1)


def _recurrence_title(event_id: int | None) -> str:
    if event_id is not None:
        return f"Flood footprint — event {event_id}"
    return "Flood recurrence (recorded floods per pixel)"


def plot_recurrence(
    frame: Any,
    *,
    event_id: int | None = None,
    boundary: bool = True,
    basemap: bool = False,
    tiles: str = "OpenStreetMap",
    ax: Any = None,
    cmap: str | None = None,
    vmax: float | None = None,
    **kwargs: Any,
) -> Any:
    """Plot the flood-recurrence heatmap for a historic catalogue's ROI.

    Darker = more recorded floods. Never-flooded pixels (0) are transparent. No
    source raster is downloaded — the counts come from the index.

    Returns:
        The matplotlib ``Axes``.
    """
    require("matplotlib")
    from rasterio.plot import plotting_extent

    counts, transform, _crs, roi = recurrence_grid(frame, event_id=event_id)
    ax = _new_ax(ax)
    # Integer count: scale to the true max (like the interactive map). Use
    # ax.imshow, NOT rasterio.plot.show, which rescales the data to [0, 1] and so
    # collapses every count to the palest colour under our vmin/vmax.
    top = vmax if vmax is not None else max(1, int(counts.max()))
    ax.imshow(
        mask_nodata(counts),
        extent=plotting_extent(counts, transform),
        origin="upper",
        cmap=cmap or RECURRENCE_CMAP,
        vmin=1,
        vmax=top,
        **kwargs,
    )
    images = ax.get_images()
    if images:
        label = "flooded" if event_id is not None else "recorded floods"
        ax.figure.colorbar(images[0], ax=ax, label=label)
    if boundary:
        _draw_boundary(ax, roi)
    if basemap:
        _add_basemap(ax, tiles=tiles)
    ax.set_title(_recurrence_title(event_id))
    return ax


def plot_footprints(
    frame: Any,
    *,
    boundary: bool = True,
    basemap: bool = False,
    tiles: str = "OpenStreetMap",
    ax: Any = None,
    column: str = "year",
    cmap: str | None = None,
    alpha: float = 0.4,
    legend: bool = True,
    **kwargs: Any,
) -> Any:
    """Plot each event's flood extent as a semi-transparent overlay colored by year.

    Extents come from the index (no download) and overlap where events share ground.
    Returns the matplotlib ``Axes``.
    """
    require("matplotlib")
    from matplotlib.patches import Patch

    gdf = event_footprints(frame)  # date-sorted, empties dropped
    ax = _new_ax(ax)
    colour_col = column if column in gdf.columns else "event_id"
    year_map = year_colors(gdf[colour_col].dropna().tolist(), cmap=cmap or "tab20")

    def _colour(value: Any) -> str:
        try:
            return year_map.get(int(value), "#3186cc")
        except (TypeError, ValueError):
            return "#3186cc"

    gdf.plot(
        ax=ax,
        color=[_colour(v) for v in gdf[colour_col]],
        alpha=alpha,
        edgecolor="none",
        **kwargs,
    )
    if legend and year_map:
        handles = [Patch(facecolor=c, label=str(v)) for v, c in year_map.items()]
        ax.legend(handles=handles, title=colour_col, fontsize="small")
    if boundary:
        _draw_boundary(ax, frame.geometry.iloc[0])
    # Zoom to the footprints, not the (much larger) query ROI boundary.
    if len(gdf):
        _zoom_to(ax, gdf.total_bounds)
    if basemap:
        _add_basemap(ax, tiles=tiles)
    ax.set_title("Flood extents by event")
    return ax


def plot_depth(
    path: str | Path,
    *,
    ax: Any = None,
    cmap: str | None = None,
    vmax: float | None = None,
    scale: float = 0.01,
    basemap: bool = False,
    tiles: str = "OpenStreetMap",
    title: str | None = None,
    **kwargs: Any,
) -> Any:
    """Plot a downloaded flood-depth GeoTIFF (metres) with a colorbar.

    Args:
        path: A downloaded depth GeoTIFF.
        ax: An existing Axes to draw on (a new figure/axes is made if ``None``).
        cmap: Colormap name (default ``"Blues"``).
        vmax: Upper depth bound; defaults to the image's max depth (full range).
        scale: Multiplier applied to the raster values. EFAS depth is stored in
            centimetres, so the default ``0.01`` converts to metres; pass ``1.0``
            for a raster already in metres.
        basemap: Add a contextily basemap (needs network); default off.
        tiles: Basemap style when ``basemap=True`` (e.g. ``"grayscale"``).
        title: Plot title.
        **kwargs: Forwarded to ``ax.imshow``.

    Returns:
        The matplotlib ``Axes``.
    """
    require("matplotlib")
    from rasterio.plot import plotting_extent

    # depth_array reprojects to EPSG:4326 (lat/lon). Use ax.imshow, NOT
    # rasterio.plot.show, which rescales the data to [0, 1] and washes it out.
    array, transform, _crs, nodata = depth_array(path)
    ax = _new_ax(ax)
    masked = mask_nodata(array, nodata) * scale  # cm -> m
    top = vmax if vmax is not None else float(masked.max())
    ax.imshow(
        masked,
        extent=plotting_extent(array, transform),
        origin="upper",
        cmap=cmap or DEPTH_CMAP,
        vmin=0,
        vmax=top,
        **kwargs,
    )
    _zoom_to(ax, valid_bounds(masked, transform))  # zoom to the wet area
    images = ax.get_images()
    if images:
        ax.figure.colorbar(images[0], ax=ax, label="Water depth (m)")
    if basemap:
        _add_basemap(ax, tiles=tiles)
    ax.set_title(title or f"Flood depth — {Path(path).stem}")
    return ax


def _context_title(frame: Any) -> str:
    n = len(frame)
    if is_hazard(frame):
        rps = sorted({int(r) for r in frame["return_period"].tolist()}) if n else []
        return f"Hazard layers — RP {', '.join(map(str, rps))} yr" if rps else "Hazard"
    return f"Flood catalogue — {n} event(s)"


def plot_context(
    frame: Any,
    *,
    boundary: bool = True,
    basemap: bool = False,
    tiles: str = "OpenStreetMap",
    ax: Any = None,
    **kwargs: Any,
) -> Any:
    """Plot a cheap context view: the query ROI + a summarizing title.

    Used as the default for hazard frames (which have no recurrence index) and as
    a fallback. Returns the matplotlib ``Axes``.
    """
    require("matplotlib")
    ax = _new_ax(ax)
    if len(frame):
        roi = frame.geometry.iloc[0]
        gpd.GeoSeries([roi], crs="EPSG:4326").plot(
            ax=ax, facecolor="tab:blue", alpha=0.15, edgecolor="tab:blue"
        )
        if boundary:
            _draw_boundary(ax, roi)
    if basemap:
        _add_basemap(ax, tiles=tiles)
    ax.set_title(_context_title(frame))
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    return ax
