"""Test-only helpers for building resources out of plain functions.

The engine has one way to declare a resource -- a pair of tasks. Most tests only
care about what acquire returns and what release saw, so these wrap a function
in a task to keep them short. Not part of the public API.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.resource_task import Resource
from sonata_engine.core.task import Task


class CallTask[T](Task[T]):
    """A task whose body is `fn`."""

    def __init__(
        self, title: str, fn: Callable[[TaskInputs], T], *, idempotent: bool = False
    ) -> None:
        self.title = title
        self.idempotent = idempotent
        self._fn = fn

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[T]:
        return TaskOutcome(value=self._fn(inputs))


def task_resource(
    title: str,
    acquire_fn: Callable[[TaskInputs], Any],
    release_fn: Callable[[TaskInputs, Any], object],
    *,
    idempotent: bool = False,
    **kwargs: Any,
) -> Resource[Any]:
    """Build a resource whose acquire is `acquire_fn` and release `release_fn`.

    `release_fn(inputs, value)` gets the acquired value from `inputs.upstream()`,
    the way a release task reads it. `idempotent` is the acquire task's.
    """

    def release(inputs: TaskInputs) -> None:
        release_fn(inputs, inputs.upstream())

    return Resource(
        title=title,
        acquire=CallTask(title, acquire_fn, idempotent=idempotent),
        release=CallTask(f"Release {title}", release),
        **kwargs,
    )
