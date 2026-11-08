"""Releasing what a kept run left behind, from a later process."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from support import task_resource

from sonata_engine import Resource, Workflow
from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.task import Task
from sonata_engine.journal import JournalConfig
from sonata_engine.retention import UnknownRetainedResourceError, release_retained


@dataclasses.dataclass(frozen=True)
class _VmInfo:
    name: str
    host: str


class _Use(Task[None]):
    title = "Use"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        del inputs
        return TaskOutcome()


def _resource(title: str, value: object, calls: list[str], **kwargs) -> Resource:
    return task_resource(
        title=title,
        acquire_fn=lambda _inputs: value,
        release_fn=lambda _inputs, released: calls.append(f"{title}:{released}"),
        **kwargs,
    )


def _kept_run(
    tmp_path: Path, resources: tuple[Resource, ...], calls: list[str]
) -> Path:
    workflow = Workflow(workflow_id="wf", keep=True)
    workflow.add(_Use(), requires=resources)
    path = tmp_path / "journal.jsonl"
    workflow.run(journal=JournalConfig(path))
    assert calls == []
    return path


def test_releases_retained_resources_in_reverse_order(tmp_path: Path) -> None:
    calls: list[str] = []
    stack = _resource("Acquire stack", "s", calls)
    loadgen = _resource("Acquire loadgen", "l", calls)
    path = _kept_run(tmp_path, (stack, loadgen), calls)

    release_retained({r.title: r for r in (stack, loadgen)}, JournalConfig(path))

    assert calls == ["Acquire loadgen:l", "Acquire stack:s"]


def test_revive_rebuilds_the_value_the_release_expects(tmp_path: Path) -> None:
    """Rebuild the value the release callback expects from its own record.

    The journal can only hold JSON, but a release written against a dataclass
    would break on a dict. The resource says how to read its own record back.
    """
    seen: list[object] = []
    vm = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: _VmInfo(name="stack", host="10.0.0.1"),
        release_fn=lambda _inputs, info: seen.append(info),
        revive=lambda raw: _VmInfo(**raw),
    )
    workflow = Workflow(workflow_id="wf", keep=True)
    workflow.add(_Use(), requires=(vm,))
    path = tmp_path / "journal.jsonl"
    workflow.run(journal=JournalConfig(path))

    release_retained({vm.title: vm}, JournalConfig(path))

    assert seen == [_VmInfo(name="stack", host="10.0.0.1")]


def test_a_second_teardown_is_a_no_op(tmp_path: Path) -> None:
    calls: list[str] = []
    stack = _resource("Acquire stack", "s", calls)
    path = _kept_run(tmp_path, (stack,), calls)
    config = JournalConfig(path)

    release_retained({stack.title: stack}, config)
    release_retained({stack.title: stack}, config)

    assert calls == ["Acquire stack:s"]


def test_a_retained_resource_the_caller_cannot_build_is_reported(
    tmp_path: Path,
) -> None:
    """Report a retained resource the caller did not supply.

    Silently skipping it would report a clean teardown while the thing keeps
    running. Refusing outright would be worse: the resources the caller *did*
    supply are usually the expensive ones, and they would stay up too.
    """
    calls: list[str] = []
    stack = _resource("Acquire stack", "s", calls)
    orphan = _resource("Acquire orphan", "o", calls)
    path = _kept_run(tmp_path, (stack, orphan), calls)

    with pytest.raises(UnknownRetainedResourceError, match="Acquire orphan"):
        release_retained({stack.title: stack}, JournalConfig(path))

    assert calls == ["Acquire stack:s"]


def test_one_failing_release_does_not_strand_the_others(tmp_path: Path) -> None:
    calls: list[str] = []
    stack = _resource("Acquire stack", "s", calls)
    broken = task_resource(
        title="Acquire broken",
        acquire_fn=lambda _inputs: "b",
        release_fn=_boom,
    )
    path = _kept_run(tmp_path, (stack, broken), calls)

    with pytest.raises(RuntimeError, match="boom"):
        release_retained({r.title: r for r in (stack, broken)}, JournalConfig(path))

    assert calls == ["Acquire stack:s"]


def _boom(_inputs: TaskInputs, _value: object) -> None:
    raise RuntimeError("boom")


def test_records_the_release_so_the_journal_stays_truthful(tmp_path: Path) -> None:
    calls: list[str] = []
    stack = _resource("Acquire stack", "s", calls)
    path = _kept_run(tmp_path, (stack,), calls)

    release_retained({stack.title: stack}, JournalConfig(path))

    kinds = [
        json.loads(line).get("kind")
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    assert kinds.count("retained") == 1
    assert kinds.count("released") == 1
