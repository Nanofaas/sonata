"""Helpers for building the shell results that VM adapters return."""

from __future__ import annotations

from subprocess_toolkit.backend import ShellExecutionResult


def successful_result(command: list[str], *, stdout: str = "") -> ShellExecutionResult:
    """Build a zero exit-code result for ``command`` carrying ``stdout``.

    Used by adapters that answer a probe from local knowledge rather than by
    actually invoking the command.
    """
    return ShellExecutionResult(command=command, return_code=0, stdout=stdout)
