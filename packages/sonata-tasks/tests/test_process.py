from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import override

import pytest
from resource_stubs import acquired

from sonata_engine import Resource, TaskInputs
from sonata_tasks.process import managed_process_resource


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.waited = False
        self._alive = True

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.waited = True
        return 0


def _spawner(process: FakeProcess, seen: list[dict[str, object]]):
    def spawn(argv, **kwargs):
        seen.append({"argv": argv, **kwargs})
        return process

    return spawn


def _ready_once_spawned():
    """Build a readiness probe that reports ready only from its second call.

    The first call is the pre-spawn "is anything already up?" check and returns
    False; from the second call onward the process we spawned is up.
    """
    calls = {"n": 0}

    def ready() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    return ready


def test_it_builds_a_sonata_resource() -> None:
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: True,
        spawn=_spawner(FakeProcess(), []),
    )

    assert isinstance(resource, Resource)
    assert resource.title == "Acquire thing"
    assert resource.release_title == "Release thing"


def test_the_resource_keeps_the_spawner_process_type() -> None:
    process = FakeProcess()
    resource: Resource[FakeProcess] = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=_ready_once_spawned(),
        spawn=_spawner(process, []),
    )

    assert resource.acquire.run(TaskInputs.empty()).value is process


def test_acquire_spawns_with_argv_and_cwd() -> None:
    seen: list[dict[str, object]] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("java", "-jar", "app.jar"),
        cwd=Path("/repo"),
        ready=_ready_once_spawned(),
        spawn=_spawner(FakeProcess(), seen),
    )

    resource.acquire.run(TaskInputs.empty())

    assert seen[0]["argv"] == ("java", "-jar", "app.jar")
    assert seen[0]["cwd"] == Path("/repo")


def test_acquire_refuses_to_start_when_something_is_already_answering() -> None:
    seen: list[dict[str, object]] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: True,
        spawn=_spawner(FakeProcess(), seen),
    )

    with pytest.raises(RuntimeError, match="already answering"):
        resource.acquire.run(TaskInputs.empty())

    # Something is already up, so we must never spawn a second, competing process.
    assert seen == []


def test_acquire_waits_until_ready() -> None:
    # The first value covers the pre-spawn "is anything already up?" check.
    attempts = iter([False, False, False, True])
    slept: list[float] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: next(attempts),
        spawn=_spawner(FakeProcess(), []),
        readiness_interval=0.5,
        sleep=slept.append,
    )

    resource.acquire.run(TaskInputs.empty())

    assert slept == [0.5, 0.5]


def test_acquire_gives_up_and_stops_the_process() -> None:
    process = FakeProcess()
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: False,
        spawn=_spawner(process, []),
        readiness_attempts=3,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="never became ready"):
        resource.acquire.run(TaskInputs.empty())

    # A failed acquire is never released by the engine, so it must clean up itself.
    assert process.terminated is True


def test_acquire_fails_immediately_when_the_process_exits() -> None:
    class Exited(FakeProcess):
        @override
        def poll(self) -> int | None:
            return 23

    slept: list[float] = []
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: False,
        spawn=_spawner(Exited(), []),
        sleep=slept.append,
    )

    with pytest.raises(RuntimeError, match="exited with code 23"):
        resource.acquire.run(TaskInputs.empty())

    assert slept == []


def test_acquire_stops_the_process_when_the_readiness_wait_raises() -> None:
    process = FakeProcess()
    calls = {"n": 0}

    def ready() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            return False  # pre-spawn check: nothing already running
        raise KeyboardInterrupt

    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=ready,
        spawn=_spawner(process, []),
    )

    with pytest.raises(KeyboardInterrupt):
        resource.acquire.run(TaskInputs.empty())

    # An interrupt (or any other exception) during the readiness wait must not
    # leak the process we just spawned.
    assert process.terminated is True


def test_release_terminates_a_live_process() -> None:
    process = FakeProcess()
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=_ready_once_spawned(),
        spawn=_spawner(process, []),
    )

    process = acquired(resource)
    resource.release.run(replace(TaskInputs.empty(), _upstream=process))

    assert (process.terminated, process.killed) == (True, False)


def test_release_kills_a_process_that_ignores_terminate() -> None:
    import subprocess

    class Stubborn(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self._waits = 0

        @override
        def terminate(self) -> None:
            self.terminated = True  # stays alive on purpose

        @override
        def wait(self, timeout: float | None = None) -> int:
            self._waits += 1
            if self._waits == 1:
                raise subprocess.TimeoutExpired(cmd="run", timeout=timeout or 0)
            return 0

    process = Stubborn()
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=_ready_once_spawned(),
        spawn=_spawner(process, []),
    )

    process = acquired(resource)
    resource.release.run(replace(TaskInputs.empty(), _upstream=process))

    assert process.killed is True


def test_release_is_a_no_op_for_an_already_exited_process() -> None:
    process = FakeProcess()
    process.terminate()
    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: True,
        spawn=_spawner(FakeProcess(), []),
    )

    resource.release.run(replace(TaskInputs.empty(), _upstream=process))
    assert process.killed is False


def test_a_failed_stop_is_noted_without_masking_why_the_process_never_came_up() -> None:
    """The reason the process never started is the interesting one.

    Before this shared the compensation's note handling, a `terminate` that
    raised propagated in place of the original error, so the run reported a
    cleanup problem and hid the fact that the process had never become ready.
    """

    class Unstoppable(FakeProcess):
        @override
        def terminate(self) -> None:
            raise OSError("terminate refused")

    resource = managed_process_resource(
        title="Acquire thing",
        argv=("run",),
        ready=lambda: False,
        spawn=_spawner(Unstoppable(), []),
        readiness_attempts=2,
        sleep=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError, match="never became ready") as captured:
        resource.acquire.run(TaskInputs.empty())

    notes = getattr(captured.value, "__notes__", [])
    assert len(notes) == 1
    assert "stop for Acquire thing" in notes[0]
    assert "terminate refused" in notes[0]
