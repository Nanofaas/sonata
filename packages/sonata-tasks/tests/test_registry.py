from dataclasses import dataclass, field

import pytest

from sonata_engine import TaskInputs
from sonata_tasks.registry import docker_registry_resource
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


@dataclass
class RecordingExecutor:
    inspect_code: int = 1
    inspect_stdout: str = ""
    fail_action: str | None = None

    def binding_key(self, role: str) -> str:
        return f"test-recording:{role}"

    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        code = (
            self.inspect_code
            if task.argv[1] == "inspect"
            else 1
            if task.argv[1] == self.fail_action
            else 0
        )
        return TaskResult(
            task_id="",
            status="passed" if code in task.options.expected_exit_codes else "failed",
            return_code=code,
            stdout=self.inspect_stdout if task.argv[1] == "inspect" else "",
            stderr="failed" if code else "",
        )


def test_registry_resource_creates_and_removes_only_its_container() -> None:
    executor = RecordingExecutor()
    resource = docker_registry_resource(
        executor=executor,
        container="example-registry",
        role="host",
        ready=lambda: True,
    )

    state = resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), state)

    assert state == "created"
    assert [task.argv[:2] for task in executor.seen] == [
        ("docker", "inspect"),
        ("docker", "run"),
        ("docker", "rm"),
    ]
    # The registry image declares `VOLUME /var/lib/registry`, so the container
    # docker ran carries an anonymous volume holding every layer pushed to it.
    # Removing the container without `-v` is what leaves that volume dangling.
    assert executor.seen[-1].argv == (
        "docker",
        "rm",
        "--force",
        "-v",
        "example-registry",
    )


def test_registry_starts_and_stops_a_preexisting_stopped_container() -> None:
    executor = RecordingExecutor(inspect_code=0, inspect_stdout="false\n")
    resource = docker_registry_resource(
        executor=executor,
        container="example-registry",
        ready=lambda: True,
    )

    state = resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), state)

    assert state == "started"
    assert [task.argv[1] for task in executor.seen] == ["inspect", "start", "stop"]
    # Whatever this registry holds predates the run, so stopping is the whole
    # job: nothing here may remove it, or its data.
    assert all("rm" not in task.argv for task in executor.seen)


def test_registry_leaves_a_running_preexisting_container_untouched() -> None:
    executor = RecordingExecutor(inspect_code=0, inspect_stdout="true\n")
    resource = docker_registry_resource(
        executor=executor,
        container="example-registry",
        ready=lambda: True,
    )

    state = resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), state)

    assert state == "existing"
    assert [task.argv[1] for task in executor.seen] == ["inspect"]


def test_registry_times_out_and_compensates_what_it_created() -> None:
    executor = RecordingExecutor()
    resource = docker_registry_resource(
        executor=executor,
        container="example-registry",
        ready=lambda: False,
        readiness_attempts=2,
        readiness_interval=0,
    )

    with pytest.raises(RuntimeError, match="never became ready"):
        resource.acquire(TaskInputs.empty())

    assert [task.argv[1] for task in executor.seen][-1] == "rm"


def test_registry_validates_configuration() -> None:
    with pytest.raises(ValueError, match="container"):
        docker_registry_resource(executor=RecordingExecutor(), container="")
    with pytest.raises(ValueError, match="positive"):
        docker_registry_resource(
            executor=RecordingExecutor(),
            container="example-registry",
            readiness_attempts=0,
        )
