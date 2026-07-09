"""Tests for euroflood.logging.setup_logging and import-time logging behavior."""

import io
import logging

import structlog

import euroflood
from euroflood.logging import (
    LOGGER_NAME,
    _install_quiet_default,
    _pooch_logger,
    setup_logging,
)


def test_setup_logging_is_exported():
    """setup_logging is part of the public API."""
    assert euroflood.setup_logging is setup_logging


def test_setup_logging_configures_only_euroflood_logger():
    """setup_logging touches only the 'euroflood' logger, never the root logger."""
    root = logging.getLogger()
    root_handlers_before = list(root.handlers)

    setup_logging(level="DEBUG", stream=io.StringIO())

    euro = logging.getLogger(LOGGER_NAME)
    assert len(euro.handlers) == 1
    assert euro.level == logging.DEBUG
    assert euro.propagate is False
    # The root logger is left exactly as it was.
    assert root.handlers == root_handlers_before


def test_setup_logging_is_idempotent():
    """Repeated calls reset handlers rather than stacking them."""
    stream = io.StringIO()
    setup_logging(stream=stream)
    setup_logging(stream=stream)

    euro = logging.getLogger(LOGGER_NAME)
    assert len(euro.handlers) == 1


def test_setup_logging_emits_json_to_stream():
    """json=True renders structured JSON to the provided stream."""
    stream = io.StringIO()
    setup_logging(level="INFO", json=True, stream=stream)

    structlog.get_logger("euroflood.test_json").info("hello_event", foo="bar")

    out = stream.getvalue()
    assert "hello_event" in out
    assert "foo" in out
    assert out.lstrip().startswith("{")


def test_setup_logging_console_renderer_when_not_json():
    """json=False renders human-readable console output (not JSON)."""
    stream = io.StringIO()
    setup_logging(level="INFO", json=False, stream=stream)

    structlog.get_logger("euroflood.test_console").info("console_event")

    out = stream.getvalue()
    assert "console_event" in out
    assert not out.lstrip().startswith("{")


def test_setup_logging_autodetects_json_for_non_tty():
    """With json=None and a non-TTY stream, JSON rendering is selected."""
    stream = io.StringIO()  # StringIO.isatty() is False
    setup_logging(level="INFO", stream=stream)

    structlog.get_logger("euroflood.test_auto").info("auto_event")

    assert stream.getvalue().lstrip().startswith("{")


# --- quiet-by-default (import-time) -----------------------------------------
def test_quiet_default_silences_the_euroflood_logger(capsys):
    """After the quiet default, an INFO event on the euroflood logger emits nothing."""
    _install_quiet_default()
    euro = logging.getLogger(LOGGER_NAME)
    assert euro.propagate is False
    assert euro.level == logging.WARNING
    assert any(isinstance(h, logging.NullHandler) for h in euro.handlers)

    structlog.get_logger("euroflood.quiet_test").info("should_be_silent", x=1)
    assert capsys.readouterr().out == ""


def test_quiet_default_is_idempotent():
    """Calling it twice does not stack NullHandlers."""
    _install_quiet_default()
    _install_quiet_default()
    euro = logging.getLogger(LOGGER_NAME)
    nulls = [h for h in euro.handlers if isinstance(h, logging.NullHandler)]
    assert len(nulls) == 1


def test_quiet_default_quiets_pooch():
    _install_quiet_default()
    assert _pooch_logger().level == logging.WARNING


def test_setup_logging_info_unquiets_pooch():
    """Opting into INFO/DEBUG lets pooch's download lines through; WARNING re-quiets."""
    setup_logging(level="INFO", stream=io.StringIO())
    assert _pooch_logger().level == logging.INFO
    setup_logging(level="WARNING", stream=io.StringIO())
    assert _pooch_logger().level == logging.WARNING
