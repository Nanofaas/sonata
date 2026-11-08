from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest
from support import task_resource

from sonata_engine.core.compiled import CompiledTask, CompiledWorkflow
from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.task import ReusableTask, Task
from sonata_engine.core.workflow import Workflow
from sonata_engine.errors import MissingAcquireUnitError


class _NoopTask(Task[None]):
    def __init__(self, title: str) -> None:
        self.title = title

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome()


class _ReusableNoop(ReusableTask):
    title = "Build"

    def __init__(self, reuse_key: str) -> None:
        self._reuse_key = reuse_key

    @property
    def reuse_key(self) -> str:
        return self._reuse_key

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome()


def _legacy_single_task_fingerprint(
    task: Task[object], task_id: str, payload: object
) -> str:
    """Return the pre-refactor canonical fingerprint, the compatibility oracle."""
    topology = [
        (
            task_id,
            "consumer",
            f"{type(task).__module__}.{type(task).__qualname__}",
            payload,
            (),
        )
    ]
    canonical = json.dumps(
        ["w", topology],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def test_fingerprint_is_unchanged_for_a_task_contributing_no_payload() -> None:
    task = _NoopTask("Plain")
    workflow = Workflow(workflow_id="w")
    workflow.add(task)

    assert task._fingerprint_payload() is None
    assert workflow.compile().fingerprint == _legacy_single_task_fingerprint(
        task, "001.plain", None
    )


def test_reusable_task_still_contributes_its_reuse_key() -> None:
    task = _ReusableNoop("key-1")
    workflow = Workflow(workflow_id="w")
    workflow.add(task)

    assert task._fingerprint_payload() == "key-1"
    assert workflow.compile().fingerprint == _legacy_single_task_fingerprint(
        task, "001.build", "key-1"
    )


def test_a_payload_change_changes_the_fingerprint() -> None:
    class Payloaded(Task[None]):
        title = "Payloaded"

        def __init__(self, payload: object) -> None:
            self._payload = payload

        def _fingerprint_payload(self) -> object:
            return self._payload

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            return TaskOutcome()

    def fingerprint_for(payload: object) -> str:
        workflow = Workflow(workflow_id="w")
        workflow.add(Payloaded(payload))
        return workflow.compile().fingerprint

    assert fingerprint_for(["a"]) != fingerprint_for(["b"])


def test_compiled_task_defaults_to_no_required_resources() -> None:
    compiled_task = CompiledTask(task_id="001.noop", task=_NoopTask("Noop"))

    assert compiled_task.required_resources == ()


def test_compiled_task_is_frozen() -> None:
    compiled_task = CompiledTask(task_id="001.noop", task=_NoopTask("Noop"))

    with pytest.raises(dataclasses.FrozenInstanceError):
        compiled_task.task_id = "002.noop"  # type: ignore[misc]


def test_compiled_workflow_is_frozen() -> None:
    compiled_task = CompiledTask(task_id="001.noop", task=_NoopTask("Noop"))
    compiled_workflow = CompiledWorkflow(workflow_id="wf", tasks=(compiled_task,))

    with pytest.raises(dataclasses.FrozenInstanceError):
        compiled_workflow.workflow_id = "other"  # type: ignore[misc]


def test_compiled_task_holds_the_only_id() -> None:
    task_instance = _NoopTask("Noop")
    compiled_task = CompiledTask(task_id="001.noop", task=task_instance)

    assert not hasattr(compiled_task.task, "task_id")
    assert compiled_task.task_id == "001.noop"


def test_reusable_task_configuration_changes_workflow_fingerprint() -> None:
    first = CompiledWorkflow(
        workflow_id="wf",
        tasks=(CompiledTask(task_id="001.build", task=_ReusableNoop("source=old")),),
    )
    second = CompiledWorkflow(
        workflow_id="wf",
        tasks=(CompiledTask(task_id="001.build", task=_ReusableNoop("source=new")),),
    )

    assert first.fingerprint != second.fingerprint


def test_resource_dependency_edges_change_workflow_fingerprint() -> None:
    vm = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )
    helm = task_resource(
        title="Acquire helm",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
        requires=(vm,),
    )
    task = _NoopTask("Deploy")
    first = CompiledWorkflow(
        workflow_id="wf",
        tasks=(
            CompiledTask(
                task_id="001.acquire-vm",
                task=_NoopTask("Acquire vm"),
                resource=vm,
                kind="acquire",
            ),
            CompiledTask(
                task_id="002.acquire-helm",
                task=_NoopTask("Acquire helm"),
                resource=helm,
                kind="acquire",
                required_resources=(vm,),
            ),
            CompiledTask(task_id="003.deploy", task=task, required_resources=(helm,)),
        ),
    )
    second = CompiledWorkflow(
        workflow_id="wf",
        tasks=(
            CompiledTask(
                task_id="001.acquire-vm",
                task=_NoopTask("Acquire vm"),
                resource=vm,
                kind="acquire",
            ),
            CompiledTask(
                task_id="002.acquire-helm",
                task=_NoopTask("Acquire helm"),
                resource=helm,
                kind="acquire",
                required_resources=(vm,),
            ),
            CompiledTask(task_id="003.deploy", task=task, required_resources=(vm,)),
        ),
    )

    assert first.fingerprint != second.fingerprint


def test_fingerprint_raises_typed_error_for_resource_with_no_acquire_unit() -> None:
    """Assert a unit with a resource but no acquire unit raises a typed error.

    `CompiledWorkflow`/`CompiledTask` are public exports: a hand-built one that
    names a resource with no matching acquire unit must fail with a readable,
    typed error instead of a bare `KeyError`.
    """
    orphan = task_resource(
        title="Acquire orphan",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )
    task = CompiledTask(
        task_id="001.use", task=_NoopTask("Use"), required_resources=(orphan,)
    )
    compiled = CompiledWorkflow(workflow_id="wf", tasks=(task,))

    with pytest.raises(MissingAcquireUnitError, match="Acquire orphan"):
        _ = compiled.fingerprint
