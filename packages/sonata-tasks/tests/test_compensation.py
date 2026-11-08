from dataclasses import replace
from typing import override

import pytest

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.compensation import best_effort, compensated_resource


class _Step(Task[None]):
    def __init__(self, title: str, log: list[str], *, fail: bool = False) -> None:
        self.title = title
        self._log = log
        self._fail = fail

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self._log.append(self.title)
        if self._fail:
            raise RuntimeError(f"{self.title} failed")
        return TaskOutcome(value=None)


def test_a_failed_acquire_compensates_then_reraises() -> None:
    log: list[str] = []
    resource = compensated_resource(
        title="Acquire thing",
        acquire=_Step("acquire", log, fail=True),
        compensate=_Step("compensate", log),
    )

    with pytest.raises(RuntimeError, match="acquire failed"):
        resource.acquire.run(TaskInputs.empty())

    assert log == ["acquire", "compensate"]


def test_release_defaults_to_compensate_and_can_be_overridden() -> None:
    log: list[str] = []
    default = compensated_resource(
        title="Acquire thing",
        acquire=_Step("acquire", log),
        compensate=_Step("compensate", log),
    )
    custom = compensated_resource(
        title="Acquire thing",
        acquire=_Step("acquire", log),
        compensate=_Step("compensate", log),
        release=_Step("release", log),
    )

    default.release.run(replace(TaskInputs.empty(), _upstream="v"))
    custom.release.run(replace(TaskInputs.empty(), _upstream="v"))

    assert log == ["compensate", "release"]
    assert default.acquire.title == "Acquire thing"


def test_best_effort_propagates_programming_errors() -> None:
    error = RuntimeError("acquire failed")

    with pytest.raises(ValueError, match="bad cleanup contract"):
        best_effort(
            error,
            lambda: (_ for _ in ()).throw(ValueError("bad cleanup contract")),
            what="cleanup",
        )


def test_best_effort_notes_operational_cleanup_error_on_the_primary_error() -> None:
    error = RuntimeError("acquire failed")

    best_effort(
        error,
        lambda: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
        what="cleanup",
    )

    assert error.__notes__ == [
        "Best-effort cleanup after a failed acquire failed: cleanup failed"
    ]
