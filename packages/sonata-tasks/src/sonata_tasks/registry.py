"""Run a local Docker registry container as a resource."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal
from urllib.request import urlopen

from sonata_engine import Resource, TaskInputs
from sonata_tasks.compensation import best_effort
from sonata_tasks.docker import DockerTask
from sonata_tasks.execution.models import CommandOptions, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor

RegistryState = Literal["created", "started", "existing"]
RegistryCommand = Callable[..., TaskResult]


def _answers(port: int) -> bool:
    """Return whether a registry answers its v2 API on ``port``.

    The port is the only variable part of the URL below: the scheme, host and
    path are literals, so B310's file:/custom-scheme concern cannot apply.
    """
    try:
        with urlopen(  # nosec B310
            f"http://127.0.0.1:{port}/v2/", timeout=1
        ) as response:
            return response.status == 200
    except OSError:
        return False


def _registry_state(inspected: TaskResult) -> RegistryState:
    if inspected.return_code != 0:
        return "created"
    if inspected.stdout.strip() == "true":
        return "existing"
    return "started"


def _wait_until_ready(
    ready: Callable[[], bool],
    *,
    attempts: int,
    interval: float,
    sleep: Callable[[float], None],
) -> None:
    for attempt in range(attempts):
        if ready():
            return
        if attempt < attempts - 1:
            sleep(interval)
    raise RuntimeError("local Docker registry never became ready")


def _start_registry(
    run: RegistryCommand,
    inputs: TaskInputs,
    state: RegistryState,
    *,
    container: str,
    port: int,
    image: str,
) -> None:
    if state == "started":
        _ = run(inputs, "start", container)
    elif state == "created":
        _ = run(
            inputs,
            "run",
            "--detach",
            "--restart",
            "unless-stopped",
            "--name",
            container,
            "--publish",
            f"{port}:5000",
            image,
        )


def docker_registry_resource(
    *,
    executor: CommandTaskExecutor,
    container: str,
    role: str = "host",
    options: CommandOptions | None = None,
    image: str = "registry:2",
    port: int = 5000,
    ready: Callable[[], bool] | None = None,
    readiness_attempts: int = 30,
    readiness_interval: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[RegistryState]:
    """Acquire a running local registry container and report how it got there.

    Acquiring inspects the container: a running one is adopted, a stopped one is
    started, and a missing one is run detached with ``--restart unless-stopped``
    and published on ``port``. It then polls ``ready`` (defaulting to the
    registry's ``/v2/`` endpoint) until it answers, or fails. The returned state
    tells the release apart: only containers this resource started or created
    are stopped or removed again, and a container it created is removed with its
    data -- see ``release``.

    Raises:
        ValueError: If ``container`` is empty or ``readiness_attempts`` is below
            one.

    """
    if not container:
        raise ValueError("container must not be empty")
    if readiness_attempts < 1:
        raise ValueError("readiness_attempts must be positive")
    current = options or CommandOptions()
    is_ready = ready or (lambda: _answers(port))

    def run(
        inputs: TaskInputs, *args: str, expected: frozenset[int] = frozenset({0})
    ) -> TaskResult:
        outcome = DockerTask(
            *args,
            executor=executor,
            role=role,
            options=replace(current, expected_exit_codes=expected),
        ).run(inputs)
        if outcome.value is None:
            raise RuntimeError("docker registry command returned no result")
        return outcome.value

    def release(inputs: TaskInputs, state: RegistryState) -> None:
        if state == "created":
            # `-v` removes the anonymous volumes attached to this container, and
            # the registry image declares `VOLUME /var/lib/registry`, so docker
            # gave it one holding every layer the run pushed. Removing the
            # container without it leaves that volume behind: one per run,
            # invisible to the next run because it gets a fresh one, and never
            # reclaimed by anything. Only this container's volumes go; a
            # registry this resource merely started keeps the data it had.
            _ = run(
                inputs,
                "rm",
                "--force",
                "-v",
                container,
                expected=frozenset({0, 1}),
            )
        elif state == "started":
            _ = run(inputs, "stop", container)

    def acquire(inputs: TaskInputs) -> RegistryState:
        inspected = run(
            inputs,
            "inspect",
            "--format={{.State.Running}}",
            container,
            expected=frozenset({0, 1}),
        )
        state = _registry_state(inspected)
        try:
            _start_registry(
                run,
                inputs,
                state,
                container=container,
                port=port,
                image=image,
            )
            _wait_until_ready(
                is_ready,
                attempts=readiness_attempts,
                interval=readiness_interval,
                sleep=sleep,
            )
            return state
        except BaseException as error:
            if state != "existing":
                best_effort(
                    error,
                    lambda: release(inputs, state),
                    what="cleanup for Acquire local registry",
                )
            raise

    return Resource(
        title="Acquire local registry",
        acquire=acquire,
        release=release,
        requires=requires,
    )
