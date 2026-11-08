from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from support import task_resource

from sonata_engine import (
    Evidence,
    JournalConfig,
    Resource,
    ReusableTask,
    Steps,
    Task,
    TaskInputs,
    TaskOutcome,
    Workflow,
    WorkflowEvent,
    bind_workflow_sink,
    subtask,
)


class _Sink:
    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    @contextmanager
    def status(self, _label: str) -> Generator[None, None, None]:
        yield


class _Prepare(Task[str]):
    title = "Prepare"

    def __init__(self, operations: list[str]) -> None:
        self._operations = operations

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        with subtask(task_id="prepare/check-config", title="Check config"):
            self._operations.append("prepare")
        return TaskOutcome(value="source")


class _BuildImage(ReusableTask):
    title = "Build image"
    reuse_key = "image-v1"

    def __init__(self, builder: Resource[str], operations: list[str]) -> None:
        self._builder = builder
        self._operations = operations

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        assert inputs.resource(self._builder) == "builder-1"
        self._operations.append("build")
        return TaskOutcome(evidence=(Evidence("e2e", "image:v1"),))


class _Package(Task[str]):
    title = "Create package"

    def __init__(self, builder: Resource[str], operations: list[str]) -> None:
        self._builder = builder
        self._operations = operations

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        assert inputs.upstream() is None
        builder = inputs.resource(self._builder)
        self._operations.append("package")
        return TaskOutcome(value=f"{builder}/artifact")


class _Sign(Task[str]):
    title = "Sign package"

    def __init__(self, operations: list[str]) -> None:
        self._operations = operations

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        self._operations.append("sign")
        return TaskOutcome(value=f"{inputs.upstream()}:signed")


class _Deploy(Task[None]):
    title = "Deploy"

    def __init__(self, builder: Resource[str], operations: list[str]) -> None:
        self._builder = builder
        self._operations = operations

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        assert inputs.resource(self._builder) == "builder-1"
        self._operations.append("deploy")
        return TaskOutcome()


class _Report(Task[None]):
    title = "Report"

    def __init__(self, operations: list[str]) -> None:
        self._operations = operations

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self._operations.append("report")
        return TaskOutcome()


def _workflow(operations: list[str]) -> Workflow:
    builder = task_resource(
        title="Acquire builder",
        acquire_fn=lambda _inputs: operations.append("acquire") or "builder-1",
        release_fn=lambda _inputs, _value: operations.append("release"),
    )
    package = Steps(
        title="Package artifact",
        steps=(_Package(builder, operations), _Sign(operations)),
    )

    workflow = Workflow(workflow_id="release-e2e")
    workflow.add(_Prepare(operations))
    workflow.add(
        Steps(
            title="Build release",
            steps=(_BuildImage(builder, operations), package),
        ),
        requires=(builder,),
    )
    workflow.add(_Deploy(builder, operations), requires=(builder,))
    workflow.add(_Report(operations))
    return workflow


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_full_workflow_runs_and_resumes_end_to_end(tmp_path: Path) -> None:
    journal = JournalConfig(path=tmp_path / "release.jsonl")
    verifiers = {"e2e": lambda _evidence: True}
    operations: list[str] = []
    first_sink = _Sink()

    with bind_workflow_sink(first_sink):
        first = _workflow(operations).run(journal=journal, verifiers=verifiers)

    assert [execution.task_id for execution in first.tasks] == [
        "001.prepare",
        "002.acquire-builder",
        "003.build-release",
        "004.deploy",
        "005.release-builder",
        "006.report",
    ]
    composite = first.by_id("003.build-release")
    assert composite.outcome is not None
    assert composite.outcome.value == "builder-1/artifact:signed"
    assert operations == [
        "prepare",
        "acquire",
        "build",
        "package",
        "sign",
        "deploy",
        "release",
        "report",
    ]
    reported = next(
        event
        for event in first_sink.events
        if event.kind == "task.started" and event.task_id == "prepare/check-config"
    )
    assert reported.parent_task_id == "001.prepare"

    operations.clear()
    resumed_sink = _Sink()
    with bind_workflow_sink(resumed_sink):
        resumed = _workflow(operations).run(
            journal=journal,
            resume=True,
            verifiers=verifiers,
        )

    assert operations == [
        "prepare",
        "acquire",
        "package",
        "sign",
        "deploy",
        "release",
        "report",
    ]
    assert resumed.by_id("003.build-release").status == "passed"
    skipped = next(
        event for event in resumed_sink.events if event.kind == "task.skipped"
    )
    assert skipped.task_id == "003.build-release/build-image"
    nested = next(
        event
        for event in resumed_sink.events
        if event.kind == "task.started"
        and event.task_id == "003.build-release/package-artifact/sign-package"
    )
    assert nested.parent_task_id == "003.build-release/package-artifact"

    latest = {record["task_id"]: record["status"] for record in _records(journal.path)}
    assert latest["003.build-release/build-image"] == "skipped"
    assert latest["003.build-release/package-artifact/create-package"] == "passed"
    assert latest["005.release-builder"] == "passed"
    assert "prepare/check-config" not in latest
