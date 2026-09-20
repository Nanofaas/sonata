"""Optional append-only JSON Lines journal and resume-decision logic.

The journal records the lifecycle of a compiled workflow so an interrupted run can
resume. Storage is one JSON object per line; a single logical task (`task_id`) can
own several physical attempt records. Recovery truncates only a malformed,
non-newline-terminated final record left by an interrupted append.

Each record carries `schema_version` and is read only at the value `SCHEMA_VERSION`
declares; anything else raises `UnsupportedJournalSchemaError` rather than being
migrated or silently accepted. The generation is named in that one place on
purpose: this docstring used to call it "schema v2" and went on doing so long
after the constant had moved to 3, because a version written down twice drifts.

The records hold generic workflow/task/attempt/evidence data plus one record per
retained resource, naming the resource, its retention order and its acquired
value for `release_retained` to read back. Release identity, semantic versions,
registries and artifact policy stay out: those do not belong in Sonata.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import uuid
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sonata_engine.core.compiled import CompiledTask, CompiledWorkflow
from sonata_engine.core.outcome import Evidence
from sonata_engine.core.task import ReusableTask, Task
from sonata_engine.errors import (
    AmbiguousTaskStateError,
    CorruptJournalError,
    UnsupportedJournalSchemaError,
    WorkflowTopologyMismatchError,
)

SCHEMA_VERSION = 3


def _jsonable(value: object) -> object:
    """Return a JSON-serializable view of an acquired resource value.

    Raises `TypeError` when the value has no faithful JSON representation.

    Deliberately narrow. Dataclasses are the common shape for resource values (a
    VM's info, an endpoint pair) so they convert; everything else must already be
    a JSON primitive. A value needing custom encoding is one a later process
    could not faithfully reconstruct either, and pretending otherwise is how a
    resource ends up retained with no way to release it.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"resource value is not journalable: {type(value).__name__}")


Verifier = Callable[[Evidence], bool]
ResumeAction = Literal["run", "skip"]


# --- Evidence verification registry ------------------------------------------


def _verify_file_digest(evidence: Evidence) -> bool:
    """Return whether `file-digest` evidence still matches the file at `reference`."""
    if evidence.digest is None:
        return False
    try:
        data = Path(evidence.reference).read_bytes()
    except OSError:
        return False
    expected = evidence.digest.removeprefix("sha256:")
    return hashlib.sha256(data).hexdigest() == expected


DEFAULT_VERIFIERS: dict[str, Verifier] = {
    "file-digest": _verify_file_digest,
}


def _resolve_verifiers(verifiers: Mapping[str, Verifier] | None) -> dict[str, Verifier]:
    """Injected verifiers are MERGED over the built-in defaults (same key overrides)."""
    resolved = dict(DEFAULT_VERIFIERS)
    if verifiers:
        resolved.update(verifiers)
    return resolved


def _all_verified(
    evidence: tuple[Evidence, ...], verifiers: Mapping[str, Verifier]
) -> bool:
    """Every evidence entry must verify. Unknown `kind` (no verifier) fails closed."""
    if not evidence:
        return False
    for entry in evidence:
        verifier = verifiers.get(entry.kind)
        if verifier is None or not verifier(entry):
            return False
    return True


# --- Resume decision ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TaskState:
    """The latest recorded state of one logical task.

    Folded from that task's attempt records, so it carries the highest attempt
    seen and the status and evidence that attempt ended with.
    """

    task_id: str
    attempt: int
    status: str
    evidence: tuple[Evidence, ...]


def decide_resume(
    prior: TaskState | None,
    *,
    idempotent: bool,
    reusable: bool,
    verifiers: Mapping[str, Verifier],
) -> ResumeAction:
    """Decide whether to run or skip a consumer task given its prior recorded state.

    - No prior record, or a topology-initialized `pending` record: run fresh.
    - Done (passed/skipped): a reusable task whose every evidence entry verifies is
      skipped; anything else (ordinary task, or stale/unverifiable evidence) reruns.
    - Interrupted after `started`, or genuinely `failed`: an idempotent task may retry
      safely; a non-idempotent task cannot be resumed automatically -> raise.
    """
    if prior is None or prior.status == "pending":
        return "run"
    if prior.status in ("passed", "skipped"):
        if reusable and _all_verified(prior.evidence, verifiers):
            return "skip"
        return "run"
    # "started" (no terminal record) or "failed": only idempotent tasks retry.
    if idempotent:
        return "run"
    raise AmbiguousTaskStateError(
        f"{prior.task_id} is {prior.status!r} and non-idempotent; "
        "refusing automatic resume"
    )


# --- Storage -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalConfig:
    """Where the append-only journal lives."""

    path: Path


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _evidence_to_json(evidence: tuple[Evidence, ...]) -> list[dict[str, str | None]]:
    return [
        {"kind": e.kind, "reference": e.reference, "digest": e.digest} for e in evidence
    ]


def _evidence_from_json(raw: object) -> tuple[Evidence, ...]:
    if not isinstance(raw, list):
        raise TypeError("evidence must be a list")
    parsed: list[Evidence] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise TypeError("evidence entries must be objects")
        kind = entry.get("kind")
        reference = entry.get("reference")
        digest = entry.get("digest")
        if not isinstance(kind, str) or not isinstance(reference, str):
            raise TypeError("evidence kind and reference must be strings")
        if digest is not None and not isinstance(digest, str):
            raise TypeError("evidence digest must be a string or null")
        parsed.append(Evidence(kind=kind, reference=reference, digest=digest))
    return tuple(parsed)


class Journal:
    """Append-only JSON Lines reader/writer plus the per-task resume index.

    On construction it reads the whole file once, rejects any foreign
    `schema_version`, and folds the records into one `TaskState` per `task_id`.
    Writes are append-only and flushed+fsynced before the task executes, so a
    crash mid-task leaves a durable `started` record.

    Intended model: one journal file per workflow. `_load()` defensively filters
    records by `workflow_id` in case a file is ever shared across workflows.

    The constructor receives the complete compiled workflow and, only when
    `resume=True`, validates its deterministic fingerprint against every existing
    record, including each reusable task's semantic key -- resume refuses changed
    definitions instead of reusing evidence under a stale task ID. A non-resuming
    run treats fingerprint-mismatched records as belonging to a different workflow
    shape (same handling as a `workflow_id` mismatch): it simply ignores them
    rather than raising, since it never consults recorded state for a skip/retry
    decision anyway. Either way, attempt-0 `pending` records are created for every
    task missing from the (filtered) journal state.
    """

    def __init__(
        self,
        config: JournalConfig,
        compiled: CompiledWorkflow,
        verifiers: Mapping[str, Verifier] | None = None,
        *,
        resume: bool = False,
    ) -> None:
        """Open the journal for `compiled`, validating and folding prior records.

        The file is read once here. Records for another workflow, or for another
        fingerprint of this one, are filtered out -- unless `resume=True`, which
        refuses them with `WorkflowTopologyMismatchError` instead. Every compiled
        task with no surviving state is then written an attempt-0 `pending`
        record, so the file always describes the whole workflow. `verifiers` are
        merged over the built-in defaults.
        """
        self.path = config.path
        self.workflow_id = compiled.workflow_id
        self.workflow_fingerprint = compiled.fingerprint
        self._resume = resume
        # ponytail: uuid4 hex, not a ULID -- unique-per-run is all we need, no new dep.
        self.run_id = uuid.uuid4().hex
        self.verifiers = _resolve_verifiers(verifiers)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # started_at captured per attempt so the terminal record can repeat it.
        self._started_at: dict[tuple[str, int], str] = {}
        self._retained: list[dict[str, Any]] = []
        self._states = self._load()
        for task in compiled.tasks:
            if task.task_id not in self._states:
                self._write(task.task_id, 0, "pending")

    def _load(self) -> dict[str, TaskState]:
        states: dict[str, TaskState] = {}
        if not self.path.exists():
            return states
        lines = self.path.read_bytes().splitlines(keepends=True)
        offset = 0
        # _load runs exactly once per Journal instance (from __init__), so the
        # warn-once flag is per-instance; a future second load would warn again.
        self._warned_mismatch = False
        for index, raw_line in enumerate(lines):
            line_start = offset
            offset += len(raw_line)
            is_torn_tail = index == len(lines) - 1 and not raw_line.endswith(
                (b"\n", b"\r")
            )
            record = self._parse_record(raw_line, index, is_torn_tail, line_start)
            if record is None:
                continue
            version = record.get("schema_version")
            if version != SCHEMA_VERSION:
                raise UnsupportedJournalSchemaError(
                    f"{self.path}: schema_version {version!r}, "
                    f"expected {SCHEMA_VERSION}"
                )
            if record.get("workflow_id") != self.workflow_id:
                continue
            if self._check_fingerprint(record):
                continue
            if record.get("kind") == "retained":
                # Resource retention, not a task outcome: no task_id, no part in
                # resume decisions. `release_retained` is what reads these back.
                self._retained.append(record)
                continue
            self._fold_record(record, index, states)
        return states

    def _check_fingerprint(self, record: dict[str, Any]) -> bool:
        """Return True when the record's fingerprint mismatches and must be skipped.

        On resume a mismatch refuses the journal (WorkflowTopologyMismatchError).
        Otherwise it warns once per load and ignores the mismatched records (a
        Sonata upgrade invalidates prior journals -- see README.md).
        """
        fingerprint = record.get("workflow_fingerprint")
        if fingerprint == self.workflow_fingerprint:
            return False
        if self._resume:
            raise WorkflowTopologyMismatchError(
                f"{self.path}: workflow {self.workflow_id!r} has fingerprint "
                f"{fingerprint!r}, expected {self.workflow_fingerprint!r}"
            )
        if not self._warned_mismatch:
            self._warned_mismatch = True
            warnings.warn(
                f"{self.path}: existing journal records for workflow "
                f"{self.workflow_id!r} have fingerprint {fingerprint!r}, expected "
                f"{self.workflow_fingerprint!r}; ignoring them and appending a new "
                "topology to the same file (a Sonata upgrade invalidates prior "
                "journals -- see README.md)",
                stacklevel=2,
            )
        return True

    def _parse_record(
        self, raw_line: bytes, index: int, is_torn_tail: bool, line_start: int
    ) -> dict[str, Any] | None:
        """Decode and parse one journal line; None for a blank line or torn tail.

        A torn tail (crash mid-write) is truncated and dropped. Any other
        malformed line raises `CorruptJournalError`.
        """
        try:
            line = raw_line.decode("utf-8")
        except UnicodeError as exc:
            if is_torn_tail:
                self._truncate_torn_tail(line_start)
                return None
            raise CorruptJournalError(
                f"{self.path}:{index + 1}: record is not valid UTF-8"
            ) from exc
        if not line.strip():
            return None
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if is_torn_tail:
                self._truncate_torn_tail(line_start)
                return None
            raise CorruptJournalError(
                f"{self.path}:{index + 1}: malformed JSON record"
            ) from exc
        if not isinstance(record, dict):
            raise CorruptJournalError(
                f"{self.path}:{index + 1}: journal record must be an object"
            )
        return record

    def _fold_record(
        self, record: dict[str, Any], index: int, states: dict[str, TaskState]
    ) -> None:
        """Merge one task-outcome record into `states`; raises on malformed data.

        Records are appended in order: a later record for the same (or higher)
        attempt supersedes an earlier one -- terminal replaces started.
        """
        try:
            task_id = str(record["task_id"])
            attempt = int(record["attempt"])
            status = str(record["status"])
            raw_evidence = record.get("evidence", [])
            evidence = _evidence_from_json(raw_evidence)
        except (KeyError, TypeError, ValueError) as exc:
            raise CorruptJournalError(
                f"{self.path}:{index + 1}: malformed journal record"
            ) from exc
        if status not in {"pending", "started", "passed", "failed", "skipped"}:
            raise CorruptJournalError(
                f"{self.path}:{index + 1}: invalid task status {status!r}"
            )
        existing = states.get(task_id)
        if existing is None or attempt >= existing.attempt:
            states[task_id] = TaskState(
                task_id=task_id,
                attempt=attempt,
                status=status,
                evidence=evidence,
            )

    def _truncate_torn_tail(self, offset: int) -> None:
        """Remove only a non-newline-terminated final record after a crash."""
        with self.path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            os.fsync(handle.fileno())

    def next_attempt(self, task_id: str) -> int:
        """Return the attempt number the next record for `task_id` should carry."""
        state = self._states.get(task_id)
        return state.attempt + 1 if state is not None else 1

    def decide(self, compiled_task: CompiledTask[object]) -> ResumeAction:
        """Resume decision for a consumer task (may raise `AmbiguousTaskStateError`)."""
        return self.decide_task(compiled_task.task_id, compiled_task.task)

    def decide_task(self, task_id: str, task: Task[Any]) -> ResumeAction:
        """Return the resume decision for anything with a journal identity.

        That is a compiled unit or one step of a composite; both are decided from
        the same recorded state plus the task's own idempotence and reusability.
        """
        return decide_resume(
            self._states.get(task_id),
            idempotent=task.idempotent,
            reusable=isinstance(task, ReusableTask) and task.reusable,
            verifiers=self.verifiers,
        )

    def record_started(self, task_id: str, attempt: int) -> None:
        """Record that an attempt has begun, before the task runs."""
        self._write(
            task_id, attempt, "started", started_at=_utc_iso(), finished_at=None
        )

    def record_passed(
        self, task_id: str, attempt: int, evidence: tuple[Evidence, ...] = ()
    ) -> None:
        """Record that an attempt passed, carrying the evidence it produced."""
        self._write(
            task_id, attempt, "passed", finished_at=_utc_iso(), evidence=evidence
        )

    def record_failed(self, task_id: str, attempt: int) -> None:
        """Record that an attempt ended in failure, with no evidence."""
        self._write(task_id, attempt, "failed", finished_at=_utc_iso())

    def record_skipped(self, task_id: str, attempt: int) -> None:
        """Record a skip, carrying forward the evidence that justified it."""
        prior = self._states.get(task_id)
        evidence = prior.evidence if prior is not None else ()
        self._write(
            task_id, attempt, "skipped", finished_at=_utc_iso(), evidence=evidence
        )

    def record_retained(self, resource_title: str, order: int, value: object) -> bool:
        """Write down a resource `keep` held on to, with the value release() needs.

        A later process has no `_RunState`, so this record is its only way to ever
        release the resource. Returns False when the value cannot be written down:
        the caller must then release it now rather than strand it, because a
        retention it cannot record is a promise it cannot keep.
        """
        try:
            encoded = _jsonable(value)
            json.dumps(encoded, separators=(",", ":"))
        except (TypeError, ValueError):
            return False
        record = {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "workflow_fingerprint": self.workflow_fingerprint,
            "run_id": self.run_id,
            "kind": "retained",
            "resource": resource_title,
            "order": order,
            "value": encoded,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True

    def _write(
        self,
        task_id: str,
        attempt: int,
        status: str,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        evidence: tuple[Evidence, ...] = (),
    ) -> None:
        if started_at is not None:
            self._started_at[(task_id, attempt)] = started_at
        record = {
            "schema_version": SCHEMA_VERSION,
            "workflow_id": self.workflow_id,
            "workflow_fingerprint": self.workflow_fingerprint,
            "run_id": self.run_id,
            "task_id": task_id,
            "attempt": attempt,
            "status": status,
            "started_at": self._started_at.get((task_id, attempt)),
            "finished_at": finished_at,
            "evidence": _evidence_to_json(evidence),
        }
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if status == "started":
                # Only the pre-execution record needs disk-level durability: it's the
                # one a crash mid-task must leave behind. Terminal records losing their
                # fsync race just look like `started` on the next resume, which
                # decide_resume already handles safely.
                os.fsync(handle.fileno())
        self._states[task_id] = TaskState(task_id, attempt, status, evidence)
