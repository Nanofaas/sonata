"""Helpers for undoing partial work without masking the original failure."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome


def best_effort(
    error: BaseException, cleanup: Callable[[], object], *, what: str
) -> None:
    """Run cleanup without replacing the error that required it."""
    try:
        _ = cleanup()
    except (OSError, RuntimeError) as cleanup_error:
        error.add_note(
            f"Best-effort {what} after a failed acquire failed: {cleanup_error}"
        )


class _CompensatedAcquire[T](Task[T]):
    """Run ``acquire``; if it fails, compensate best-effort and re-raise."""

    def __init__(self, title: str, acquire: Task[T], compensate: Task[Any]) -> None:
        self.title = title
        self.idempotent = acquire.idempotent
        self._acquire = acquire
        self._compensate = compensate

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[T]:
        try:
            return self._acquire.run(inputs)
        except BaseException as error:
            best_effort(
                error,
                lambda: self._compensate.run(inputs),
                what=f"compensation for {self.title}",
            )
            raise


def compensated_resource[T](
    *,
    title: str,
    acquire: Task[T],
    compensate: Task[Any],
    release: Task[None] | None = None,
    requires: tuple[Resource[Any], ...] = (),
    always_release: bool = False,
    revive: Callable[[Any], T] | None = None,
) -> Resource[T]:
    """Build a resource whose failed acquire performs best-effort compensation.

    ``release`` defaults to ``compensate``.
    """
    return Resource(
        title=title,
        acquire=_CompensatedAcquire(title, acquire, compensate),
        release=compensate if release is None else release,
        requires=requires,
        always_release=always_release,
        revive=revive,
    )
