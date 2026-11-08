from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import task_resource

from sonata_engine import (
    Evidence,
    JournalConfig,
    ReusableTask,
    Selection,
    Steps,
    StepScopeUnavailableError,
    Task,
    TaskInputs,
    TaskOutcome,
    Workflow,
)
from sonata_engine.errors import (
    InvalidTaskOutcomeError,
    NoUpstreamValueError,
)
from sonata_engine.journal import SCHEMA_VERSION


class _Produce(Task[str]):
    def __init__(self, title: str, value: str) -> None:
        self.title = title
        self._value = value

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        return TaskOutcome(value=self._value)


class _Forward(Task[str]):
    title = "Forward"

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        return TaskOutcome(value=inputs.upstream())


class _Decorate(Task[str]):
    title = "Decorate"

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        return TaskOutcome(value=f"http://{inputs.upstream()}.svc")


def _run(*steps: Task[object], title: str = "Deploy") -> object:
    workflow = Workflow(workflow_id="w")
    workflow.add(Steps(title=title, steps=tuple(steps)))
    execution = workflow.run().tasks[0]
    assert execution.outcome is not None
    return execution.outcome.value


def test_a_value_flows_through_the_pipeline() -> None:
    assert (
        _run(_Produce("Install", "rel-42"), _Forward(), _Decorate())
        == "http://rel-42.svc"
    )


def test_none_flows_as_a_legitimate_value() -> None:
    class ProduceNone(Task[None]):
        title = "Produce none"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            return TaskOutcome()

    class AssertNone(Task[str]):
        title = "Assert none"

        def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
            assert inputs.upstream() is None
            return TaskOutcome(value="ok")

    assert _run(ProduceNone(), AssertNone()) == "ok"


def test_upstream_raises_in_the_first_step_of_a_top_level_composite() -> None:
    with pytest.raises(NoUpstreamValueError):
        _run(_Forward())


def test_a_nested_composite_receives_the_outer_upstream() -> None:
    """Composition is transparent: the inner pipeline continues the outer one."""
    inner = Steps(title="Inner", steps=(_Forward(),))
    assert _run(_Produce("Install", "rel-42"), inner) == "rel-42"


def test_a_composite_compiles_to_one_unit_and_selection_keeps_it_whole() -> None:
    workflow = Workflow(workflow_id="w")
    workflow.add(
        Steps(title="Deploy", steps=(_Produce("Install", "rel-42"), _Decorate()))
    )

    compiled = workflow.compile()
    assert [task.task_id for task in compiled.tasks] == ["001.deploy"]

    selected = workflow.compile(select=Selection(only="deploy"))
    assert [task.task_id for task in selected.tasks] == ["001.deploy"]


def test_an_already_written_task_serves_as_a_step_unmodified() -> None:
    """The central promise: nothing about Task changed to make this work."""

    class WrittenBefore(Task[str]):
        title = "Written before"

        def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
            return TaskOutcome(value="unchanged")

    assert _run(WrittenBefore()) == "unchanged"


def test_construction_rejects_an_empty_step_list() -> None:
    with pytest.raises(ValueError, match="at least one step"):
        Steps(title="Deploy", steps=())


def test_construction_rejects_duplicate_normalized_slugs() -> None:
    """'Build A' and 'Build-A' normalize to the same journal identity."""
    with pytest.raises(ValueError, match="duplicate step slug 'build-a'"):
        Steps(
            title="Deploy", steps=(_Produce("Build A", "1"), _Produce("Build-A", "2"))
        )


def test_construction_rejects_a_title_that_normalizes_to_nothing() -> None:
    with pytest.raises(ValueError, match="produces an empty slug"):
        Steps(title="Deploy", steps=(_Produce("---", "1"),))


def test_a_step_returning_a_non_outcome_raises() -> None:
    class Bad(Task[None]):
        title = "Bad"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            return "not-an-outcome"  # type: ignore[return-value]

    with pytest.raises(InvalidTaskOutcomeError):
        _run(Bad())


def test_a_reusable_step_returning_a_value_raises() -> None:
    """Inherited from the shared executor, not restated in Steps."""

    class BadReusable(ReusableTask):
        title = "Bad reusable"
        reuse_key = "bad"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            return TaskOutcome(value=42)  # type: ignore[arg-type]

    with pytest.raises(InvalidTaskOutcomeError):
        _run(BadReusable())


def test_steps_is_idempotent_so_a_failed_unit_can_be_re_entered() -> None:
    assert Steps(title="Deploy", steps=(_Forward(),)).idempotent is True


def test_running_steps_outside_a_workflow_raises() -> None:
    """Assert that running steps outside a workflow raises.

    Calling `run()` directly bypasses the runner, so there is no step scope
    to run steps in -- a user error, not an engine crash.
    """
    with pytest.raises(StepScopeUnavailableError, match="must be run by the workflow"):
        Steps(title="Deploy", steps=(_Forward(),)).run(TaskInputs.empty())


def test_a_failing_step_still_releases_the_composites_resources() -> None:
    """Assert a failing composite still releases its declared resource.

    A composite that declares a resource and fails partway through must not
    leak it: cleanup runs exactly as it would for a plain consumer task.
    """
    released: list[str] = []
    resource = task_resource(
        title="Acquire token",
        acquire_fn=lambda _inputs: "token-42",
        release_fn=lambda _inputs, value: released.append(value),
    )

    class Boom(Task[None]):
        title = "Boom"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            assert inputs.resource(resource) == "token-42"
            raise RuntimeError("boom")

    workflow = Workflow(workflow_id="w")
    workflow.add(Steps(title="Deploy", steps=(Boom(),)), requires=(resource,))

    with pytest.raises(RuntimeError, match="boom"):
        workflow.run()

    assert released == ["token-42"]


def test_the_fingerprint_payload_covers_the_steps() -> None:
    def payload(*steps: Task[object]) -> object:
        return Steps(title="Deploy", steps=tuple(steps))._fingerprint_payload()

    class OtherProduce(Task[str]):
        title = "Install"

        def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
            return TaskOutcome(value="1")

    class Reusable(ReusableTask):
        title = "Build"

        def __init__(self, key: str) -> None:
            self._key = key

        @property
        def reuse_key(self) -> str:
            return self._key

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            return TaskOutcome()

    a = _Produce("Install", "1")
    b = _Produce("Resolve", "2")
    assert payload(a, b) != payload(b, a)
    assert payload(a) != payload(a, b)
    assert payload(_Produce("Install", "1")) != payload(_Produce("Resolve", "1"))
    assert payload(_Produce("Install", "1")) != payload(OtherProduce())
    assert payload(Reusable("key-1")) != payload(Reusable("key-2"))

    nested_a = Steps(title="Inner", steps=(_Produce("Install", "1"),))
    nested_b = Steps(title="Inner", steps=(_Produce("Resolve", "1"),))
    assert payload(nested_a) != payload(nested_b)


def _seed_step(
    path: Path,
    task_id: str,
    status: str,
    *,
    evidence: tuple[Evidence, ...],
    workflow_fingerprint: str,
    workflow_id: str = "w",
) -> None:
    """Append one journal record for a step.

    The record follows the shape `Journal` reads (see
    `tests/test_resume.py::_seed` for the top-level-task twin).
    """
    record = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "workflow_fingerprint": workflow_fingerprint,
        "run_id": "seed",
        "task_id": task_id,
        "attempt": 1,
        "status": status,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:01Z",
        "evidence": [
            {"kind": e.kind, "reference": e.reference, "digest": e.digest}
            for e in evidence
        ],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def test_a_skipped_step_contributes_none_as_the_next_upstream(tmp_path: Path) -> None:
    """Assert a skipped step hands `None`, not its recorded value, downstream.

    The subtlest claim in the design: a skipped step must not hand its
    (nonexistent) recorded value to the next step -- it hands `None`, the only
    value a skippable `ReusableTask` may ever legally produce. A regression that
    forwarded the *last recorded* upstream instead of reconstructing `None`
    would pass every other test in this file.
    """

    class _Build(ReusableTask):
        title = "Build"
        reuse_key = "build-v1"

        def __init__(self) -> None:
            self.ran = False

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            self.ran = True
            return TaskOutcome(evidence=(Evidence("custom", "ok"),))

    seen: list[object] = []

    class _Capture(Task[None]):
        title = "Capture"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            seen.append(inputs.upstream())
            return TaskOutcome()

    build = _Build()
    workflow = Workflow(workflow_id="w")
    workflow.add(Steps(title="Deploy", steps=(build, _Capture())))
    fingerprint = workflow.compile().fingerprint

    config = JournalConfig(path=tmp_path / "journal.jsonl")
    _seed_step(
        config.path,
        "001.deploy/build",
        "passed",
        evidence=(Evidence("custom", "ok"),),
        workflow_fingerprint=fingerprint,
    )

    workflow.run(journal=config, resume=True, verifiers={"custom": lambda _e: True})

    assert build.ran is False  # confirms the step was actually skipped, not rerun
    assert seen == [None]
