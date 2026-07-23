"""User-facing terminal output for the EuroFlood CLI.

Two output concerns are split by stream. ``structlog`` owns *logs* on stderr (JSON
on a non-TTY, which the HPC runbook relies on, see `logging`); this
module owns *user-facing* output on stdout (status lines, result tables, and
progress) via a single `Console`. Because progress lives on
stdout it can never be scribbled over by the stderr log stream.

The module is imported only by `cli`, so ``import euroflood`` never
pulls in rich. Errors render to a separate stderr Console as a clean one-block
message (no traceback) via `error_block`.

Message text is always emitted through `Text` (never markup), so
arbitrary content (place names, ``[dry-run]`` prefixes, file paths) is printed
literally and never mis-parsed as a style tag.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich import box
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    import pandas as pd

# A progress handle: call it to advance the bar by ``n`` steps (default 1).
Advance = Callable[[int], None]


@dataclass
class _Options:
    """Presentation flags set once per CLI invocation by `configure`."""

    quiet: bool = False
    json_mode: bool = False
    no_color: bool = False
    verbose: int = 0


_options = _Options()
_console: Console | None = None
_err_console: Console | None = None


def configure(
    *,
    quiet: bool = False,
    json_mode: bool = False,
    no_color: bool = False,
    verbose: int = 0,
) -> None:
    """Set output options and (re)build the stdout/stderr consoles.

    Called once from the CLI group callback per invocation. The consoles are
    rebuilt here rather than at import time because test runners swap
    ``sys.stdout`` per invocation; a Console built at import would bind to a
    stale stream.

    Args:
        quiet: Suppress secondary status/progress (primary results still show).
        json_mode: Render results as JSON instead of tables/status.
        no_color: Disable ANSI styling (``NO_COLOR`` is also honored by rich).
        verbose: Verbosity count (``-v``); ``>0`` restores tracebacks on error.
    """
    global _console, _err_console, _options
    _options = _Options(
        quiet=quiet, json_mode=json_mode, no_color=no_color, verbose=verbose
    )
    _console = Console(highlight=False, emoji=False, no_color=no_color)
    _err_console = Console(stderr=True, highlight=False, emoji=False, no_color=no_color)


def get_console() -> Console:
    """Return the stdout Console, lazily building a default if unconfigured."""
    global _console
    if _console is None:
        _console = Console(highlight=False, emoji=False, no_color=_options.no_color)
    return _console


def _err() -> Console:
    """Return the stderr Console, lazily building a default if unconfigured."""
    global _err_console
    if _err_console is None:
        _err_console = Console(
            stderr=True, highlight=False, emoji=False, no_color=_options.no_color
        )
    return _err_console


def verbosity() -> int:
    """The ``-v`` verbosity count (0 = default)."""
    return _options.verbose


# --- status lines ---------------------------------------------------------


def success(message: str) -> None:
    """Print a primary success confirmation (green ✓); hidden under ``--json``."""
    if _options.json_mode:
        return
    text = Text()
    text.append("✓ ", style="bold green")
    text.append(message)
    get_console().print(text)


def note(message: str) -> None:
    """Print a secondary status line; hidden under ``--quiet``/``--json``."""
    if _options.quiet or _options.json_mode:
        return
    get_console().print(Text(message), style="dim")


def echo(message: str) -> None:
    """Print a plain primary line literally (e.g. a ``[dry-run]`` plan)."""
    get_console().print(Text(message))


# --- result tables --------------------------------------------------------

_CATALOGUE_COLUMNS: dict[str, tuple[str, ...]] = {
    "floods": ("event_id", "date", "area_km2", "filename"),
    "hazard": ("return_period", "n_tiles", "area_km2", "filename"),
}
_COLUMN_LABELS: dict[str, str] = {
    "event_id": "event",
    "date": "date",
    "area_km2": "area km²",
    "filename": "file",
    "return_period": "RP yr",
    "n_tiles": "tiles",
}
_RIGHT_ALIGNED = frozenset({"event_id", "area_km2", "n_tiles", "return_period"})
_NO_WRAP = frozenset({"event_id", "filename"})


def _fmt_cell(column: str, value: Any) -> str:
    """Format one catalogue cell for display (thousands sep, ints, blanks)."""
    if value is None or (isinstance(value, float) and value != value):  # NaN
        return ""
    if column == "area_km2":
        return f"{float(value):,.2f}"
    if column in ("n_tiles", "return_period", "event_id"):
        return f"{int(value):,}"
    return str(value)


def render_catalogue(
    df: pd.DataFrame,
    *,
    kind: str,
    place: str | None = None,
    limit: int = 20,
) -> None:
    """Render a queried catalogue as a summary line + aligned table (or JSON).

    Args:
        df: The catalogue (a ``FloodFrame``/``GeoDataFrame``).
        kind: ``"floods"`` or ``"hazard"``, selects the column set + noun.
        place: The queried place, echoed in the summary line.
        limit: Max rows shown; the rest are summarized as "… and N more".
    """
    if _options.json_mode:
        _print_json_records(df)
        return

    n = len(df)
    noun = "flood event" if kind == "floods" else "hazard layer"
    summary = Text()
    summary.append(str(n), style="bold cyan")
    summary.append(f" {noun}(s)")
    if place is not None:
        summary.append(f" for {place!r}")
    summary.append(".")
    console = get_console()
    console.print(summary)
    if n == 0:
        return

    columns = [c for c in _CATALOGUE_COLUMNS[kind] if c in df.columns]
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold", pad_edge=False)
    for column in columns:
        table.add_column(
            _COLUMN_LABELS.get(column, column),
            justify="right" if column in _RIGHT_ALIGNED else "left",
            no_wrap=column in _NO_WRAP,
        )
    for _, row in df.head(limit).iterrows():
        table.add_row(*[_fmt_cell(c, row[c]) for c in columns])
    console.print(table)
    if n > limit:
        console.print(Text(f"… and {n - limit} more", style="dim"))


def _print_json_records(df: pd.DataFrame) -> None:
    """Emit the catalogue (geometry dropped) as JSON records to stdout."""
    frame = df.drop(columns="geometry") if "geometry" in df.columns else df
    records = json.loads(frame.to_json(orient="records"))
    get_console().print_json(data=records)


# --- progress -------------------------------------------------------------


def _noop_advance(n: int = 1) -> None:
    """A do-nothing progress handle (used when progress is disabled)."""
    return None


@contextlib.contextmanager
def progress(description: str, total: int | None = None) -> Iterator[Advance]:
    """Yield a step-progress handle; a no-op under ``--quiet`` or non-TTY.

    Args:
        description: Label shown beside the bar.
        total: Expected step count (``None`` = indeterminate).

    Yields:
        A callable ``advance(n=1)`` that advances the bar.
    """
    console = get_console()
    if _options.quiet or not console.is_terminal:
        yield _noop_advance
        return
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as bar:
        task = bar.add_task(description, total=total)

        def advance(n: int = 1) -> None:
            bar.advance(task, n)

        yield advance


@contextlib.contextmanager
def download_progress(description: str, total: int | None = None) -> Iterator[Advance]:
    """Yield a byte-progress handle (rate + size); a no-op when disabled.

    Args:
        description: Label shown beside the bar.
        total: Expected byte count (``None`` = unknown size).

    Yields:
        A callable ``advance(n_bytes)`` that advances the transfer bar.
    """
    console = get_console()
    if _options.quiet or not console.is_terminal:
        yield _noop_advance
        return
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as bar:
        task = bar.add_task(description, total=total)

        def advance(n: int = 1) -> None:
            bar.advance(task, n)

        yield advance


# --- errors ---------------------------------------------------------------


def error_block(exc: BaseException, *, next_step: str | None = None) -> None:
    """Render an exception as a clean one-block message on stderr (no traceback).

    Args:
        exc: The caught exception; its ``str()`` (which often already carries a
            hint, e.g. geocoding's "Did you mean …?") is shown verbatim.
        next_step: An optional suggested remedy, shown on a second line.
    """
    console = _err()
    header = Text()
    header.append("Error ", style="bold red")
    header.append(f"[{type(exc).__name__}] ", style="red")
    header.append(str(exc))
    console.print(header)
    if next_step:
        hint = Text()
        hint.append("→ ", style="cyan")
        hint.append(next_step)
        console.print(hint)
