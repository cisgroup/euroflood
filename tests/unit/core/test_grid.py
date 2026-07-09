"""Tests for the Global Grid system."""

import numpy as np

from euroflood.core.grid import GlobalGrid


def test_grid_constants():
    """Verify grid definitions haven't drifted."""
    assert GlobalGrid.WIDTH_PX > 0
    assert GlobalGrid.HEIGHT_PX > 0
    assert GlobalGrid.RESOLUTION == 1 / 1200.0


def test_latlon_to_grid_valid():
    """Test conversion of specific known points."""
    # Origin point (should be 0,0)
    lats = np.array([GlobalGrid.ORIGIN_Y])
    lons = np.array([GlobalGrid.ORIGIN_X])

    cols, rows = GlobalGrid.latlon_to_grid(lats, lons)

    assert cols[0] == 0
    assert rows[0] == 0


def test_latlon_to_grid_floor_rejects_below_origin():
    """A lon just below the grid origin floors to -1 (rejected), not 0.

    Truncation toward zero would map a tiny negative index to 0 and wrongly pass
    is_valid(); floor maps it to -1 so it is correctly dropped.
    """
    lon = GlobalGrid.ORIGIN_X - 1e-6  # just outside the western edge
    lat = GlobalGrid.ORIGIN_Y - 1.0  # well inside vertically
    cols, rows = GlobalGrid.latlon_to_grid(np.array([lat]), np.array([lon]))

    assert cols[0] == -1
    assert bool(GlobalGrid.is_valid(cols, rows)[0]) is False


def test_latlon_to_grid_rejects_non_finite():
    """NaN/inf inputs map out of bounds so is_valid() drops them."""
    lats = np.array([np.nan, np.inf, 50.0])
    lons = np.array([10.0, 10.0, 10.0])

    cols, rows = GlobalGrid.latlon_to_grid(lats, lons)
    mask = GlobalGrid.is_valid(cols, rows)

    assert bool(mask[0]) is False
    assert bool(mask[1]) is False
    assert bool(mask[2]) is True


def test_is_valid_mask():
    """Test the bounds checking logic."""
    # Create points: [Inside, Outside Left, Outside Top]
    cols = np.array([100, -1, 100], dtype=np.int32)
    rows = np.array([100, 100, -1], dtype=np.int32)

    mask = GlobalGrid.is_valid(cols, rows)

    assert bool(mask[0]) is True  # Inside
    assert bool(mask[1]) is False  # Negative col
    assert bool(mask[2]) is False  # Negative row


def test_is_valid_upper_bounds_are_exclusive():
    """The grid is half-open: WIDTH_PX/HEIGHT_PX are out of bounds, max-1 is in."""
    cols = np.array([GlobalGrid.WIDTH_PX - 1, GlobalGrid.WIDTH_PX, 0], dtype=np.int32)
    rows = np.array([GlobalGrid.HEIGHT_PX - 1, 0, GlobalGrid.HEIGHT_PX], dtype=np.int32)
    mask = GlobalGrid.is_valid(cols, rows)
    assert bool(mask[0]) is True
    assert bool(mask[1]) is False
    assert bool(mask[2]) is False


def test_is_valid_zero_origin_is_inside():
    """(0, 0) is the inclusive top-left corner and must be valid."""
    mask = GlobalGrid.is_valid(
        np.array([0], dtype=np.int32), np.array([0], dtype=np.int32)
    )
    assert bool(mask[0]) is True


def test_latlon_to_grid_negative_infinity_rejected():
    """-inf inputs are flagged invalid alongside +inf and NaN."""
    lats = np.array([-np.inf, np.nan, np.inf])
    lons = np.array([-np.inf, np.inf, np.nan])
    cols, rows = GlobalGrid.latlon_to_grid(lats, lons)
    assert not GlobalGrid.is_valid(cols, rows).any()


def test_latlon_to_grid_empty_arrays():
    """Empty input arrays yield empty index arrays (no crash on empty tiles)."""
    cols, rows = GlobalGrid.latlon_to_grid(np.array([]), np.array([]))
    assert cols.shape == (0,)
    assert rows.shape == (0,)
    assert GlobalGrid.is_valid(cols, rows).shape == (0,)


def test_latlon_to_grid_far_out_of_bounds_rejected():
    """Coordinates well outside the European extent map to invalid indices."""
    lats = np.array([-89.0, 0.0])
    lons = np.array([0.0, -179.0])
    cols, rows = GlobalGrid.latlon_to_grid(lats, lons)
    assert not GlobalGrid.is_valid(cols, rows).any()


def test_latlon_to_grid_dtype_is_int32():
    """Indices are int32 (the dtype downstream parquet/casting relies on)."""
    cols, rows = GlobalGrid.latlon_to_grid(np.array([50.0]), np.array([10.0]))
    assert cols.dtype == np.int32
    assert rows.dtype == np.int32


def test_latlon_to_grid_large_n_stable_and_in_bounds():
    """A large batch of in-extent points all map to valid, monotonic indices."""
    n = 100_000
    rng = np.random.default_rng(0)
    lons = rng.uniform(-40.0, 60.0, n)
    lats = rng.uniform(30.0, 80.0, n)
    cols, rows = GlobalGrid.latlon_to_grid(lats, lons)
    assert cols.shape == (n,) and rows.shape == (n,)
    assert GlobalGrid.is_valid(cols, rows).all()
    order = np.argsort(lons)
    assert np.all(np.diff(cols[order]) >= 0)
