"""Portable managed processes and bounded Linux command ownership."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, overload

from sonata_engine import Resource, TaskInputs
from sonata_tasks.compensation import best_effort
from sonata_tasks.owned_process import OwnedCommandResult as OwnedCommandResult
from sonata_tasks.owned_process import OwnedCommandRunner as OwnedCommandRunner
from sonata_tasks.owned_process import run_owned_command as run_owned_command


class ManagedProcess(Protocol):
    """The lifecycle operations needed from a spawned process."""

    def poll(self) -> int | None:
        """Return the exit code, or ``None`` while the process still runs."""
        ...

    def terminate(self) -> None:
        """Ask the process to stop."""
        ...

    def kill(self) -> None:
        """Stop the process forcefully."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Block until the process exits and return its exit code."""
        ...


@overload
def managed_process_resource[P: ManagedProcess](
    *,
    title: str,
    argv: tuple[str, ...],
    ready: Callable[[], bool],
    cwd: Path | None = None,
    spawn: Callable[..., P],
    readiness_attempts: int = 90,
    readiness_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Resource[P]: ...


@overload
def managed_process_resource(
    *,
    title: str,
    argv: tuple[str, ...],
    ready: Callable[[], bool],
    cwd: Path | None = None,
    spawn: None = None,
    readiness_attempts: int = 90,
    readiness_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Resource[subprocess.Popen[Any]]: ...


def _wait_until_ready(
    *,
    title: str,
    current: ManagedProcess,
    ready: Callable[[], bool],
    readiness_attempts: int,
    readiness_interval: float,
    sleep: Callable[[float], None],
) -> ManagedProcess:
    """Wait until the spawned process answers `ready()`, raising if it exits."""
    for attempt in range(readiness_attempts):
        exit_code = current.poll()
        if exit_code is not None:
            raise RuntimeError(
                f"{title} exited with code {exit_code} before becoming ready"
            )
        if ready():
            return current
        if attempt < readiness_attempts - 1:
            sleep(readiness_interval)
    raise RuntimeError(f"{title} never became ready")


def managed_process_resource(
    *,
    title: str,
    argv: tuple[str, ...],
    ready: Callable[[], bool],
    cwd: Path | None = None,
    spawn: Callable[..., ManagedProcess] | None = None,
    readiness_attempts: int = 90,
    readiness_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Resource[Any]:
    """Model a long-running local process as a Sonata resource.

    The acquire hands back a process that is already answering `ready()`, so
    consumers never have to poll for it themselves. The release always stops it,
    and the compiler runs that release even when a consumer fails.

    The process is modeled directly as a Sonata resource, so acquisition,
    compensation, and release share the engine's normal lifecycle.
    """
    actual_spawn = subprocess.Popen if spawn is None else spawn

    def stop(_inputs: TaskInputs, process: ManagedProcess) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            _ = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            _ = process.wait(timeout=5)

    def acquire(_inputs: TaskInputs) -> ManagedProcess:
        if ready():
            # Something is already answering before we've spawned anything: an
            # orphan from a previous run, a concurrent invocation, or someone's
            # own instance. Starting our process on top of it would make this
            # acquire pass against the WRONG process (the original bug's shape),
            # so refuse instead of racing it.
            raise RuntimeError(
                f"{title}: refusing to start — something is already "
                "answering the readiness check"
            )
        current = actual_spawn(
            argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
        try:
            return _wait_until_ready(
                title=title,
                current=current,
                ready=ready,
                readiness_attempts=readiness_attempts,
                readiness_interval=readiness_interval,
                sleep=sleep,
            )
        except BaseException as error:
            # The engine never releases an acquire that did not complete, and
            # the wait can end in more ways than "gave up": ready() can raise,
            # or a KeyboardInterrupt can land during the up-to-90s wait. Any of
            # those must still stop the process we just spawned, or it leaks.
            #
            # This cannot use `compensated_resource`: only the acquire holds the
            # process it started, so nothing outside it could stop that process.
            # It shares the other half — a failed stop must not replace the
            # reason the process never came up.
            best_effort(
                error,
                lambda: stop(TaskInputs.empty(), current),
                what=f"stop for {title}",
            )
            raise

    # always_release on purpose: `keep` holds on to everything that did not ask to
    # be released, and a spawned child process that outlives the run is a leak, not
    # something a user asked to keep. A process always gets stopped.
    return Resource(title=title, acquire=acquire, release=stop, always_release=True)
