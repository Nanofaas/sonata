"""Tests for buildx builder resource."""

from dataclasses import dataclass, field
from typing import override

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


@dataclass
class ExclusiveExecutor:
    """Model Buildx's external client store, including partial command effects."""

    builders: dict[str, tuple[str, ...]] = field(default_factory=dict)
    seen: list[CommandTaskSpec] = field(default_factory=list)
    failure: str = ""
    inspect_unavailable: bool = False
    remove_unavailable: bool = False
    driver: str = "docker-container"

    def binding_key(self, role: str) -> str:
        return f"isolated-buildx:{role}"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        args = task.argv[2:]
        rc, stdout, stderr = 0, "", ""
        if args[0] == "ls":
            stdout = "\n".join(self.builders)
        elif args[0] == "create":
            name = args[args.index("--name") + 1]
            node = args[args.index("--node") + 1]
            if name in self.builders:
                rc, stderr = 1, "node not found, did you mean to append?"
            elif self.failure == "empty-create":
                rc, stderr = 1, "create failed before publication"
            else:
                self.builders[name] = (
                    ("foreign-node",) if self.failure == "foreign-create" else (node,)
                )
                if self.failure == "cancel-create":
                    raise KeyboardInterrupt("cancelled after creation")
                if self.failure in {"partial-create", "foreign-create"}:
                    rc, stderr = 1, "partial create failure"
        elif args[0] == "inspect":
            name = args[-1]
            if self.inspect_unavailable or name not in self.builders:
                rc, stderr = 1, "cannot inspect builder"
            else:
                stdout = f"Name: {name}\nDriver: {self.driver}\nNodes:\n" + "".join(
                    f"Name: {node}\nEndpoint: unix:///var/run/docker.sock\n"
                    "Status: running\nPlatforms: linux/amd64, linux/arm64\n"
                    for node in self.builders[name]
                )
                if "--bootstrap" in args and self.failure == "bootstrap":
                    rc, stderr = 1, "bootstrap failed"
        elif args[0] == "rm":
            if self.remove_unavailable:
                rc, stderr = 1, "remove failed"
            else:
                del self.builders[args[-1]]
        else:
            raise AssertionError(task.argv)
        return TaskResult(
            task_id="",
            status="passed" if rc in task.options.expected_exit_codes else "failed",
            return_code=rc,
            stdout=stdout,
            stderr=stderr,
        )


def exclusive(executor, **kwargs):
    return buildx_builder_resource(
        name="ordinary-app",
        executor=executor,
        exclusive=True,
        owner_node="ordinary-owner",
        use=False,
        **kwargs,
    )


def test_exclusive_builder_keeps_selection_and_removes_only_its_node():
    executor = ExclusiveExecutor()
    resource = exclusive(executor)
    state = resource.acquire(TaskInputs.empty())
    assert state == "ordinary-app"
    create = next(task for task in executor.seen if "create" in task.argv)
    assert create.argv[create.argv.index("--node") + 1] == "ordinary-owner"
    assert "--use" not in create.argv
    resource.release(TaskInputs.empty(), state)
    assert executor.builders == {}
    assert executor.seen[-1].argv == ("docker", "buildx", "rm", "ordinary-app")
    count = len(executor.seen)
    resource.release(TaskInputs.empty(), state)
    assert len(executor.seen) == count


def test_exclusive_builder_refuses_preexisting_without_mutation():
    executor = ExclusiveExecutor(builders={"ordinary-app": ("operator-node",)})
    resource = exclusive(executor)
    with pytest.raises(RuntimeError, match="already exists"):
        resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), "ordinary-app")
    assert executor.builders == {"ordinary-app": ("operator-node",)}
    assert [task.argv[2] for task in executor.seen] == ["ls"]


@pytest.mark.parametrize("failure", ["empty-create", "partial-create", "bootstrap"])
def test_exclusive_builder_compensates_only_published_owned_state(failure):
    executor = ExclusiveExecutor(failure=failure)
    resource = exclusive(executor)
    with pytest.raises(RuntimeError, match=r"failed|failure"):
        resource.acquire(TaskInputs.empty())
    assert executor.builders == {}
    removes = [task for task in executor.seen if task.argv[2] == "rm"]
    assert len(removes) == (0 if failure == "empty-create" else 1)


def test_exclusive_builder_reconciles_cancellation_after_partial_create():
    executor = ExclusiveExecutor(failure="cancel-create")
    with pytest.raises(KeyboardInterrupt, match="after creation"):
        exclusive(executor).acquire(TaskInputs.empty())
    assert executor.builders == {}


def test_exclusive_builder_keeps_primary_failure_when_partial_owner_is_foreign():
    executor = ExclusiveExecutor(failure="foreign-create")
    with pytest.raises(RuntimeError, match="partial create failure") as caught:
        exclusive(executor).acquire(TaskInputs.empty())
    assert executor.builders == {"ordinary-app": ("foreign-node",)}
    assert any("identity" in note for note in caught.value.__notes__)
    assert all(task.argv[2] != "rm" for task in executor.seen)


@pytest.mark.parametrize(
    "nodes", [("foreign-node",), ("ordinary-owner", "operator-node")]
)
def test_exclusive_builder_release_rejects_foreign_or_additional_nodes(nodes):
    executor = ExclusiveExecutor()
    resource = exclusive(executor)
    state = resource.acquire(TaskInputs.empty())
    executor.builders[state] = nodes
    with pytest.raises(RuntimeError, match="identity"):
        resource.release(TaskInputs.empty(), state)
    assert executor.builders[state] == nodes
    assert all(task.argv[2] != "rm" for task in executor.seen)


def test_exclusive_builder_release_requires_the_original_driver():
    executor = ExclusiveExecutor()
    resource = exclusive(executor)
    state = resource.acquire(TaskInputs.empty())
    executor.driver = "remote"
    with pytest.raises(RuntimeError, match="identity"):
        resource.release(TaskInputs.empty(), state)
    assert state in executor.builders


def test_exclusive_builder_release_accepts_confirmed_absence():
    executor = ExclusiveExecutor()
    resource = exclusive(executor)
    state = resource.acquire(TaskInputs.empty())
    del executor.builders[state]
    resource.release(TaskInputs.empty(), state)
    assert all(task.argv[2] != "rm" for task in executor.seen)


@pytest.mark.parametrize("failure", ["inspect_unavailable", "remove_unavailable"])
def test_exclusive_builder_cleanup_failure_is_reported_and_retryable(failure):
    executor = ExclusiveExecutor()
    resource = exclusive(executor)
    state = resource.acquire(TaskInputs.empty())
    setattr(executor, failure, True)
    with pytest.raises(RuntimeError, match="failed"):
        resource.release(TaskInputs.empty(), state)
    assert state in executor.builders
    setattr(executor, failure, False)
    resource.release(TaskInputs.empty(), state)
    assert executor.builders == {}


def test_exclusive_builder_generates_distinct_owner_nodes():
    nodes = []
    for _ in range(2):
        executor = ExclusiveExecutor()
        resource = buildx_builder_resource(
            name="ordinary-app", executor=executor, exclusive=True
        )
        state = resource.acquire(TaskInputs.empty())
        nodes.extend(executor.builders[state])
        resource.release(TaskInputs.empty(), state)
    assert len(set(nodes)) == 2
    assert all(node.startswith("ordinary-app-") for node in nodes)


def test_exclusive_builder_preserves_role_and_command_options():
    from sonata_tasks.execution.models import CommandOptions

    executor = ExclusiveExecutor()
    options = CommandOptions(
        env={"DOCKER_CONFIG": "/isolated/client"}, timeout_seconds=5
    )
    resource = exclusive(executor, role="remote", options=options)
    state = resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), state)
    assert all(
        task.role == "remote" and task.options == options for task in executor.seen
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exclusive": True, "replace_existing": True},
        {"owner_node": "an-owner"},
        {"exclusive": True, "name": "-unsafe"},
        {"exclusive": True, "owner_node": "-unsafe"},
        {"exclusive": True, "owner_node": ""},
        {"exclusive": True, "name": "default"},
    ],
)
def test_exclusive_builder_rejects_unsafe_or_incompatible_options(kwargs):
    executor = ExclusiveExecutor()
    arguments = {"name": "ordinary-app", "executor": executor} | kwargs
    with pytest.raises(ValueError, match=r"exclusive|owner_node|reserved"):
        buildx_builder_resource(**arguments)
    assert executor.seen == []


def test_exclusive_builder_normalizes_names_as_the_native_client_does():
    executor = ExclusiveExecutor()
    resource = buildx_builder_resource(
        name="Ordinary-App",
        executor=executor,
        exclusive=True,
        owner_node="Ordinary-Owner",
    )
    state = resource.acquire(TaskInputs.empty())
    assert state == "ordinary-app"
    assert executor.builders[state] == ("ordinary-owner",)
    resource.release(TaskInputs.empty(), state)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_exclusive_validation_failure_preserves_primary_and_compensates(cleanup_fails):
    executor = ExclusiveExecutor(remove_unavailable=cleanup_fails)
    observed = []

    def validate(stdout):
        observed.append(stdout)
        raise ValueError("application platform rejected")

    resource = exclusive(executor, validate=validate, validation_key="app-platform-v1")
    with pytest.raises(ValueError, match="application platform rejected") as caught:
        resource.acquire(TaskInputs.empty())
    assert len(observed) == 1
    assert "Platforms:" in observed[0]
    assert bool(executor.builders) is cleanup_fails
    if cleanup_fails:
        assert any("remove failed" in note for note in caught.value.__notes__)
        executor.remove_unavailable = False
        resource.release(TaskInputs.empty(), "ordinary-app")
        assert executor.builders == {}


def test_exclusive_bootstrap_rejects_foreign_identity_before_validation():
    class ReplacedExecutor(ExclusiveExecutor):
        @override
        def run(self, task, *, dry_run=False):
            if "--bootstrap" in task.argv:
                self.builders["ordinary-app"] = ("replacement-node",)
            return super().run(task, dry_run=dry_run)

    executor = ReplacedExecutor()
    observed = []
    resource = exclusive(
        executor, validate=observed.append, validation_key="receipt-v1"
    )
    with pytest.raises(RuntimeError, match="bootstrap identity"):
        resource.acquire(TaskInputs.empty())
    assert observed == []
    assert executor.builders == {"ordinary-app": ("replacement-node",)}
    assert all(task.argv[2] != "rm" for task in executor.seen)


def test_exclusive_failed_removal_cannot_confirm_cleanup_from_missing_record():
    class RemovedRecordExecutor(ExclusiveExecutor):
        daemon_alive = True

        @override
        def run(self, task, *, dry_run=False):
            if task.argv[2] == "rm":
                self.seen.append(task)
                del self.builders[task.argv[-1]]
                raise RuntimeError("client record removed but daemon stop failed")
            return super().run(task, dry_run=dry_run)

    executor = RemovedRecordExecutor(failure="bootstrap")
    resource = exclusive(executor)
    with pytest.raises(RuntimeError, match="bootstrap failed") as caught:
        resource.acquire(TaskInputs.empty())
    assert any("daemon stop failed" in note for note in caught.value.__notes__)
    assert executor.builders == {}
    with pytest.raises(RuntimeError, match="unconfirmed"):
        resource.release(TaskInputs.empty(), "ordinary-app")
    assert executor.daemon_alive


@pytest.mark.parametrize("cleanup_error", [KeyboardInterrupt, ValueError])
def test_exclusive_cleanup_exception_never_replaces_primary_failure(cleanup_error):
    class InterruptedCleanupExecutor(ExclusiveExecutor):
        @override
        def run(self, task, *, dry_run=False):
            if task.argv[2] == "inspect":
                self.seen.append(task)
                raise cleanup_error("secondary cleanup failure")
            return super().run(task, dry_run=dry_run)

    executor = InterruptedCleanupExecutor(failure="partial-create")
    with pytest.raises(RuntimeError, match="partial create failure") as caught:
        exclusive(executor).acquire(TaskInputs.empty())
    assert executor.builders == {"ordinary-app": ("ordinary-owner",)}
    assert any("secondary cleanup failure" in note for note in caught.value.__notes__)
