"""Executors that delegate commands to an injected host or VM runner."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from sonata_tasks.core.fingerprint import semantic_key
from sonata_tasks.errors import UnsupportedCommandOptionError
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult
from sonata_tasks.execution.ports import HostCommandRunner, VmCommandRunner


def _result(
    task: CommandTaskSpec, return_code: int, stdout: str, stderr: str
) -> TaskResult:
    expected = task.options.expected_exit_codes
    return TaskResult(
        task_id=task.task_id,
        status="passed" if return_code in expected else "failed",
        return_code=return_code,
        expected_exit_codes=expected,
        stdout=stdout,
        stderr=stderr,
    )


class HostCommandTaskExecutor:
    """Runs host commands through an injected runner and scores the result.

    Options the runner cannot honour are rejected before it is called, so an
    unsupported ``remote_dir`` or ``timeout_seconds`` fails without side
    effects.
    """

    def __init__(self, runner: HostCommandRunner, *, target_key: str = "local") -> None:
        """Store ``runner`` and the binding key reported for every role.

        Raises:
            ValueError: If ``target_key`` is empty.

        """
        if not target_key:
            raise ValueError("target_key must not be empty")
        self._runner = runner
        self._target_key = target_key

    def binding_key(self, role: str) -> str:
        """Return the configured target key, ignoring ``role``.

        All tasks on a host executor share one destination, so there is
        nothing for the role to change.
        """
        del role
        return self._target_key

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        """Run ``task`` on the host and classify its exit code.

        Raises:
            UnsupportedCommandOptionError: If ``remote_dir`` or
                ``timeout_seconds`` is set, which the host runner cannot
                honour.

        """
        options = task.options
        if options.remote_dir is not None:
            raise UnsupportedCommandOptionError(
                "the host executor does not support remote_dir"
            )
        if options.timeout_seconds is not None:
            raise UnsupportedCommandOptionError(
                "the injected host runner does not support timeout_seconds"
            )
        result = self._runner.run(
            list(task.argv), cwd=options.cwd, env=dict(options.env), dry_run=dry_run
        )
        return _result(task, result.return_code, result.stdout, result.stderr)


class VmCommandTaskExecutor:
    """Runs VM commands through an injected runner and scores the result.

    Paired project roots enable local ``cwd`` translation to a POSIX remote
    directory. Without roots, local ``cwd`` remains unsupported. The runner
    cannot honour ``timeout_seconds``.
    """

    def __init__(
        self,
        runner: VmCommandRunner,
        *,
        target_key: str,
        local_root: Path | None = None,
        remote_root: str | None = None,
    ) -> None:
        """Store the runner, target and optional project-directory mapping.

        Roots must be supplied together. The local root is resolved; the
        remote root is interpreted as POSIX without remote canonicalization.
        Relative remote roots retain the injected runner's interpretation.

        Raises:
            ValueError: If the target or remote root is empty, or only one
                mapping root is supplied.

        """
        if not target_key:
            raise ValueError("target_key must not be empty")
        if (local_root is None) != (remote_root is None):
            raise ValueError("local_root and remote_root must be supplied together")
        if remote_root == "":
            raise ValueError("remote_root must not be empty")
        self._runner = runner
        self._local_root = local_root.resolve() if local_root is not None else None
        self._remote_root = (
            PurePosixPath(remote_root) if remote_root is not None else None
        )
        self._target_key = (
            semantic_key(
                "vm-project",
                {
                    "target": target_key,
                    "local_root": self._local_root,
                    "remote_root": str(self._remote_root),
                },
            )
            if self._local_root is not None
            else target_key
        )

    def binding_key(self, role: str) -> str:
        """Return the target and mapping identity, ignoring ``role``.

        All tasks on a VM executor share one destination, so there is nothing
        for the role to change.
        """
        del role
        return self._target_key

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        """Run ``task`` in the target VM and classify its exit code.

        Raises:
            UnsupportedCommandOptionError: If ``cwd`` is set without mapping
                roots, or ``timeout_seconds`` is set.
            ValueError: If both directory options are set, or resolved ``cwd``
                escapes the local project root.

        """
        options = task.options
        remote_dir = options.remote_dir
        if options.cwd is not None:
            if self._local_root is None or self._remote_root is None:
                raise UnsupportedCommandOptionError(
                    "the VM executor does not support local cwd"
                )
            if remote_dir is not None:
                raise ValueError(
                    "a remote command cannot declare both cwd and remote_dir"
                )
            local = (
                options.cwd
                if options.cwd.is_absolute()
                else self._local_root / options.cwd
            )
            try:
                relative = local.resolve().relative_to(self._local_root)
            except ValueError as error:
                raise ValueError(
                    f"remote command cwd {local} is outside project root "
                    f"{self._local_root}"
                ) from error
            remote_dir = str(self._remote_root.joinpath(*relative.parts))
        if options.timeout_seconds is not None:
            raise UnsupportedCommandOptionError(
                "the injected VM runner does not support timeout_seconds"
            )
        result = self._runner.run_vm_command(
            task.argv,
            env=dict(options.env),
            remote_dir=remote_dir,
            dry_run=dry_run,
        )
        return _result(task, result.return_code, result.stdout, result.stderr)
