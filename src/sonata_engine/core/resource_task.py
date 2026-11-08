"""Resources and the acquire/release units that manage their lifetime.

A `Resource` bundles the two tasks that obtain and later give back an
expensive thing (a VM, a token, a mounted image). The compiler turns each one
into `ResourceOp` units spliced around its consumers, so acquire and release run
through the normal runner: journalled, reported, resumable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, override

from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import TaskOutcome
from sonata_engine.core.task import Task


@dataclass(frozen=True, slots=True, eq=False)
class Resource[T]:
    """A pair of acquire/release tasks a consumer task depends on.

    `acquire` produces the value consumers read with `inputs.resource(...)`;
    whether an interrupted acquire may be retried on resume is its own
    `idempotent`. `release` receives that value as `inputs.upstream()`. Either
    may be a `Steps`, whose steps are then journalled individually.

    A `Resource` is not itself added to a workflow; consumer tasks reference it
    via `Workflow.add(task, requires=(resource,))`. `compile()` splices an
    acquire unit before the first consumer and a release unit after the last.

    Identity semantics (`eq=False`): the compiler keys resources by `id()` and
    deliberately treats two structurally identical `Resource` instances as
    distinct (each gets its own acquire/release pair and runtime value). Value
    equality would contradict that -- and would also make `hash()` recurse
    forever on a cyclic `requires` graph.
    """

    title: str
    acquire: Task[T] = field(repr=False)
    release: Task[None] = field(repr=False)
    requires: tuple[Resource[Any], ...] = ()
    # `keep` retains a resource so a later run need not rebuild it. A resource
    # that holds a secret -- a staged token, a signing key, an open credential
    # lease -- declares always_release instead, so leaving it behind is not
    # something a caller can cause by forgetting to classify it.
    always_release: bool = False
    # Rebuilds an acquired value from its journal record, for a teardown running
    # in a later process. The journal can only hold JSON, so a release written
    # against a dataclass needs its own type back rather than a dict.
    revive: Callable[[Any], T] | None = field(default=None, repr=False)

    @property
    def release_title(self) -> str:
        """Title for the release unit, derived from the resource's own title."""
        return f"Release {self.title.removeprefix('Acquire ')}"


class ResourceOperation(StrEnum):
    """Which half of a resource's lifecycle a `ResourceOp` performs."""

    ACQUIRE = "acquire"
    RELEASE = "release"


@dataclass(frozen=True, slots=True, eq=True)
class ResourceOp(Task[object]):
    """Engine-owned lifecycle unit identified by a resource and operation.

    It delegates to the resource's own acquire or release task. `idempotent` is
    the acquire task's for an acquire unit (the compiler sets it) and `False`
    for a release, which never consults the journal.
    """

    title: str
    resource: Resource[Any] = field(repr=False)
    operation: ResourceOperation = ResourceOperation.ACQUIRE
    idempotent: bool = False

    @property
    def _delegate(self) -> Task[Any]:
        if self.operation is ResourceOperation.ACQUIRE:
            return self.resource.acquire
        return self.resource.release

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[object]:
        return self._delegate.run(inputs)

    @override
    def _fingerprint_payload(self) -> object:
        task = self._delegate
        return (
            f"{type(task).__module__}.{type(task).__qualname__}",
            task._fingerprint_payload(),
        )
