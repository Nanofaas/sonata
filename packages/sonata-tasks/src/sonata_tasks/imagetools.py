"""Create and inspect multi-platform image manifests with buildx imagetools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor


def select_platform_manifest_digest(index: dict[str, Any], os: str, arch: str) -> str:
    """Select one real platform manifest, ignoring attestation descriptors."""
    matches = [
        row.get("digest")
        for row in index.get("manifests", [])
        if row.get("platform", {}).get("os") == os
        and row.get("platform", {}).get("architecture") == arch
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        raise ValueError(f"Published image has no unique {os}/{arch} platform")
    return matches[0]


def _docker_options(
    options: CommandOptions | None, docker_config: str
) -> CommandOptions:
    current = options or CommandOptions()
    return CommandOptions(
        cwd=current.cwd,
        env={**current.env, "DOCKER_CONFIG": docker_config},
        remote_dir=current.remote_dir,
        expected_exit_codes=current.expected_exit_codes,
        timeout_seconds=current.timeout_seconds,
    )


class ImagetoolsCreateTask(CommandTask):
    """Create a manifest list tagging ``sources`` as one multi-platform image."""

    def __init__(
        self,
        *,
        tag: str,
        sources: tuple[str, ...],
        docker_config: str,
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
        title: str | None = None,
        verify: Callable[[TaskResult], None] | None = None,
        semantic_key: str | None = None,
    ) -> None:
        """Configure ``docker buildx imagetools create --tag tag sources``.

        ``docker_config`` is exported as ``DOCKER_CONFIG`` for the command, and
        ``title`` defaults to one naming the tag.
        """
        super().__init__(
            title=title or f"Create manifest {tag}",
            argv=("docker", "buildx", "imagetools", "create", "--tag", tag, *sources),
            executor=executor,
            role=role,
            options=_docker_options(options, docker_config),
            verify=verify,
            semantic_key=semantic_key,
        )


class ImagetoolsInspectTask(CommandTask):
    """Inspect a manifest list or image reference and print its platforms."""

    def __init__(
        self,
        *,
        reference: str,
        docker_config: str,
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
        title: str | None = None,
        verify: Callable[[TaskResult], None] | None = None,
        semantic_key: str | None = None,
    ) -> None:
        """Configure ``docker buildx imagetools inspect reference``.

        ``docker_config`` is exported as ``DOCKER_CONFIG`` for the command, and
        ``title`` defaults to one naming the reference.
        """
        super().__init__(
            title=title or f"Inspect manifest {reference}",
            argv=("docker", "buildx", "imagetools", "inspect", reference),
            executor=executor,
            role=role,
            options=_docker_options(options, docker_config),
            verify=verify,
            semantic_key=semantic_key,
        )
