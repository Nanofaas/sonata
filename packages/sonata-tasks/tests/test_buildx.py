"""Tests for buildx builder resource."""

from dataclasses import dataclass, field

import pytest

from sonata_engine import TaskInputs
from sonata_tasks.buildx import buildx_builder_resource
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


@dataclass
class RecordingExecutor:
    def binding_key(self, role: str) -> str:
        return f"test-recording:{role}"

    seen: list[CommandTaskSpec] = field(default_factory=list)
    builder_exists: bool = False
    fail_bootstrap: bool = False
    fail_create: bool = False

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        if task.argv[1:3] == ("buildx", "inspect") and "--bootstrap" not in task.argv:
            rc = 0 if self.builder_exists else 1
            stdout = (
                (f"Name: {task.argv[-1]}\nPlatforms: linux/amd64*, linux/arm64*\n")
                if self.builder_exists
                else ""
            )
            return TaskResult(
                task_id="",
                status="passed",
                return_code=rc,
                stdout=stdout,
            )
        if "--bootstrap" in task.argv and self.fail_bootstrap:
            return TaskResult(task_id="", status="failed", return_code=1, stderr="boom")
        if "create" in task.argv and self.fail_create:
            return TaskResult(
                task_id="", status="failed", return_code=1, stderr="partial create"
            )
        return TaskResult(task_id="", status="passed", return_code=0)


def test_buildx_builder_creates_bootstraps_and_removes() -> None:
    executor = RecordingExecutor(builder_exists=False)
    resource = buildx_builder_resource(
        name="release-builder",
        executor=executor,
        role="stack",
    )
    state = resource.acquire(TaskInputs.empty())
    assert state == "release-builder"
    resource.release(TaskInputs.empty(), state)

    argv_seqs = [tuple(s.argv) for s in executor.seen]
    assert argv_seqs == [
        ("docker", "buildx", "inspect", "release-builder"),
        (
            "docker",
            "buildx",
            "create",
            "--name",
            "release-builder",
            "--driver",
            "docker-container",
            "--use",
        ),
        ("docker", "buildx", "inspect", "--bootstrap", "release-builder"),
        ("docker", "buildx", "rm", "--force", "release-builder"),
    ]


def test_buildx_builder_passes_driver_options_to_the_create() -> None:
    """A builder that must reach a registry on the host's loopback needs these.

    A `docker-container` builder runs buildkitd in a container of its own, so
    its `localhost` is itself and a push to a registry published on the host's
    loopback cannot resolve. `network=host` is the option that makes it work,
    and it is a `--driver-opt` given at creation -- a `buildkitd_config` with
    `[worker.oci] networkMode = "host"` was measured not to substitute, because
    that governs the network of build steps rather than the daemon that pushes.
    """
    executor = RecordingExecutor(builder_exists=False)
    resource = buildx_builder_resource(
        name="release-builder",
        executor=executor,
        role="stack",
        driver_options=("network=host",),
    )

    resource.acquire(TaskInputs.empty())

    create = next(tuple(s.argv) for s in executor.seen if "create" in s.argv)
    assert create == (
        "docker",
        "buildx",
        "create",
        "--name",
        "release-builder",
        "--driver",
        "docker-container",
        "--driver-opt",
        "network=host",
        "--use",
    )


def test_buildx_builder_preexisting_is_not_removed() -> None:
    executor = RecordingExecutor(builder_exists=True)
    resource = buildx_builder_resource(
        name="reuse-me",
        executor=executor,
        role="stack",
    )
    state = resource.acquire(TaskInputs.empty())
    assert state == "existing"
    resource.release(TaskInputs.empty(), state)
    assert len(executor.seen) == 1  # only inspect, no create or rm
    assert resource.always_release is True


def test_buildx_builder_validates_preexisting_builder() -> None:
    executor = RecordingExecutor(builder_exists=True)
    resource = buildx_builder_resource(
        name="reuse-me",
        executor=executor,
        role="arm-builder",
        validate=lambda _output: (_ for _ in ()).throw(RuntimeError("wrong platform")),
        validation_key="linux-amd64-arm64",
    )

    with pytest.raises(RuntimeError, match="wrong platform"):
        resource.acquire(TaskInputs.empty())

    assert len(executor.seen) == 1


def test_buildx_builder_can_replace_and_cleanup_a_stale_named_builder() -> None:
    executor = RecordingExecutor(builder_exists=True)
    resource = buildx_builder_resource(
        name="release-arm",
        executor=executor,
        role="arm-builder",
        replace_existing=True,
    )

    state = resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), state)

    commands = [task.argv for task in executor.seen]
    assert sum("create" in command for command in commands) == 1
    assert commands.count(("docker", "buildx", "rm", "--force", "release-arm")) == 2


def test_buildx_builder_uses_buildkit_config_validates_and_compensates_bootstrap() -> (
    None
):
    executor = RecordingExecutor(fail_bootstrap=True)
    resource = buildx_builder_resource(
        name="arm-builder",
        executor=executor,
        role="arm-builder",
        buildkitd_config="/release/buildkitd.toml",
        validate=lambda _output: None,
        validation_key="linux-amd64-arm64",
    )

    with pytest.raises(RuntimeError):
        resource.acquire(TaskInputs.empty())

    commands = [task.argv for task in executor.seen]
    create = next(argv for argv in commands if "create" in argv)
    assert create.count("create") == 1
    assert create[create.index("--buildkitd-config") + 1] == "/release/buildkitd.toml"
    assert commands[-1] == ("docker", "buildx", "rm", "--force", "arm-builder")


def test_buildx_builder_compensates_a_failed_partial_create() -> None:
    executor = RecordingExecutor(fail_create=True)
    resource = buildx_builder_resource(
        name="arm-builder", executor=executor, role="arm-builder"
    )

    with pytest.raises(RuntimeError, match="partial create"):
        resource.acquire(TaskInputs.empty())

    assert executor.seen[-1].argv == (
        "docker",
        "buildx",
        "rm",
        "--force",
        "arm-builder",
    )
