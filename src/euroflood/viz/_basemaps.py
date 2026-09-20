"""Basemap providers shared by the static and interactive plots.

Both plotting paths resolve the same preset names here, so a provider change lands in
one place instead of drifting between a matplotlib figure and a folium map.

Two upstream policies shape these choices:

- **CARTO** (Positron, DarkMatter) now stamps ``API KEY REQUIRED`` diagonally across
  every tile served without a key. It still returns HTTP 200 and real cartography, so
  nothing errors -- the watermark just appears in the output, and it does so for a
  reader's browser exactly as for a bulk fetch.
- **OpenStreetMap**'s tile usage policy requires a *valid identifying* User-Agent.
  contextily defaults to ``contextily-<random hex>``, which OSM blocks with a 403
  "Access blocked" tile. Sending `USER_AGENT` is what makes OSM work, and is what the
  policy asks for; it is not a workaround.

There is no keyless dark basemap left, so ``"dark"`` is produced by inverting a light
one -- see `INVERTED`.
"""

from __future__ import annotations

from typing import Any

#: Identifies this library to tile servers, as OSM's usage policy requires.
USER_AGENT = "euroflood (+https://github.com/cisgroup/euroflood)"
TILE_HEADERS = {"user-agent": USER_AGENT}

#: Preset name -> dotted xyzservices/contextily provider path.
PRESETS: dict[str, str] = {
    "grayscale": "Esri.WorldGrayCanvas",
    "greyscale": "Esri.WorldGrayCanvas",
    "gray": "Esri.WorldGrayCanvas",
    "grey": "Esri.WorldGrayCanvas",
    "light": "Esri.WorldGrayCanvas",
    "dark": "Esri.WorldGrayCanvas",
    "osm": "OpenStreetMap.Mapnik",
    "openstreetmap": "OpenStreetMap.Mapnik",
}

#: Presets rendered by inverting their provider's tiles.
INVERTED = frozenset({"dark"})


def resolve(tiles: str) -> tuple[str, bool]:
    """Map a preset name to a provider path and whether to invert it.

    Args:
        tiles: A preset name (``"grayscale"``, ``"dark"``, ...) or a provider path
            passed straight through (``"Esri.WorldImagery"``).

    Returns:
        ``(provider_path, invert)``.
    """
    key = str(tiles).lower()
    return PRESETS.get(key, tiles), key in INVERTED


def provider(path: str) -> Any:
    """Look up a dotted provider path in the xyzservices registry.

    Args:
        path: e.g. ``"Esri.WorldGrayCanvas"``.

    Returns:
        The `xyzservices.TileProvider`.
    """
    from ._deps import require

    found: Any = require("xyzservices").providers
    for part in path.split("."):
        found = found[part]
    return found
