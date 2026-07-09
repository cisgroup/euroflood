"""UI-agnostic progress reporting for the core pipelines.

Pipelines report progress through `progress_bar`, a context manager that is
a **no-op by default** — so ``import euroflood`` and library/API use stay silent
and never import a UI library. The CLI injects a rich-backed factory via
`set_factory` (``console.progress``), so long producer commands (``ingest``,
``mirror``, ``export``) show a live bar on stdout — off the stderr log stream, so
logs never corrupt the bar.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager

# A progress handle: advance the bar by ``n`` steps (default 1).
Advance = Callable[[int], None]
# A factory: (description, total) -> a context manager yielding an Advance.
Factory = Callable[[str, "int | None"], AbstractContextManager[Advance]]


def _noop_advance(n: int = 1) -> None:
    """Discard a progress step (used when no UI factory is installed)."""
    return None


@contextlib.contextmanager
def _noop_factory(description: str, total: int | None = None) -> Iterator[Advance]:
    """Yield a do-nothing progress handle."""
    yield _noop_advance


_factory: Factory = _noop_factory


def set_factory(factory: Factory) -> None:
    """Install a progress factory (the CLI passes ``console.progress``)."""
    global _factory
    _factory = factory


def reset_factory() -> None:
    """Restore the silent no-op factory (used by tests)."""
    global _factory
    _factory = _noop_factory


def progress_bar(
    description: str, total: int | None = None
) -> AbstractContextManager[Advance]:
    """Return the active progress context manager (no-op unless a UI is wired).

    Args:
        description: Label shown beside the bar.
        total: Expected step count (``None`` = indeterminate).

    Returns:
        A context manager yielding an ``advance(n=1)`` callable.
    """
    return _factory(description, total)


# --- library-side, environment-aware bars (work in Jupyter + interactive TTYs) ---
#
# Unlike the CLI factory above, these render directly (no `set_factory` needed) so
# `ef.floods(...).download()` and the first-query table mirror show progress in a
# notebook. They are a no-op when not interactive, or when `settings.show_progress`
# is off — so scripts, pipes, and CI (EUROFLOOD_SHOW_PROGRESS=0) stay clean. `rich`
# is a core dependency but imported lazily to keep `import euroflood` light.


def _has_ipywidgets() -> bool:
    """True when ipywidgets is importable (rich needs it to render a bar in Jupyter)."""
    import importlib.util

    return importlib.util.find_spec("ipywidgets") is not None


def _interactive() -> bool:
    """True when a rich bar can render now (an interactive TTY, or Jupyter + ipywidgets).

    In a Jupyter kernel rich needs ipywidgets for a live bar; without it rich warns and
    degrades, so we simply skip the bar there (staying silent) rather than warn.
    """
    from .config import get_settings

    if not get_settings().show_progress:
        return False
    try:
        from rich.console import Console

        console = Console()
    except Exception:  # pragma: no cover - rich is a core dep; purely defensive
        return False
    if console.is_jupyter:
        return _has_ipywidgets()
    return bool(console.is_terminal)


@contextlib.contextmanager
def download_bar(description: str, total: int | None = None) -> Iterator[Advance]:
    """Yield a byte-transfer handle (size + rate); a no-op unless interactive+enabled."""
    if not _interactive():
        yield _noop_advance
        return
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        transient=True,
    ) as bar:
        task = bar.add_task(description, total=total)

        def advance(n: int = 1) -> None:
            bar.advance(task, n)

        yield advance


@contextlib.contextmanager
def steps_bar(description: str, total: int | None = None) -> Iterator[Advance]:
    """Yield a per-item step handle (Spinner + M-of-N bar); no-op unless interactive.

    Advances one step per completed item — used by the one-time index-table mirror
    and by parallel downloads (where a cumulative byte bar would race across
    threads and a per-file count is clearer).
    """
    if not _interactive():
        yield _noop_advance
        return
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        transient=True,
    ) as bar:
        task = bar.add_task(description, total=total)

        def advance(n: int = 1) -> None:
            bar.advance(task, n)

        yield advance


@contextlib.contextmanager
def mirror_bar(description: str, total: int | None = None) -> Iterator[Advance]:
    """Step handle for the one-time index-table mirror (see `steps_bar`)."""
    with steps_bar(description, total) as advance:
        yield advance


@contextlib.contextmanager
def file_progress(
    total: int, *, enabled: bool, description: str = "Downloading maps"
) -> Iterator[Advance]:
    """Per-file step bar for parallel downloads; advance one per completed file.

    A no-op when ``enabled`` is False — i.e. the caller drives its own byte bar via
    ``on_bytes`` (the CLI) — so two rich bars never fight over the terminal, and
    also (via `steps_bar`) when not interactive or progress is disabled.
    """
    if not enabled or not total:
        yield _noop_advance
        return
    with steps_bar(description, total) as advance:
        yield advance
