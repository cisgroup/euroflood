"""Tests for the viz public API: dispatch, FloodFrame methods, DepthRaster, guardrail."""

from pathlib import Path

import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("folium")
import matplotlib

matplotlib.use("Agg", force=True)

import euroflood as ef
from euroflood.exceptions import ProcessingError, VisualizationError
from euroflood.pipelines.discovery import FloodFrame
from euroflood.viz import (
    DepthRaster,
    explore,
    open_depth,
    plot,
)
from euroflood.viz._deps import require


def test_top_level_exports_are_lazy():
    # Accessible from the package root, served lazily from euroflood.viz.
    assert ef.plot is plot
    assert ef.DepthRaster is DepthRaster


def test_plot_dispatch_frame_default_recurrence(historic_frame):
    ax = plot(historic_frame)
    assert ax.get_images()  # default view is the recurrence heatmap


def test_plot_dispatch_path(sample_tif_path):
    ax = plot(str(sample_tif_path))
    assert "depth" in ax.get_title().lower()


def test_plot_dispatch_list(sample_tif_path):
    ax = plot([sample_tif_path])
    assert ax is not None


def test_plot_empty_list_raises():
    with pytest.raises(VisualizationError, match="Empty"):
        plot([])


def test_explore_dispatch_frame(historic_frame):
    m = explore(historic_frame)
    assert m.get_root().render()


def test_explore_dispatch_path_and_list(sample_tif_path):
    assert explore(str(sample_tif_path)).get_root().render()
    assert explore([sample_tif_path]).get_root().render()


def test_explore_empty_list_raises():
    with pytest.raises(VisualizationError, match="Empty"):
        explore([])


def test_dispatch_depthraster(sample_tif_path):
    dr = open_depth(sample_tif_path)
    assert plot(dr).get_title()
    assert explore(dr).get_root().render()


def test_plot_frame_hazard_routes_to_context(hazard_frame):
    ax = plot(hazard_frame)
    assert "Hazard" in ax.get_title()


def test_explore_frame_hazard_routes_to_context(hazard_frame):
    assert explore(hazard_frame).get_root().render()


def test_explore_frame_depth(historic_frame, sample_tif_path, patch_download):
    patch_download(sample_tif_path)
    assert historic_frame.explore(depth=True).get_root().render()


def test_open_depth_roundtrip(sample_tif_path, tmp_path):
    dr = open_depth(sample_tif_path)
    assert isinstance(dr, DepthRaster)
    array, _transform, _crs, nodata = dr.read()
    assert array.shape == (10, 10)
    assert nodata == 0
    png = dr.save(tmp_path / "depth.png")
    assert png.exists() and png.stat().st_size > 0


def test_depthraster_save_html(sample_tif_path, tmp_path):
    out = open_depth(sample_tif_path).save(tmp_path / "map.html")
    assert out.exists()
    assert "leaflet" in out.read_text().lower()


def test_frame_plot_method_default(historic_frame):
    assert historic_frame.plot().get_images()


def test_frame_plot_geo_fallback(historic_frame):
    # geo=True is the raw geopandas plot of the catalogue geometry.
    ax = historic_frame.plot(geo=True)
    assert ax is not None


def test_frame_explore_method(historic_frame):
    assert historic_frame.explore().get_root().render()


def test_depth_true_downloads_then_plots(
    historic_frame, sample_tif_path, patch_download
):
    patch_download(sample_tif_path)
    ax = historic_frame.plot(depth=True)
    assert "depth" in ax.get_title().lower()


def test_depth_multi_event_max_composite(
    two_event_frame, depth_tif_3035, patch_download
):
    # No event_id + 2 events -> download both -> per-pixel max composite.
    patch_download(depth_tif_3035, depth_tif_3035)
    ax = two_event_frame.plot(depth=True)
    assert "max of 2 events" in ax.get_title()


def test_depth_reuses_already_downloaded_files(historic_frame, depth_tif_3035, mocker):
    # A frame that already carries its rasters must not re-download to plot them.
    historic_frame["path"] = [str(depth_tif_3035)]
    boom = mocker.patch.object(
        FloodFrame, "download", side_effect=AssertionError("should not re-download")
    )
    ax = historic_frame.plot(depth=True)
    assert "depth" in ax.get_title().lower()
    boom.assert_not_called()


def test_depth_scale_inferred_by_kind(
    historic_frame, hazard_frame, sample_tif_path, mocker
):
    # Historic (EFAS cm) -> scale 0.01; hazard (GLOFAS m) -> scale 1.0.
    import euroflood.viz as vz

    spy = mocker.patch.object(vz, "plot_depth", return_value="AX")

    historic_frame["path"] = [str(sample_tif_path)]
    vz.plot_frame(historic_frame, depth=True)
    assert spy.call_args.kwargs["scale"] == 0.01

    hf = hazard_frame.head(1).copy()
    hf["path"] = [str(sample_tif_path)]
    vz.plot_frame(hf, depth=True)
    assert spy.call_args.kwargs["scale"] == 1.0

    # An explicit scale still wins over the inferred one.
    vz.plot_frame(hf, depth=True, scale=0.5)
    assert spy.call_args.kwargs["scale"] == 0.5


def test_depth_guardrail_requires_limit(make_historic_frame):
    frame = make_historic_frame(5)
    with pytest.raises(VisualizationError, match="cap"):
        plot(frame, depth=True)


def test_depth_guardrail_ok_with_limit(
    make_historic_frame, sample_tif_path, patch_download
):
    frame = make_historic_frame(5)
    patch_download(sample_tif_path)
    ax = plot(frame, depth=True, limit=10)
    assert ax is not None


def test_require_missing_extra_message():
    with pytest.raises(ImportError, match=r"euroflood\[viz\]"):
        require("a_module_that_does_not_exist_xyz")


def test_footprints_method_and_functional(two_event_frame):
    import euroflood as ef

    gdf = two_event_frame.footprints()
    assert len(gdf) == 2
    assert "extent_km2" in gdf.columns
    assert len(ef.footprints(two_event_frame)) == 2


def test_frame_plot_footprints_flag(two_event_frame):
    assert "extents" in two_event_frame.plot(footprints=True).get_title().lower()


def test_frame_explore_footprints_flag(two_event_frame):
    assert two_event_frame.explore(footprints=True).get_root().render()


def test_footprints_export_roundtrip(two_event_frame, tmp_path):
    import geopandas as gpd

    out = tmp_path / "extents.geojson"
    two_event_frame.footprints().to_file(out, driver="GeoJSON")
    assert len(gpd.read_file(out)) == 2


# --- actionable frame: .files / .depths() / repr indicator -----------------
def test_frame_is_actionable_after_download(historic_frame, sample_tif_path):
    # Simulate a completed download by attaching the file to the frame.
    historic_frame["path"] = [str(sample_tif_path)]
    assert historic_frame.files == [Path(sample_tif_path)]
    assert "downloaded" in repr(historic_frame)  # repr shows the download state
    # path travels through slicing (it's a column, so head()/filters keep it).
    assert historic_frame.head(1).files == [Path(sample_tif_path)]
    depths = historic_frame.depths()
    assert len(depths) == 1 and isinstance(depths[0], DepthRaster)


def test_frame_files_empty_before_download(historic_frame):
    assert historic_frame.files == []
    assert historic_frame.depths() == []


def test_frame_stats_and_summary(two_event_frame, depth_tif_3035):
    two_event_frame["path"] = [str(depth_tif_3035), str(depth_tif_3035)]
    df = two_event_frame.stats(scale=1.0)
    assert len(df) == 2
    assert (df["max_depth_m"] == 2.5).all()
    summary = two_event_frame.summary(scale=1.0)
    assert summary["n_events"] == 2
    assert summary["max_depth_m"] == 2.5


def test_frame_stats_without_download_raises(two_event_frame):
    with pytest.raises(ProcessingError, match="download"):
        two_event_frame.stats()
