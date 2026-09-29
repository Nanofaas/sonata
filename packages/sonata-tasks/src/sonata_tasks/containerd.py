"""Inspect an owned containerd image and require a published manifest digest."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, override

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor


@dataclass(frozen=True, slots=True)
class ContainerdImageIdentity:
    """The raw container and image inspections retained for caller evidence."""

    container: dict[str, Any]
    image: dict[str, Any]


def require_containerd_image_identity(
    identity: ContainerdImageIdentity, *, reference: str, digest: str
) -> None:
    """Require the owned container to use the expected published manifest."""
    image_ref = identity.container.get("Image", identity.container.get("image"))
    if image_ref != reference:
        raise ValueError(f"containerd image is {image_ref!r}, expected {reference!r}")
    repository = reference.split("@", 1)[0]
    if ":" in repository.rsplit("/", 1)[-1]:
        repository = repository.rsplit(":", 1)[0]
    digests = identity.image.get("RepoDigests")
    if not isinstance(digests, list) or f"{repository}@{digest}" not in digests:
        raise ValueError(f"containerd manifest digest differs for {reference}")


class ContainerdImageInspectTask(Task[ContainerdImageIdentity]):
    """Inspect an owned container and its image in the same containerd namespace.

    The caller supplies commands that inspect its own container and image in
    the same containerd namespace. The first prints a ctr container object;
    the second prints nerdctl's one-element image-inspect array.
    """

    def __init__(
        self,
        *,
        container_argv: tuple[str, ...],
        image_argv: tuple[str, ...],
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
        title: str | None = None,
    ) -> None:
        """Record the two runtime inspection commands."""
        self.title = title or "Inspect containerd image identity"
        self.container_argv = container_argv
        self.image_argv = image_argv
        self.executor = executor
        self.role = role
        self._binding_key = executor.binding_key(role)
        self.options = options

    def _inspect(self, inputs: TaskInputs, argv: tuple[str, ...]) -> object:
        result = (
            CommandTask(
                title=self.title,
                argv=argv,
                executor=self.executor,
                role=self.role,
                options=self.options,
            )
            .run(inputs)
            .value
        )
        if result is None:
            raise RuntimeError("containerd inspection returned no result")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ValueError("containerd inspection returned invalid JSON") from error

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[ContainerdImageIdentity]:
        """Return both runtime objects for evidence and identity checks."""
        container = self._inspect(inputs, self.container_argv)
        if not isinstance(container, dict):
            raise ValueError("containerd inspection returned no container object")
        images = self._inspect(inputs, self.image_argv)
        if not isinstance(images, list) or len(images) != 1:
            raise ValueError("containerd image inspection is ambiguous")
        image = images[0]
        if not isinstance(image, dict):
            raise ValueError("containerd image inspection returned no image object")
        return TaskOutcome(value=ContainerdImageIdentity(container, image))

    @override
    def _fingerprint_payload(self) -> object:
        return {
            "containerArgv": self.container_argv,
            "imageArgv": self.image_argv,
            "role": self.role,
            "bindingKey": self._binding_key,
            "options": repr(self.options),
        }
