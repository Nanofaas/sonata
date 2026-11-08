"""The exception hierarchy raised by the engine.

Every error a workflow author can catch lives here, so a caller can import them
all from one module. They carry a message describing what went wrong and the
context needed to act on it; none of them define extra attributes.
"""

from __future__ import annotations


class InvalidTaskOutcomeError(Exception):
    """Raised when a task's `run()` returns something other than `TaskOutcome`."""


class UndeclaredResourceError(Exception):
    """Raised when a task tries to read a resource it did not declare."""


class ResourceUnavailableError(Exception):
    """Raised when a declared resource has not been acquired."""


class NoUpstreamValueError(Exception):
    """Raised when a step asks for an upstream value and none precedes it."""


class ResourceDependencyCycleError(Exception):
    """Raised when resource lifecycle dependencies contain a cycle."""


class ResumeConfigurationError(Exception):
    """Raised when `resume=True` is requested without a `JournalConfig`."""


class AmbiguousTaskStateError(Exception):
    """Raised when a journal's recorded state cannot be safely resumed automatically.

    Covers both a non-idempotent task interrupted after `started` with no terminal
    record, and a non-idempotent task with a genuinely recorded `failed`: resuming
    past either without human intervention is exactly the unsafe automatic action
    this error exists to prevent.
    """


class UnsupportedJournalSchemaError(Exception):
    """Raised when a record declares a `schema_version` this engine cannot read."""


class WorkflowTopologyMismatchError(Exception):
    """Raised when a journal belongs to a different compiled workflow topology."""


class MissingAcquireUnitError(Exception):
    """Raised when a compiled task names a resource with no acquire unit.

    A well-formed `CompiledWorkflow` produced by `Workflow.compile()` never
    triggers this: every resource in `required_resources` has a matching
    acquire unit. `CompiledWorkflow`/`CompiledTask` are public exports, though,
    so a hand-built or hand-edited one can violate that invariant.
    """


class CorruptJournalError(Exception):
    """Raised when a journal contains a malformed complete record."""


class SelectionError(Exception):
    """Raised when a `Selection` cannot be resolved against a workflow's tasks.

    Covers a malformed selection (mutually exclusive fields, inverted range) and
    one that does not resolve to exactly one task per endpoint (unknown or
    ambiguous slug).
    """


class StepScopeUnavailableError(Exception):
    """Raised when a `Steps` (or other step-scope consumer) runs without a scope.

    A step scope is attached by the workflow runner to consumer and acquire
    units. This error normally means the task was called directly rather than
    through `Workflow.run()` -- add it to a `Workflow` instead of calling
    `run()` on it yourself.
    """


class CompositeMemberRequiredError(ValueError):
    """Raised when a composite's member is also required outside the composite.

    The composite acquires its members itself; requiring one directly as well
    would acquire it a second time.
    """
