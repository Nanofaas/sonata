"""subprocess-toolkit re-exports plus a workflow-aware SubprocessShell."""

from __future__ import annotations

from pathlib import Path
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
    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Stream output while a workflow sink is active."""
        if has_workflow_sink():
            shell = _ToolkitSubprocessShell(output_listener=self._emit_output)
            return shell.run(command, cwd=cwd, env=env, dry_run=dry_run)
        return super().run(command, cwd=cwd, env=env, dry_run=dry_run)

    @override
    def _emit_output(self, stream: str, line: str) -> None:
        super()._emit_output(stream, line)
        if has_workflow_sink():
            workflow_log(line, stream=stream)
