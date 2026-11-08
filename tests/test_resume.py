from __future__ import annotations

import hashlib
import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from support import task_resource

from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import Evidence, TaskOutcome
from sonata_engine.core.task import ReusableTask, Task
from sonata_engine.core.workflow import Workflow
from sonata_engine.errors import (
    AmbiguousTaskStateError,
    ResumeConfigurationError,
    UnsupportedJournalSchemaError,
    WorkflowTopologyMismatchError,
)
from sonata_engine.journal import SCHEMA_VERSION, Journal, JournalConfig
from sonata_engine.workflow.context import bind_workflow_sink
from sonata_engine.workflow.events import WorkflowEvent


class _Tracker(Task[None]):
    def __init__(
        self,
        title: str,
        *,
        idempotent: bool = False,
        evidence: tuple[Evidence, ...] = (),
    ) -> None:
        self.title = title
        self.idempotent = idempotent
        self._evidence = evidence
        self.ran = False

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self.ran = True
        return TaskOutcome(evidence=self._evidence)


class _ReusableTracker(ReusableTask):
    def __init__(
        self,
        title: str,
        evidence: tuple[Evidence, ...] = (),
        reuse_key: str | None = None,
    ) -> None:
        self.title = title
        self._evidence = evidence
        self._reuse_key = reuse_key or title
        self.ran = False

    @property
    def reuse_key(self) -> str:
        return self._reuse_key

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self.ran = True
        return TaskOutcome(evidence=self._evidence)


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    @contextmanager
    def status(self, _label: str) -> Generator[None, None, None]:
        yield


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _seed(
    path: Path,
    task_id: str,
    status: str,
    *,
    attempt: int = 1,
    evidence: tuple[Evidence, ...] = (),
    workflow_id: str = "wf",
    schema_version: int = SCHEMA_VERSION,
    workflow_fingerprint: str | None = None,
) -> None:
    record = {
        "schema_version": schema_version,
        "workflow_id": workflow_id,
        "workflow_fingerprint": workflow_fingerprint,
        "run_id": "seed",
        "task_id": task_id,
        "attempt": attempt,
        "status": status,
        "started_at": "2026-01-01T00:00:00Z",
        "finished_at": None if status == "started" else "2026-01-01T00:00:01Z",
        "evidence": [
            {"kind": e.kind, "reference": e.reference, "digest": e.digest}
            for e in evidence
        ],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _fingerprint(task: Task[None]) -> str:
    workflow = Workflow(workflow_id="wf")
    workflow.add(task)
    return workflow.compile().fingerprint


def _run(task: Task[None], config: JournalConfig, **kwargs: object) -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(task)
    workflow.run(journal=config, **kwargs)  # type: ignore[arg-type]


# --- Resume matrix -----------------------------------------------------------


def test_exact_value_without_injected_verifier_reruns(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build", evidence=(Evidence("exact-value", "v1.2.3"),))
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("exact-value", "v1.2.3"),),
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is True
    assert _records(config.path)[-1]["status"] == "passed"


def test_empty_evidence_reusable_reruns(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is True


def test_injected_exact_value_verifier_enables_skip(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("exact-value", "v1.2.3"),),
        workflow_fingerprint=_fingerprint(task),
    )

    sink = _RecordingSink()
    with bind_workflow_sink(sink):
        _run(task, config, resume=True, verifiers={"exact-value": lambda _e: True})

    assert task.ran is False
    assert _records(config.path)[-1]["status"] == "skipped"
    assert [(event.kind, event.task_id) for event in sink.events] == [
        ("task.skipped", "001.build")
    ]


def test_reusable_flag_can_disable_skipping(tmp_path: Path) -> None:
    class AlwaysRun(_ReusableTracker):
        reusable = False

    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = AlwaysRun("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("exact-value", "v1.2.3"),),
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True, verifiers={"exact-value": lambda _e: True})

    assert task.ran is True


def test_changed_reusable_semantics_cannot_reuse_still_valid_evidence(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("stable")
    digest = "sha256:" + hashlib.sha256(b"stable").hexdigest()
    evidence = (Evidence("file-digest", str(artifact), digest),)
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    original = _ReusableTracker("Build", evidence=evidence, reuse_key="source=old")
    _run(original, config)
    changed = _ReusableTracker("Build", evidence=evidence, reuse_key="source=new")

    with pytest.raises(WorkflowTopologyMismatchError):
        _run(changed, config, resume=True)

    assert changed.ran is False


def test_passed_valid_file_digest_reusable_skips(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("stable")
    digest = "sha256:" + hashlib.sha256(b"stable").hexdigest()
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("file-digest", str(artifact), digest),),
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is False


def test_passed_stale_evidence_reusable_runs(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("original")
    digest = "sha256:" + hashlib.sha256(b"original").hexdigest()
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("file-digest", str(artifact), digest),),
        workflow_fingerprint=_fingerprint(task),
    )
    artifact.write_text("mutated")  # digest no longer matches -> stale

    _run(task, config, resume=True)

    assert task.ran is True


def test_passed_ordinary_task_runs(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _Tracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("exact-value", "v1"),),
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is True


def test_started_only_idempotent_retries(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _Tracker("Build", idempotent=True)
    _seed(
        config.path,
        "001.build",
        "started",
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is True
    assert max(r["attempt"] for r in _records(config.path)) == 2


def test_started_only_non_idempotent_raises(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _Tracker("Build", idempotent=False)
    _seed(
        config.path,
        "001.build",
        "started",
        workflow_fingerprint=_fingerprint(task),
    )

    with pytest.raises(AmbiguousTaskStateError):
        _run(task, config, resume=True)

    assert task.ran is False


def test_failed_non_idempotent_no_automatic_retry(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _Tracker("Build", idempotent=False)
    _seed(
        config.path,
        "001.build",
        "failed",
        workflow_fingerprint=_fingerprint(task),
    )

    with pytest.raises(AmbiguousTaskStateError):
        _run(task, config, resume=True)

    assert task.ran is False


def test_failed_idempotent_retries(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _Tracker("Build", idempotent=True)
    _seed(
        config.path,
        "001.build",
        "failed",
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is True


# --- Resource acquire on resume -----------------------------------------------


@pytest.mark.parametrize("prior_status", ["started", "failed"])
def test_non_idempotent_acquire_never_retries_ambiguous_state(
    tmp_path: Path, prior_status: str
) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    calls: list[str] = []
    resource = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: calls.append("acquire"),
        release_fn=lambda _inputs, _value: calls.append("release"),
        idempotent=False,
    )
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Tracker("Use vm"), requires=(resource,))
    _seed(
        config.path,
        "001.acquire-vm",
        prior_status,
        workflow_fingerprint=workflow.compile().fingerprint,
    )

    with pytest.raises(AmbiguousTaskStateError):
        workflow.run(journal=config, resume=True)

    assert calls == []


@pytest.mark.parametrize("prior_status", ["started", "failed"])
def test_idempotent_acquire_retries_ambiguous_state(
    tmp_path: Path, prior_status: str
) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    calls: list[str] = []
    resource = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: calls.append("acquire"),
        release_fn=lambda _inputs, _value: calls.append("release"),
        idempotent=True,
    )
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Tracker("Use vm"), requires=(resource,))
    _seed(
        config.path,
        "001.acquire-vm",
        prior_status,
        workflow_fingerprint=workflow.compile().fingerprint,
    )

    workflow.run(journal=config, resume=True)

    assert calls == ["acquire", "release"]


def test_acquire_passed_always_reruns(tmp_path: Path) -> None:
    """Rerun an acquire task that passed in a prior run.

    Matches "passed non-reusable tasks run again": a resource is never
    journal-skipped just because it was successfully acquired in a prior run.
    """
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    calls: list[str] = []
    resource = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: calls.append("acquire"),
        release_fn=lambda _inputs, _value: calls.append("release"),
    )
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Tracker("Use vm"), requires=(resource,))
    _seed(
        config.path,
        "001.acquire-vm",
        "passed",
        workflow_fingerprint=workflow.compile().fingerprint,
    )

    workflow.run(journal=config, resume=True)

    assert "acquire" in calls


# --- Journal I/O failure during release cleanup -------------------------------


def test_release_journal_failure_does_not_mask_main_error_or_abort_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    calls: list[str] = []
    resource = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: calls.append("acquire"),
        release_fn=lambda _inputs, _value: calls.append("release"),
    )

    class _FailTask(Task[None]):
        title = "Consume"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            raise RuntimeError("consume failed")

    workflow = Workflow(workflow_id="wf")
    workflow.add(_FailTask(), requires=(resource,))
    compiled = workflow.compile()
    release_id = next(ct.task_id for ct in compiled.tasks if ct.kind == "release")

    original_record_started = Journal.record_started

    def _flaky_record_started(self: Journal, task_id: str, attempt: int) -> None:
        if task_id == release_id:
            raise OSError("disk full")
        original_record_started(self, task_id, attempt)

    monkeypatch.setattr(Journal, "record_started", _flaky_record_started)

    with pytest.raises(RuntimeError, match="consume failed"):
        workflow.run(journal=config)

    assert "release" in calls  # cleanup still ran despite the journal write failure


# --- Evidence verifier registry ----------------------------------------------


def test_file_digest_evidence_without_digest_is_unverified(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("stable")
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("file-digest", str(artifact), digest=None),),
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is True  # no digest to check against -> never verified


def test_file_digest_evidence_missing_file_is_unverified(tmp_path: Path) -> None:
    missing = tmp_path / "gone.txt"
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("file-digest", str(missing), "sha256:" + "0" * 64),),
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is True  # file no longer exists -> unverifiable, not verified


def test_unknown_evidence_kind_fails_closed_and_runs(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("mystery-kind", "x"),),
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True)

    assert task.ran is True  # unverifiable evidence never skips


def test_injected_verifier_enables_skip(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _ReusableTracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        evidence=(Evidence("custom", "ok"),),
        workflow_fingerprint=_fingerprint(task),
    )

    _run(task, config, resume=True, verifiers={"custom": lambda _e: True})

    assert task.ran is False


# --- Configuration / schema guards -------------------------------------------


def test_resume_without_journal_raises() -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Tracker("Build"))

    with pytest.raises(ResumeConfigurationError):
        workflow.run(resume=True)


def test_schema_v1_is_rejected(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    _seed(config.path, "001.build", "passed", schema_version=1)
    task = _Tracker("Build")

    with pytest.raises(UnsupportedJournalSchemaError):
        _run(task, config, resume=True)


def test_non_resume_run_warns_on_fingerprint_mismatch(tmp_path: Path) -> None:
    """Warn instead of raising on a topology mismatch in a non-resume run.

    A non-resume run against a journal from a different (e.g. pre-upgrade)
    topology doesn't raise -- but it must not stay silent either: it ignores the
    stale records and appends a new topology into the same file.
    """
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    task = _Tracker("Build")
    _seed(
        config.path,
        "001.build",
        "passed",
        workflow_fingerprint="sha256:" + "0" * 64,
    )

    with pytest.warns(UserWarning, match="ignoring them and appending"):
        _run(task, config)

    assert task.ran is True
    records = _records(config.path)
    fingerprints = {record["workflow_fingerprint"] for record in records}
    assert fingerprints == {"sha256:" + "0" * 64, _fingerprint(task)}
