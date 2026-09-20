"""Colormap and colour-scaling helpers for EuroFlood plots.

Pure ``numpy`` (no I/O, no plotting), so these are trivially unit-testable. Two
project-wide NoData sentinels are always masked out (rendered transparent): ``0``
(the historic index / depth NoData) and ``-9999`` (the GLOFAS hazard NoData).
"""

from __future__ import annotations

from typing import Any

import numpy as np

# NoData sentinels masked (made transparent) in every raster plot.
NODATA_SENTINELS: tuple[float, ...] = (0.0, -9999.0)

DEPTH_CMAP = "Blues"  # light -> dark = shallow -> deep water
RECURRENCE_CMAP = "YlOrRd"  # light -> dark = few -> many recorded floods


def mask_nodata(array: Any, nodata: float | None = None) -> Any:
    """Return a float masked array with NoData hidden.

    Masks NaN/inf, the two project sentinels (``0`` and ``-9999``), and an
    optional dataset-specific ``nodata`` value.

    Args:
        array: The raster values (any numeric dtype).
        nodata: An extra sentinel to mask (e.g. a raster's declared ``nodata``).

    Returns:
        A ``numpy.ma.MaskedArray`` of ``float64`` with NoData masked.
    """
    data = np.ma.masked_invalid(np.asarray(array, dtype="float64"))
    sentinels = set(NODATA_SENTINELS)
    if nodata is not None:
        sentinels.add(float(nodata))
    for value in sentinels:
        data = np.ma.masked_equal(data, value)
    return data


def year_colors(years: Any, cmap: str = "tab20") -> dict[int, str]:
    """Map each distinct year to a hex colour (evenly sampled; handles >20 years).

    Shared by the static and interactive footprint renderers so the same year gets
    the same colour in both.

    Args:
        years: An iterable of years (ints or castable).
        cmap: A matplotlib colormap name to sample.

    Returns:
        ``{year: "#rrggbb"}`` for the sorted distinct years.
    """
    import matplotlib
    from matplotlib.colors import to_hex

    uniq = sorted({int(y) for y in years})
    if not uniq:
        return {}
    colormap = matplotlib.colormaps[cmap]
    denom = max(1, len(uniq) - 1)
    return {y: to_hex(colormap(i / denom)) for i, y in enumerate(uniq)}
