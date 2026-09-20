"""subprocess-toolkit re-exports plus a workflow-aware SubprocessShell."""

from __future__ import annotations

from typing import override

from subprocess_toolkit.backend import (
    OutputListener,
    RecordingShell,
    ScriptedShell,
    ShellBackend,
    ShellExecutionResult,
)
from subprocess_toolkit.backend import (
    SubprocessShell as _ToolkitSubprocessShell,
)

from sonata_engine.workflow.context import has_workflow_sink
from sonata_engine.workflow.reporting import workflow_log

__all__ = [
    "OutputListener",
    "RecordingShell",
    "ScriptedShell",
    "ShellBackend",
    "ShellExecutionResult",
    "SubprocessShell",
]


class SubprocessShell(_ToolkitSubprocessShell):
    """SubprocessShell with TUI workflow-log integration.

    Routes each output line to workflow_log when a workflow sink is active,
    in addition to any explicitly set output_listener.
    """

    @override
    def _emit_output(self, stream: str, line: str) -> None:
        super()._emit_output(stream, line)
        if has_workflow_sink():
            workflow_log(line, stream=stream)
