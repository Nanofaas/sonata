"""Install and uninstall a Helm release as a resource."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.compensation import compensated_resource
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.execution.ports import CommandTaskExecutor


@dataclass(frozen=True, slots=True)
class HelmReleaseSpec:
    """A Helm release to manage.

    ``values`` holds the extra ``--set``/``-f`` arguments passed through
    verbatim, and ``timeout`` bounds how long install and uninstall wait.
    """

    release: str
    chart: str
    namespace: str
    values: tuple[str, ...]
    timeout: str = "5m"


class HelmInstallTask(CommandTask):
    """Install a release, upgrading it if it already exists."""

    def __init__(
        self,
        spec: HelmReleaseSpec,
        *,
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
    ) -> None:
        """Configure ``helm upgrade --install`` for ``spec``.

        The namespace is created if needed and the command waits for the
        release to become ready, up to ``spec.timeout``.
        """
        super().__init__(
            title=f"Install Helm release {spec.release}",
            argv=(
                "helm",
                "upgrade",
                "--install",
                spec.release,
                spec.chart,
                "--namespace",
                spec.namespace,
                "--create-namespace",
                "--wait",
                "--timeout",
                spec.timeout,
                *spec.values,
            ),
            executor=executor,
            role=role,
            options=options,
        )


class HelmUninstallTask(CommandTask):
    """Uninstall a release, tolerating one that is already gone."""

    def __init__(
        self,
        spec: HelmReleaseSpec,
        *,
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
    ) -> None:
        """Configure ``helm uninstall`` for ``spec``, waiting for teardown."""
        super().__init__(
            title=f"Uninstall Helm release {spec.release}",
            argv=(
                "helm",
                "uninstall",
                spec.release,
                "--namespace",
                spec.namespace,
                "--ignore-not-found",
                "--wait",
            ),
            executor=executor,
            role=role,
            options=options,
        )


class _AcquireHelmRelease(Task[HelmReleaseSpec]):
    def __init__(
        self, title: str, spec: HelmReleaseSpec, install: HelmInstallTask
    ) -> None:
        self.title = title
        self._spec = spec
        self._install = install

    @override
    def _fingerprint_payload(self) -> object:
        task = self._install
        return (
            f"{type(task).__module__}.{type(task).__qualname__}",
            task._fingerprint_payload(),
        )

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[HelmReleaseSpec]:
        _ = self._install.run(inputs)
        return TaskOutcome(value=self._spec)


def helm_release_resource(
    spec: HelmReleaseSpec,
    *,
    executor: CommandTaskExecutor,
    role: str = "host",
    options: CommandOptions | None = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[HelmReleaseSpec]:
    """Install ``spec`` and return it on acquire, uninstalling it on failure.

    Acquiring runs :class:`HelmInstallTask` and yields the spec; a failed
    acquire compensates by running :class:`HelmUninstallTask`.
    """
    install = HelmInstallTask(spec, executor=executor, role=role, options=options)
    uninstall = HelmUninstallTask(spec, executor=executor, role=role, options=options)

    title = f"Acquire Helm release {spec.release}"
    return compensated_resource(
        title=title,
        acquire=_AcquireHelmRelease(title, spec, install),
        compensate=uninstall,
        requires=requires,
    )
