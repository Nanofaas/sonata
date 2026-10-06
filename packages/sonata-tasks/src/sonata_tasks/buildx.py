"""Manage a docker buildx builder as a resource."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any
from uuid import uuid4

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
    *,
    owner_node: str | None = None,
    use: bool = True,
) -> tuple[str, ...]:
    argv = ["create", "--name", name, "--driver", "docker-container"]
    if owner_node is not None:
        argv.extend(("--node", owner_node))
    if buildkitd_config is not None:
        argv.extend(("--buildkitd-config", buildkitd_config))
    for option in driver_options:
        argv.extend(("--driver-opt", option))
    if use:
        argv.append("--use")
    return tuple(argv)


def _validate_output(validate: Callable[[str], None] | None, stdout: str) -> None:
    if validate is not None:
        validate(stdout)


def _matches_owner(stdout: str, name: str, node: str) -> bool:
    # Inspect's top-level name and the one node must both match. An added
    # foreign node makes the entire builder unsafe to remove.
    names = re.findall(r"^[ \t]*Name:[ \t]*(.*?)[ \t]*$", stdout, re.MULTILINE)
    drivers = re.findall(r"^[ \t]*Driver:[ \t]*(.*?)[ \t]*$", stdout, re.MULTILINE)
    return (
        names == [name, node]
        and drivers == ["docker-container"]
        and re.search(r"^[ \t]*Nodes:[ \t]*$", stdout, re.MULTILINE) is not None
    )


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
    exclusive: bool = False,
    owner_node: str | None = None,
    use: bool = True,
) -> Resource[str]:
    """Acquire a named docker buildx builder, bootstrapping it when missing.

    Acquiring inspects the builder. A missing one — or one that
    ``replace_existing`` asks to redo, which is removed first — is created as a
    ``docker-container`` builder with ``--use`` and any ``driver_options``,
    bootstrapped, and checked through ``validate``. Acquiring returns the builder
    name for a builder this resource created, or ``"existing"`` when it left a
    pre-existing builder untouched; releasing removes only the former.

    ``exclusive`` refuses existing builders and creates a unique owner node.
    Cleanup reconciles partial creation and requires the same single node and
    driver before removal. A failed removal can erase the client record while
    leaving its daemon alive; later record absence remains unconfirmed and
    requires operator reconciliation. ``owner_node`` retains a unique receipt;
    it requires exclusive mode. ``use=False`` preserves the client's selection.
    Callers serialize external client-store mutation between identity inspection
    and removal; the CLI does not offer atomic compare-and-delete.

    Raises:
        ValueError: If ``validate`` is configured without a ``validation_key``.

    """
    if validate is not None and not validation_key:
        raise ValueError("validation_key is required when validate is configured")
    if exclusive and replace_existing:
        raise ValueError("exclusive builders cannot replace existing builders")
    if owner_node is not None and not exclusive:
        raise ValueError("owner_node requires exclusive acquisition")
    if exclusive:
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name) is None
        ):
            raise ValueError("invalid exclusive builder name")
        name = name.lower()
        if name == "default":
            raise ValueError("default is a reserved builder name")
        if owner_node is None:
            owner_node = f"{name}-{uuid4().hex}"
        if (
            not isinstance(owner_node, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", owner_node) is None
        ):
            raise ValueError("invalid exclusive owner node")
        owner_node = owner_node.lower()
    current = options or CommandOptions()
    creation_attempted = False
    removal_unconfirmed = False

    def builder_exists(inputs: TaskInputs) -> bool:
        result = _run(inputs, executor, role, current, "ls", "--format", "{{.Name}}")
        return name in {line.strip().rstrip("*") for line in result.stdout.splitlines()}

    def remove_owned(inputs: TaskInputs) -> None:
        nonlocal creation_attempted, removal_unconfirmed
        if not creation_attempted:
            return
        if builder_exists(inputs):
            result = _run(inputs, executor, role, current, "inspect", name)
            if owner_node is None or not _matches_owner(
                result.stdout, name, owner_node
            ):
                raise RuntimeError(
                    f"Owned buildx builder {name} cleanup identity conflict"
                )
            removal_unconfirmed = True
            _ = _run(inputs, executor, role, current, "rm", name)
            removal_unconfirmed = False
        elif removal_unconfirmed:
            raise RuntimeError(
                f"Owned buildx builder {name} daemon removal unconfirmed; "
                "client record disappeared after an unsuccessful removal"
            )
        creation_attempted = False

    def remove(inputs: TaskInputs) -> None:
        if exclusive:
            remove_owned(inputs)
        else:
            _ = _run(inputs, executor, role, current, "rm", "--force", name)

    def bootstrap(inputs: TaskInputs) -> None:
        nonlocal creation_attempted
        try:
            creation_attempted = True
            _ = _run(
                inputs,
                executor,
                role,
                current,
                *_create_argv(
                    name,
                    buildkitd_config,
                    driver_options,
                    owner_node=owner_node,
                    use=use,
                ),
            )
            result = _run(
                inputs, executor, role, current, "inspect", "--bootstrap", name
            )
            if exclusive and (
                owner_node is None
                or not _matches_owner(result.stdout, name, owner_node)
            ):
                raise RuntimeError(
                    f"Owned buildx builder {name} bootstrap identity conflict"
                )
            _validate_output(validate, result.stdout)
        except BaseException as error:
            if exclusive:
                try:
                    remove_owned(inputs)
                except BaseException as cleanup_error:
                    error.add_note(
                        f"Cleanup failed buildx builder {name}: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
            else:
                best_effort(
                    error,
                    lambda: remove(inputs),
                    what=f"cleanup failed buildx builder {name}",
                )
            raise

    def acquire(inputs: TaskInputs) -> str:
        if exclusive:
            if builder_exists(inputs):
                raise RuntimeError(f"Buildx builder {name} already exists")
            bootstrap(inputs)
            return name
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
        if exclusive or state != "existing":
            remove(inputs)

    return Resource(
        title=f"Acquire {name} buildx builder",
        acquire=acquire,
        release=release,
        requires=requires,
        always_release=True,
    )
