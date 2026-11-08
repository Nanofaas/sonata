"""Minimal task resources for tests that only need something to depend on."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome


class _Call[T](Task[T]):
    def __init__(self, title: str, fn: Callable[[TaskInputs], T]) -> None:
        self.title = title
        self._fn = fn

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[T]:
        return TaskOutcome(value=self._fn(inputs))


def stub_resource(
    title: str,
    value: Any = None,
    *,
    on_acquire: Callable[[], object] = lambda: None,
    on_release: Callable[[], object] = lambda: None,
) -> Resource[Any]:
    """Build a resource that yields ``value``, calling the hooks as it is used."""

    def acquire(_inputs: TaskInputs) -> Any:
        _ = on_acquire()
        return value

    def release(_inputs: TaskInputs) -> None:
        _ = on_release()

    return Resource(
        title=title,
        acquire=_Call(title, acquire),
        release=_Call(f"Release {title}", release),
    )


def acquired[T](resource: Resource[T]) -> T:
    """Run the resource's acquire task on empty inputs and return its value."""
    value = resource.acquire.run(TaskInputs.empty()).value
    assert value is not None
    return value
