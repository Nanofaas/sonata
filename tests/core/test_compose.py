"""A composite resource: an ordered sequence of task resources."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import replace
from pathlib import Path

import pytest
from support import CallTask, task_resource

from sonata_engine import (
    CompositeMemberRequiredError,
    JournalConfig,
    Members,
    Resource,
    TaskInputs,
    Workflow,
    compose,
)
from sonata_engine.core.workflow import _StepScope
from sonata_engine.retention import release_retained


def _member(
    title: str, calls: list[str], value: object = None, **kwargs: object
) -> Resource[object]:
    def acquire(_inputs: TaskInputs) -> object:
        calls.append(f"acquire.{title}")
        return title.upper() if value is None else value

    def release(_inputs: TaskInputs, _value: object) -> None:
        calls.append(f"release.{title}")

    return task_resource(title, acquire, release, **kwargs)


def _consumer(calls: list[str]) -> CallTask[None]:
    return CallTask("Consumer", lambda _inputs: calls.append("consumer") and None)


def _run(platform: Resource[Members], task: CallTask[None], **kwargs: object) -> None:
    workflow = Workflow(workflow_id="wf", keep=bool(kwargs.pop("keep", False)))
    workflow.add(task, requires=(platform,))
    workflow.run(**kwargs)  # type: ignore[arg-type]


def test_compose_acquires_in_order_releases_in_reverse() -> None:
    calls: list[str] = []
    platform = compose(
        "Acquire platform", tuple(_member(t, calls) for t in ("a", "b", "c"))
    )

    _run(platform, _consumer(calls))

    assert calls == [
        "acquire.a",
        "acquire.b",
        "acquire.c",
        "consumer",
        "release.c",
        "release.b",
        "release.a",
    ]


def test_consumer_reads_member_values() -> None:
    calls: list[str] = []
    a, b = _member("a", calls), _member("b", calls)
    platform = compose("Acquire platform", (a, b))
    seen: list[object] = []

    _run(
        platform,
        CallTask(
            "Consumer", lambda inputs: seen.append(inputs.resource(platform).of(b))
        ),
    )

    assert seen == ["B"]


def test_members_of_unknown_resource_names_the_title() -> None:
    with pytest.raises(KeyError, match="nope"):
        Members().of(_member("nope", []))


def test_later_member_reads_earlier_member() -> None:
    calls: list[str] = []
    a = _member("a", calls)
    b = task_resource(
        "b",
        lambda inputs: f"b-on-{inputs.resource(a)}",
        lambda _inputs, _value: None,
        requires=(a,),
    )
    platform = compose("Acquire platform", (a, b))
    seen: list[object] = []

    _run(
        platform,
        CallTask(
            "Consumer", lambda inputs: seen.append(inputs.resource(platform).of(b))
        ),
    )

    assert platform.requires == ()
    assert seen == ["b-on-A"]


def test_member_requires_outside_the_composite_become_its_requires() -> None:
    calls: list[str] = []
    net = _member("net", calls)
    a = task_resource(
        "a",
        lambda inputs: f"a-on-{inputs.resource(net)}",
        lambda _i, _v: None,
        requires=(net,),
    )
    b = task_resource(
        "b",
        lambda inputs: f"b-on-{inputs.resource(net)}",
        lambda _i, _v: None,
        requires=(net,),
    )
    platform = compose("Acquire platform", (a, b))
    seen: list[object] = []

    _run(
        platform,
        CallTask(
            "Consumer", lambda inputs: seen.append(inputs.resource(platform).of(b))
        ),
    )

    assert platform.requires == (net,)
    assert seen == ["b-on-NET"]


def _failing_member(title: str, error: BaseException, calls: list[str]) -> Resource:
    def acquire(_inputs: TaskInputs) -> object:
        calls.append(f"acquire.{title}")
        raise error

    return task_resource(
        title, acquire, lambda _inputs, _value: calls.append(f"release.{title}")
    )


def test_compose_acquire_failure_releases_acquired_members() -> None:
    calls: list[str] = []
    error = RuntimeError("b")
    platform = compose(
        "Acquire platform",
        (_member("a", calls), _failing_member("b", error, calls), _member("c", calls)),
    )

    with pytest.raises(RuntimeError) as raised:
        _run(platform, _consumer(calls))

    assert raised.value is error
    assert calls == ["acquire.a", "acquire.b", "release.a"]


def test_compose_acquire_unwinds_on_interrupt() -> None:
    calls: list[str] = []
    platform = compose(
        "Acquire platform",
        (_member("a", calls), _failing_member("b", KeyboardInterrupt(), calls)),
    )

    with pytest.raises(KeyboardInterrupt):
        _run(platform, _consumer(calls))

    assert calls == ["acquire.a", "acquire.b", "release.a"]


def test_unwind_failure_is_a_note_on_the_original_error() -> None:
    calls: list[str] = []

    def broken_release(_inputs: TaskInputs, _value: object) -> None:
        raise RuntimeError("release a broke")

    a = task_resource("a", lambda _inputs: "A", broken_release)
    error = RuntimeError("b")
    platform = compose("Acquire platform", (a, _failing_member("b", error, calls)))

    with pytest.raises(RuntimeError) as raised:
        _run(platform, _consumer(calls))

    assert raised.value is error
    assert any("release a broke" in note for note in error.__notes__)


def _raise(message: str) -> None:
    raise RuntimeError(message)


def test_compose_release_attempts_every_member() -> None:
    calls: list[str] = []
    a, c = _member("a", calls), _member("c", calls)
    b = task_resource("b", lambda _inputs: "B", lambda _i, _v: _raise("b release"))
    platform = compose(
        "Acquire platform",
        (a, b, c),
        collect=(CallTask("Snapshot", lambda _inputs: _raise("snapshot")),),
    )

    with pytest.raises(RuntimeError) as raised:
        _run(platform, _consumer(calls))

    assert "snapshot" in str(raised.value)
    assert "b release" in str(raised.value)
    assert calls[-2:] == ["release.c", "release.a"]


def test_a_single_release_failure_is_raised_itself() -> None:
    calls: list[str] = []
    error = RuntimeError("b release")

    def broken(_inputs: TaskInputs, _value: object) -> None:
        raise error

    a, b = _member("a", calls), task_resource("b", str, broken)
    platform = compose("Acquire platform", (a, b))
    inputs = TaskInputs.empty()
    scope = _StepScope(prefix="p", base_inputs=inputs, jrnl=None, resume=False)

    with pytest.raises(RuntimeError) as raised:
        platform.release.run(
            replace(inputs, _upstream=Members({"a": "A", "b": "B"}), _step_scope=scope)
        )

    assert raised.value is error
    assert calls == ["release.a"]


def test_a_critical_release_failure_wins() -> None:
    calls: list[str] = []

    def interrupted(_inputs: TaskInputs, _value: object) -> None:
        raise KeyboardInterrupt

    platform = compose(
        "Acquire platform",
        (
            _member("a", calls),
            task_resource("b", str, interrupted),
            task_resource("c", str, lambda _i, _v: _raise("c release")),
        ),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _run(platform, _consumer(calls))

    assert calls == ["acquire.a", "consumer", "release.a"]
    assert any("c release" in note for note in raised.value.__notes__)


def test_collect_runs_before_member_releases() -> None:
    calls: list[str] = []
    a = _member("a", calls)

    def snapshot(inputs: TaskInputs) -> None:
        calls.append(f"collect.{inputs.upstream().of(a)}")

    platform = compose(
        "Acquire platform",
        (a, _member("b", calls)),
        collect=(CallTask("Snapshot", snapshot),),
    )

    _run(platform, _consumer(calls))

    assert calls[3:] == ["collect.A", "release.b", "release.a"]


def test_compose_is_always_release_when_a_member_is() -> None:
    calls: list[str] = []
    platform = compose(
        "Acquire platform",
        (_member("a", calls), _member("b", calls, always_release=True)),
    )

    _run(platform, _consumer(calls), keep=True)

    assert platform.always_release
    assert calls[-2:] == ["release.b", "release.a"]


@dataclasses.dataclass(frozen=True)
class _Endpoint:
    host: str


def test_release_retained_composite(tmp_path: Path) -> None:
    calls: list[str] = []
    released: list[object] = []

    def member(title: str) -> Resource[_Endpoint]:
        return task_resource(
            title,
            lambda _inputs: _Endpoint(title),
            lambda _inputs, value: released.append(value),
            revive=lambda raw: _Endpoint(**raw),
        )

    platform = compose("Acquire platform", (member("a"), member("b"), member("c")))
    config = JournalConfig(tmp_path / "journal.jsonl")
    _run(platform, _consumer(calls), keep=True, journal=config)
    assert released == []

    assert release_retained({"Acquire platform": platform}, config) == (
        "Acquire platform",
    )

    assert released == [_Endpoint("c"), _Endpoint("b"), _Endpoint("a")]


def test_compose_steps_are_journalled(tmp_path: Path) -> None:
    calls: list[str] = []
    platform = compose(
        "Acquire platform",
        (_member("a", calls), _member("b", calls)),
        collect=(CallTask("Snapshot", lambda _inputs: None),),
    )
    path = tmp_path / "journal.jsonl"

    _run(platform, _consumer(calls), journal=JournalConfig(path))

    ids = {
        record["task_id"]
        for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
        if "task_id" in record
    }
    assert {
        "001.acquire-platform/acquire-a",
        "001.acquire-platform/acquire-b",
        "003.release-platform/collect-snapshot",
        "003.release-platform/release-b",
        "003.release-platform/release-a",
    } <= ids


def test_fingerprint_covers_members_and_collect() -> None:
    def platform(title: str, collect: tuple[CallTask[None], ...] = ()) -> Resource:
        return compose("Acquire platform", (_member(title, []),), collect=collect)

    def fingerprint(resource: Resource) -> str:
        workflow = Workflow(workflow_id="wf")
        workflow.add(_consumer([]), requires=(resource,))
        return workflow.compile().fingerprint

    snapshot = (CallTask("Snapshot", lambda _inputs: None),)
    assert fingerprint(platform("a")) == fingerprint(platform("a"))
    assert fingerprint(platform("a")) != fingerprint(platform("b"))
    assert fingerprint(platform("a")) != fingerprint(platform("a", snapshot))


def test_compile_rejects_directly_required_member() -> None:
    calls: list[str] = []
    a = _member("a", calls)
    platform = compose("Acquire platform", (a, _member("b", calls)))
    workflow = Workflow(workflow_id="wf")
    workflow.add(_consumer(calls), requires=(platform, a))

    with pytest.raises(CompositeMemberRequiredError, match=r"'a'.*'Acquire platform'"):
        workflow.compile()


def test_compile_rejects_a_member_required_by_another_resource() -> None:
    calls: list[str] = []
    a = _member("a", calls)
    platform = compose("Acquire platform", (a,))
    other = _member("other", calls, requires=(a,))
    workflow = Workflow(workflow_id="wf")
    workflow.add(_consumer(calls), requires=(platform, other))

    with pytest.raises(CompositeMemberRequiredError, match="'other'"):
        workflow.compile()


def test_compose_rejects_empty_and_duplicate_members() -> None:
    with pytest.raises(ValueError, match="at least one"):
        compose("Acquire platform", ())
    with pytest.raises(ValueError, match="duplicate"):
        compose("Acquire platform", (_member("a", []), _member("A", [])))
    with pytest.raises(ValueError, match="duplicate"):
        compose(
            "Acquire platform",
            (_member("a", []),),
            collect=(CallTask("x", str), CallTask("X", str)),
        )
    with pytest.raises(ValueError, match="empty slug"):
        compose("Acquire platform", (_member("!", []),))


def test_composite_run_directly_needs_a_step_scope() -> None:
    from sonata_engine import StepScopeUnavailableError

    platform = compose("Acquire platform", (_member("a", []),))

    with pytest.raises(StepScopeUnavailableError):
        platform.acquire.run(TaskInputs.empty())
    with pytest.raises(StepScopeUnavailableError):
        platform.release.run(TaskInputs.empty())


def test_compile_rejects_a_member_shared_by_two_composites() -> None:
    calls: list[str] = []
    a = _member("a", calls)
    first = compose("Acquire first", (a, _member("b", calls)))
    second = compose("Acquire second", (a, _member("c", calls)))
    workflow = Workflow(workflow_id="wf")
    workflow.add(_consumer(calls), requires=(first, second))

    with pytest.raises(CompositeMemberRequiredError, match="'a'"):
        workflow.compile()
    assert calls == []


def test_compile_rejects_a_member_shared_through_a_nested_composite() -> None:
    calls: list[str] = []
    a = _member("a", calls)
    inner = compose("Acquire inner", (a,))
    first = compose("Acquire first", (inner,))
    second = compose("Acquire second", (a, _member("c", calls)))
    workflow = Workflow(workflow_id="wf")
    workflow.add(_consumer(calls), requires=(first, second))

    with pytest.raises(CompositeMemberRequiredError, match="'a'"):
        workflow.compile()


def test_compose_rejects_a_member_requiring_a_later_member() -> None:
    calls: list[str] = []
    c = _member("c", calls)
    b = task_resource(
        "b", lambda inputs: inputs.resource(c), lambda _i, _v: None, requires=(c,)
    )

    with pytest.raises(ValueError, match=r"'b' requires 'c'"):
        compose("Acquire platform", (_member("a", calls), b, c))


def test_resumed_unwind_releases_a_member_whose_earlier_unwind_failed(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    failures = [1]

    def flaky_release(_inputs: TaskInputs, _value: object) -> None:
        calls.append("release.a")
        if failures[0]:
            failures[0] -= 1
            raise RuntimeError("release a broke")

    def failing_acquire(_inputs: TaskInputs) -> object:
        calls.append("acquire.b")
        raise RuntimeError("b")  # a fresh error each run: notes stay per run

    a = task_resource("a", lambda _i: "A", flaky_release, idempotent=True)
    b = task_resource("b", failing_acquire, lambda _i, _v: None, idempotent=True)
    platform = compose("Acquire platform", (a, b))
    journal = JournalConfig(tmp_path / "journal.jsonl")
    with pytest.raises(RuntimeError):
        _run(platform, _consumer(calls), journal=journal)
    calls.clear()

    with pytest.raises(RuntimeError) as raised:
        _run(platform, _consumer(calls), journal=journal, resume=True)

    assert raised.value.args == ("b",)
    assert calls == ["acquire.b", "release.a"]
    assert not getattr(raised.value, "__notes__", [])


def test_a_failing_journal_still_releases_every_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sonata_engine.journal import Journal

    original = Journal.record_started

    def failing(self: Journal, task_id: str, attempt: int) -> None:
        if "release" in task_id:
            raise OSError("disk full")
        original(self, task_id, attempt)

    monkeypatch.setattr(Journal, "record_started", failing)
    calls: list[str] = []
    platform = compose("Acquire platform", (_member("a", calls), _member("b", calls)))

    with pytest.raises(RuntimeError, match="disk full"):
        _run(platform, _consumer(calls), journal=JournalConfig(tmp_path / "j.jsonl"))

    assert calls[-2:] == ["release.b", "release.a"]


def test_a_failing_journal_still_runs_the_unwind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sonata_engine.journal import Journal

    original = Journal.record_started

    def failing(self: Journal, task_id: str, attempt: int) -> None:
        if "release" in task_id:
            raise OSError("disk full")
        original(self, task_id, attempt)

    monkeypatch.setattr(Journal, "record_started", failing)
    calls: list[str] = []
    error = RuntimeError("b")
    platform = compose(
        "Acquire platform", (_member("a", calls), _failing_member("b", error, calls))
    )

    with pytest.raises(RuntimeError) as raised:
        _run(platform, _consumer(calls), journal=JournalConfig(tmp_path / "j.jsonl"))

    assert raised.value is error
    assert calls == ["acquire.a", "acquire.b", "release.a"]
    assert any("disk full" in note for note in error.__notes__)


def test_unwind_interrupt_note_is_not_blank() -> None:
    def interrupted(_inputs: TaskInputs, _value: object) -> None:
        raise KeyboardInterrupt

    error = RuntimeError("b")
    platform = compose(
        "Acquire platform",
        (
            task_resource("a", lambda _i: "A", interrupted),
            _failing_member("b", error, []),
        ),
    )

    with pytest.raises(RuntimeError):
        _run(platform, _consumer([]))

    assert error.__notes__ == ["Cleanup error: KeyboardInterrupt()"]
