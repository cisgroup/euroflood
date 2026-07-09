"""Tests for euroflood.console (the rich-backed CLI presentation layer)."""

import io
import json

import geopandas as gpd
import pytest
from rich.console import Console
from shapely.geometry import box

from euroflood import console


@pytest.fixture(autouse=True)
def _reset_console():
    """Reset module state to defaults before each test."""
    console.configure()
    yield
    console.configure()


def _floods_catalogue(n=1):
    return gpd.GeoDataFrame(
        {
            "event_id": list(range(10, 10 + n)),
            "date": ["2020-05-01"] * n,
            "area_km2": [8.0] * n,
            "filename": ["a.tif"] * n,
            "geometry": [box(0, 0, 1, 1)] * n,
        },
        geometry="geometry",
        crs="EPSG:4326",
    )


def _hazard_catalogue():
    return gpd.GeoDataFrame(
        {
            "return_period": [100],
            "n_tiles": [2],
            "area_km2": [891.2],
            "filename": ["hazard_RP100.tif"],
            "geometry": [box(0, 0, 1, 1)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )


# --- configuration --------------------------------------------------------


def test_configure_sets_flags():
    console.configure(quiet=True, json_mode=True, no_color=True, verbose=2)
    assert console._options.quiet is True
    assert console._options.json_mode is True
    assert console.verbosity() == 2


def test_get_console_lazy_when_unset():
    console._console = None
    assert isinstance(console.get_console(), Console)


def test_err_console_lazy_when_unset():
    console._err_console = None
    assert isinstance(console._err(), Console)


# --- status lines ---------------------------------------------------------


def test_success_prints_with_check(capsys):
    console.success("Downloaded 2 flood map(s).")
    out = capsys.readouterr().out
    assert "✓" in out
    assert "Downloaded 2 flood map(s)." in out


def test_success_suppressed_under_json():
    console.configure(json_mode=True)
    buf = io.StringIO()
    console._console = Console(file=buf)
    console.success("hidden")
    assert buf.getvalue() == ""


def test_note_suppressed_under_quiet():
    console.configure(quiet=True)
    buf = io.StringIO()
    console._console = Console(file=buf)
    console.note("secondary")
    assert buf.getvalue() == ""


def test_note_prints_when_verbose(capsys):
    console.note("cached the tables")
    assert "cached the tables" in capsys.readouterr().out


def test_echo_prints_dry_run_literally(capsys):
    # Brackets must NOT be parsed as rich markup.
    console.echo("[dry-run] mirror: 4 to download")
    assert "[dry-run] mirror: 4 to download" in capsys.readouterr().out


# --- cell formatting ------------------------------------------------------


def test_fmt_cell_variants():
    assert console._fmt_cell("area_km2", 1234.5) == "1,234.50"
    assert console._fmt_cell("n_tiles", 2) == "2"
    assert console._fmt_cell("event_id", 10) == "10"
    assert console._fmt_cell("filename", "a.tif") == "a.tif"
    assert console._fmt_cell("date", None) == ""
    assert console._fmt_cell("area_km2", float("nan")) == ""


# --- catalogue table ------------------------------------------------------


def test_render_floods_table(capsys):
    console.render_catalogue(_floods_catalogue(), kind="floods", place="Berlin")
    out = capsys.readouterr().out
    assert "1 flood event(s) for 'Berlin'." in out
    assert "a.tif" in out


def test_render_hazard_table(capsys):
    console.render_catalogue(_hazard_catalogue(), kind="hazard", place="Cologne")
    out = capsys.readouterr().out
    assert "1 hazard layer(s) for 'Cologne'." in out
    assert "hazard_RP100.tif" in out


def test_render_empty_skips_table(capsys):
    empty = _floods_catalogue(0)
    console.render_catalogue(empty, kind="floods")
    out = capsys.readouterr().out
    assert "0 flood event(s)." in out
    # No table rule characters when empty.
    assert "━" not in out


def test_render_truncates_with_footer(capsys):
    console.render_catalogue(_floods_catalogue(25), kind="floods", limit=20)
    out = capsys.readouterr().out
    assert "25 flood event(s)." in out
    assert "and 5 more" in out


def test_render_json_mode_emits_records(capsys):
    console.configure(json_mode=True)
    console.render_catalogue(_floods_catalogue(), kind="floods")
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data[0]["event_id"] == 10
    assert "geometry" not in data[0]


def test_print_json_records_without_geometry(capsys):
    df = _floods_catalogue().drop(columns="geometry")
    console._print_json_records(df)
    data = json.loads(capsys.readouterr().out)
    assert data[0]["filename"] == "a.tif"


# --- progress -------------------------------------------------------------


def test_progress_noop_when_not_terminal(capsys):
    # capsys stdout is not a TTY -> progress is a no-op that still advances.
    with console.progress("Working", total=3) as advance:
        advance(1)
        advance(2)
    assert capsys.readouterr().out == ""


def test_progress_noop_when_quiet():
    console.configure(quiet=True)
    buf = io.StringIO()
    console._console = Console(file=buf, force_terminal=True)
    with console.progress("Working", total=1) as advance:
        advance(1)
    assert buf.getvalue() == ""


def test_progress_renders_on_terminal():
    buf = io.StringIO()
    console._console = Console(file=buf, force_terminal=True, width=80)
    with console.progress("Working", total=2) as advance:
        advance(1)
        advance(1)
    assert buf.getvalue() != ""


def test_download_progress_renders_on_terminal():
    buf = io.StringIO()
    console._console = Console(file=buf, force_terminal=True, width=80)
    with console.download_progress("Fetching", total=100) as advance:
        advance(50)
        advance(50)
    assert buf.getvalue() != ""


def test_download_progress_noop_when_not_terminal(capsys):
    with console.download_progress("Fetching", total=100) as advance:
        advance(100)
    assert capsys.readouterr().out == ""


# --- errors ---------------------------------------------------------------


def test_error_block_no_traceback(capsys):
    console.error_block(ValueError("bad place. Did you mean: cologne?"))
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "Did you mean: cologne?" in err
    assert "Traceback" not in err


def test_error_block_with_next_step(capsys):
    console.error_block(
        RuntimeError("no index"), next_step="run 'euroflood build-index'"
    )
    err = capsys.readouterr().err
    assert "no index" in err
    assert "run 'euroflood build-index'" in err
