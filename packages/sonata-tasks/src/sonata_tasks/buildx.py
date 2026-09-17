"""Manage a docker buildx builder as a resource."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import best_effort
from sonata_tasks.execution.models import CommandOptions, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor


def _run(
    inputs: TaskInputs,
    executor: CommandTaskExecutor,
    role: str,
    options: CommandOptions,
    *args: str,
    expected: frozenset[int] = frozenset({0}),
) -> TaskResult:
    outcome = CommandTask(
        title=f"docker buildx {' '.join(args)}",
        argv=("docker", "buildx", *args),
        executor=executor,
        role=role,
        options=replace(options, expected_exit_codes=expected),
    ).run(inputs)
    if outcome.value is None:
        raise RuntimeError("docker buildx returned no command result")
    return outcome.value


def _create_argv(
    name: str,
    buildkitd_config: str | None,
    driver_options: Sequence[str],
) -> tuple[str, ...]:
    argv = ["create", "--name", name, "--driver", "docker-container"]
    if buildkitd_config is not None:
        argv.extend(("--buildkitd-config", buildkitd_config))
    for option in driver_options:
        argv.extend(("--driver-opt", option))
    argv.append("--use")
    return tuple(argv)


def _validate_output(validate: Callable[[str], None] | None, stdout: str) -> None:
    if validate is not None:
        validate(stdout)


def buildx_builder_resource(
    *,
    name: str,
    executor: CommandTaskExecutor,
    role: str = "host",
    options: CommandOptions | None = None,
    requires: tuple[Resource[Any], ...] = (),
    buildkitd_config: str | None = None,
    # Driver options the created builder gets, as `--driver-opt` arguments. A
    # `docker-container` builder runs buildkitd in a container of its own, so
    # its `localhost` is itself: reaching a registry on the host's loopback
    # needs `network=host` here, and buildkitd's own `[worker.oci] networkMode`
    # does not substitute, because that governs the network of build steps
    # rather than the daemon that pushes.
    driver_options: Sequence[str] = (),
    validate: Callable[[str], None] | None = None,
    validation_key: str | None = None,
    replace_existing: bool = False,
) -> Resource[str]:
    """Acquire a named docker buildx builder, bootstrapping it when missing.

    Acquiring inspects the builder. A missing one — or one that
    ``replace_existing`` asks to redo, which is removed first — is created as a
    ``docker-container`` builder with ``--use`` and any ``driver_options``,
    bootstrapped, and checked through ``validate``. Acquiring returns the builder
    name for a builder this resource created, or ``"existing"`` when it left a
    pre-existing builder untouched; releasing removes only the former.

    Raises:
        ValueError: If ``validate`` is configured without a ``validation_key``.

    """
    if validate is not None and not validation_key:
        raise ValueError("validation_key is required when validate is configured")
    current = options or CommandOptions()

    def remove(inputs: TaskInputs) -> None:
        _ = _run(inputs, executor, role, current, "rm", "--force", name)

    def bootstrap(inputs: TaskInputs) -> None:
        try:
            _ = _run(
                inputs,
                executor,
                role,
                current,
                *_create_argv(name, buildkitd_config, driver_options),
            )
            result = _run(
                inputs, executor, role, current, "inspect", "--bootstrap", name
            )
            _validate_output(validate, result.stdout)
        except BaseException as error:
            best_effort(
                error,
                lambda: remove(inputs),
                what=f"cleanup failed buildx builder {name}",
            )
            raise

    def acquire(inputs: TaskInputs) -> str:
        inspected = _run(
            inputs, executor, role, current, "inspect", name, expected=frozenset({0, 1})
        )
        if inspected.return_code != 0:
            bootstrap(inputs)
            return name
        if not replace_existing:
            _validate_output(validate, inspected.stdout)
            return "existing"
        remove(inputs)
        bootstrap(inputs)
        return name

    def release(inputs: TaskInputs, state: str) -> None:
        if state != "existing":
            remove(inputs)

    return Resource(
        title=f"Acquire {name} buildx builder",
        acquire=acquire,
        release=release,
        requires=requires,
        always_release=True,
    )
