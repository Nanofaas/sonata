from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import pytest
from support import task_resource

from sonata_engine import Task, TaskInputs, TaskOutcome, Workflow
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_engine.workflow.events import WorkflowEvent


class _FakeSink:
    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    @contextmanager
    def status(self, label: str) -> Generator[None, None, None]:
        yield


class _Echo(Task[str]):
    title = "Echo"

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        return TaskOutcome(value="echoed")


def test_a_unit_receives_a_step_scope() -> None:
    """Assert every consumer or acquire unit is given a step scope.

    The runner attaches one to every consumer/acquire unit, so any such task
    can be a composite. Release units are the exception: `_release` calls
    `task.run()` directly (its own cleanup-specific path), so a release task's
    `_step_scope` is always `None` -- harmless today since releases are always
    engine-generated, never user-written composites.
    """
    seen: list[object] = []

    class Peeker(Task[None]):
        title = "Peeker"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            seen.append(inputs._step_scope)
            return TaskOutcome()

    workflow = Workflow(workflow_id="w")
    workflow.add(Peeker())
    workflow.run()

    assert seen[0] is not None


def test_the_scope_names_a_step_under_the_compiled_unit_id() -> None:
    ids: list[str] = []

    class Runner(Task[None]):
        title = "Runner"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            scope = inputs._step_scope
            assert scope is not None
            ids.append(scope.run_step(_Echo(), "echo", upstream=None).task_id)
            return TaskOutcome()

    workflow = Workflow(workflow_id="w")
    workflow.add(Runner())

    sink = _FakeSink()
    with bind_workflow_sink(sink):
        workflow.run()

    assert ids == ["001.runner/echo"]
    started = [e.task_id for e in sink.events if e.kind == "task.started"]
    assert started == ["001.runner", "001.runner/echo"]
    child = next(e for e in sink.events if e.task_id == "001.runner/echo")
    assert child.parent_task_id == "001.runner"


def test_the_step_receives_the_upstream_and_the_composite_resources() -> None:
    seen: list[object] = []
    resource = task_resource(
        title="Acquire token",
        acquire_fn=lambda _inputs: "token-42",
        release_fn=lambda _inputs, _value: None,
    )

    class Reader(Task[None]):
        title = "Reader"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            seen.append((inputs.upstream(), inputs.resource(resource)))
            return TaskOutcome()

    class Runner(Task[None]):
        title = "Runner"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            scope = inputs._step_scope
            assert scope is not None
            scope.run_step(Reader(), "reader", upstream="rel-42")
            return TaskOutcome()

    workflow = Workflow(workflow_id="w")
    workflow.add(Runner(), requires=(resource,))
    workflow.run()

    assert seen == [("rel-42", "token-42")]


def test_a_step_gets_its_own_nested_scope() -> None:
    """So a composite among the steps nests one level further."""
    ids: list[str] = []

    class Inner(Task[None]):
        title = "Inner"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            scope = inputs._step_scope
            assert scope is not None
            ids.append(scope.run_step(_Echo(), "echo", upstream=None).task_id)
            return TaskOutcome()

    class Outer(Task[None]):
        title = "Outer"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            scope = inputs._step_scope
            assert scope is not None
            scope.run_step(Inner(), "inner", upstream=None)
            return TaskOutcome()

    workflow = Workflow(workflow_id="w")
    workflow.add(Outer())

    sink = _FakeSink()
    with bind_workflow_sink(sink):
        workflow.run()

    assert ids == ["001.outer/inner/echo"]
    echo = next(
        event
        for event in sink.events
        if event.kind == "task.started" and event.task_id == "001.outer/inner/echo"
    )
    assert echo.parent_task_id == "001.outer/inner"


@pytest.mark.parametrize("bad_slug", ["", "with/slash"])
def test_run_step_rejects_an_empty_or_slash_containing_slug(bad_slug: str) -> None:
    """Assert the step scope rejects an empty or slash-containing slug.

    `Steps.__init__` rejects empty and duplicate slugs, but the scope is also
    reachable directly (as this test does) -- the pattern a downstream author
    will copy, so it must reject a malformed slug too.
    """

    class Runner(Task[None]):
        title = "Runner"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            scope = inputs._step_scope
            assert scope is not None
            scope.run_step(_Echo(), bad_slug, upstream=None)
            return TaskOutcome()

    workflow = Workflow(workflow_id="w")
    workflow.add(Runner())

    with pytest.raises(ValueError, match="must be non-empty and contain no '/'"):
        workflow.run()


def test_a_failing_step_emits_child_and_parent_failures() -> None:
    class Boom(Task[None]):
        title = "Boom"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            raise RuntimeError("boom")

    class Outer(Task[None]):
        title = "Outer"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            scope = inputs._step_scope
            assert scope is not None
            scope.run_step(Boom(), "boom", upstream=None)
            return TaskOutcome()

    workflow = Workflow(workflow_id="w")
    workflow.add(Outer())
    sink = _FakeSink()

    with bind_workflow_sink(sink), pytest.raises(RuntimeError, match="boom"):
        workflow.run()

    failed = [event.task_id for event in sink.events if event.kind == "task.failed"]
    assert failed == ["001.outer/boom", "001.outer"]
