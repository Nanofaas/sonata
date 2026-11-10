"""A resource made of an ordered sequence of task resources.

`compose()` returns an ordinary `Resource` whose acquire task acquires each
member in order and whose release task runs the collect tasks, then releases
the members in reverse. Every member acquire and release is a step of the
composite's unit, so it is journalled and reported individually.
"""

from __future__ import annotations

from typing import Any, cast, override

from sonata_engine.core.inputs import _NO_UPSTREAM, TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.resource_task import Resource
from sonata_engine.core.slug import slugify
from sonata_engine.core.step_scope import _StepScopeProtocol
from sonata_engine.core.task import Task
from sonata_engine.errors import StepScopeUnavailableError


class Members(dict[str, object]):
    """A composite's value: each member's title mapped to its acquired value."""

    def of[T](self, resource: Resource[T]) -> T:
        """Return the value `resource` was acquired with, typed as its value."""
        try:
            return cast(T, self[resource.title])
        except KeyError:
            raise KeyError(f"{resource.title!r} is not a member") from None


def _unique_slugs(tasks: tuple[Task[Any], ...] | tuple[Resource[Any], ...]) -> None:
    seen: set[str] = set()
    for item in tasks:
        slug = slugify(item.title)
        if not slug:
            raise ValueError(f"title {item.title!r} produces an empty slug")
        if slug in seen:
            raise ValueError(f"duplicate slug {slug!r}")
        seen.add(slug)


def _scope(task: Task[Any], inputs: TaskInputs) -> _StepScopeProtocol:
    if inputs._step_scope is None:
        raise StepScopeUnavailableError(
            f"{type(task).__name__} {task.title!r} must be run by the workflow "
            "runner: add it to a Workflow rather than calling run() directly"
        )
    return inputs._step_scope


def _with_members(
    inputs: TaskInputs, members: tuple[Resource[Any], ...], values: Members
) -> TaskInputs:
    """Extend `inputs` with read access to `members`, valued from `values`."""
    return TaskInputs(
        {**inputs._values, **{id(m): values[m.title] for m in members}},
        inputs._accessible | {id(m) for m in members},
    )


def _type_name(task: Task[Any]) -> str:
    return f"{type(task).__module__}.{type(task).__qualname__}"


def _payload(
    members: tuple[Resource[Any], ...], collect: tuple[Task[Any], ...]
) -> object:
    return (
        [
            (
                member.title,
                _type_name(member.acquire),
                member.acquire._fingerprint_payload(),
                _type_name(member.release),
                member.release._fingerprint_payload(),
            )
            for member in members
        ],
        [(_type_name(task), task._fingerprint_payload()) for task in collect],
    )


def _release_members(
    scope: _StepScopeProtocol,
    inputs: TaskInputs,
    members: tuple[Resource[Any], ...],
    values: Members,
) -> list[BaseException]:
    """Release `members` in reverse, attempting every one; return the failures.

    Never resumed (a release always runs, like the engine's own `_release`) and
    never stopped by a failing journal: those failures are returned too.
    """
    errors: list[BaseException] = []
    cleanup = scope.for_cleanup(_with_members(inputs, members, values), errors)
    for member in reversed(members):
        try:
            cleanup.run_step(
                member.release,
                f"release-{slugify(member.title)}",
                values[member.title],
            )
        except BaseException as error:  # NOSONAR S5754 - release every member
            errors.append(error)
    return errors


class _AcquireMembers(Task[Members]):
    """Acquire each member in order, unwinding the acquired ones on failure.

    `idempotent` is `True` for the same reason as `Steps`: the coordinator is
    safe to re-enter, since a failure releases what it acquired; each member's
    acquire step carries its own flag.
    """

    idempotent = True

    def __init__(
        self,
        title: str,
        members: tuple[Resource[Any], ...],
        collect: tuple[Task[Any], ...],
    ) -> None:
        self.title = title
        self.members = members
        self._collect = collect

    @override
    def _fingerprint_payload(self) -> object:
        return _payload(self.members, self._collect)

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[Members]:
        scope = _scope(self, inputs)
        values = Members()
        acquired: list[Resource[Any]] = []
        upstream: Any = _NO_UPSTREAM
        try:
            for member in self.members:

                def acquired_value(
                    outcome: TaskOutcome[Any], current: Resource[Any] = member
                ) -> None:
                    # Own the value before reporting or journalling can fail.
                    values[current.title] = outcome.value
                    acquired.append(current)

                execution = scope.rebased(
                    _with_members(inputs, tuple(acquired), values)
                ).run_step(
                    member.acquire,
                    f"acquire-{slugify(member.title)}",
                    upstream,
                    on_outcome=acquired_value,
                )
                if execution.outcome is None:
                    acquired_value(TaskOutcome())
                upstream = values[member.title]
        except BaseException as exc:
            for error in _release_members(scope, inputs, tuple(acquired), values):
                exc.add_note(f"Cleanup error: {error!r}")
            raise
        return TaskOutcome(value=values)


class _ReleaseMembers(Task[None]):
    """Run the collect tasks, then release every member in reverse."""

    def __init__(
        self,
        title: str,
        members: tuple[Resource[Any], ...],
        collect: tuple[Task[Any], ...],
    ) -> None:
        self.title = title
        self._members = members
        self._collect = collect

    @override
    def _fingerprint_payload(self) -> object:
        return _payload(self._members, self._collect)

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        scope = _scope(self, inputs)
        values = cast(Members, inputs.upstream())
        errors: list[BaseException] = []
        for task in self._collect:
            try:
                scope.run_step(task, f"collect-{slugify(task.title)}", values)
            except BaseException as error:  # NOSONAR S5754 - never block releases
                errors.append(error)
        errors += _release_members(scope, inputs, self._members, values)
        _raise_all(errors)
        return TaskOutcome()


def _raise_all(errors: list[BaseException]) -> None:
    """Raise one error as itself, several as one; a critical error wins."""
    if not errors:
        return
    critical = next((e for e in errors if not isinstance(e, Exception)), None)
    if critical is not None:
        for error in errors:
            if error is not critical:
                critical.add_note(f"Cleanup error: {error!r}")
        raise critical
    if len(errors) == 1:
        raise errors[0]
    raise RuntimeError(
        "releasing composite members failed:\n"
        + "\n".join(repr(error) for error in errors)
    )


def compose(
    title: str,
    members: tuple[Resource[Any], ...],
    *,
    collect: tuple[Task[Any], ...] = (),
) -> Resource[Members]:
    """Build one resource that acquires `members` in order and releases them.

    The value is a `Members` mapping; a consumer reads one member with
    `inputs.resource(composite).of(member)`. A member may require an earlier
    member; whatever the members require from outside becomes the composite's
    `requires`. `collect` tasks run at release, before any member is released,
    and receive the `Members` as their upstream. The composite is
    `always_release` when any member is, so `keep` never strands one.
    """
    if not members:
        raise ValueError("compose requires at least one member")
    _unique_slugs(members)
    _unique_slugs(collect)

    member_ids = {id(member) for member in members}
    earlier: set[int] = set()
    requires: dict[int, Resource[Any]] = {}
    for member in members:
        for dependency in member.requires:
            if id(dependency) not in member_ids:
                requires.setdefault(id(dependency), dependency)
            elif id(dependency) not in earlier:
                raise ValueError(
                    f"{member.title!r} requires {dependency.title!r}, which is "
                    "not an earlier member: order the members so it comes first"
                )
        earlier.add(id(member))

    def revive(raw: dict[str, Any]) -> Members:
        return Members(
            {
                member.title: (
                    member.revive(raw[member.title])
                    if member.revive is not None
                    else raw[member.title]
                )
                for member in members
            }
        )

    return Resource(
        title=title,
        acquire=_AcquireMembers(title, members, collect),
        release=_ReleaseMembers(
            f"Release {title.removeprefix('Acquire ')}", members, collect
        ),
        requires=tuple(requires.values()),
        always_release=any(member.always_release for member in members),
        revive=revive,
    )
