"""A resource is a pair of tasks, run through the normal runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import override

import pytest
from support import CallTask

from sonata_engine import (
    JournalConfig,
    Resource,
    Steps,
    Task,
    TaskInputs,
    TaskOutcome,
    Workflow,
)
from sonata_engine.core.workflow import _StepScope
from sonata_engine.errors import AmbiguousTaskStateError
from sonata_engine.retention import release_retained


class _Use(Task[None]):
    title = "Use"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        del inputs
        return TaskOutcome()


def _task_ids(path: Path) -> list[str]:
    return [
        record["task_id"]
        for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if "task_id" in record
    ]


def _workflow(resource: Resource[object], *, keep: bool = False) -> Workflow:
    workflow = Workflow(workflow_id="wf", keep=keep)
    workflow.add(_Use(), requires=(resource,))
    return workflow


def test_release_task_reads_acquired_value_as_upstream() -> None:
    released: list[object] = []
    resource = Resource(
        title="Acquire vm",
        acquire=CallTask("Acquire vm", lambda _inputs: "vm-1"),
        release=CallTask(
            "Release vm", lambda inputs: released.append(inputs.upstream())
        ),
    )

    _workflow(resource).run()

    assert released == ["vm-1"]


@pytest.mark.parametrize("idempotent", [True, False])
def test_acquire_idempotence_comes_from_the_task(
    tmp_path: Path, idempotent: bool
) -> None:
    attempts: list[int] = []

    def acquire(_inputs: TaskInputs) -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("acquire failed")
        return "db"

    resource = Resource(
        title="Acquire db",
        acquire=CallTask("Acquire db", acquire, idempotent=idempotent),
        release=CallTask("Release db", lambda _inputs: None),
    )
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    with pytest.raises(RuntimeError, match="acquire failed"):
        _workflow(resource).run(journal=config)

    if idempotent:
        _workflow(resource).run(journal=config, resume=True)
        assert attempts == [1, 2]
    else:
        with pytest.raises(AmbiguousTaskStateError):
            _workflow(resource).run(journal=config, resume=True)
        assert attempts == [1]


def _steps_resource(ran: list[str]) -> Resource[object]:
    def step(name: str, value: object = None) -> CallTask[object]:
        return CallTask(name, lambda inputs: ran.append(name) or value)

    return Resource(
        title="Acquire db",
        acquire=Steps(title="Acquire db", steps=(step("a"), step("b", "db-1"))),
        release=Steps(
            title="Release db",
            steps=(
                CallTask(
                    "stop", lambda inputs: ran.append(f"stop:{inputs.upstream()}")
                ),
                step("wipe"),
            ),
        ),
    )


def test_steps_acquire_is_journalled_per_step(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")

    _workflow(_steps_resource([])).run(journal=config)

    ids = _task_ids(config.path)
    assert "001.acquire-db/a" in ids
    assert "001.acquire-db/b" in ids


def test_steps_release_runs_under_runner(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    ran: list[str] = []

    _workflow(_steps_resource(ran)).run(journal=config)

    assert ran == ["a", "b", "stop:db-1", "wipe"]
    ids = _task_ids(config.path)
    assert "003.release-db/stop" in ids
    assert "003.release-db/wipe" in ids


def test_release_retained_runs_steps_release(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    ran: list[str] = []
    resource = _steps_resource(ran)
    _workflow(resource, keep=True).run(journal=config)
    assert ran == ["a", "b"]

    released = release_retained({"Acquire db": resource}, config)

    assert released == ("Acquire db",)
    assert ran == ["a", "b", "stop:db-1", "wipe"]


class _Payload(Task[None]):
    title = "Acquire db"

    def __init__(self, payload: str) -> None:
        self._payload = payload

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        del inputs
        return TaskOutcome()

    @override
    def _fingerprint_payload(self) -> object:
        return self._payload


def test_fingerprint_changes_with_acquire_task_payload() -> None:
    def fingerprint(payload: str) -> str:
        resource = Resource(
            title="Acquire db",
            acquire=_Payload(payload),
            release=CallTask("Release db", lambda _inputs: None),
        )
        return _workflow(resource).compile().fingerprint

    assert fingerprint("v1") == fingerprint("v1")
    assert fingerprint("v1") != fingerprint("v2")


def test_rebased_scope_keeps_prefix_journal_and_resume() -> None:
    scope = _StepScope(
        prefix="001.acquire-db",
        base_inputs=TaskInputs.empty(),
        jrnl=None,
        resume=True,
    )
    inputs = TaskInputs.empty()

    rebased = scope.rebased(inputs)

    assert rebased.base_inputs is inputs
    assert (rebased.prefix, rebased.jrnl, rebased.resume) == (
        "001.acquire-db",
        None,
        True,
    )
