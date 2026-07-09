"""Tests for euroflood._progress (the UI-agnostic progress abstraction)."""

import contextlib

import pytest

from euroflood import _progress


@pytest.fixture(autouse=True)
def _reset_factory():
    """Ensure a clean no-op factory before/after each test."""
    _progress.reset_factory()
    yield
    _progress.reset_factory()


def test_default_factory_is_noop():
    """With no factory installed, progress_bar is a silent no-op that advances."""
    with _progress.progress_bar("work", total=3) as step:
        step(1)
        step(2)  # no error, no output


def test_set_factory_routes_calls():
    """An installed factory receives the description/total and advance calls."""
    seen: list[object] = []

    @contextlib.contextmanager
    def fake(description, total=None):
        seen.append(("open", description, total))
        yield lambda n=1: seen.append(("advance", n))

    _progress.set_factory(fake)
    with _progress.progress_bar("mirror", total=5) as step:
        step(2)

    assert ("open", "mirror", 5) in seen
    assert ("advance", 2) in seen


def test_reset_factory_restores_noop():
    """reset_factory drops back to the silent no-op."""

    @contextlib.contextmanager
    def fake(description, total=None):
        yield lambda n=1: None

    _progress.set_factory(fake)
    _progress.reset_factory()
    # Back to no-op: still works, produces nothing.
    with _progress.progress_bar("x") as step:
        step(1)


# --- library-side, environment-aware bars (download / mirror) ---------------
def test_interactive_false_when_progress_disabled(mock_settings):
    mock_settings.show_progress = False
    assert _progress._interactive() is False


def test_interactive_tty_needs_no_ipywidgets(mock_settings, mocker):
    mock_settings.show_progress = True
    console = mocker.Mock(is_terminal=True, is_jupyter=False)
    mocker.patch("rich.console.Console", return_value=console)
    assert _progress._interactive() is True


def test_interactive_jupyter_requires_ipywidgets(mock_settings, mocker):
    """In Jupyter a bar renders only when ipywidgets is available (else silent)."""
    mock_settings.show_progress = True
    console = mocker.Mock(is_terminal=False, is_jupyter=True)
    mocker.patch("rich.console.Console", return_value=console)
    mocker.patch("euroflood._progress._has_ipywidgets", return_value=True)
    assert _progress._interactive() is True
    mocker.patch("euroflood._progress._has_ipywidgets", return_value=False)
    assert _progress._interactive() is False


def test_interactive_false_when_not_a_terminal(mock_settings, mocker):
    mock_settings.show_progress = True
    console = mocker.Mock(is_terminal=False, is_jupyter=False)
    mocker.patch("rich.console.Console", return_value=console)
    assert _progress._interactive() is False


@pytest.mark.parametrize("bar_fn", ["download_bar", "mirror_bar", "steps_bar"])
def test_bar_is_noop_when_not_interactive(bar_fn, mocker):
    mocker.patch("euroflood._progress._interactive", return_value=False)
    with getattr(_progress, bar_fn)("work", total=10) as advance:
        advance(5)  # no error; no rich Progress constructed


@pytest.mark.parametrize("bar_fn", ["download_bar", "mirror_bar", "steps_bar"])
def test_bar_renders_and_advances_when_interactive(bar_fn, mocker):
    mocker.patch("euroflood._progress._interactive", return_value=True)
    bar = mocker.MagicMock()
    bar.__enter__.return_value = bar
    bar.add_task.return_value = 7
    mocker.patch("rich.progress.Progress", return_value=bar)
    with getattr(_progress, bar_fn)("work", total=10) as advance:
        advance(3)
    bar.add_task.assert_called_once()
    bar.advance.assert_called_once_with(7, 3)


def test_file_progress_noop_when_disabled(mocker):
    """enabled=False (the caller drives its own byte bar) short-circuits any bar."""
    interactive = mocker.patch("euroflood._progress._interactive")
    with _progress.file_progress(3, enabled=False) as advance:
        advance(1)  # no error, no bar
    interactive.assert_not_called()  # returns before touching rich at all


def test_file_progress_noop_when_zero_total(mocker):
    """Nothing to download (total 0, e.g. all cached) -> no bar even if interactive."""
    mocker.patch("euroflood._progress._interactive", return_value=True)
    prog = mocker.patch("rich.progress.Progress")
    with _progress.file_progress(0, enabled=True) as advance:
        advance(1)
    prog.assert_not_called()


def test_file_progress_renders_when_enabled_and_interactive(mocker):
    mocker.patch("euroflood._progress._interactive", return_value=True)
    bar = mocker.MagicMock()
    bar.__enter__.return_value = bar
    bar.add_task.return_value = 1
    mocker.patch("rich.progress.Progress", return_value=bar)
    with _progress.file_progress(
        2, enabled=True, description="Downloading maps"
    ) as adv:
        adv(1)
    bar.add_task.assert_called_once()
    bar.advance.assert_called_once_with(1, 1)
