from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import pytest
from support import task_resource

from sonata_engine import subtask
from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.selection import Selection
from sonata_engine.core.task import ReusableTask, Task
from sonata_engine.core.workflow import Workflow
from sonata_engine.errors import InvalidTaskOutcomeError
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_engine.workflow.events import WorkflowEvent
from sonata_engine.workflow.observers import WorkflowCompletion


class _NoopTask(Task[None]):
    """A minimal real `Task[T]` subclass, used to exercise the compiler."""

    def __init__(self, title: str) -> None:
        self.title = title

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome()


class _ValueTask(Task[int]):
    title = "Produce value"

    def run(self, inputs: TaskInputs) -> TaskOutcome[int]:
        return TaskOutcome(value=42)


def test_run_is_the_single_v2_entrypoint_and_preserves_outcomes() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_ValueTask())

    result = workflow.run()

    execution = result.by_id("001.produce-value")
    assert execution.status == "passed"
    assert execution.outcome == TaskOutcome(value=42)
    assert not hasattr(workflow, "run_compiled")


def test_invalid_task_outcome_error_is_not_re_exported_from_workflow_module() -> None:
    """Assert the workflow module does not re-export `InvalidTaskOutcomeError`.

    `InvalidTaskOutcomeError` lives in `sonata_engine.errors` only -- v2 has no
    compatibility layer, so this module must not re-export it under its own name.
    """
    import sonata_engine.core.workflow as workflow_module

    assert "InvalidTaskOutcomeError" not in vars(workflow_module).get("__all__", ())


# --- Compiler: add()/compile() -------------------------------------------


def test_compile_preserves_insertion_order() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_NoopTask("Prepare source"))
    workflow.add(_NoopTask("Build amd64"))
    workflow.add(_NoopTask("Publish manifest"))

    compiled = workflow.compile()

    assert [ct.task.title for ct in compiled.tasks] == [
        "Prepare source",
        "Build amd64",
        "Publish manifest",
    ]


def test_compile_generates_ordinal_slug_ids() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_NoopTask("Prepare source"))
    workflow.add(_NoopTask("Build amd64"))
    workflow.add(_NoopTask("Publish manifest"))

    compiled = workflow.compile()

    assert [ct.task_id for ct in compiled.tasks] == [
        "001.prepare-source",
        "002.build-amd64",
        "003.publish-manifest",
    ]


def test_compile_disambiguates_duplicate_titles_by_ordinal() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_NoopTask("Build"))
    workflow.add(_NoopTask("Build"))

    compiled = workflow.compile()

    ids = [ct.task_id for ct in compiled.tasks]
    assert ids == ["001.build", "002.build"]
    assert len(set(ids)) == 2


def test_task_objects_expose_no_task_id() -> None:
    task_instance = _NoopTask("Prepare source")
    workflow = Workflow(workflow_id="wf")
    workflow.add(task_instance)

    compiled = workflow.compile()

    assert not hasattr(task_instance, "task_id")
    assert compiled.tasks[0].task_id == "001.prepare-source"


def test_repeated_compilation_is_deterministic() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_NoopTask("Prepare source"))
    workflow.add(_NoopTask("Build amd64"))

    first = workflow.compile()
    second = workflow.compile()

    assert first == second
    assert first.workflow_id == second.workflow_id == "wf"


def test_compile_requires_non_empty_workflow_id() -> None:
    workflow = Workflow(workflow_id="")
    workflow.add(_NoopTask("Prepare source"))

    with pytest.raises(ValueError, match="workflow_id"):
        workflow.compile()


# --- run(): lifecycle events ----------------------------------------------


class _FakeSink:
    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    @contextmanager
    def status(self, label: str) -> Generator[None, None, None]:
        yield


class _FailOnTaskFailedSink(_FakeSink):
    def emit(self, event: WorkflowEvent) -> None:
        super().emit(event)
        if event.kind == "task.failed":
            raise OSError("sink secondary")


class _BoomTask(Task[None]):
    title = "Boom"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        raise RuntimeError("boom")


class _BadOutcomeTask(Task[None]):
    title = "Bad outcome"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return "not-an-outcome"  # type: ignore[return-value]


class _BadReusableValue(ReusableTask):
    title = "Bad reusable value"
    reuse_key = "bad-reusable-value"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome(value=42)  # type: ignore[arg-type]


class _RecordingObserver:
    def __init__(self, events: list[str] | None = None) -> None:
        self.completions: list[WorkflowCompletion] = []
        self._events = events

    def finished(self, completion: WorkflowCompletion) -> None:
        if self._events is not None:
            self._events.append("observer")
        self.completions.append(completion)


def test_run_notifies_observer_after_success() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_NoopTask("Work"))
    observer = _RecordingObserver()

    workflow.run(observers=(observer,))

    assert len(observer.completions) == 1
    completion = observer.completions[0]
    assert completion.workflow_id == "wf"
    assert completion.error is None
    assert completion.finished_at >= completion.started_at


def test_run_notifies_observer_after_failure_cleanup() -> None:
    events: list[str] = []
    resource = task_resource(
        title="Acquire test resource",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _state: events.append("cleanup"),
    )
    workflow = Workflow(workflow_id="wf")
    workflow.add(_BoomTask(), requires=(resource,))
    observer = _RecordingObserver(events)

    with pytest.raises(RuntimeError, match="boom"):
        workflow.run(observers=(observer,))

    assert events == ["cleanup", "observer"]
    assert isinstance(observer.completions[0].error, RuntimeError)


def test_run_emits_started_then_passed() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_NoopTask("Prepare source"))

    sink = _FakeSink()
    with bind_workflow_sink(sink):
        workflow.run()

    assert [e.kind for e in sink.events] == ["task.started", "task.passed"]
    assert all(e.task_id == "001.prepare-source" for e in sink.events)


def test_run_emits_failed_on_exception_and_propagates() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_BoomTask())

    sink = _FakeSink()
    with bind_workflow_sink(sink), pytest.raises(RuntimeError, match="boom"):
        workflow.run()

    assert [e.kind for e in sink.events] == ["task.started", "task.failed"]
    assert all(e.task_id == "001.boom" for e in sink.events)


def test_failed_event_error_does_not_mask_task_root_cause() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_BoomTask())

    sink = _FailOnTaskFailedSink()
    with (
        bind_workflow_sink(sink),
        pytest.raises(RuntimeError, match="boom") as exc_info,
    ):
        workflow.run()

    assert any("sink secondary" in note for note in exc_info.value.__notes__)


def test_run_rejects_non_task_outcome_result() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_BadOutcomeTask())

    sink = _FakeSink()
    with bind_workflow_sink(sink), pytest.raises(InvalidTaskOutcomeError):
        workflow.run()

    assert [e.kind for e in sink.events] == ["task.started", "task.failed"]


def test_run_rejects_runtime_values_from_reusable_tasks() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_BadReusableValue())

    with pytest.raises(InvalidTaskOutcomeError, match="reusable"):
        workflow.run()


def test_run_stops_on_first_failure() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_BoomTask())
    workflow.add(_NoopTask("Never runs"))

    sink = _FakeSink()
    with bind_workflow_sink(sink), pytest.raises(RuntimeError, match="boom"):
        workflow.run()

    assert [e.kind for e in sink.events] == ["task.started", "task.failed"]


def test_run_is_noop_safe_without_sink() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_NoopTask("Prepare source"))

    workflow.run()  # no sink bound, must not raise


def test_subtasks_do_not_become_compiled_units() -> None:
    """Assert a step made of many subtasks stays one compiled unit.

    The whole point: a step made of many stays one unit, so ordinals and
    selection are unaffected by what a task does inside its own run().
    """

    class Composite(Task[None]):
        title = "Build images"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            for name in ("cp", "fn"):
                with subtask(task_id=f"build-images/{name}", title=name):
                    pass
            return TaskOutcome()

    sink = _FakeSink()
    workflow = Workflow(workflow_id="w")
    workflow.add(Composite())

    compiled = workflow.compile()
    with bind_workflow_sink(sink):
        workflow.run()

    assert [task.task_id for task in compiled.tasks] == ["001.build-images"]
    assert [event.task_id for event in sink.events if event.kind == "task.started"] == [
        "001.build-images",
        "build-images/cp",
        "build-images/fn",
    ]

    selected = workflow.compile(select=Selection(only="build-images"))
    assert [task.task_id for task in selected.tasks] == ["001.build-images"]
