from __future__ import annotations

import pytest
from support import task_resource

from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.resource_task import Resource
from sonata_engine.core.selection import Selection
from sonata_engine.core.task import Task
from sonata_engine.core.workflow import Workflow
from sonata_engine.errors import SelectionError


class _Noop(Task[None]):
    def __init__(self, title: str) -> None:
        self.title = title

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome()


def _resource(title: str = "Acquire vm") -> Resource:
    return task_resource(
        title=title,
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )


def _workflow_with_resource() -> tuple[Workflow, Resource]:
    """Build -> [acquire] -> list -> invoke -> [release]."""
    resource = _resource()
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("Build"))
    workflow.add(_Noop("List"), requires=(resource,))
    workflow.add(_Noop("Invoke"), requires=(resource,))
    return workflow, resource


def test_no_selection_compiles_every_task() -> None:
    workflow, _resource = _workflow_with_resource()

    compiled = workflow.compile()

    assert [task.task_id for task in compiled.tasks] == [
        "001.build",
        "002.acquire-vm",
        "003.list",
        "004.invoke",
        "005.release-vm",
    ]


def test_empty_selection_is_the_same_as_no_selection() -> None:
    workflow, _resource = _workflow_with_resource()

    assert workflow.compile(select=Selection()).tasks == workflow.compile().tasks


def test_only_keeps_one_consumer_and_its_resource_lifecycle() -> None:
    workflow, _resource = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(only="invoke"))

    assert [task.task_id for task in compiled.tasks] == [
        "001.acquire-vm",
        "002.invoke",
        "003.release-vm",
    ]


def test_only_retains_transitive_resource_lifecycle() -> None:
    vm = _resource()
    helm = task_resource(
        title="Acquire helm",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
        requires=(vm,),
    )
    function = task_resource(
        title="Acquire function",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
        requires=(helm,),
    )
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("List"))
    workflow.add(_Noop("Invoke"), requires=(function,))

    compiled = workflow.compile(select=Selection(only="invoke"))

    assert [task.task.title for task in compiled.tasks] == [
        "Acquire vm",
        "Acquire helm",
        "Acquire function",
        "Invoke",
        "Release function",
        "Release helm",
        "Release vm",
    ]
    assert compiled.tasks[3].required_resources == (function,)
    assert compiled.tasks[2].required_resources == (helm,)


def test_only_on_a_task_without_resources_drops_the_lifecycle() -> None:
    workflow, _resource = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(only="build"))

    assert [task.task_id for task in compiled.tasks] == ["001.build"]


def test_start_keeps_the_inclusive_tail() -> None:
    workflow, _resource = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(start="list"))

    assert [task.task_id for task in compiled.tasks] == [
        "001.acquire-vm",
        "002.list",
        "003.invoke",
        "004.release-vm",
    ]


def test_until_keeps_the_inclusive_head() -> None:
    workflow, _resource = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(until="list"))

    assert [task.task_id for task in compiled.tasks] == [
        "001.build",
        "002.acquire-vm",
        "003.list",
        "004.release-vm",
    ]


def test_start_and_until_delimit_an_inclusive_range() -> None:
    workflow, _resource = _workflow_with_resource()

    compiled = workflow.compile(select=Selection(start="list", until="list"))

    assert [task.task_id for task in compiled.tasks] == [
        "001.acquire-vm",
        "002.list",
        "003.release-vm",
    ]


def test_unknown_slug_is_rejected_and_lists_what_is_available() -> None:
    workflow, _resource = _workflow_with_resource()

    with pytest.raises(SelectionError, match="no task matches slug 'deploy'") as error:
        workflow.compile(select=Selection(only="deploy"))

    assert "build" in str(error.value)


def test_a_resource_slug_is_not_selectable() -> None:
    workflow, _resource = _workflow_with_resource()

    with pytest.raises(SelectionError, match="no task matches slug 'acquire-vm'"):
        workflow.compile(select=Selection(only="acquire-vm"))


def test_duplicate_titles_are_ambiguous_when_selected() -> None:
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("Build"))
    workflow.add(_Noop("Build"))

    with pytest.raises(SelectionError, match="matches 2 tasks"):
        workflow.compile(select=Selection(only="build"))


def test_duplicate_titles_stay_legal_without_a_selection() -> None:
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("Build"))
    workflow.add(_Noop("Build"))

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.build",
        "002.build",
    ]


def test_inverted_range_is_rejected() -> None:
    workflow, _resource = _workflow_with_resource()

    with pytest.raises(SelectionError, match="comes after"):
        workflow.compile(select=Selection(start="invoke", until="build"))


def test_a_title_with_no_slug_characters_is_a_compile_error() -> None:
    workflow = Workflow(workflow_id="demo")
    workflow.add(_Noop("!!!"))

    with pytest.raises(ValueError, match="empty slug"):
        workflow.compile()


def test_compilation_stays_deterministic_under_selection() -> None:
    workflow, _resource = _workflow_with_resource()
    selection = Selection(start="list")

    assert (
        workflow.compile(select=selection).tasks
        == workflow.compile(select=selection).tasks
    )


def test_selection_does_not_mutate_the_workflow() -> None:
    workflow, _resource = _workflow_with_resource()

    workflow.compile(select=Selection(only="build"))

    assert len(workflow.compile().tasks) == 5
