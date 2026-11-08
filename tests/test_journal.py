from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from support import task_resource

from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import Evidence, TaskOutcome
from sonata_engine.core.task import ReusableTask, Task
from sonata_engine.core.workflow import Workflow
from sonata_engine.errors import CorruptJournalError, WorkflowTopologyMismatchError
from sonata_engine.journal import SCHEMA_VERSION, JournalConfig


class _Ok(Task[None]):
    def __init__(self, title: str, evidence: tuple[Evidence, ...] = ()) -> None:
        self.title = title
        self._evidence = evidence

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome(evidence=self._evidence)


class _ReusableOk(ReusableTask):
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


class _Boom(Task[None]):
    title = "Boom"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        raise RuntimeError("boom")


class _Replacement(Task[None]):
    def __init__(self, title: str) -> None:
        self.title = title

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome()


def _records(path: Path) -> list[dict]:
    """Task records only -- retention records carry no task_id."""
    return [
        record
        for record in (
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        )
        if record.get("kind") != "retained"
    ]


def _five_task_workflow() -> Workflow:
    workflow = Workflow(workflow_id="release")
    for index in range(5):
        workflow.add(_Ok(f"Task {index}"))
    return workflow


# --- Step 1: topology --------------------------------------------------------


def test_complete_topology_is_pending_before_first_task_runs(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    seen: list[dict] = []

    class _Inspect(Task[None]):
        title = "Inspect"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            seen.extend(_records(config.path))
            return TaskOutcome()

    workflow = Workflow(workflow_id="wf")
    workflow.add(_Inspect())
    workflow.add(_Ok("Second"))
    workflow.add(_Ok("Third"))

    workflow.run(journal=config)

    pending = [record for record in seen if record["status"] == "pending"]
    assert [record["task_id"] for record in pending] == [
        "001.inspect",
        "002.second",
        "003.third",
    ]
    assert {record["attempt"] for record in pending} == {0}


def test_unreachable_task_remains_in_journal_topology(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("First"))
    workflow.add(_Boom())
    workflow.add(_Ok("Unreachable"))

    with pytest.raises(RuntimeError, match="boom"):
        workflow.run(journal=config)

    unreachable = [
        record
        for record in _records(config.path)
        if record["task_id"] == "003.unreachable"
    ]
    assert [record["status"] for record in unreachable] == ["pending"]


def test_retained_resource_finalizer_is_journaled_as_skipped(
    tmp_path: Path,
) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    resource = task_resource(
        title="Acquire cluster",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )
    workflow = Workflow(workflow_id="wf", keep=True)
    workflow.add(_Ok("Use cluster"), requires=(resource,))

    workflow.run(journal=config)

    release_records = [
        record
        for record in _records(config.path)
        if record["task_id"].endswith("release-cluster")
    ]
    assert [record["status"] for record in release_records] == ["pending", "skipped"]


def test_changed_task_type_rejects_existing_journal(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    original = Workflow(workflow_id="wf")
    original.add(_Ok("Build"))
    original.run(journal=config)

    changed = Workflow(workflow_id="wf")
    changed.add(_Replacement("Build"))

    with pytest.raises(WorkflowTopologyMismatchError):
        changed.run(journal=config, resume=True)


def test_changed_ordered_topology_rejects_existing_journal(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    original = Workflow(workflow_id="wf")
    original.add(_Ok("Build"))
    original.run(journal=config)

    changed = Workflow(workflow_id="wf")
    changed.add(_Ok("Prepare"))
    changed.add(_Ok("Build"))

    with pytest.raises(WorkflowTopologyMismatchError):
        changed.run(journal=config, resume=True)


def test_changed_topology_does_not_reject_a_non_resuming_run(tmp_path: Path) -> None:
    """Block a topology mismatch only when the run resumes.

    The check exists to stop resume from reusing evidence under a stale task ID.
    A fresh (non-resuming) run pointed at a journal file left over from a
    differently-shaped workflow must not be blocked; mismatched records are
    simply ignored, the same as a `workflow_id` mismatch.
    """
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    original = Workflow(workflow_id="wf")
    original.add(_Ok("Build"))
    original.run(journal=config)

    changed = Workflow(workflow_id="wf")
    changed.add(_Ok("Prepare"))
    changed.add(_Ok("Build"))

    with pytest.warns(UserWarning, match="ignoring them and appending"):
        changed.run(journal=config)  # resume=False (default) -- must not raise

    records = [r for r in _records(config.path) if r["task_id"] == "001.prepare"]
    assert any(r["status"] == "passed" for r in records)


def test_record_without_fingerprint_is_not_backward_compatible(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    config.path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "workflow_id": "wf",
                "run_id": "legacy",
                "task_id": "001.build",
                "attempt": 1,
                "status": "passed",
                "started_at": None,
                "finished_at": None,
                "evidence": [],
            }
        )
        + "\n"
    )
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Build"))

    with pytest.raises(WorkflowTopologyMismatchError):
        workflow.run(journal=config, resume=True)


def test_five_tasks_produce_five_logical_entries(tmp_path: Path) -> None:
    workflow = _five_task_workflow()
    config = JournalConfig(path=tmp_path / "journal.jsonl")

    workflow.run(journal=config)

    records = _records(config.path)
    assert len({r["task_id"] for r in records}) == 5
    assert all(r["schema_version"] == SCHEMA_VERSION for r in records)
    assert all(r["workflow_fingerprint"].startswith("sha256:") for r in records)
    assert len([r for r in records if r["status"] == "pending"]) == 5
    assert len([r for r in records if r["status"] == "started"]) == 5
    assert len([r for r in records if r["status"] == "passed"]) == 5


def test_retries_add_attempts_without_new_logical_task(tmp_path: Path) -> None:
    workflow = _five_task_workflow()
    config = JournalConfig(path=tmp_path / "journal.jsonl")

    # Ordinary (non-reusable) tasks: a passed task runs again on the next run.
    workflow.run(journal=config)
    workflow.run(journal=config)

    records = _records(config.path)
    assert len({r["task_id"] for r in records}) == 5  # still five logical tasks
    assert max(r["attempt"] for r in records) == 2
    for task_id in {r["task_id"] for r in records}:
        assert {r["attempt"] for r in records if r["task_id"] == task_id} == {0, 1, 2}


# --- Step 2: automatic recording --------------------------------------------


def test_task_run_never_receives_a_journal_object() -> None:
    # The engine records to the journal; task bodies receive only TaskInputs.
    assert list(inspect.signature(_Ok.run).parameters) == ["self", "inputs"]
    assert list(inspect.signature(_Boom.run).parameters) == ["self", "inputs"]


def test_records_started_then_passed(tmp_path: Path) -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Build"))
    config = JournalConfig(path=tmp_path / "journal.jsonl")

    workflow.run(journal=config)

    records = _records(config.path)
    assert [r["status"] for r in records] == ["pending", "started", "passed"]
    assert records[1]["started_at"] is not None
    assert records[2]["finished_at"] is not None
    # The terminal record repeats the attempt's started_at.
    assert records[2]["started_at"] == records[1]["started_at"]


def test_records_started_then_failed(tmp_path: Path) -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Boom())
    config = JournalConfig(path=tmp_path / "journal.jsonl")

    with pytest.raises(RuntimeError, match="boom"):
        workflow.run(journal=config)

    records = _records(config.path)
    assert [r["status"] for r in records] == ["pending", "started", "failed"]


def test_records_finalizer_outcomes(tmp_path: Path) -> None:
    resource = task_resource(
        title="Acquire db",
        acquire_fn=lambda _inputs: None,
        release_fn=lambda _inputs, _value: None,
    )
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Use"), requires=(resource,))
    config = JournalConfig(path=tmp_path / "journal.jsonl")

    workflow.run(journal=config)

    records = _records(config.path)
    ids = {r["task_id"] for r in records}
    assert any(i.endswith("acquire-db") for i in ids)
    assert any(i.endswith("release-db") for i in ids)
    assert [r["status"] for r in records if r["task_id"].endswith("release-db")] == [
        "pending",
        "started",
        "passed",
    ]


def test_started_record_is_durable_before_task_body_runs(tmp_path: Path) -> None:
    config = JournalConfig(path=tmp_path / "journal.jsonl")
    seen: dict[str, list[dict]] = {}

    class _Peek(Task[None]):
        title = "Peek"

        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            # The started record must already be flushed to disk mid-task.
            seen["records"] = _records(config.path)
            return TaskOutcome()

    workflow = Workflow(workflow_id="wf")
    workflow.add(_Peek())
    workflow.run(journal=config)

    assert [r["status"] for r in seen["records"]] == ["pending", "started"]


def test_release_failure_records_failed_outcome(tmp_path: Path) -> None:
    def _boom(_inputs: TaskInputs, _value: object) -> None:
        raise RuntimeError("release boom")

    resource = task_resource(
        title="Acquire db", acquire_fn=lambda _inputs: None, release_fn=_boom
    )
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Use"), requires=(resource,))
    config = JournalConfig(path=tmp_path / "journal.jsonl")

    with pytest.raises(RuntimeError, match="Cleanup failed"):
        workflow.run(journal=config)

    records = _records(config.path)
    release_records = [r for r in records if r["task_id"].endswith("release-db")]
    assert [r["status"] for r in release_records] == ["pending", "started", "failed"]


def test_load_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Build"))
    config = JournalConfig(path=path)
    workflow.run(journal=config)

    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")  # blank line between attempts, must not break parsing

    # A fresh Journal (via a second run) re-reads the file including the
    # blank line; a second, ordinary (non-reusable) task run must still succeed.
    workflow.run(journal=config)
    records = _records(path)
    assert max(r["attempt"] for r in records) == 2


def test_load_ignores_only_an_incomplete_final_json_line(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    config = JournalConfig(path=path)
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Build"))
    workflow.run(journal=config)

    with path.open("a", encoding="utf-8") as handle:
        handle.write(f'{{"schema_version":{SCHEMA_VERSION},"workflow_id":"wf"')

    workflow.run(journal=config)

    assert max(record["attempt"] for record in _records(path)) == 2


def test_load_rejects_malformed_newline_terminated_record(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text("{not-json}\n")
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Build"))

    with pytest.raises(CorruptJournalError):
        workflow.run(journal=JournalConfig(path=path))


def test_load_rejects_malformed_interior_record(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text('{not-json}\n{"partial":')
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Build"))

    with pytest.raises(CorruptJournalError):
        workflow.run(journal=JournalConfig(path=path))


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("kind", 1),
        ("reference", None),
        ("digest", 42),
    ],
)
def test_load_rejects_evidence_with_invalid_field_types(
    tmp_path: Path, field: str, invalid_value: object
) -> None:
    path = tmp_path / "journal.jsonl"
    config = JournalConfig(path=path)
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Build"))
    workflow.run(journal=config)
    malformed = dict(_records(path)[-1])
    evidence = {"kind": "file-digest", "reference": "artifact", "digest": None}
    evidence[field] = invalid_value
    malformed["attempt"] = 2
    malformed["evidence"] = [evidence]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(malformed) + "\n")

    with pytest.raises(CorruptJournalError):
        workflow.run(journal=config, resume=True)


def test_load_filters_records_by_workflow_id(tmp_path: Path) -> None:
    """Never skip on evidence recorded by a different workflow sharing the file.

    That is the actual danger the `workflow_id` filter in `_load()` protects
    against: a `passed` record with verified evidence from a DIFFERENT workflow
    in the same journal file must not cause an incorrect skip (an ordinary,
    non-reusable task always reruns regardless, so the scenario needs a reusable
    task to be meaningful).
    """
    path = tmp_path / "journal.jsonl"
    config = JournalConfig(path=path)
    evidence = (Evidence("exact-value", "v1"),)

    other = Workflow(workflow_id="other-workflow")
    other.add(_ReusableOk("Build", evidence=evidence))
    other.run(journal=config)  # records "001.build" passed

    task = _ReusableOk("Build", evidence=evidence)
    workflow = Workflow(workflow_id="wf")
    workflow.add(task)
    workflow.run(journal=config, resume=True)

    assert task.ran is True  # not skipped based on the other workflow's evidence


def test_no_journal_writes_no_file(tmp_path: Path) -> None:
    workflow = Workflow(workflow_id="wf")
    workflow.add(_Ok("Build"))
    path = tmp_path / "journal.jsonl"

    workflow.run()  # journal is optional

    assert not path.exists()


def _kept(tmp_path: Path, value: object, *, always_release: bool = False):
    """One kept run holding a single resource; returns (calls, journal path)."""
    calls: list[str] = []
    resource = task_resource(
        title="Acquire vm",
        acquire_fn=lambda _inputs: value,
        release_fn=lambda _inputs, released: calls.append(f"release:{released}"),
        always_release=always_release,
    )
    workflow = Workflow(workflow_id="wf", keep=True)
    workflow.add(_Ok("Use"), requires=(resource,))
    path = tmp_path / "journal.jsonl"
    workflow.run(journal=JournalConfig(path))
    return calls, path


def _retained_records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("kind") == "retained"
    ]


def test_a_retained_resource_records_its_acquired_value(tmp_path: Path) -> None:
    """Journal the acquired value so a later process can release the resource.

    A later process has no _RunState, so the value release() needs must be in
    the journal or the resource can never be released again.
    """
    calls, path = _kept(tmp_path, {"name": "stack", "host": "10.0.0.1"})

    assert calls == []
    records = _retained_records(path)
    assert len(records) == 1
    assert records[0]["resource"] == "Acquire vm"
    assert records[0]["value"] == {"name": "stack", "host": "10.0.0.1"}


def test_an_always_release_resource_is_never_recorded_as_retained(
    tmp_path: Path,
) -> None:
    calls, path = _kept(tmp_path, {"token": "secret"}, always_release=True)

    assert calls == ["release:{'token': 'secret'}"]
    assert _retained_records(path) == []


def test_an_unjournalable_value_is_released_rather_than_silently_stranded(
    tmp_path: Path,
) -> None:
    """Release a value that cannot be journaled rather than strand it.

    Retention is a promise that a later teardown can finish the job. When the
    value cannot be written down that promise cannot be kept, so releasing now is
    the only outcome that leaves nothing unmanaged.
    """
    calls, path = _kept(tmp_path, object())

    assert len(calls) == 1
    assert _retained_records(path) == []
