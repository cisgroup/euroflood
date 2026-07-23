"""Logging configuration for EuroFlood.

Importing `euroflood` installs a **quiet default** (see
`_install_quiet_default`): structlog is routed through the standard library
with a `NullHandler` on the ``euroflood`` logger, and pooch's own
logger is quieted, so the library is **silent** unless you opt in. Call
`setup_logging` to see logs (the CLI does this automatically). The quiet
default is polite: it only configures structlog when the host application hasn't
already, and it never touches the root logger.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

import structlog

LOGGER_NAME = "euroflood"

_TIMESTAMPER = structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S")

# Processors that also apply to records coming from the stdlib logging system
# (i.e. not originating from structlog) when rendered via the ProcessorFormatter.
_FOREIGN_PRE_CHAIN: list[structlog.types.Processor] = [
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    _TIMESTAMPER,
]


def _configure_structlog() -> None:
    """Route structlog events through the standard library logging system.

    Kept separate from `setup_logging` so that test suites can enable
    stdlib routing (so ``caplog`` can capture events) without installing a
    handler or disabling propagation.
    """
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            _TIMESTAMPER,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def setup_logging(
    level: str = "INFO",
    *,
    json: bool | None = None,
    stream: TextIO | None = None,
) -> None:
    """Configure structured logging for the ``euroflood`` logger.

    Idempotent: repeated calls reset the handlers rather than stacking them.
    Only the ``euroflood`` logger is configured (with ``propagate=False``), so
    this never attaches handlers to the root logger or changes the log level of
    unrelated libraries.

    Args:
        level: Log level name (e.g. ``"INFO"``, ``"DEBUG"``).
        json: Force JSON rendering (``True``) or console rendering (``False``).
            When ``None`` (default), auto-detect at call time: console when the
            output stream is a TTY, JSON otherwise.
        stream: Output stream. Defaults to `stderr`.
    """
    out_stream: TextIO = stream if stream is not None else sys.stderr
    if json is None:
        json = not out_stream.isatty()

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )

    _configure_structlog()

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_FOREIGN_PRE_CHAIN,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(out_stream)
    handler.setFormatter(formatter)

    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False

    # When the user opts into INFO/DEBUG, let pooch's download lines through too;
    # otherwise keep it quiet (the import-time default already set WARNING).
    _pooch_logger().setLevel(
        level.upper() if level.upper() in ("DEBUG", "INFO") else logging.WARNING
    )


def _pooch_logger() -> logging.Logger:
    """Return pooch's (detached) stdlib logger, or a throwaway if pooch is absent."""
    try:
        import pooch

        logger: logging.Logger = pooch.get_logger()
        return logger
    except Exception:  # pragma: no cover - pooch is a hard dep; defensive only
        return logging.getLogger("pooch")


def _install_quiet_default() -> None:
    """Make the library quiet by default (called once at import).

    Routes structlog through the standard library (only if the host hasn't already
    configured structlog), installs a `NullHandler` on the
    ``euroflood`` logger at ``WARNING`` with ``propagate=False``, and quiets pooch's
    own logger. Net effect: ``import euroflood; ef.floods(...)`` emits nothing unless
    `setup_logging` is called. Idempotent.
    """
    if not structlog.is_configured():
        _configure_structlog()
    logger = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
        logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    _pooch_logger().setLevel(logging.WARNING)
