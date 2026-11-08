from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from sonata_engine import TaskInputs
from sonata_tasks.vm import vm_resource
from sonata_tasks.vm.models import VmConfig, VmInfo
from sonata_tasks.vm.tasks import DestroyVm, EnsureVmRunning


@dataclass
class _FakeLifecycle:
    vm_info: VmInfo
    destroyed: list[VmInfo]

    def ensure_running(self, config: VmConfig) -> VmInfo:
        return self.vm_info

    def destroy(self, info: VmInfo) -> None:
        self.destroyed.append(info)


def _make_lifecycle(name: str = "test-vm") -> _FakeLifecycle:
    return _FakeLifecycle(
        vm_info=VmInfo(name=name, host="10.0.0.1", user="ubuntu", home="/home/ubuntu"),
        destroyed=[],
    )


def test_ensure_vm_running_returns_vm_info() -> None:
    lifecycle = _make_lifecycle("my-vm")
    task = EnsureVmRunning(
        task_id="vm.ensure_running",
        title="Ensure VM running",
        lifecycle=lifecycle,
        config=VmConfig(name="my-vm"),
    )
    info = task.run()
    assert info.name == "my-vm"
    assert info.host == "10.0.0.1"


def test_destroy_vm_calls_lifecycle_destroy() -> None:
    lifecycle = _make_lifecycle()
    info = VmInfo(name="my-vm", host="10.0.0.1", user="ubuntu", home="/home/ubuntu")
    task = DestroyVm(
        task_id="vm.destroy", title="Destroy VM", lifecycle=lifecycle, info=info
    )
    task.run()
    assert info in lifecycle.destroyed


def _info(name: str) -> VmInfo:
    return VmInfo(name=name, host="10.0.0.1", user="ubuntu", home="/home/ubuntu")


def test_vm_resource_releases_the_acquired_vm() -> None:
    lifecycle = _make_lifecycle("made")
    resource = vm_resource(
        title="Acquire VM",
        lifecycle=lifecycle,
        config=VmConfig(name="made"),
        fallback_info=_info("fallback"),
    )

    acquired = resource.acquire.run(TaskInputs.empty()).value
    resource.release.run(replace(TaskInputs.empty(), _upstream=acquired))

    assert lifecycle.destroyed == [lifecycle.vm_info]


def test_vm_resource_destroys_the_fallback_when_acquire_fails() -> None:
    lifecycle = _make_lifecycle()

    def fail(_config: VmConfig) -> VmInfo:
        raise RuntimeError("no capacity")

    lifecycle.ensure_running = fail  # type: ignore[method-assign]
    resource = vm_resource(
        title="Acquire VM",
        lifecycle=lifecycle,
        config=VmConfig(name="made"),
        fallback_info=_info("fallback"),
    )

    with pytest.raises(RuntimeError, match="no capacity"):
        resource.acquire.run(TaskInputs.empty())

    assert lifecycle.destroyed == [_info("fallback")]


def test_an_external_vm_is_never_destroyed() -> None:
    lifecycle = _make_lifecycle()
    resource = vm_resource(
        title="Acquire VM",
        lifecycle=lifecycle,
        config=VmConfig(name="made"),
        fallback_info=_info("fallback"),
        external=True,
    )

    acquired = resource.acquire.run(TaskInputs.empty()).value
    resource.release.run(replace(TaskInputs.empty(), _upstream=acquired))

    assert lifecycle.destroyed == []
