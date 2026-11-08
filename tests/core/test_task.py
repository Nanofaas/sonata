from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import override

import pytest
from support import task_resource

from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import Evidence, TaskOutcome
from sonata_engine.core.task import ReusableTask, Task
from sonata_engine.errors import ResourceUnavailableError, UndeclaredResourceError


class ValidTask(Task[int]):
    title = "Valid"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[int]:
        assert inputs == TaskInputs.empty()
        return TaskOutcome(value=42)


def test_valid_task_subclass_is_accepted() -> None:
    task = ValidTask()
    outcome = task.run(TaskInputs.empty())
    assert isinstance(task, Task)
    assert outcome == TaskOutcome(value=42)


def test_old_structural_dataclass_is_not_a_task() -> None:
    @dataclass
    class LegacyTask:
        task_id: str = "legacy"
        title: str = "Legacy"

        def run(self) -> int:
            return 1

    assert not isinstance(LegacyTask(), Task)


def test_task_is_abstract_and_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        Task()  # type: ignore[abstract]


def test_reusable_task_returns_none_outcome() -> None:
    class MyReusable(ReusableTask):
        title = "Reusable"
        reuse_key = "reusable-v1"

        @override
        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            return TaskOutcome()

    task = MyReusable()
    assert isinstance(task, Task)
    assert task.reusable is True
    assert task.run(TaskInputs.empty()) == TaskOutcome()


def test_reusable_task_requires_a_semantic_reuse_key() -> None:
    class MissingReuseKey(ReusableTask):
        title = "Reusable"

        @override
        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            return TaskOutcome()

    with pytest.raises(TypeError):
        MissingReuseKey()  # type: ignore[abstract]


def test_evidence_digest_defaults_to_none() -> None:
    evidence = Evidence(kind="log", reference="s3://bucket/key")
    assert evidence.digest is None


def test_task_inputs_only_exposes_declared_resource_values() -> None:
    database = task_resource(
        title="Acquire database",
        acquire_fn=lambda _inputs: "postgres://db",
        release_fn=lambda _inputs, _value: None,
    )
    cache = task_resource(
        title="Acquire cache",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )

    inputs = TaskInputs._for_resources(
        {database: "postgres://db", cache: None}, {database}
    )

    assert inputs.resource(database) == "postgres://db"
    with pytest.raises(UndeclaredResourceError):
        inputs.resource(cache)


def test_task_inputs_rejects_a_declared_but_unavailable_resource() -> None:
    cache = task_resource(
        title="Acquire cache",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )

    inputs = TaskInputs._for_resources({}, {cache})

    with pytest.raises(ResourceUnavailableError):
        inputs.resource(cache)


def test_task_inputs_constructor_snapshots_mutable_arguments() -> None:
    database = task_resource(
        title="Acquire database",
        acquire_fn=lambda _inputs: "postgres://db",
        release_fn=lambda _inputs, _value: None,
    )
    values: dict[int, object] = {id(database): "postgres://original"}
    accessible = {id(database)}
    inputs = TaskInputs(values, accessible)

    values[id(database)] = "postgres://mutated"
    accessible.clear()

    assert inputs.resource(database) == "postgres://original"


def test_task_inputs_resource_preserves_its_static_type() -> None:
    fixture = Path(__file__).parent.parent / "typecheck" / "resource_contracts.py"
    result = subprocess.run(
        [sys.executable, "-m", "basedpyright", str(fixture)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout


def test_invalid_run_override_fails_static_type_check() -> None:
    """Assert the negative type-check fixture fails basedpyright.

    The fixture (`InvalidTask.run` returning `None`) must fail basedpyright.
    Run as a subprocess so the normal type-check suite (`uv run basedpyright`,
    scoped to src/) stays green.
    """
    fixture = Path(__file__).parent.parent / "typecheck" / "task_contracts.py"
    result = subprocess.run(
        [sys.executable, "-m", "basedpyright", str(fixture)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout
    assert "reportIncompatibleMethodOverride" in result.stdout
    assert "reportAbstractUsage" in result.stdout
