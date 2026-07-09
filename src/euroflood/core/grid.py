"""The fixed global grid every flood map and the index are aligned to.

All source maps are resampled onto one lat/lon grid (EPSG:4326, ~90 m pixels)
covering Europe, so "the same place" is "the same ``(row, col)`` pixel" across
every event. That stable addressing is what lets the sparse index encode, per
pixel, the *set* of events that flooded it (a ``combo_id``) — see
`export` and the Concepts page in the docs.
"""

import numpy as np
import numpy.typing as npt


class GlobalGrid:
    """Defines the standardized 3-arc-second (~90m) grid for Europe.

    This class provides static methods to convert between geographic coordinates
    (Latitude/Longitude) and the internal pixel grid (Column/Row).

    It aligns with the extent of the Flood Depth Maps dataset but uses the
    resolution of the EFAS Hazard Maps (1/1200 degrees).

    Attributes:
        ORIGIN_X (float): Top-Left Longitude (-46.8).
        ORIGIN_Y (float): Top-Left Latitude (85.1).
        RESOLUTION (float): Grid cell size in degrees (1/1200.0).
        WIDTH_DEG (float): Total width in degrees.
        HEIGHT_DEG (float): Total height in degrees.
        WIDTH_PX (int): Total width in pixels.
        HEIGHT_PX (int): Total height in pixels.
    """

    ORIGIN_X: float = -46.8  # Top-Left Longitude
    ORIGIN_Y: float = 85.1  # Top-Left Latitude
    RESOLUTION: float = 1 / 1200.0

    # Calculated constants
    WIDTH_DEG: float = 63.0 - (-46.8)
    HEIGHT_DEG: float = 85.1 - 27.0
    WIDTH_PX: int = round(WIDTH_DEG / RESOLUTION)
    HEIGHT_PX: int = round(HEIGHT_DEG / RESOLUTION)

    @staticmethod
    def latlon_to_grid(
        lats: npt.NDArray[np.float64], lons: npt.NDArray[np.float64]
    ) -> tuple[npt.NDArray[np.int32], npt.NDArray[np.int32]]:
        """Vectorized conversion of Lat/Lon arrays to Grid Col/Row indices.

        Uses the formula:
            col = (lon - origin_x) / resolution
            row = (origin_y - lat) / resolution

        Args:
            lats (npt.NDArray[np.float64]): 1D Array of latitude values.
            lons (npt.NDArray[np.float64]): 1D Array of longitude values.

        Returns:
            Tuple[npt.NDArray[np.int32], npt.NDArray[np.int32]]: A tuple containing:
                - cols: Array of column indices (int32).
                - rows: Array of row indices (int32).
        """
        col_f = (lons - GlobalGrid.ORIGIN_X) / GlobalGrid.RESOLUTION
        row_f = (GlobalGrid.ORIGIN_Y - lats) / GlobalGrid.RESOLUTION
        # Non-finite inputs (NaN/inf) map to an out-of-bounds index so is_valid()
        # rejects them, rather than producing garbage from casting to int32. Use
        # floor so a coordinate maps to the cell whose half-open range contains it
        # (truncation would be off-by-one at boundaries / for tiny negative error).
        invalid = ~np.isfinite(col_f) | ~np.isfinite(row_f)
        cols = np.floor(np.where(invalid, -1.0, col_f)).astype("int32")
        rows = np.floor(np.where(invalid, -1.0, row_f)).astype("int32")
        return cols, rows

    @staticmethod
    def is_valid(
        cols: npt.NDArray[np.int32], rows: npt.NDArray[np.int32]
    ) -> npt.NDArray[np.bool_]:
        """Return boolean mask of valid pixels inside the grid boundaries.

        Checks if the provided column and row indices fall within the
        [0, WIDTH_PX) and [0, HEIGHT_PX) ranges respectively.

        Args:
            cols (npt.NDArray[np.int32]): Array of column indices.
            rows (npt.NDArray[np.int32]): Array of row indices.

        Returns:
            npt.NDArray[np.bool_]: Boolean array where True indicates the pixel is within bounds.
        """
        return (
            (cols >= 0)
            & (cols < GlobalGrid.WIDTH_PX)
            & (rows >= 0)
            & (rows < GlobalGrid.HEIGHT_PX)
        )
