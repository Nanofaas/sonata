"""Package-private: build a resource from an acquire body and a release body."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome


class _Acquire[T](Task[T]):
    def __init__(self, title: str, body: Callable[[TaskInputs], T]) -> None:
        self.title = title
        self._body = body

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[T]:
        return TaskOutcome(value=self._body(inputs))


class _Release[T](Task[None]):
    def __init__(self, title: str, body: Callable[[TaskInputs, T], object]) -> None:
        self.title = title
        self._body = body

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        _ = self._body(inputs, inputs.upstream())
        return TaskOutcome(value=None)


def resource_of[T](
    title: str,
    acquire: Callable[[TaskInputs], T],
    release: Callable[[TaskInputs, T], object],
    **options: Any,  # noqa: ANN401
) -> Resource[T]:
    """Build a `Resource` whose tasks run ``acquire`` and ``release``.

    The release task reads the acquired value from `inputs.upstream()` and is
    titled the way the engine titles the release unit.
    """
    return Resource(
        title,
        _Acquire(title, acquire),
        _Release(f"Release {title.removeprefix('Acquire ')}", release),
        **options,
    )
