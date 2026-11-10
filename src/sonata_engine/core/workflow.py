"""The workflow builder, its compiler, and the runner that executes it.

`Workflow` records tasks in the order they were added, `compile()` rewrites them
into a flat sequence of `CompiledTask` units with resources spliced in around
their consumers, and `_run_compiled` executes that sequence -- owning the
journal records, the resume decisions, the lifecycle events, and the cleanup
that runs when a task fails part way through.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

from sonata_engine.core.compiled import (
    CompiledTask,
    CompiledWorkflow,
    TaskExecution,
    WorkflowResult,
)
from sonata_engine.core.compose import _AcquireMembers
from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.resource_task import Resource, ResourceOp, ResourceOperation
from sonata_engine.core.selection import Selection
from sonata_engine.core.slug import slugify as _slugify
from sonata_engine.core.task import ReusableTask, Task
from sonata_engine.errors import (
    CompositeMemberRequiredError,
    InvalidTaskOutcomeError,
    ResourceDependencyCycleError,
    ResumeConfigurationError,
    SelectionError,
)
from sonata_engine.journal import Journal, JournalConfig, Verifier
from sonata_engine.workflow.observers import WorkflowCompletion, WorkflowObserver
from sonata_engine.workflow.reporting import _task_lifecycle, _task_skipped


@dataclass
class _RunState:
    """Runner-private mutable resource values for one workflow execution."""

    values: dict[int, object] = field(default_factory=dict)

    def inputs_for(self, accessible: tuple[Resource[Any], ...]) -> TaskInputs:
        return TaskInputs._for_resource_values(self.values, accessible)

    def publish(self, resource: Resource[Any], value: object) -> None:
        self.values[id(resource)] = value

    def remove(self, resource: Resource[Any]) -> None:
        """Drop the reference so a large acquired value can be collected."""
        self.values.pop(id(resource), None)


def _resolve_slug(slugs: list[str], wanted: str) -> int:
    """Index of the single consumer whose slug is `wanted`.

    Ambiguity is an error rather than an implicit multi-select: duplicate titles
    are legal (the ordinal disambiguates their IDs) but stop being addressable.
    """
    matches = [index for index, slug in enumerate(slugs) if slug == wanted]
    if not matches:
        available = ", ".join(sorted(set(slugs)))
        raise SelectionError(f"no task matches slug {wanted!r}; available: {available}")
    if len(matches) > 1:
        raise SelectionError(
            f"slug {wanted!r} matches {len(matches)} tasks; "
            "titles must be unique for a task to be selectable"
        )
    return matches[0]


def _maybe_resume_skip(
    *, task_id: str, task: Task[Any], jrnl: Journal | None, resume: bool
) -> TaskExecution | None:
    """Return the 'skipped' execution when resume says skip, and None otherwise."""
    if jrnl is None or not resume:
        return None
    if jrnl.decide_task(task_id, task) != "skip":
        return None
    jrnl.record_skipped(task_id, jrnl.next_attempt(task_id))
    _task_skipped(task_id=task_id, title=task.title)
    return TaskExecution(task_id=task_id, status="skipped", outcome=None)


def _execute_recorded(
    *,
    task: Task[Any],
    task_id: str,
    make_inputs: Callable[[], TaskInputs],
    jrnl: Journal | None,
    resume: bool,
    on_outcome: Callable[[TaskOutcome[Any]], None] | None = None,
    journal_errors: list[BaseException] | None = None,
) -> TaskExecution:
    """Run one thing that has a journal identity, recording its outcome.

    Shared by compiled consumer/acquire units and by the steps of a composite,
    so the resume decision, the started/passed/failed records, the outcome
    validation and the journal-failure note exist once. Release units keep
    their own path in `_release`: cleanup must not abort on a journal error.
    A step run as cleanup passes `journal_errors`: a journal write failing then
    lands there instead of aborting, and the step runs anyway.

    `make_inputs` is a callable, not a value, because it is called inside the
    lifecycle -- a failure resolving resources must be reported as a task
    failure, not escape unreported.
    """
    skip = _maybe_resume_skip(task_id=task_id, task=task, jrnl=jrnl, resume=resume)
    if skip is not None:
        return skip

    def record(write: Callable[[], None]) -> None:
        if journal_errors is None:
            write()
            return
        try:
            write()
        except OSError as journal_error:
            journal_error.add_note(f"while recording {task_id}")
            journal_errors.append(journal_error)

    attempt = jrnl.next_attempt(task_id) if jrnl is not None else 0
    if jrnl is not None:
        # Flush the started record BEFORE executing, so a crash mid-task is durable.
        record(lambda: jrnl.record_started(task_id, attempt))
    try:
        with _task_lifecycle(task_id=task_id, title=task.title):
            outcome = task.run(make_inputs())
            if not isinstance(outcome, TaskOutcome):
                raise InvalidTaskOutcomeError(
                    f"{task_id} returned {outcome!r}, expected TaskOutcome"
                )
            if isinstance(task, ReusableTask) and outcome.value is not None:
                raise InvalidTaskOutcomeError(
                    f"{task_id} is reusable and returned a runtime value"
                )
            if on_outcome is not None:
                on_outcome(outcome)
    except BaseException as exc:
        if jrnl is not None:
            try:
                jrnl.record_failed(task_id, attempt)
            except OSError as journal_error:
                exc.add_note(f"Failed to record task failure: {journal_error}")
        raise
    if jrnl is not None:
        record(lambda: jrnl.record_passed(task_id, attempt, outcome.evidence))
    return TaskExecution(task_id=task_id, status="passed", outcome=outcome)


@dataclass(frozen=True, slots=True)
class _StepScope:
    """The runner's `StepScope`: everything a composite must not know."""

    prefix: str
    base_inputs: TaskInputs
    jrnl: Journal | None
    resume: bool
    # Set on a cleanup scope: journal failures are collected here, not raised.
    journal_errors: list[BaseException] | None = None

    def run_step(
        self,
        step: Task[Any],
        slug: str,
        upstream: Any,  # noqa: ANN401
        *,
        on_outcome: Callable[[TaskOutcome[Any]], None] | None = None,
    ) -> TaskExecution:
        if not slug or "/" in slug:
            raise ValueError(f"step slug {slug!r} must be non-empty and contain no '/'")
        step_id = f"{self.prefix}/{slug}"

        def make_inputs() -> TaskInputs:
            return cast(
                TaskInputs,
                replace(
                    self.base_inputs,
                    _upstream=upstream,
                    _step_scope=replace(self, prefix=step_id),
                ),
            )

        return _execute_recorded(
            task=step,
            task_id=step_id,
            make_inputs=make_inputs,
            jrnl=self.jrnl,
            resume=self.resume,
            on_outcome=on_outcome,
            journal_errors=self.journal_errors,
        )

    def rebased(self, inputs: TaskInputs) -> _StepScope:
        return replace(self, base_inputs=inputs)

    def for_cleanup(
        self, inputs: TaskInputs, errors: list[BaseException]
    ) -> _StepScope:
        return replace(self, base_inputs=inputs, resume=False, journal_errors=errors)


@dataclass
class _MergeState:
    """DFS state for `_merge_resources`, threaded through the recursion."""

    # Keys deliberately use object identity: resource callbacks and a malformed
    # cyclic graph must never be hashed while the compiler is diagnosing it.
    first: dict[int, int] = field(default_factory=dict)
    last: dict[int, int] = field(default_factory=dict)
    resources: dict[int, Resource[Any]] = field(default_factory=dict)
    discovery: list[int] = field(default_factory=list)
    colors: dict[int, str] = field(default_factory=dict)
    stack: list[Resource[Any]] = field(default_factory=list)
    seen_at_index: dict[int, int] = field(default_factory=dict)


def _register_dependency(
    state: _MergeState, resource: Resource[Any], index: int
) -> None:
    """Register one resource at one consumer index, walking its dependencies.

    Depth-first visit; cycles raise `ResourceDependencyCycleError`. A 'done'
    node is re-walked only when re-encountered at a later index -- without
    this, a diamond-shaped graph re-walks whole shared subtrees on every path
    to them, doubling work per level -- exponential in depth.
    """
    key = id(resource)
    color = state.colors.get(key, "unseen")
    if color == "visiting":
        cycle_start = next(i for i, item in enumerate(state.stack) if item is resource)
        cycle = (*state.stack[cycle_start:], resource)
        raise ResourceDependencyCycleError(
            "resource dependency cycle: " + " -> ".join(item.title for item in cycle)
        )
    if color == "unseen":
        state.colors[key] = "visiting"
        state.stack.append(resource)
        for dependency in resource.requires:
            _register_dependency(state, dependency, index)
        state.stack.pop()
        state.colors[key] = "done"
        state.resources[key] = resource
        state.discovery.append(key)
        state.seen_at_index[key] = index
    elif state.seen_at_index.get(key) != index:
        state.seen_at_index[key] = index
        for dependency in resource.requires:
            _register_dependency(state, dependency, index)
    state.first.setdefault(key, index)
    state.last[key] = index


def _reject_required_members(
    definitions: list[tuple[Task[Any], tuple[Resource, ...]]], state: _MergeState
) -> None:
    """Refuse a composite member that is also required outside its composite.

    The composite acquires its members itself, so a direct requirement would
    acquire one twice -- and so would two composites sharing a member, at any
    depth of nesting. Members never reach `state` through a composite (its
    `requires` excludes them), so any requirement naming one is a direct one.
    """
    owner: dict[int, Resource[Any]] = {}

    def claim(composite: _AcquireMembers, top: Resource[Any]) -> None:
        for member in composite.members:
            if id(member) in owner:
                raise CompositeMemberRequiredError(
                    f"{member.title!r} is a member of composite "
                    f"{owner[id(member)].title!r} and of {top.title!r}: "
                    "a resource can belong to one composite only"
                )
            owner[id(member)] = top
            if isinstance(member.acquire, _AcquireMembers):
                claim(member.acquire, top)

    for resource in state.resources.values():
        if isinstance(resource.acquire, _AcquireMembers):
            claim(resource.acquire, resource)
    requirers = [(task.title, requires) for task, requires in definitions] + [
        (resource.title, resource.requires) for resource in state.resources.values()
    ]
    for title, requires in requirers:
        for required in requires:
            composite = owner.get(id(required))
            if composite is not None:
                raise CompositeMemberRequiredError(
                    f"{title!r} requires {required.title!r}, a member of "
                    f"composite {composite.title!r}: require the composite instead"
                )


@dataclass
class Workflow:
    """Ordered workflow builder, compiler, and executor."""

    workflow_id: str
    keep: bool = False
    _definitions: list[tuple[Task[Any], tuple[Resource, ...]]] = field(
        default_factory=list, init=False, repr=False
    )
    # Position of each retention record, so a teardown can release in reverse.
    _retention_order: int = field(default=0, init=False, repr=False)

    def run(
        self,
        *,
        journal: JournalConfig | None = None,
        resume: bool = False,
        verifiers: Mapping[str, Verifier] | None = None,
        select: Selection | None = None,
        observers: tuple[WorkflowObserver, ...] = (),
    ) -> WorkflowResult:
        """Compile and run this workflow using compiler-owned task identities.

        `select` narrows the run to a slice of consumer tasks; their resources
        are still acquired and released around them. A sliced run has a
        different fingerprint, so `resume` across one fails closed.
        """
        started_at = datetime.now(UTC)
        error: BaseException | None = None
        try:
            return self._run_compiled(
                self.compile(select=select),
                journal=journal,
                resume=resume,
                verifiers=verifiers,
            )
        except BaseException as exc:
            error = exc
            raise
        finally:
            completion = WorkflowCompletion(
                workflow_id=self.workflow_id,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                error=error,
            )
            for observer in observers:
                try:
                    observer.finished(completion)
                except BaseException as observer_error:
                    if error is not None:
                        error.add_note(f"Workflow observer failed: {observer_error}")

    def _run_compiled(
        self,
        compiled: CompiledWorkflow,
        *,
        journal: JournalConfig | None = None,
        resume: bool = False,
        verifiers: Mapping[str, Verifier] | None = None,
    ) -> WorkflowResult:
        """Run compiled tasks in order, owning their lifecycle events.

        With no `journal` and `resume=False` this behaves exactly as before -- the
        journal is optional. When a `journal` is configured the runner records every
        `started`/`passed`/`failed`/`skipped` outcome (tasks never touch the journal
        themselves). When `resume=True` it consults the recorded state to skip verified
        reusable tasks and retry safely; `resume=True` without a `journal` is rejected.

        Happy path: acquire/consumer/release units execute in the linear order
        `compile()` placed them -- releases already sit after their last consumer.

        Failure path: when a consumer or acquire raises, we stop the main walk and run
        the release unit for every resource acquired-but-not-yet-released, in reverse
        acquisition order. Release errors never abort remaining releases; they are
        collected and combined with the primary error. `keep` skips every release
        except those whose `Resource` sets `always_release`.
        """
        if resume and journal is None:
            raise ResumeConfigurationError("resume=True requires a JournalConfig")
        jrnl = (
            Journal(journal, compiled, verifiers, resume=resume)
            if journal is not None
            else None
        )

        self._retention_order = 0
        release_for = {
            id(task.resource): task
            for task in compiled.tasks
            if task.kind == "release" and task.resource is not None
        }
        retained_resources = self._retained_resources(compiled)
        # Release units for acquired-but-not-yet-released resources, in
        # acquisition order.
        pending: list[CompiledTask[object]] = []
        release_errors: list[BaseException] = []
        main_error: BaseException | None = None
        executions: list[TaskExecution] = []
        state = _RunState()

        try:
            for compiled_task in compiled.tasks:
                if compiled_task.kind == "release":
                    self._run_release_step(
                        compiled_task,
                        executions,
                        pending,
                        release_errors,
                        state,
                        jrnl,
                        retained_resources,
                    )
                    continue
                executions.append(
                    self._run_unit(
                        compiled_task,
                        jrnl,
                        state,
                        resume=resume,
                        on_executed=self._on_executed_for(
                            compiled_task, release_for, pending
                        ),
                    )
                )
        except BaseException as exc:  # NOSONAR S5754 - stop the walk so pending releases can run; re-raised by _raise_final  # noqa: E501
            main_error = exc

        if main_error is not None:
            self._release_pending(
                pending, release_errors, state, jrnl, retained_resources
            )
        self._raise_final(main_error, release_errors)
        return WorkflowResult(workflow_id=compiled.workflow_id, tasks=tuple(executions))

    def _retained_resources(self, compiled: CompiledWorkflow) -> frozenset[int]:
        """Everything `keep` holds on to: all of it but what asked to be released.

        No dependency walk any more. Retention used to be opt-in, so keeping a
        resource meant also keeping whatever made it usable; now it is opt-out,
        so the only resources released under `keep` are the ones that said so,
        and they are released in the same reverse order as always.
        """
        if not self.keep:
            return frozenset()
        return frozenset(
            id(task.resource)
            for task in compiled.tasks
            if task.kind == "acquire"
            and task.resource is not None
            and not task.resource.always_release
        )

    def _release_pending(
        self,
        pending: list[CompiledTask[object]],
        release_errors: list[BaseException],
        state: _RunState,
        jrnl: Journal | None,
        retained_resources: frozenset[int],
    ) -> None:
        """Run every pending release in reverse acquisition order."""
        for compiled_task in reversed(pending):
            self._release(
                compiled_task, release_errors, state, jrnl, retained_resources
            )

    def _run_release_step(
        self,
        compiled_task: CompiledTask[object],
        executions: list[TaskExecution],
        pending: list[CompiledTask[object]],
        release_errors: list[BaseException],
        state: _RunState,
        jrnl: Journal | None,
        retained_resources: frozenset[int],
    ) -> None:
        """Run one release unit in the main walk, collecting its outcome."""
        execution = self._release(
            compiled_task, release_errors, state, jrnl, retained_resources
        )
        if execution is not None:
            executions.append(execution)
        pending[:] = [p for p in pending if p is not compiled_task]

    def _on_executed_for(
        self,
        compiled_task: CompiledTask[object],
        release_for: Mapping[int, CompiledTask[object]],
        pending: list[CompiledTask[object]],
    ) -> Callable[[], None] | None:
        """Return the callback that queues an acquire unit's release, else None."""
        if compiled_task.kind != "acquire":
            return None
        resource = compiled_task.resource
        if resource is None:
            raise RuntimeError("acquire task has no resource")
        return partial(pending.append, release_for[id(resource)])

    def _raise_final(
        self, main_error: BaseException | None, release_errors: list[BaseException]
    ) -> None:
        """Raise the run's outcome once the walk and any cleanup have finished.

        A critical error (`KeyboardInterrupt`, `SystemExit`) wins over everything
        and carries the others as notes. Otherwise the primary task error is
        raised, or `RuntimeError` if there were also cleanup errors, or the
        cleanup errors alone when the tasks themselves succeeded.
        """
        critical_error = (
            main_error
            if main_error is not None and not isinstance(main_error, Exception)
            else next(
                (error for error in release_errors if not isinstance(error, Exception)),
                None,
            )
        )
        if critical_error is not None:
            if main_error is not None and main_error is not critical_error:
                critical_error.add_note(f"Task error: {main_error}")
            for error in release_errors:
                if error is not critical_error:
                    critical_error.add_note(f"Cleanup error: {error}")
            raise critical_error
        if main_error is not None:
            if release_errors:
                combined = f"{main_error}\n\nCleanup errors:\n" + "\n".join(
                    str(error) for error in release_errors
                )
                raise RuntimeError(combined) from main_error
            raise main_error
        if release_errors:
            raise RuntimeError(
                "Cleanup failed:\n" + "\n".join(str(error) for error in release_errors)
            )

    def _next_attempt(self, jrnl: Journal | None, task_id: str) -> int:
        return jrnl.next_attempt(task_id) if jrnl is not None else 0

    def _run_unit(
        self,
        compiled_task: CompiledTask[object],
        jrnl: Journal | None,
        state: _RunState,
        *,
        resume: bool,
        on_executed: Callable[[], None] | None = None,
    ) -> TaskExecution:
        """Run one consumer or acquire unit, recording its journal outcome.

        A failure is recorded and then propagates. Consumers and acquire units
        consult the same resume decision matrix. The acquire task's `idempotent`
        controls whether an interrupted or failed acquire can retry; a passed
        acquire always reruns because it is non-reusable. Releases are handled
        separately by `_release`.

        A journal-write failure here is allowed to propagate (treated like any
        other task failure) -- unlike `_release`, which must keep attempting
        every pending release even if the journal itself is failing.
        """

        def make_inputs() -> TaskInputs:
            base = state.inputs_for(
                compiled_task.resource.requires
                if compiled_task.kind == "acquire"
                and compiled_task.resource is not None
                else compiled_task.required_resources
            )
            return cast(
                TaskInputs,
                replace(
                    base,
                    _step_scope=_StepScope(
                        prefix=compiled_task.task_id,
                        base_inputs=base,
                        jrnl=jrnl,
                        resume=resume,
                    ),
                ),
            )

        def on_outcome(outcome: TaskOutcome[Any]) -> None:
            if compiled_task.kind == "acquire" and compiled_task.resource is not None:
                state.publish(compiled_task.resource, outcome.value)
            if on_executed is not None:
                on_executed()

        return _execute_recorded(
            task=compiled_task.task,
            task_id=compiled_task.task_id,
            make_inputs=make_inputs,
            jrnl=jrnl,
            resume=resume,
            on_outcome=on_outcome,
        )

    def _record_release_outcome(
        self, release_errors: list[BaseException], record: Callable[[], None]
    ) -> None:
        """Run a journal write, collecting an I/O failure rather than propagating it.

        Cleanup must keep attempting every pending release even if the journal
        itself is failing (e.g. disk full) -- a journal-write error here must
        never mask the real release failure or abort the reverse-release loop.
        """
        try:
            record()
        except OSError as exc:
            exc.add_note("while recording release outcome")
            release_errors.append(exc)

    def _release(
        self,
        compiled_task: CompiledTask[object],
        release_errors: list[BaseException],
        state: _RunState,
        jrnl: Journal | None = None,
        retained_resources: frozenset[int] = frozenset(),
    ) -> TaskExecution | None:
        """Run one release unit, honoring retention, collecting errors.

        A release is a non-reusable finalizer: it always runs, never consults prior
        state, but its outcome is still recorded when a journal is configured.
        The release task reads the acquired value as `inputs.upstream()`; a
        `Steps` release journals its steps beneath this unit, never resuming.
        """
        resource = compiled_task.resource
        if resource is not None and id(resource) in retained_resources:
            skipped = self._retention_skip(
                resource=resource,
                compiled_task=compiled_task,
                release_errors=release_errors,
                state=state,
                jrnl=jrnl,
            )
            if skipped is not None:
                return skipped
        task_id = compiled_task.task_id
        attempt = self._next_attempt(jrnl, task_id)
        if jrnl is not None:
            self._record_release_outcome(
                release_errors, lambda: jrnl.record_started(task_id, attempt)
            )
        try:
            with _task_lifecycle(task_id=task_id, title=compiled_task.task.title):
                if resource is None:
                    raise RuntimeError("release task has no resource")
                base = state.inputs_for((resource, *resource.requires))
                outcome = compiled_task.task.run(
                    replace(
                        base,
                        _upstream=base.resource(resource),
                        _step_scope=_StepScope(
                            prefix=task_id,
                            base_inputs=base,
                            jrnl=jrnl,
                            resume=False,
                            journal_errors=release_errors,
                        ),
                    )
                )
                if not isinstance(outcome, TaskOutcome):
                    raise InvalidTaskOutcomeError(
                        f"{task_id} returned {outcome!r}, expected TaskOutcome"
                    )
        except BaseException as exc:  # NOSONAR S5754 - collect the failure; every pending release must still run  # noqa: E501
            release_errors.append(exc)
            if jrnl is not None:
                self._record_release_outcome(
                    release_errors, lambda: jrnl.record_failed(task_id, attempt)
                )
        else:
            if jrnl is not None:
                self._record_release_outcome(
                    release_errors, lambda: jrnl.record_passed(task_id, attempt)
                )
            return TaskExecution(task_id=task_id, status="passed", outcome=outcome)
        finally:
            if resource is not None:
                state.remove(resource)
        return None

    def _retention_skip(
        self,
        *,
        resource: Resource[Any],
        compiled_task: CompiledTask[object],
        release_errors: list[BaseException],
        state: _RunState,
        jrnl: Journal | None,
    ) -> TaskExecution | None:
        """Record the retention and report the skipped release.

        Returns `None` when the retention record could not be written, in which
        case the caller falls through and releases the resource for real.

        Retention is a promise that a later run can finish the job, and that
        run will have no `_RunState`. With a journal configured, a promise
        that cannot be written down cannot be kept, so releasing now is the
        lesser evil: a resource held with no way to release it is worse than
        one released early. Without a journal there is nothing to record, and
        the retention is kept silently.
        """
        if jrnl is not None and not jrnl.record_retained(
            resource.title, self._retention_order, state.values.get(id(resource))
        ):
            return None
        self._retention_order += 1
        if jrnl is not None:
            self._record_release_outcome(
                release_errors,
                lambda: jrnl.record_skipped(
                    compiled_task.task_id,
                    jrnl.next_attempt(compiled_task.task_id),
                ),
            )
        try:
            _task_skipped(task_id=compiled_task.task_id, title=compiled_task.task.title)
        except BaseException as exc:  # NOSONAR S5754 - a reporting failure must not abort the retention skip  # noqa: E501
            release_errors.append(exc)
        state.remove(resource)
        return TaskExecution(
            task_id=compiled_task.task_id, status="skipped", outcome=None
        )

    def add(self, task: Task[Any], requires: tuple[Resource, ...] = ()) -> Workflow:
        """Record a task definition and the resources it consumes. Order preserved."""
        self._definitions.append((task, requires))
        return self

    def compile(self, *, select: Selection | None = None) -> CompiledWorkflow:
        """Assign stable, deterministic IDs to the recorded task definitions.

        Each `Resource` referenced across `requires` gets one acquire unit
        spliced immediately before its first consumer and one release unit
        immediately after its last consumer. Releases landing at the same point
        run in reverse acquisition order (last-acquired-first-released). IDs are
        `{ordinal:03d}.{slug}` derived from position in the final merged
        sequence; duplicate titles are disambiguated by ordinal.

        `select` filters consumer definitions BEFORE resources are spliced, so
        the surviving consumers still get their acquire/release units. Ordinals
        renumber over the survivors: a sliced run is a different topology, and
        the journal fingerprint will refuse to resume across it.
        """
        if not self.workflow_id:
            raise ValueError("Workflow.compile() requires a non-empty workflow_id")

        merged = self._merge_resources(self._select(select))
        compiled_tasks = tuple(
            CompiledTask(
                task_id=f"{ordinal:03d}.{_slugify(entry.task.title)}",
                task=entry.task,
                required_resources=entry.required_resources,
                kind=entry.kind,
                resource=entry.resource,
            )
            for ordinal, entry in enumerate(merged, start=1)
        )
        return CompiledWorkflow(workflow_id=self.workflow_id, tasks=compiled_tasks)

    def _select(
        self, select: Selection | None
    ) -> list[tuple[Task[Any], tuple[Resource, ...]]]:
        """Filter consumer definitions by title slug.

        Resources are left to the compiler, which re-splices their acquire and
        release units around whichever consumers survive.
        """
        slugs = [_slugify(task.title) for task, _requires in self._definitions]
        for slug, (task, _requires) in zip(slugs, self._definitions, strict=True):
            if not slug:
                raise ValueError(f"task title {task.title!r} produces an empty slug")
        if select is None or select.is_empty:
            return list(self._definitions)
        if select.only is not None:
            return [self._definitions[_resolve_slug(slugs, select.only)]]
        first = _resolve_slug(slugs, select.start) if select.start is not None else 0
        last = (
            _resolve_slug(slugs, select.until)
            if select.until is not None
            else len(self._definitions) - 1
        )
        if first > last:
            raise SelectionError(
                f"start {select.start!r} comes after until {select.until!r}"
            )
        return list(self._definitions[first : last + 1])

    def _merge_resources(
        self, definitions: list[tuple[Task[Any], tuple[Resource, ...]]]
    ) -> list[CompiledTask[Any]]:
        """Splice acquire/release units around consumers (IDs assigned by caller)."""
        # First/last consumer index per resource, in DFS declaration order.
        state = _MergeState()
        for index, (_task, requires) in enumerate(definitions):
            for resource in requires:
                _register_dependency(state, resource, index)
        _reject_required_members(definitions, state)

        # Acquisition order: by first-consumer index, then discovery order.
        acquire_rank = {key: rank for rank, key in enumerate(state.discovery)}
        acquires_before: dict[int, list[int]] = {}
        releases_after: dict[int, list[int]] = {}
        for key in state.discovery:
            acquires_before.setdefault(state.first[key], []).append(key)
            releases_after.setdefault(state.last[key], []).append(key)
        # Same-point acquires keep acquisition order; same-point releases reverse it.
        for group in acquires_before.values():
            group.sort(key=lambda key: acquire_rank[key])
        for group in releases_after.values():
            group.sort(key=lambda key: acquire_rank[key], reverse=True)

        merged: list[CompiledTask[Any]] = []
        for index, (task, requires) in enumerate(definitions):
            for key in acquires_before.get(index, ()):
                resource = state.resources[key]
                op = ResourceOp(
                    title=resource.title,
                    resource=resource,
                    operation=ResourceOperation.ACQUIRE,
                    idempotent=resource.acquire.idempotent,
                )
                merged.append(
                    CompiledTask(
                        task_id="",
                        task=op,
                        required_resources=resource.requires,
                        kind="acquire",
                        resource=resource,
                    )
                )
            merged.append(
                CompiledTask(
                    task_id="", task=task, required_resources=requires, kind="consumer"
                )
            )
            for key in releases_after.get(index, ()):
                resource = state.resources[key]
                op = ResourceOp(
                    title=resource.release_title,
                    resource=resource,
                    operation=ResourceOperation.RELEASE,
                )
                merged.append(
                    CompiledTask(
                        task_id="",
                        task=op,
                        required_resources=resource.requires,
                        kind="release",
                        resource=resource,
                    )
                )
        return merged
