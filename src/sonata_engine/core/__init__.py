"""Core workflow primitives: tasks, resources, selection, and compiled results.

This package is the stable surface for building and running a workflow: the
`Task` hierarchy and its `Steps` composite, the `Resource` lifecycle pairing,
`Workflow` itself, and the immutable compiled/result types the runner returns.
Everything here is re-exported for import from `sonata_engine.core`; the
sibling modules are implementation detail behind that list.
"""

from sonata_engine.core.compiled import (
    CompiledTask,
    CompiledWorkflow,
    TaskExecution,
    WorkflowResult,
)
from sonata_engine.core.compose import Members, compose
from sonata_engine.core.inputs import TaskInputs
from sonata_engine.core.outcome import Evidence, TaskOutcome
from sonata_engine.core.resource_task import Resource
from sonata_engine.core.selection import Selection
from sonata_engine.core.steps import Steps
from sonata_engine.core.task import ReusableTask, Task
from sonata_engine.core.workflow import Workflow

__all__ = [
    "CompiledTask",
    "CompiledWorkflow",
    "Evidence",
    "Members",
    "Resource",
    "ReusableTask",
    "Selection",
    "Steps",
    "Task",
    "TaskExecution",
    "TaskInputs",
    "TaskOutcome",
    "Workflow",
    "WorkflowResult",
    "compose",
]
