"""VM lifecycle resources for the task catalogue."""

from __future__ import annotations

from typing import override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.vm.models import VmConfig, VmInfo
from sonata_tasks.vm.ports import VmLifecycleProtocol
from sonata_tasks.vm.tasks import DestroyVm, EnsureVmRunning

__all__ = ["vm_resource"]


class _AcquireVm(Task[VmInfo]):
    def __init__(self, title: str, ensure: EnsureVmRunning) -> None:
        self.title = title
        self._ensure = ensure

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[VmInfo]:
        return TaskOutcome(value=self._ensure.run())


class _DestroyVm(Task[None]):
    """Destroy ``fixed`` if given, else the VM acquire handed back."""

    def __init__(
        self,
        lifecycle: VmLifecycleProtocol,
        external: bool,
        fixed: VmInfo | None = None,
    ) -> None:
        self.title = "Destroy VM" if fixed is None else f"Destroy {fixed.name}"
        self._lifecycle = lifecycle
        self._external = external
        self._fixed = fixed

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        if not self._external:
            info = self._fixed if self._fixed is not None else inputs.upstream()
            DestroyVm(
                f"{info.name}-destroy", f"Destroy {info.name}", self._lifecycle, info
            ).run()
        return TaskOutcome(value=None)


def vm_resource(
    *,
    title: str,
    lifecycle: VmLifecycleProtocol,
    config: VmConfig,
    fallback_info: VmInfo,
    external: bool = False,
) -> Resource[VmInfo]:
    """Build a resource that ensures a VM is running and destroys it afterwards.

    Acquiring the resource starts (or adopts) the VM described by ``config`` and
    yields its connection details. Releasing it destroys that same VM, and a
    failed acquire destroys ``fallback_info`` instead, which names the VM the
    caller expected to already be in place. When ``external`` is set the VM
    outlives the resource, so neither release nor compensation destroys
    anything.
    """
    ensure = EnsureVmRunning(f"{config.name}-ensure", title, lifecycle, config)
    return compensated_resource(
        title=title,
        acquire=_AcquireVm(title, ensure),
        compensate=_DestroyVm(lifecycle, external, fallback_info),
        release=_DestroyVm(lifecycle, external),
        revive=lambda raw: VmInfo(**raw),
    )
