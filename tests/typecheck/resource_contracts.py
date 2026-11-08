"""Positive static-check fixture for typed resource inputs."""

from typing import assert_type, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome


class AcquireName(Task[str]):
    title = "Acquire name"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        return TaskOutcome(value="builder")


class ReleaseName(Task[None]):
    title = "Release name"

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        return TaskOutcome()


resource: Resource[str] = Resource(
    title="Acquire name", acquire=AcquireName(), release=ReleaseName()
)


def read_name(inputs: TaskInputs) -> None:
    assert_type(inputs.resource(resource), str)
