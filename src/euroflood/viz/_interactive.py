"""Interactive (folium) maps: recurrence, depth, footprints, and context views.

folium is geopandas' own ``.explore()`` engine: it emits a self-contained HTML
map that renders inline in notebooks and needs no build server. Basemap tiles are
referenced (fetched by the browser), so nothing is downloaded at render time.

All maps share one layout (built via `_new_map`): a **toggleable basemap**
(with a grayscale option), the data layer(s), a **toggleable "query area"
boundary**, a colour **legend**, and a layer control.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import numpy as np

from ._colormaps import DEPTH_CMAP, RECURRENCE_CMAP, mask_nodata, year_colors
from ._deps import require
from ._raster import (
    depth_array,
    event_footprints,
    recurrence_regions,
    valid_bounds,
)

_SUPPORTED_BACKENDS = ("folium",)

# Basemap presets — including a grayscale option (CartoDB Positron).
_TILE_PRESETS = {
    "grayscale": "CartoDB positron",
    "greyscale": "CartoDB positron",
    "gray": "CartoDB positron",
    "grey": "CartoDB positron",
    "light": "CartoDB positron",
    "dark": "CartoDB dark_matter",
    "osm": "OpenStreetMap",
}

_BOUNDARY_STYLE = {"color": "#555", "weight": 2, "dashArray": "5, 5", "fill": False}


def _check_backend(backend: str) -> None:
    if backend not in _SUPPORTED_BACKENDS:
        raise ValueError(
            f"Unknown interactive backend {backend!r}; supported: "
            f"{', '.join(_SUPPORTED_BACKENDS)} (lonboard is planned)."
        )


def _center(frame: Any) -> list[float]:
    """[lat, lon] centre of the catalogue's ROI (or Europe if empty)."""
    if len(frame):
        minx, miny, maxx, maxy = frame.geometry.iloc[0].bounds
        return [(miny + maxy) / 2, (minx + maxx) / 2]
    return [50.0, 10.0]


def _new_map(location: list[float], *, tiles: str = "OpenStreetMap") -> tuple[Any, Any]:
    """A folium Map with a single toggleable basemap layer.

    Returns ``(map, basemap_layer)``. ``tiles`` accepts a folium tile name or a
    preset (``"grayscale"``, ``"dark"``, ...).
    """
    folium = require("folium")
    provider = _TILE_PRESETS.get(str(tiles).lower(), tiles)
    m = folium.Map(location=location, tiles=None, zoom_start=9)
    base = folium.TileLayer(
        provider, name="basemap", overlay=True, control=True, show=True
    )
    base.add_to(m)
    return m, base


def _add_boundary(m: Any, frame: Any, *, control: bool = True) -> Any:
    """Add the query ROI outline (dashed grey, toggleable). Returns the layer."""
    if not len(frame):
        return None
    folium = require("folium")
    layer = folium.GeoJson(
        frame.geometry.iloc[0].__geo_interface__,
        style_function=lambda _f: _BOUNDARY_STYLE,
        name="query area",
        control=control,
        overlay=True,
        show=True,
    )
    layer.add_to(m)
    return layer


def _add_legend(m: Any, cmap_name: str, vmin: float, vmax: float, caption: str) -> None:
    """Add a branca colour legend matching a matplotlib colormap."""
    import branca.colormap as bcm
    import matplotlib
    from matplotlib.colors import to_hex

    ramp = matplotlib.colormaps[cmap_name]
    bcm.LinearColormap(
        [to_hex(ramp(x)) for x in np.linspace(0.15, 1.0, 8)],
        vmin=vmin,
        vmax=vmax if vmax > vmin else vmin + 1.0,
        caption=caption,
    ).add_to(m)


def _fit_roi(m: Any, frame: Any) -> None:
    if len(frame):
        minx, miny, maxx, maxy = frame.geometry.iloc[0].bounds
        m.fit_bounds([[miny, minx], [maxy, maxx]])


def explore_recurrence(
    frame: Any,
    *,
    event_id: int | None = None,
    boundary: bool = True,
    cmap: str | None = None,
    tiles: str = "OpenStreetMap",
    **kwargs: Any,
) -> Any:
    """Interactive flood-recurrence map with hover tooltips + a colour legend.

    Hover shows the recorded-flood count and the year range; click a region for the
    full list of dates. Returns a ``folium.Map``.
    """
    folium = require("folium")
    matplotlib = require("matplotlib")
    from matplotlib.colors import Normalize, to_hex

    gdf = recurrence_regions(frame, event_id=event_id)
    m, _base = _new_map(_center(frame), tiles=tiles)
    if len(gdf):
        cmax = max(1, int(gdf["count"].max()))
        colormap = matplotlib.colormaps[cmap or RECURRENCE_CMAP]
        norm = Normalize(vmin=1, vmax=cmax)

        def style(feature: Any) -> dict[str, Any]:
            return {
                "fillColor": to_hex(colormap(norm(feature["properties"]["count"]))),
                "color": "none",
                "fillOpacity": 0.75,
            }

        folium.GeoJson(
            gdf.__geo_interface__,
            style_function=style,
            tooltip=folium.GeoJsonTooltip(
                fields=["count", "when"],
                aliases=["Recorded floods:", "When:"],
                sticky=True,
            ),
            popup=folium.GeoJsonPopup(fields=["events"], aliases=["Flood dates:"]),
            name="flood recurrence",
        ).add_to(m)
        _add_legend(m, cmap or RECURRENCE_CMAP, 1, cmax, "Recorded floods")
    if boundary:
        _add_boundary(m, frame)
    folium.LayerControl(collapsed=True).add_to(m)
    _fit_roi(m, frame)
    return m


def explore_footprints(
    frame: Any,
    *,
    boundary: bool = True,
    cmap: str | None = None,
    tiles: str = "OpenStreetMap",
    **kwargs: Any,
) -> Any:
    """Interactive per-event flood extents, grouped into folders by year.

    Each event is a toggleable layer (coloured by year, hover tooltip with date /
    duration / area); events are foldered by year via a grouped layer control, next
    to a "Map" folder with the basemap + query boundary. Returns a ``folium.Map``.
    """
    folium = require("folium")
    require("matplotlib")
    plugins = require("folium.plugins")

    gdf = event_footprints(frame)  # date-sorted, empties dropped
    m, base = _new_map(_center(frame), tiles=tiles)
    year_map = (
        year_colors(gdf["year"].dropna().tolist(), cmap=cmap or "tab20")
        if "year" in gdf.columns
        else {}
    )

    groups: dict[str, list[Any]] = {}
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        color = "#3186cc"
        year_key = "events"
        with contextlib.suppress(TypeError, ValueError):
            year = int(row.get("year"))
            color = year_map.get(year, color)
            year_key = str(year)
        date = str(row.get("date") or "")
        eid = row.get("event_id")
        name = f"{date} · #{int(eid)}" if eid is not None else date

        bits = [f"<b>{date}</b>"] if date else []
        dur = row.get("duration_days")
        if dur is not None and dur == dur:  # not NaN
            bits.append(f"{int(dur)} days")
        extent = row.get("extent_km2")
        if extent is not None:
            bits.append(f"{extent} km² in view")
        if eid is not None:
            bits.append(f"event {int(eid)}")

        group = folium.FeatureGroup(name=name, show=True)
        folium.GeoJson(
            geom.__geo_interface__,
            style_function=lambda _f, c=color: {
                "fillColor": c,
                "color": c,
                "weight": 1,
                "fillOpacity": 0.4,
            },
            tooltip=folium.Tooltip("<br>".join(bits)),
        ).add_to(group)
        group.add_to(m)
        groups.setdefault(year_key, []).append(group)

    context = [base]
    if boundary:
        bnd = _add_boundary(m, frame)
        if bnd is not None:
            context.append(bnd)
    foldered = {"Map": context, **{y: groups[y] for y in sorted(groups)}}
    plugins.GroupedLayerControl(
        groups=foldered, exclusive_groups=False, collapsed=True
    ).add_to(m)
    _fit_roi(m, frame)
    return m


def _array_bounds(array: Any, transform: Any) -> list[list[float]]:
    """Geographic bounds ``[[south, west], [north, east]]`` for an ImageOverlay."""
    from rasterio.transform import array_bounds

    height, width = array.shape[-2], array.shape[-1]
    west, south, east, north = array_bounds(height, width, transform)
    return [[south, west], [north, east]]


def _colorize(masked: Any, cmap_name: str, vmin: float, vmax: float) -> Any:
    """Map a masked array to an RGBA uint8 image (transparent where masked)."""
    import matplotlib
    from matplotlib.colors import Normalize

    colormap = matplotlib.colormaps[cmap_name]
    norm = Normalize(vmin=vmin, vmax=vmax if vmax > vmin else vmin + 1)
    rgba = colormap(norm(np.ma.filled(masked, np.nan)))
    rgba = (rgba * 255).astype("uint8")
    rgba[..., 3] = np.where(np.ma.getmaskarray(masked), 0, 255)
    return rgba


def explore_depth(
    path: str | Path,
    *,
    cmap: str | None = None,
    vmax: float | None = None,
    scale: float = 0.01,
    opacity: float = 0.8,
    tiles: str = "OpenStreetMap",
    boundary_geom: Any = None,
    **kwargs: Any,
) -> Any:
    """Interactive flood-depth map: an ImageOverlay + legend on a toggleable basemap.

    Depth is shown in metres (EFAS values are centimetres, so ``scale`` defaults to
    0.01; pass 1.0 for a metres raster). ``boundary_geom`` (a shapely geometry) adds
    a toggleable query-area outline. Returns a ``folium.Map``.
    """
    folium = require("folium")
    require("matplotlib")

    # depth_array reprojects to EPSG:4326, so array_bounds are valid lat/lon.
    array, transform, _crs, nodata = depth_array(path)
    masked = mask_nodata(array, nodata) * scale  # cm -> m
    top = vmax if vmax is not None else float(masked.max())
    rgba = _colorize(masked, cmap or DEPTH_CMAP, 0.0, top)
    bounds = _array_bounds(array, transform)  # full tile (the overlay's extent)

    # Fit the view to the wet area, not the mostly-NoData tile.
    wet = valid_bounds(masked, transform)
    fit = [[wet[1], wet[0]], [wet[3], wet[2]]] if wet is not None else bounds
    center = [(fit[0][0] + fit[1][0]) / 2, (fit[0][1] + fit[1][1]) / 2]

    m, _base = _new_map(center, tiles=tiles)
    folium.raster_layers.ImageOverlay(
        image=rgba, bounds=bounds, opacity=opacity, name="flood depth"
    ).add_to(m)
    _add_legend(m, cmap or DEPTH_CMAP, 0.0, top, "Water depth (m)")
    if boundary_geom is not None:
        folium.GeoJson(
            boundary_geom.__geo_interface__,
            style_function=lambda _f: _BOUNDARY_STYLE,
            name="query area",
            overlay=True,
            control=True,
            show=True,
        ).add_to(m)
    folium.LayerControl(collapsed=True).add_to(m)
    m.fit_bounds(fit)
    return m


def explore_context(
    frame: Any, *, boundary: bool = True, tiles: str = "OpenStreetMap", **kwargs: Any
) -> Any:
    """Interactive context map: the query ROI on a toggleable basemap. Returns folium.Map."""
    folium = require("folium")
    m, _base = _new_map(_center(frame), tiles=tiles)
    if boundary:
        _add_boundary(m, frame)
    folium.LayerControl(collapsed=True).add_to(m)
    _fit_roi(m, frame)
    return m
