"""Generic Linux commands: bound output and own detached descendant lifetimes."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from sonata_tasks import process

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux process ownership"
)


def _runner(tmp_path: Path, **overrides: Any) -> process.OwnedCommandRunner:
    settings = {
        "argv": [sys.executable, "-c", "print('standalone command')"],
        "cwd": tmp_path,
        "env": os.environ,
        "log_path": tmp_path / "command.log",
        "timeout_s": 2,
        "cancelled": Event(),
        "output_limit_bytes": 1024,
    }
    settings.update(overrides)
    return process.OwnedCommandRunner(**settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"argv": "echo unsafe"},
        {"argv": []},
        {"argv": ["bad\0argument"]},
        {"timeout_s": 0},
        {"timeout_s": float("nan")},
        {"timeout_s": True},
        {"output_limit_bytes": 0},
        {"output_limit_bytes": True},
        {"summary_path": Path("summary")},
        {"summary_marker": "TOKEN"},
    ],
)
def test_invalid_inputs_fail_before_launch(tmp_path: Path, overrides: dict) -> None:
    with pytest.raises(ValueError, match=r"argv|timeout|limit|summary"):
        _runner(tmp_path, **overrides)
    assert not (tmp_path / "command.log").exists()


def test_unsupported_platform_fails_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin")
        with pytest.raises(ValueError, match="Linux"):
            _runner(tmp_path)
    assert not (tmp_path / "command.log").exists()


@pytest.mark.parametrize("cancel_method", ["event", "stop"])
def test_cancel_before_run_never_launches_a_command(
    tmp_path: Path, cancel_method: str
) -> None:
    event = Event()
    runner = _runner(tmp_path, argv=["/no/such/command"], cancelled=event)
    if cancel_method == "event":
        event.set()
    else:
        runner.stop(0.1)
    result = runner.run()
    assert result.cancelled and result.reaped and result.ended_s is not None
    assert result.returncode is None and not runner.launched
    assert not (tmp_path / "command.log").exists()


def test_runner_cannot_execute_twice(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    assert runner.run().returncode == 0
    with pytest.raises(RuntimeError, match="reused"):
        runner.run()
    assert (tmp_path / "command.log").read_text() == "standalone command\n"


def test_framed_summary_keeps_log_separate_across_reads(tmp_path: Path) -> None:
    script = (
        "import os,time\n"
        "for part in [b'before\\nTO', b'KEN:START\\n{\"ok\":true}', "
        "b'\\nTOKEN:END\\nafter']:\n"
        " os.write(1, part);time.sleep(.03)"
    )
    result = _runner(
        tmp_path,
        argv=[sys.executable, "-c", script],
        summary_path=tmp_path / "summary.json",
        summary_marker="TOKEN",
    ).run()
    assert result.returncode == 0 and result.reaped and result.summary_complete
    assert not result.errors
    assert (tmp_path / "command.log").read_bytes() == b"beforeafter"
    assert (tmp_path / "summary.json").read_bytes() == b'{"ok":true}'
    assert result.log_bytes == 11 and result.summary_bytes == 11


def test_log_and_summary_share_the_output_budget(tmp_path: Path) -> None:
    result = _runner(
        tmp_path,
        argv=[sys.executable, "-c", "print('before\\nTOKEN:START\\n' + 'x'*1000)"],
        summary_path=tmp_path / "summary.json",
        summary_marker="TOKEN",
        output_limit_bytes=64,
    ).run()
    assert result.quota_exceeded and result.reaped and not result.summary_complete
    assert result.log_bytes + result.summary_bytes == 64
    assert (tmp_path / "command.log").stat().st_size + (
        tmp_path / "summary.json"
    ).stat().st_size == 64


def test_existing_output_is_preserved_and_command_is_not_started(
    tmp_path: Path,
) -> None:
    log = tmp_path / "command.log"
    log.write_text("prior evidence")
    marker = tmp_path / "spawned"
    runner = _runner(
        tmp_path,
        argv=[
            sys.executable,
            "-c",
            f"from pathlib import Path;Path({str(marker)!r}).touch()",
        ],
    )
    result = runner.run()
    assert result.returncode is None and result.reaped and result.errors
    assert log.read_text() == "prior evidence"
    assert not marker.exists()


def test_missing_executable_returns_a_cleanup_receipt(tmp_path: Path) -> None:
    result = _runner(tmp_path, argv=["/no/such/command"]).run()
    assert result.returncode is None and result.reaped and result.errors
    assert result.ended_s is not None


def test_invalid_supervisor_receipt_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sonata_tasks.owned_process as owned

    monkeypatch.setattr(owned, "_SUPERVISOR", "print('not json')")
    with pytest.raises(RuntimeError, match="cleanup receipt"):
        _runner(tmp_path).run()


def test_ultra_short_cleanup_budget_rejected_before_any_spawn(tmp_path):
    marker = tmp_path / "spawned"
    with pytest.raises(ValueError, match=r"stop_timeout_s|cleanup|minimum"):
        process.OwnedCommandRunner(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ],
            cwd=tmp_path,
            env=os.environ,
            log_path=tmp_path / "log",
            timeout_s=10,
            cancelled=Event(),
            output_limit_bytes=1024,
            stop_timeout_s=0.001,
        ).run()
    assert not marker.exists()


def test_explicit_stop_rejects_unsupported_budget_without_starting(tmp_path):
    runner = process.OwnedCommandRunner(
        [sys.executable, "-c", "pass"],
        cwd=tmp_path,
        env=os.environ,
        log_path=tmp_path / "log",
        timeout_s=1,
        cancelled=Event(),
        output_limit_bytes=1024,
    )
    with pytest.raises(ValueError, match=r"timeout|cleanup|minimum"):
        runner.stop(0.001)


@pytest.mark.parametrize("failure", ["cancel", "caller-error"])
def test_multilevel_detached_descendants_are_gone_before_return_or_raise(
    tmp_path, failure
):
    # Each process forms a new session. Killing just the root/group is insufficient.
    script = tmp_path / "tree.py"
    script.write_text("""import os,signal,subprocess,sys,time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
root=Path(sys.argv[1]); depth=int(sys.argv[2])
(root/('pid-'+str(depth))).write_text(str(os.getpid()))
if depth:
    subprocess.Popen([sys.executable,__file__,str(root),str(depth-1)],start_new_session=True)
else:
    (root/'ready').touch()
while True: time.sleep(.01)
""")
    ready = tmp_path / "ready"
    event = Event()

    class CallerError:
        def is_set(self):
            return False

        def wait(self, delay):
            if ready.exists():
                raise RuntimeError("synthetic caller failure")
            time.sleep(delay)

    runner = process.OwnedCommandRunner(
        [sys.executable, str(script), str(tmp_path), "5"],
        cwd=tmp_path,
        env=os.environ,
        log_path=tmp_path / "log",
        timeout_s=10,
        cancelled=event if failure == "cancel" else CallerError(),  # pyright: ignore[reportArgumentType]
        output_limit_bytes=1024,
        stop_timeout_s=0.1,
    )
    results, errors = [], []

    def run():
        try:
            results.append(runner.run())
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True
    )
    try:
        thread.start()
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert ready.exists()
        event.set()
        thread.join(2)
        assert not thread.is_alive()
        if failure == "caller-error":
            assert len(errors) == 1 and str(errors[0]) == "synthetic caller failure"
        else:
            assert not errors
            assert results[0].reaped and results[0].cancelled
        for path in tmp_path.glob("pid-*"):
            with pytest.raises(ProcessLookupError, match="Errno 3"):
                os.kill(int(path.read_text()), 0)
        assert unrelated.poll() is None
    finally:
        event.set()
        try:
            runner.stop(1)
        finally:
            thread.join(2)
            for path in tmp_path.glob("pid-*"):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(path.read_text()), 9)
            unrelated.terminate()
            unrelated.wait(timeout=2)


def test_shared_owned_command_preserves_cwd_env_and_exit(tmp_path):
    log = tmp_path / "command.log"
    result = process.run_owned_command(
        [
            sys.executable,
            "-c",
            'import os,sys; print(os.getcwd()); print(os.environ["SOAK_TEST_VALUE"]); '
            "sys.exit(7)",
        ],
        cwd=tmp_path,
        env={**os.environ, "SOAK_TEST_VALUE": "bound-value"},
        log_path=log,
        timeout_s=2,
        cancelled=threading.Event(),
        output_limit_bytes=1024,
    )
    assert result.returncode == 7 and result.reaped and not result.forced_stop
    assert str(tmp_path) in log.read_text() and "bound-value" in log.read_text()


@pytest.mark.parametrize("reason", ["timeout", "quota", "cancelled"])
def test_shared_owned_command_bounds_execution_and_output(tmp_path, reason):
    event = threading.Event()
    timer = threading.Timer(0.1, event.set) if reason == "cancelled" else None
    if timer:
        timer.start()
    try:
        source = (
            'import os,time\nwhile True:\n os.write(1,b"x"*2048);time.sleep(.01)'
            if reason == "quota"
            else "import time;time.sleep(60)"
        )
        result = process.run_owned_command(
            [sys.executable, "-c", source],
            cwd=tmp_path,
            env=os.environ,
            log_path=tmp_path / "command.log",
            timeout_s=0.15 if reason == "timeout" else 3,
            cancelled=event,
            output_limit_bytes=1024,
        )
        assert result.reaped
        assert (tmp_path / "command.log").stat().st_size <= 1024
        assert getattr(
            result,
            {
                "timeout": "timed_out",
                "quota": "quota_exceeded",
                "cancelled": "cancelled",
            }[reason],
        )
    finally:
        if timer:
            timer.cancel()
            timer.join()


def test_shared_runner_caller_exception_stops_and_reaps(tmp_path):
    pid_file = tmp_path / "caller-error.pid"

    class FailingWait:
        def is_set(self):
            return False

        def wait(self, timeout):
            if pid_file.exists():
                raise RuntimeError("caller wait failed")
            time.sleep(timeout)

    runner = process.OwnedCommandRunner(
        [
            sys.executable,
            "-c",
            "import os,time;from pathlib import Path;Path("
            + repr(str(pid_file))
            + ").write_text(str(os.getpid()));time.sleep(60)",
        ],
        cwd=tmp_path,
        env=os.environ,
        log_path=tmp_path / "error.log",
        timeout_s=30,
        cancelled=FailingWait(),
        output_limit_bytes=1024,
        stop_timeout_s=0.5,
    )
    try:
        with pytest.raises(RuntimeError, match="caller wait failed"):
            runner.run()
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError, match="Errno 3"):
            os.kill(pid, 0)
    finally:
        runner.stop(0.5)


def test_an_adopted_child_may_stop_on_its_own_after_the_leader(tmp_path):
    """A single-use helper that stops with the leader is not a stray.

    This supervisor is a subreaper, so a leader's orphaned child is adopted and
    was killed in the very iteration the leader exited. Gradle's `--no-daemon`
    build forks exactly such a helper and announces that it stops at the end of
    the build, so killing it reported a successful build as a forced stop.
    """
    leader = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.15)'])\n"
        "time.sleep(0.05)\n"
    )
    result = process.run_owned_command(
        [sys.executable, "-c", leader],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "leader.log",
        timeout_s=10,
        cancelled=threading.Event(),
        output_limit_bytes=1024,
    )

    assert result.returncode == 0
    assert result.reaped and not result.forced_stop


def test_an_adopted_child_that_outlives_the_grace_is_still_forced(tmp_path):
    """The grace is bounded: a child that will not leave is still killed."""
    leader = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(0.05)\n"
    )
    result = process.run_owned_command(
        [sys.executable, "-c", leader],
        cwd=tmp_path,
        env=dict(os.environ),
        log_path=tmp_path / "leader.log",
        timeout_s=10,
        cancelled=threading.Event(),
        output_limit_bytes=1024,
    )

    assert result.reaped and result.forced_stop
