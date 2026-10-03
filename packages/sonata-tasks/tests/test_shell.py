from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import override
from unittest.mock import MagicMock, patch

import pytest

from sonata_engine.workflow.context import bind_workflow_context, bind_workflow_sink
from sonata_engine.workflow.events import WorkflowContext, WorkflowEvent
from sonata_tasks.shell import (
    RecordingShell,
    ScriptedShell,
    ShellExecutionResult,
    SubprocessShell,
)


def test_shell_execution_result_captures_stdout_stderr() -> None:
    r = ShellExecutionResult(command=["cmd"], return_code=0, stdout="out", stderr="err")
    assert r.stdout == "out"
    assert r.stderr == "err"


def test_subprocess_shell_dry_run_returns_zero_without_executing() -> None:
    shell = SubprocessShell()
    result = shell.run(["rm", "-rf", "/"], dry_run=True)
    assert result.return_code == 0
    assert result.dry_run is True


def test_subprocess_shell_returns_ok_on_zero_exit() -> None:
    shell = SubprocessShell()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="hello", stderr="")
        result = shell.run(["echo", "hello"])
    assert result.return_code == 0
    assert result.stdout == "hello"


def test_recording_shell_records_commands() -> None:
    shell = RecordingShell()
    shell.run(["cmd1", "arg1"])
    shell.run(["cmd2"])
    assert shell.commands == [["cmd1", "arg1"], ["cmd2"]]


def test_scripted_shell_returns_configured_return_code() -> None:
    shell = ScriptedShell(return_code_map={("fail",): 1})
    result = shell.run(["fail"])
    assert result.return_code == 1


class _FakeSink:
    def __init__(self) -> None:
        self.events: list[WorkflowEvent] = []
        self.status_labels: list[str] = []

    def emit(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    @contextmanager
    def status(self, label: str):
        self.status_labels.append(label)
        yield


@pytest.mark.parametrize("with_listener", [False, True])
def test_subprocess_shell_streams_real_output_before_the_process_exits(
    tmp_path: Path, with_listener: bool
) -> None:
    """A child waits for the sink to observe stdout before it can exit."""
    acknowledgment = tmp_path / "observed"

    class Sink(_FakeSink):
        @override
        def emit(self, event: WorkflowEvent) -> None:
            super().emit(event)
            if event.line == "hello-line":
                acknowledgment.write_text("observed")

    sink = Sink()
    observed: list[tuple[str, str]] = []

    def listener(stream: str, line: str) -> None:
        observed.append((stream, line))

    shell = SubprocessShell(output_listener=listener if with_listener else None)
    context = WorkflowContext(flow_id="shell-stream", task_id="001.command")
    script = (
        "import pathlib, sys, time; "
        "ack = pathlib.Path(sys.argv[1]); "
        "print('hello-line', flush=True); "
        "print('error-line', file=sys.stderr, flush=True); "
        "deadline = time.monotonic() + 3; "
        "\nwhile not ack.exists() and time.monotonic() < deadline: time.sleep(.01)"
        "\nsys.exit(7 if ack.exists() else 1)"
    )
    with bind_workflow_sink(sink), bind_workflow_context(context):
        result = shell.run([sys.executable, "-u", "-c", script, str(acknowledgment)])

    assert result.return_code == 7
    assert result.stdout == "hello-line\n"
    assert result.stderr == "error-line\n"
    expected = [("stderr", "error-line"), ("stdout", "hello-line")]
    assert sorted((event.stream, event.line) for event in sink.events) == expected
    assert all(event.flow_id == context.flow_id for event in sink.events)
    assert all(event.task_id == context.task_id for event in sink.events)
    assert sorted(observed) == (expected if with_listener else [])
    assert shell.output_listener is (listener if with_listener else None)
