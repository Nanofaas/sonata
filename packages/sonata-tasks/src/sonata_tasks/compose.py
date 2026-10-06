"""Deploy, wait for, and tear down a docker compose project."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonata_engine import Resource, Steps, TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor


@dataclass(frozen=True, slots=True)
class DockerComposeProject:
    """A compose project to run: its name, file, and readiness endpoint."""

    name: str
    file: Path
    ready_url: str
    build: bool = True


class DeployDockerCompose(CommandTask):
    """Bring a compose project up detached and wait for its services."""

    def __init__(
        self,
        project: DockerComposeProject,
        *,
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
        title: str | None = None,
    ) -> None:
        """Configure the ``docker compose up -d --wait`` command.

        ``--build`` is added unless ``project.build`` is false. ``title``
        defaults to one naming the project.
        """
        super().__init__(
            title=title or f"Deploy Docker Compose project {project.name}",
            argv=(
                "docker",
                "compose",
                "-f",
                str(project.file),
                "-p",
                project.name,
                "up",
                "-d",
                *(("--build",) if project.build else ()),
                "--wait",
            ),
            executor=executor,
            role=role,
            options=options,
        )


class WaitForDockerCompose(CommandTask):
    """Poll the project's readiness URL until it answers."""

    def __init__(
        self,
        project: DockerComposeProject,
        *,
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
        title: str | None = None,
    ) -> None:
        """Configure the ``curl`` retry loop against ``project.ready_url``.

        ``title`` defaults to one naming the project.
        """
        super().__init__(
            title=title or f"Wait for Docker Compose project {project.name}",
            argv=(
                "curl",
                "-fsS",
                "--retry",
                "60",
                "--retry-delay",
                "1",
                "--retry-connrefused",
                "--retry-all-errors",
                project.ready_url,
            ),
            executor=executor,
            role=role,
            options=options,
        )


class DestroyDockerCompose(CommandTask):
    """Bring a compose project down, optionally dropping volumes and orphans."""

    def __init__(
        self,
        project: DockerComposeProject,
        *,
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
        title: str | None = None,
        remove_volumes: bool = False,
        remove_orphans: bool = False,
    ) -> None:
        """Configure the ``docker compose down`` command.

        ``remove_volumes`` and ``remove_orphans`` add ``--volumes`` and
        ``--remove-orphans`` respectively; ``title`` defaults to one naming the
        project.
        """
        argv = [
            "docker",
            "compose",
            "-f",
            str(project.file),
            "-p",
            project.name,
            "down",
        ]
        if remove_volumes:
            argv.append("--volumes")
        if remove_orphans:
            argv.append("--remove-orphans")
        super().__init__(
            title=title or f"Destroy Docker Compose project {project.name}",
            argv=tuple(argv),
            executor=executor,
            role=role,
            options=options,
        )


def docker_compose_resource[T: DockerComposeProject](
    project: T,
    *,
    executor: CommandTaskExecutor,
    role: str = "host",
    options: CommandOptions | None = None,
    pre_clean: bool = False,
    remove_volumes: bool = False,
    remove_orphans: bool = False,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[T]:
    """Deploy, wait for, and release the supplied project without copying it.

    ``pre_clean`` runs ``down`` before deployment for a caller-owned project
    namespace. Both cleanup commands use the supplied removal flags; by
    default there is no pre-clean and volumes and orphans are retained.
    Acquisition failures trigger best-effort teardown, including failures
    during pre-clean itself.
    """
    deploy = Steps(
        title=f"Acquire Docker Compose project {project.name}",
        steps=(
            *(
                (
                    DestroyDockerCompose(
                        project,
                        executor=executor,
                        role=role,
                        options=options,
                        title=f"Clear any previous {project.name} state",
                        remove_volumes=remove_volumes,
                        remove_orphans=remove_orphans,
                    ),
                )
                if pre_clean
                else ()
            ),
            DeployDockerCompose(project, executor=executor, role=role, options=options),
            WaitForDockerCompose(
                project, executor=executor, role=role, options=options
            ),
        ),
    )
    destroy = DestroyDockerCompose(
        project,
        executor=executor,
        role=role,
        options=options,
        remove_volumes=remove_volumes,
        remove_orphans=remove_orphans,
    )

    def acquire(inputs: TaskInputs) -> T:
        _ = deploy.run(inputs)
        return project

    return compensated_resource(
        title=f"Acquire Docker Compose project {project.name}",
        acquire=acquire,
        compensate=destroy.run,
        requires=requires,
    )
