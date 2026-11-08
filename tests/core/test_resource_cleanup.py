from __future__ import annotations

import json
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from support import task_resource

from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.resource_task import Resource
from sonata_engine.core.task import Task
from sonata_engine.core.workflow import Workflow
from sonata_engine.errors import ResourceDependencyCycleError, UndeclaredResourceError
from sonata_engine.journal import Journal, JournalConfig
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_engine.workflow.events import WorkflowEvent


class _RecordTask(Task[None]):
    """A real `Task[None]` consumer that records its title and can fail."""

    def __init__(self, title: str, calls: list[str], *, fail: bool = False) -> None:
        self.title = title
        self._calls = calls
        self._fail = fail

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self._calls.append(self.title)
        if self._fail:
            raise RuntimeError(f"{self.title} failed")
        return TaskOutcome()


def _resource(name: str, calls: list[str], *, always_release: bool = False) -> Resource:
    return task_resource(
        title=f"Acquire {name}",
        acquire_fn=lambda _inputs: calls.append(f"acquire.{name}"),
        release_fn=lambda _inputs, _value: calls.append(f"release.{name}"),
        always_release=always_release,
    )


def _workflow(**kwargs: object) -> Workflow:
    return Workflow(workflow_id="wf", **kwargs)  # type: ignore[arg-type]


class _FailAcquirePassedSink:
    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)
        if event.kind == "task.passed" and event.task_id == "001.acquire-db":
            raise OSError("event sink failed")

    @contextmanager
    def status(self, _label: str) -> Generator[None, None, None]:
        yield


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    @contextmanager
    def status(self, _label: str) -> Generator[None, None, None]:
        yield


# --- Step 1: topology --------------------------------------------------------


def test_acquire_before_first_consumer_release_after_last() -> None:
    calls: list[str] = []
    db = _resource("db", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("Prepare", calls))
    workflow.add(_RecordTask("Use1", calls), requires=(db,))
    workflow.add(_RecordTask("Use2", calls), requires=(db,))
    workflow.add(_RecordTask("Finish", calls))

    compiled = workflow.compile()

    assert [ct.task.title for ct in compiled.tasks] == [
        "Prepare",
        "Acquire db",
        "Use1",
        "Use2",
        "Release db",
        "Finish",
    ]
    assert [ct.kind for ct in compiled.tasks] == [
        "consumer",
        "acquire",
        "consumer",
        "consumer",
        "release",
        "consumer",
    ]


def test_resource_entries_get_ordinal_slug_ids() -> None:
    calls: list[str] = []
    db = _resource("db", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("Prepare", calls))
    workflow.add(_RecordTask("Use1", calls), requires=(db,))
    workflow.add(_RecordTask("Use2", calls), requires=(db,))
    workflow.add(_RecordTask("Finish", calls))

    compiled = workflow.compile()

    assert [ct.task_id for ct in compiled.tasks] == [
        "001.prepare",
        "002.acquire-db",
        "003.use1",
        "004.use2",
        "005.release-db",
        "006.finish",
    ]


def test_repeated_resource_compilation_is_deterministic() -> None:
    calls: list[str] = []
    db = _resource("db", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls), requires=(db,))

    assert workflow.compile() == workflow.compile()


def test_overlapping_resources_release_in_reverse_at_shared_point() -> None:
    calls: list[str] = []
    a = _resource("a", calls)
    b = _resource("b", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("T1", calls), requires=(a,))
    workflow.add(_RecordTask("T2", calls), requires=(a, b))

    compiled = workflow.compile()

    assert [ct.task.title for ct in compiled.tasks] == [
        "Acquire a",
        "T1",
        "Acquire b",
        "T2",
        "Release b",
        "Release a",
    ]


def test_single_consumer_resource_wraps_that_consumer() -> None:
    calls: list[str] = []
    r = _resource("r", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("Only", calls), requires=(r,))

    compiled = workflow.compile()

    assert [ct.task.title for ct in compiled.tasks] == [
        "Acquire r",
        "Only",
        "Release r",
    ]


# --- Step 2: lifecycle -------------------------------------------------------


def test_releases_run_in_reverse_acquisition_order() -> None:
    calls: list[str] = []
    first = _resource("first", calls)
    second = _resource("second", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls), requires=(first, second))

    workflow.run()

    assert calls == [
        "acquire.first",
        "acquire.second",
        "Use",
        "release.second",
        "release.first",
    ]


def test_structurally_equal_resources_keep_distinct_runtime_values_and_releases() -> (
    None
):
    acquired = iter(("first", "second"))
    released: list[str] = []

    def acquire(_inputs: TaskInputs) -> str:
        return next(acquired)

    def release(_inputs: TaskInputs, value: str) -> None:
        released.append(value)

    first = task_resource(
        title="Acquire service", acquire_fn=acquire, release_fn=release
    )
    second = task_resource(
        title="Acquire service", acquire_fn=acquire, release_fn=release
    )

    class _UseServices(Task[None]):
        title = "Use services"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            assert inputs.resource(first) == "first"
            assert inputs.resource(second) == "second"
            return TaskOutcome()

    Workflow(workflow_id="wf").add(_UseServices(), requires=(first, second)).run()

    assert released == ["second", "first"]


def test_resource_dependencies_acquire_and_release_in_dependency_order() -> None:
    calls: list[str] = []
    vm = _resource("vm", calls)
    helm = task_resource(
        title="Acquire helm",
        acquire_fn=lambda inputs: (
            calls.append(f"acquire.helm:{inputs.resource(vm)}") or "helm"
        ),
        release_fn=lambda inputs, _value: calls.append(
            f"release.helm:{inputs.resource(vm)}"
        ),
        requires=(vm,),
    )
    function = task_resource(
        title="Acquire function",
        acquire_fn=lambda inputs: (
            calls.append(f"acquire.function:{inputs.resource(helm)}") or "function"
        ),
        release_fn=lambda inputs, _value: calls.append(
            f"release.function:{inputs.resource(helm)}"
        ),
        requires=(helm,),
    )

    class _UseFunction(Task[None]):
        title = "Use function"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            assert inputs.resource(function) == "function"
            with pytest.raises(UndeclaredResourceError):
                inputs.resource(helm)
            with pytest.raises(UndeclaredResourceError):
                inputs.resource(vm)
            calls.append("consumer")
            return TaskOutcome()

    Workflow(workflow_id="wf").add(_UseFunction(), requires=(function,)).run()

    assert calls == [
        "acquire.vm",
        "acquire.helm:None",
        "acquire.function:helm",
        "consumer",
        "release.function:helm",
        "release.helm:None",
        "release.vm",
    ]


def test_resource_dependency_cycle_reports_the_cycle_path() -> None:
    first = _resource("first", [])
    second = task_resource(
        title="Acquire second",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
        requires=(first,),
    )
    object.__setattr__(first, "requires", (second,))
    workflow = _workflow()
    workflow.add(_RecordTask("Use", []), requires=(first,))

    with pytest.raises(
        ResourceDependencyCycleError,
        match=r"Acquire first.*Acquire second.*Acquire first",
    ):
        workflow.compile()


def test_direct_resource_dependency_cycle_reports_the_cycle_path() -> None:
    resource = _resource("loop", [])
    object.__setattr__(resource, "requires", (resource,))
    workflow = _workflow()
    workflow.add(_RecordTask("Use", []), requires=(resource,))

    with pytest.raises(
        ResourceDependencyCycleError,
        match=r"Acquire loop.*Acquire loop",
    ):
        workflow.compile()


def test_shared_dependency_is_acquired_once_and_siblings_keep_declaration_order() -> (
    None
):
    calls: list[str] = []
    vm = _resource("vm", calls)
    first = task_resource(
        title="Acquire first",
        acquire_fn=lambda inputs: calls.append(f"acquire.first:{inputs.resource(vm)}"),
        release_fn=lambda _inputs, _value: calls.append("release.first"),
        requires=(vm,),
    )
    second = task_resource(
        title="Acquire second",
        acquire_fn=lambda inputs: calls.append(f"acquire.second:{inputs.resource(vm)}"),
        release_fn=lambda _inputs, _value: calls.append("release.second"),
        requires=(vm,),
    )
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls), requires=(first, second))

    workflow.run()

    assert calls == [
        "acquire.vm",
        "acquire.first:None",
        "acquire.second:None",
        "Use",
        "release.second",
        "release.first",
        "release.vm",
    ]


def test_resource_value_is_visible_only_to_declared_consumers_and_release() -> None:
    value = {"url": "postgres://db"}
    calls: list[str] = []

    def release_database(_inputs: TaskInputs, released: dict[str, str]) -> None:
        assert released is value
        calls.append("release")

    database = task_resource(
        title="Acquire database",
        acquire_fn=lambda _inputs: value,
        release_fn=release_database,
    )

    class _ReadDatabase(Task[None]):
        title = "Read database"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            assert inputs.resource(database) is value
            calls.append("consumer")
            return TaskOutcome()

    Workflow(workflow_id="wf").add(_ReadDatabase(), requires=(database,)).run()

    assert calls == ["consumer", "release"]


def test_consumer_cannot_read_undeclared_resource() -> None:
    resource = task_resource(
        title="Acquire database",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )

    class _ReadUndeclared(Task[None]):
        title = "Read undeclared"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            inputs.resource(resource)
            return TaskOutcome()

    workflow = _workflow()
    workflow.add(_ReadUndeclared())

    with pytest.raises(UndeclaredResourceError):
        workflow.run()


def test_consumer_failure_releases_acquired_in_reverse() -> None:
    calls: list[str] = []
    first = _resource("first", calls)
    second = _resource("second", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("A", calls), requires=(first,))
    workflow.add(_RecordTask("B", calls), requires=(second,))
    workflow.add(_RecordTask("Boom", calls, fail=True), requires=(first, second))

    with pytest.raises(RuntimeError, match="Boom failed"):
        workflow.run()

    assert calls == [
        "acquire.first",
        "A",
        "acquire.second",
        "B",
        "Boom",
        "release.second",
        "release.first",
    ]


def test_acquire_failure_releases_earlier_resources_only() -> None:
    calls: list[str] = []
    good = _resource("good", calls)

    def bad_acquire(_inputs: TaskInputs) -> None:
        raise RuntimeError("acquire exploded")

    bad = task_resource(
        title="Acquire bad",
        acquire_fn=bad_acquire,
        release_fn=lambda _inputs, _value: calls.append("release.bad"),
    )
    workflow = _workflow()
    workflow.add(_RecordTask("A", calls), requires=(good,))
    workflow.add(_RecordTask("B", calls), requires=(bad,))

    with pytest.raises(RuntimeError, match="acquire exploded"):
        workflow.run()

    assert calls == ["acquire.good", "A", "release.good"]
    assert "release.bad" not in calls


def test_consumer_failure_and_release_failure_are_combined() -> None:
    def fail_release(_inputs: TaskInputs, _value: object) -> None:
        raise RuntimeError("release failed")

    resource = task_resource(
        title="Acquire res", acquire_fn=lambda _inputs: None, release_fn=fail_release
    )
    workflow = _workflow()
    workflow.add(_RecordTask("Boom", [], fail=True), requires=(resource,))

    with pytest.raises(RuntimeError) as exc_info:
        workflow.run()

    assert "Boom failed" in str(exc_info.value)
    assert "release failed" in str(exc_info.value)


def test_release_failure_alone_is_raised() -> None:
    def fail_release(_inputs: TaskInputs, _value: object) -> None:
        raise RuntimeError("release failed")

    resource = task_resource(
        title="Acquire res", acquire_fn=lambda _inputs: None, release_fn=fail_release
    )
    workflow = _workflow()
    workflow.add(_RecordTask("Use", []), requires=(resource,))

    with pytest.raises(RuntimeError, match="Cleanup failed"):
        workflow.run()


def test_keep_retains_everything_that_did_not_ask_to_be_released() -> None:
    """Assert `keep` retains everything not marked `always_release`.

    `keep` is about not paying to rebuild; `always_release` is about not
    leaving something behind. A resource holding a secret declares the latter,
    so the security property belongs to the resource, not to a classification
    the caller has to get right.
    """
    calls: list[str] = []
    vm = _resource("vm", calls)
    builder = _resource("builder", calls)
    credentials = _resource("credentials", calls, always_release=True)
    workflow = _workflow(keep=True)
    workflow.add(_RecordTask("Use", calls), requires=(vm, builder, credentials))

    workflow.run()

    assert calls == [
        "acquire.vm",
        "acquire.builder",
        "acquire.credentials",
        "Use",
        "release.credentials",
    ]


def test_keep_releases_secrets_even_when_the_run_fails() -> None:
    calls: list[str] = []
    vm = _resource("vm", calls)
    credentials = _resource("credentials", calls, always_release=True)
    workflow = _workflow(keep=True)
    workflow.add(_RecordTask("Boom", calls, fail=True), requires=(vm, credentials))

    with pytest.raises(RuntimeError):
        workflow.run()

    assert "release.credentials" in calls
    assert "release.vm" not in calls


def test_without_keep_everything_is_released() -> None:
    calls: list[str] = []
    vm = _resource("vm", calls)
    credentials = _resource("credentials", calls, always_release=True)
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls), requires=(vm, credentials))

    workflow.run()

    assert calls[-2:] == ["release.credentials", "release.vm"]


def test_acquired_resource_is_released_if_terminal_event_emission_fails() -> None:
    calls: list[str] = []
    db = _resource("db", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls), requires=(db,))

    with (
        bind_workflow_sink(_FailAcquirePassedSink()),
        pytest.raises(OSError, match="event sink failed"),
    ):
        workflow.run()

    assert calls == ["acquire.db", "release.db"]


def test_acquired_resource_is_released_if_passed_journal_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    db = _resource("db", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls), requires=(db,))
    original = Journal.record_passed

    def _fail_acquire_passed(
        self: Journal,
        task_id: str,
        attempt: int,
        evidence: tuple = (),
    ) -> None:
        if task_id == "001.acquire-db":
            raise OSError("journal failed")
        original(self, task_id, attempt, evidence)

    monkeypatch.setattr(Journal, "record_passed", _fail_acquire_passed)

    with pytest.raises(OSError, match="journal failed"):
        workflow.run(journal=JournalConfig(tmp_path / "journal.jsonl"))

    assert calls == ["acquire.db", "release.db"]


def test_failed_journal_write_does_not_mask_task_error_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    db = _resource("db", calls)
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls, fail=True), requires=(db,))
    original = Journal.record_failed

    def _fail_consumer_failed(self: Journal, task_id: str, attempt: int) -> None:
        if task_id == "002.use":
            raise OSError("journal secondary")
        original(self, task_id, attempt)

    monkeypatch.setattr(Journal, "record_failed", _fail_consumer_failed)

    with pytest.raises(RuntimeError, match="Use failed") as exc_info:
        workflow.run(journal=JournalConfig(tmp_path / "journal.jsonl"))

    assert calls == ["acquire.db", "Use", "release.db"]
    assert any("journal secondary" in note for note in exc_info.value.__notes__)


def test_retained_resource_emits_and_journals_skipped(tmp_path: Path) -> None:
    calls: list[str] = []
    vm = _resource("vm", calls)
    workflow = _workflow(keep=True)
    workflow.add(_RecordTask("Use", calls), requires=(vm,))
    config = JournalConfig(tmp_path / "journal.jsonl")
    sink = _RecordingSink()

    with bind_workflow_sink(sink):
        workflow.run(journal=config)

    skipped_events = [event for event in sink.events if event.kind == "task.skipped"]
    assert [event.task_id for event in skipped_events] == ["003.release-vm"]
    records = [
        record
        for record in (
            json.loads(line)
            for line in config.path.read_text().splitlines()
            if line.strip()
        )
        if record.get("kind") != "retained"
    ]
    release_statuses = [
        record["status"] for record in records if record["task_id"] == "003.release-vm"
    ]
    assert release_statuses == ["pending", "skipped"]


def test_keep_releases_when_the_retained_value_cannot_be_journaled(
    tmp_path: Path,
) -> None:
    """Assert an unjournalable retention releases instead of holding.

    A retention that cannot be written down is a promise that cannot be
    kept: the resource is released now rather than held with no way to
    release it.
    """
    calls: list[str] = []
    vm = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: {"1.2.3.4"},
        release_fn=lambda _inputs, _value: calls.append("release.vm"),
    )
    workflow = _workflow(keep=True)
    workflow.add(_RecordTask("Use", calls), requires=(vm,))

    workflow.run(journal=JournalConfig(tmp_path / "journal.jsonl"))

    assert "release.vm" in calls
    records = [
        json.loads(line)
        for line in (tmp_path / "journal.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [record.get("kind") for record in records].count("retained") == 0
    release_statuses = [
        record["status"] for record in records if record["task_id"] == "003.release-vm"
    ]
    assert release_statuses == ["pending", "started", "passed"]


def test_all_finalizers_run_after_release_and_journal_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _failed_release(_inputs: TaskInputs, _value: object) -> None:
        calls.append("release.second")
        raise RuntimeError("release failed")

    first = _resource("first", calls)
    second = task_resource(
        title="Acquire second",
        acquire_fn=lambda _inputs: calls.append("acquire.second"),
        release_fn=_failed_release,
    )
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls), requires=(first, second))
    original = Journal.record_started

    def _fail_second_release_journal(self: Journal, task_id: str, attempt: int) -> None:
        if task_id.endswith("release-second"):
            raise OSError("journal failed")
        original(self, task_id, attempt)

    monkeypatch.setattr(Journal, "record_started", _fail_second_release_journal)

    with pytest.raises(RuntimeError, match="Cleanup failed"):
        workflow.run(journal=JournalConfig(tmp_path / "journal.jsonl"))

    assert calls[-2:] == ["release.second", "release.first"]


def test_base_exception_in_finalizer_does_not_abort_remaining_cleanup() -> None:
    calls: list[str] = []
    first = _resource("first", calls)

    def _interrupt_release(_inputs: TaskInputs, _value: object) -> None:
        calls.append("release.second")
        raise KeyboardInterrupt

    second = task_resource(
        title="Acquire second",
        acquire_fn=lambda _inputs: calls.append("acquire.second"),
        release_fn=_interrupt_release,
    )
    workflow = _workflow()
    workflow.add(_RecordTask("Use", calls), requires=(first, second))

    with pytest.raises(KeyboardInterrupt):
        workflow.run()

    assert calls[-2:] == ["release.second", "release.first"]


def test_finalizer_base_exception_remains_primary_after_task_failure() -> None:
    calls: list[str] = []
    first = _resource("first", calls)

    def _interrupt_release(_inputs: TaskInputs, _value: object) -> None:
        calls.append("release.second")
        raise KeyboardInterrupt

    second = task_resource(
        title="Acquire second",
        acquire_fn=lambda _inputs: calls.append("acquire.second"),
        release_fn=_interrupt_release,
    )
    workflow = _workflow()
    workflow.add(
        _RecordTask("Main", calls, fail=True),
        requires=(first, second),
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        workflow.run()

    assert calls[-2:] == ["release.second", "release.first"]
    assert any("Main failed" in note for note in exc_info.value.__notes__)


# --- Step 3: dependency chain on the failure path ----------------------------


def test_dependency_chain_releases_in_reverse_on_failure_path() -> None:
    """Assert a failing dependency chain releases in reverse acquisition order.

    A fn -> helm -> vm resource chain releases through the runner's `pending`
    list (the failure path), not the linear release units the passing tests
    exercise. Release order must still be reverse-acquisition, and each release
    must still be able to read its dependency's value.
    """
    calls: list[str] = []

    vm = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: calls.append("acquire.vm") or "vm-value",
        release_fn=lambda _inputs, value: calls.append(f"release.vm:{value}"),
    )
    helm = task_resource(
        title="Acquire helm",
        acquire_fn=lambda inputs: (
            calls.append(f"acquire.helm:{inputs.resource(vm)}") or "helm-value"
        ),
        release_fn=lambda inputs, value: calls.append(
            f"release.helm:{value}:{inputs.resource(vm)}"
        ),
        requires=(vm,),
    )
    fn = task_resource(
        title="Acquire fn",
        acquire_fn=lambda inputs: (
            calls.append(f"acquire.fn:{inputs.resource(helm)}") or "fn-value"
        ),
        release_fn=lambda inputs, value: calls.append(
            f"release.fn:{value}:{inputs.resource(helm)}"
        ),
        requires=(helm,),
    )
    workflow = _workflow()
    workflow.add(_RecordTask("Boom", calls, fail=True), requires=(fn,))

    with pytest.raises(RuntimeError, match="Boom failed"):
        workflow.run()

    assert calls == [
        "acquire.vm",
        "acquire.helm:vm-value",
        "acquire.fn:helm-value",
        "Boom",
        "release.fn:fn-value:helm-value",
        "release.helm:helm-value:vm-value",
        "release.vm:vm-value",
    ]


# --- Step 4: compiler performance on deep resource graphs --------------------


def _diamond_resource(depth: int) -> Resource:
    """Build a `depth`-level diamond resource graph.

    Each level's combiner `c{level}` requires two siblings
    `a{level}`/`b{level}` that both require the previous level's combiner.
    `register()` re-walking a 'done' node's whole subtree on every path to it
    (instead of returning early) doubles the work per level on a graph shaped
    like this -- exponential in `depth`.
    """
    current = task_resource(
        title="Acquire leaf",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )
    for level in range(depth):
        a = task_resource(
            title=f"Acquire a{level}",
            acquire_fn=lambda _inputs: None,
            release_fn=lambda _inputs, _value: None,
            requires=(current,),
        )
        b = task_resource(
            title=f"Acquire b{level}",
            acquire_fn=lambda _inputs: None,
            release_fn=lambda _inputs, _value: None,
            requires=(current,),
        )
        current = task_resource(
            title=f"Acquire c{level}",
            acquire_fn=lambda _inputs: None,
            release_fn=lambda _inputs, _value: None,
            requires=(a, b),
        )
    return current


def test_deep_diamond_resource_graph_compiles_deterministically_and_fast() -> None:
    depth = 40
    resource = _diamond_resource(depth)
    workflow = _workflow()
    workflow.add(_RecordTask("Use", []), requires=(resource,))

    start = time.perf_counter()
    first = workflow.compile()
    elapsed = time.perf_counter() - start
    second = workflow.compile()

    resource_count = 1 + 3 * depth  # leaf + (a, b, c) per level
    expected_task_count = (
        2 * resource_count + 1
    )  # acquire + release per resource + consumer

    assert first == second, "same definitions in must produce same compiled IDs out"
    assert len(first.tasks) == expected_task_count
    assert first.tasks[0].task.title == "Acquire leaf"
    assert first.tasks[resource_count].task_id == f"{resource_count + 1:03d}.use"
    # Regression guard: pre-fix this graph was exponential in depth (depth=26 took
    # ~34s in measurement); post-fix it stays well under a second even at depth=40.
    assert elapsed < 2.0, (
        f"compile() took {elapsed:.2f}s -- exponential blowup regression?"
    )
