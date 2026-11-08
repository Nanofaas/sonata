from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest
from resource_stubs import acquired, stub_resource

from sonata_engine import TaskInputs
from sonata_tasks.helm import (
    HelmInstallTask,
    HelmReleaseSpec,
    HelmUninstallTask,
    helm_release_resource,
)
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


@dataclass
class RecordingExecutor:
    def binding_key(self, role: str) -> str:
        return f"test-recording:{role}"

    results: list[TaskResult] = field(default_factory=list)
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return (
            self.results.pop(0)
            if self.results
            else TaskResult(task_id="", status="passed", return_code=0)
        )


@pytest.fixture
def spec() -> HelmReleaseSpec:
    return HelmReleaseSpec(
        release="example-service",
        chart="oci://registry.example/charts/service",
        namespace="example",
        values=("--set", "image.tag=v1", "--set", "feature.enabled=true"),
    )


def test_acquire_installs_and_waits_with_values_as_separate_arguments(
    spec: HelmReleaseSpec,
) -> None:
    executor = RecordingExecutor()
    resource = helm_release_resource(spec, executor=executor, role="stack")

    value = acquired(resource)

    assert value is spec
    assert executor.seen[0].argv == (
        "helm",
        "upgrade",
        "--install",
        "example-service",
        "oci://registry.example/charts/service",
        "--namespace",
        "example",
        "--create-namespace",
        "--wait",
        "--timeout",
        "5m",
        "--set",
        "image.tag=v1",
        "--set",
        "feature.enabled=true",
    )
    assert executor.seen[0].execution_role == "stack"


def test_release_uninstalls_the_acquired_release(spec: HelmReleaseSpec) -> None:
    executor = RecordingExecutor()
    resource = helm_release_resource(spec, executor=executor)

    resource.release.run(replace(TaskInputs.empty(), _upstream=spec))

    assert executor.seen[0].argv == (
        "helm",
        "uninstall",
        "example-service",
        "--namespace",
        "example",
        "--ignore-not-found",
        "--wait",
    )


def test_resource_requires_its_vm_and_is_retained(spec: HelmReleaseSpec) -> None:
    vm = stub_resource("Acquire VM")

    resource = helm_release_resource(spec, executor=RecordingExecutor(), requires=(vm,))

    assert resource.requires == (vm,)
    # Retained by `keep`: the platform on the VM is what --keep is for.
    assert resource.always_release is False


def test_failed_acquire_best_effort_uninstalls_before_reraising(
    spec: HelmReleaseSpec,
) -> None:
    failure = TaskResult(
        task_id="", status="failed", return_code=1, stderr="install failed"
    )
    executor = RecordingExecutor(results=[failure])
    resource = helm_release_resource(spec, executor=executor)

    with pytest.raises(RuntimeError, match="install failed"):
        resource.acquire.run(TaskInputs.empty())

    assert executor.seen[1].argv[:3] == ("helm", "uninstall", "example-service")


def test_failed_cleanup_is_noted_without_masking_acquire_failure(
    spec: HelmReleaseSpec,
) -> None:
    failure = TaskResult(
        task_id="", status="failed", return_code=1, stderr="install failed"
    )
    cleanup_failure = TaskResult(
        task_id="", status="failed", return_code=1, stderr="uninstall failed"
    )
    executor = RecordingExecutor(results=[failure, cleanup_failure])
    resource = helm_release_resource(spec, executor=executor)

    with pytest.raises(RuntimeError, match="install failed") as caught:
        resource.acquire.run(TaskInputs.empty())

    assert "uninstall failed" in "\n".join(caught.value.__notes__)


def test_install_task_is_usable_on_its_own(spec: HelmReleaseSpec) -> None:
    """Use the install task alone, without acquiring the release resource.

    A workflow whose goal is to put the platform up wants the task, not the
    resource: Sonata never acquires a resource nothing consumes.
    """
    executor = RecordingExecutor()

    task = HelmInstallTask(spec, executor=executor, role="stack")
    _ = task.run(TaskInputs.empty())

    assert task.title == "Install Helm release example-service"
    assert executor.seen[0].argv[:4] == (
        "helm",
        "upgrade",
        "--install",
        "example-service",
    )
    assert executor.seen[0].execution_role == "stack"


def test_uninstall_task_is_usable_on_its_own(spec: HelmReleaseSpec) -> None:
    executor = RecordingExecutor()

    task = HelmUninstallTask(spec, executor=executor)
    _ = task.run(TaskInputs.empty())

    assert task.title == "Uninstall Helm release example-service"
    assert executor.seen[0].argv[:3] == ("helm", "uninstall", "example-service")
    assert "--ignore-not-found" in executor.seen[0].argv


def test_the_resource_runs_the_very_same_commands_as_the_tasks(
    spec: HelmReleaseSpec,
) -> None:
    """The resource composes the tasks; it must not carry a second copy of the argv."""
    from_resource = RecordingExecutor()
    from_tasks = RecordingExecutor()

    resource = helm_release_resource(spec, executor=from_resource)
    resource.acquire.run(TaskInputs.empty())
    resource.release.run(replace(TaskInputs.empty(), _upstream=spec))

    _ = HelmInstallTask(spec, executor=from_tasks).run(TaskInputs.empty())
    _ = HelmUninstallTask(spec, executor=from_tasks).run(TaskInputs.empty())

    assert [spec.argv for spec in from_resource.seen] == [
        spec.argv for spec in from_tasks.seen
    ]
