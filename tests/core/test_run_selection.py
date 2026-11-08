from __future__ import annotations

from pathlib import Path

import pytest
from support import task_resource

from sonata_engine import (
    JournalConfig,
    Selection,
    SelectionError,
    Task,
    TaskInputs,
    TaskOutcome,
    Workflow,
    WorkflowTopologyMismatchError,
)


class _Recording(Task[None]):
    def __init__(self, title: str, log: list[str]) -> None:
        self.title = title
        self._log = log

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self._log.append(self.title)
        return TaskOutcome()


def _workflow(log: list[str]) -> Workflow:
    resource = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: log.append("acquire"),
        release_fn=lambda _inputs, _value: log.append("release"),
    )
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Recording("Build", log))
    workflow.add(_Recording("List", log), requires=(resource,))
    workflow.add(_Recording("Invoke", log), requires=(resource,))
    return workflow


def test_run_without_selection_executes_everything() -> None:
    log: list[str] = []

    _workflow(log).run()

    assert log == ["Build", "acquire", "List", "Invoke", "release"]


def test_run_with_only_executes_the_slice_and_its_resource_lifecycle() -> None:
    log: list[str] = []

    result = _workflow(log).run(select=Selection(only="invoke"))

    assert log == ["acquire", "Invoke", "release"]
    assert [execution.task_id for execution in result.tasks] == [
        "001.acquire-vm",
        "002.invoke",
        "003.release-vm",
    ]


def test_run_releases_the_resource_when_a_selected_task_fails() -> None:
    log: list[str] = []

    class _Boom(Task[None]):
        title = "Invoke"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            raise RuntimeError("boom")

    resource = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: log.append("acquire"),
        release_fn=lambda _inputs, _value: log.append("release"),
    )
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Recording("Build", log))
    workflow.add(_Boom(), requires=(resource,))

    with pytest.raises(RuntimeError, match="boom"):
        workflow.run(select=Selection(only="invoke"))

    assert log == ["acquire", "release"]


def test_run_propagates_an_unresolvable_selection() -> None:
    with pytest.raises(SelectionError, match="no task matches slug"):
        _workflow([]).run(select=Selection(only="deploy"))


def test_resume_refuses_to_cross_a_sliced_topology(tmp_path: Path) -> None:
    journal = JournalConfig(tmp_path / "journal.jsonl")
    _workflow([]).run(journal=journal)

    with pytest.raises(WorkflowTopologyMismatchError):
        _workflow([]).run(journal=journal, resume=True, select=Selection(only="invoke"))
