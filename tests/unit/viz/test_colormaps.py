"""Tests for euroflood.viz._colormaps (mask_nodata is pure numpy; year_colors needs matplotlib)."""

import numpy as np
import pytest

from euroflood.viz._colormaps import mask_nodata


def test_mask_nodata_masks_both_sentinels():
    arr = np.array([[0.0, 1.0], [2.0, -9999.0]])
    m = mask_nodata(arr)
    assert m.mask[0, 0]  # 0 masked
    assert m.mask[1, 1]  # -9999 masked
    assert not m.mask[0, 1] and not m.mask[1, 0]  # real values kept


def test_mask_nodata_extra_value():
    m = mask_nodata(np.array([1.0, 5.0, 9.0]), nodata=5.0)
    assert m.mask[1] and not m.mask[0] and not m.mask[2]


def test_year_colors_distinct_and_deterministic():
    pytest.importorskip("matplotlib")  # year_colors() builds a matplotlib colormap
    from euroflood.viz._colormaps import year_colors

    colors = year_colors([2020, 2021, 2020])
    assert set(colors) == {2020, 2021}
    assert colors[2020] != colors[2021]
    assert all(v.startswith("#") for v in colors.values())
    assert colors == year_colors([2021, 2020])  # order-independent
