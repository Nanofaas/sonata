"""The runner-side handle a composite needs to execute its steps."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from sonata_engine.core.compiled import TaskExecution
    from sonata_engine.core.inputs import TaskInputs
    from sonata_engine.core.outcome import TaskOutcome
    from sonata_engine.core.task import Task


class _StepScopeProtocol(Protocol):
    """The runner-side handle a composite uses to execute its steps.

    It holds the compiled unit id, the journal and the resume flag — none of
    which a task is told — so a composite can run a step without knowing where
    it sits. A composite passes a step and a slug; the scope names it, decides,
    records, reports and runs it.

    Private (leading underscore) and exported from nowhere: `TaskInputs._step_scope`,
    the field this types, is itself underscore-private engine plumbing that a
    hand-written task is not meant to reach for. There is no public field to type
    against, so there is nothing to gain from making this Protocol public.
    """

    def run_step(
        self,
        step: Task[Any],
        slug: str,
        upstream: Any,  # noqa: ANN401
        *,
        on_outcome: Callable[[TaskOutcome[Any]], None] | None = None,
    ) -> TaskExecution:
        """Run one step beneath this scope and return how it went."""
        ...

    def rebased(self, inputs: TaskInputs) -> _StepScopeProtocol:
        """Return this scope, same prefix, journal and resume, over new inputs."""
        ...

    def for_cleanup(
        self, inputs: TaskInputs, errors: list[BaseException]
    ) -> _StepScopeProtocol:
        """Return this scope over `inputs` for releasing.

        It never resumes, and collects journal write failures into `errors`
        instead of raising them, so every release still runs.
        """
        ...
