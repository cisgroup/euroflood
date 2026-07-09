"""Tests for euroflood.viz._static (matplotlib plots). Skipped without the extra."""

import pytest

matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg", force=True)

from matplotlib.axes import Axes  # noqa: E402

from euroflood.viz._static import (  # noqa: E402
    plot_context,
    plot_depth,
    plot_footprints,
    plot_recurrence,
)


def test_plot_recurrence_returns_axes_with_image(historic_frame):
    ax = plot_recurrence(historic_frame)
    assert isinstance(ax, Axes)
    assert ax.get_images()  # a raster was drawn
    assert "recurrence" in ax.get_title().lower()


def test_plot_recurrence_event_footprint_title(historic_frame):
    ax = plot_recurrence(historic_frame, event_id=10, boundary=True)
    assert "footprint" in ax.get_title().lower()


def test_plot_recurrence_no_boundary(historic_frame):
    ax = plot_recurrence(historic_frame, boundary=False)
    assert isinstance(ax, Axes)


def test_plot_depth_returns_axes(sample_tif_path):
    ax = plot_depth(sample_tif_path)
    assert isinstance(ax, Axes)
    assert "depth" in ax.get_title().lower()
    assert ax.get_images()


def test_plot_depth_explicit_vmax(sample_tif_path):
    ax = plot_depth(sample_tif_path, vmax=3.0, cmap="viridis")
    assert isinstance(ax, Axes)


def test_plot_context_hazard(hazard_frame):
    ax = plot_context(hazard_frame)
    assert "Hazard" in ax.get_title()
    assert "RP 100" in ax.get_title()


def test_plot_context_historic(historic_frame):
    ax = plot_context(historic_frame)
    assert "catalogue" in ax.get_title().lower()


def test_plot_recurrence_reuses_ax_and_adds_basemap(historic_frame, mocker):
    import matplotlib.pyplot as plt

    cx = mocker.patch("contextily.add_basemap")
    _fig, ax = plt.subplots()
    out = plot_recurrence(historic_frame, ax=ax, basemap=True)
    assert out is ax  # the provided axes was reused
    cx.assert_called_once()


def test_plot_context_basemap(hazard_frame, mocker):
    cx = mocker.patch("contextily.add_basemap")
    plot_context(hazard_frame, basemap=True)
    cx.assert_called_once()


def test_plot_recurrence_scales_to_true_max(multicount_frame):
    from euroflood.viz._raster import recurrence_grid

    counts, *_ = recurrence_grid(multicount_frame)
    assert counts.max() == 3
    im = plot_recurrence(multicount_frame).get_images()[0]
    assert im.get_clim()[1] == 3  # colorbar top = true max
    # The data reaching imshow must span 1..3 — rasterio.plot.show would have
    # rescaled it to [0, 1], collapsing every count to the palest colour.
    assert float(im.get_array().max()) == 3.0


def test_plot_depth_axes_in_degrees(depth_tif_3035):
    ax = plot_depth(depth_tif_3035)  # source is EPSG:3035 (metres)
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    assert -180 <= x0 <= 180 and -180 <= x1 <= 180  # reprojected to lat/lon
    assert -90 <= y0 <= 90 and -90 <= y1 <= 90


def test_plot_depth_converts_cm_to_metres(depth_tif_3035):
    # the fixture's wet value is 2.5 -> 0.025 m at the default scale (cm -> m)
    im = plot_depth(depth_tif_3035).get_images()[0]
    assert abs(float(im.get_array().max()) - 0.025) < 1e-6
    im2 = plot_depth(depth_tif_3035, scale=1.0).get_images()[0]  # already-metres raster
    assert float(im2.get_array().max()) == 2.5


def test_plot_footprints_returns_axes_with_legend(two_event_frame):
    ax = plot_footprints(two_event_frame)
    assert isinstance(ax, Axes)
    assert "extents" in ax.get_title().lower()
    assert ax.get_legend() is not None  # categorical legend by year


def test_plot_footprints_via_flag(two_event_frame):
    ax = two_event_frame.plot(footprints=True)
    assert "extents" in ax.get_title().lower()
