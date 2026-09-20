"""Multipass VM provider: lifecycle, remote execution, and file transfer."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import cast

from multipass_vm_sdk import (
    MultipassClient,
    MultipassCommandError,
    VmNotFoundError,
    find_ssh_public_key,
)
from subprocess_toolkit.backend import (
    ShellBackend,
    ShellExecutionResult,
    SubprocessShell,
)

from sonata_tasks.vm.models import VmRequest, vm_remote_home
from sonata_tasks.vm.results import successful_result
from sonata_tasks.vm.ssh import find_ssh_private_key_path

_SSH_CREDENTIALS_UNRESOLVED = object()


def _vm_name_default(request: VmRequest) -> str:
    if not request.name:
        raise ValueError("a managed Multipass VM requires an explicit name")
    return request.name


def _sdk_error(e: MultipassCommandError) -> ShellExecutionResult:
    return ShellExecutionResult(
        command=e.args_list,
        return_code=e.returncode,
        stdout=e.stdout,
        stderr=e.stderr,
    )


def resolve_connection_host(
    request: VmRequest,
    client: MultipassClient,
    *,
    dry_run: bool = False,
) -> str:
    """Resolve the host a caller should connect to for this request.

    Returns the request's explicit host for the external lifecycle, a
    placeholder during a dry run, and otherwise the VM's first IPv4 address.
    Raises RuntimeError when the VM is unknown or has no IPv4 address.
    """
    if request.lifecycle == "external":
        if not request.host:
            raise RuntimeError("external VM lifecycle requires a host")
        return request.host
    if dry_run:
        return f"<multipass-ip:{_vm_name_default(request)}>"
    try:
        info = client.get_vm(_vm_name_default(request)).info()
    except VmNotFoundError as error:
        raise RuntimeError(
            f"Unable to resolve Multipass VM '{_vm_name_default(request)}'"
        ) from error
    if info.ipv4:
        return info.ipv4[0]
    raise RuntimeError(
        f"Multipass VM '{_vm_name_default(request)}' has no IPv4 address"
    )


class MultipassVmProvider:
    """Generic multipass VM provider: lifecycle, command execution, file transfer.

    Product-specific orchestration can compose or subclass this provider.
    Takes workspace_root directly — no ToolPaths dependency.
    """

    def __init__(
        self,
        workspace_root: Path,
        shell: ShellBackend | None = None,
        multipass_client: MultipassClient | None = None,
    ) -> None:
        """Store the workspace root and the shell and Multipass clients.

        ``shell`` and ``multipass_client`` default to a SubprocessShell and a
        fresh MultipassClient. SSH credentials are discovered lazily on first
        use.
        """
        self.workspace_root = Path(workspace_root)
        self.shell = shell or SubprocessShell()
        self._client = multipass_client or MultipassClient()
        self._ssh_public_key: str | object | None = _SSH_CREDENTIALS_UNRESOLVED
        self._private_key_path: Path | None = None

    def _ssh_credentials(self) -> tuple[str | None, Path | None]:
        if self._ssh_public_key is _SSH_CREDENTIALS_UNRESOLVED:
            public_key = find_ssh_public_key()
            self._ssh_public_key = public_key
            self._private_key_path = find_ssh_private_key_path(public_key)
        return cast(str | None, self._ssh_public_key), self._private_key_path

    def _vm_name(self, request: VmRequest) -> str:
        return _vm_name_default(request)

    def _remote_home(self, request: VmRequest) -> str:
        return vm_remote_home(request)

    def _shell_run(
        self, command: list[str], *, dry_run: bool = False
    ) -> ShellExecutionResult:
        # cwd here is the *local* working directory of the ssh/scp process, which
        # is not where the caller stands. Any local path in `command` must
        # therefore already be absolute — see _local_path — or it silently
        # resolves against the workspace instead of against the caller.
        return self.shell.run(command, cwd=self.workspace_root, dry_run=dry_run)

    def _ssh_options(self) -> list[str]:
        _, private_key = self._ssh_credentials()
        options = [
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
        if private_key is not None:
            options.extend(("-i", str(private_key)))
        return options

    def _ssh_target(self, request: VmRequest, *, dry_run: bool) -> str:
        return f"{request.user}@{self.connection_host(request, dry_run=dry_run)}"

    @staticmethod
    def _build_exec_script(
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        remote_dir: str | None = None,
    ) -> str:
        parts: list[str] = []
        if remote_dir:
            parts.append(f"cd {shlex.quote(remote_dir)}")
        for k, v in (env or {}).items():
            parts.append(f"export {k}={shlex.quote(v)}")
        parts.append(shlex.join(list(argv)))
        return " && ".join(parts)

    def vm_name(self, request: VmRequest) -> str:
        """Return the VM name, raising ValueError when the request has none."""
        return self._vm_name(request)

    def remote_home(self, request: VmRequest) -> str:
        """Return the remote user's home directory for the request."""
        return self._remote_home(request)

    def resolve_multipass_ipv4(
        self, request: VmRequest, *, dry_run: bool = False
    ) -> str:
        """Return the VM's IPv4 address, or a placeholder during a dry run."""
        return resolve_connection_host(request, self._client, dry_run=dry_run)

    def connection_host(self, request: VmRequest, *, dry_run: bool = False) -> str:
        """Return the host to use for SSH connections to the VM."""
        return resolve_connection_host(request, self._client, dry_run=dry_run)

    def ensure_running(
        self, request: VmRequest, *, dry_run: bool = False
    ) -> ShellExecutionResult:
        """Start or create the VM and return the command that was run.

        The external lifecycle only probes SSH. A dry run returns the
        multipass launch command without contacting Multipass, and otherwise
        the VM is created (or left running) through the SDK.
        """
        if request.lifecycle == "external":
            return self._shell_run(
                ["ssh", f"{request.user}@{request.host}", "true"], dry_run=dry_run
            )
        name = self._vm_name(request)
        launch_cmd = [
            "multipass",
            "launch",
            "--name",
            name,
            "--cpus",
            str(request.cpus),
            "--memory",
            request.memory,
            "--disk",
            request.disk,
        ]
        if dry_run:
            return successful_result(launch_cmd)
        public_key, _ = self._ssh_credentials()
        cloud_init_config = (
            {"ssh_authorized_keys": [public_key]} if public_key else None
        )
        self._client.ensure_running(
            name,
            cpus=request.cpus,
            memory=request.memory,
            disk=request.disk,
            cloud_init_config=cloud_init_config,
        )
        return successful_result(launch_cmd)

    def teardown(
        self, request: VmRequest, *, dry_run: bool = False
    ) -> ShellExecutionResult:
        """Delete the VM permanently, leaving external-lifecycle VMs untouched.

        ``multipass delete`` without ``--purge`` only marks the instance as
        deleted: it stays in ``multipass list`` and keeps its disk until a purge,
        which is why asking for a plain delete here reported success while the
        VM was still on the host. ``--purge`` on the delete targets this instance
        only -- the system-wide ``multipass purge`` would take every soft-deleted
        instance with it, including ones another caller deleted to recover.

        A dry run returns the multipass delete command without contacting
        Multipass. SDK errors are returned as a failed result rather than
        raised, and a VM that is already gone counts as success.
        """
        if request.lifecycle == "external":
            return self._shell_run(
                ["echo", "Skipping teardown for external VM lifecycle"], dry_run=dry_run
            )
        name = self._vm_name(request)
        delete_cmd = ["multipass", "delete", "--purge", name]
        if dry_run:
            return successful_result(delete_cmd)
        try:
            self._client.get_vm(name).delete(purge=True)
        except (VmNotFoundError, MultipassCommandError) as e:
            if isinstance(e, MultipassCommandError):
                return _sdk_error(e)
        return successful_result(delete_cmd)

    def inspect(
        self, request: VmRequest, *, dry_run: bool = False
    ) -> ShellExecutionResult:
        """Return a ``multipass info`` result describing the VM's state.

        The stdout carries the VM's name, state, and IPv4 addresses; external
        VMs report their hostname over SSH instead. SDK errors come back as a
        failed result.
        """
        if request.lifecycle == "external":
            return self._shell_run(
                ["ssh", f"{request.user}@{request.host}", "hostname"], dry_run=dry_run
            )
        name = self._vm_name(request)
        if dry_run:
            return successful_result(["multipass", "info", name])
        try:
            info = self._client.get_vm(name).info()
            stdout = (
                f"Name:  {info.name}\n"
                f"State: {info.state.value}\n"
                f"IPv4:  {', '.join(info.ipv4) or '-'}\n"
            )
            return successful_result(["multipass", "info", name], stdout=stdout)
        except MultipassCommandError as e:
            return _sdk_error(e)

    def exec_argv(
        self,
        request: VmRequest,
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        remote_dir: str | None = None,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Run ``argv`` on the VM and return the command's result.

        ``env`` and ``remote_dir`` are folded into a single shell command, so
        their values are quoted and the remote directory, if any, is entered
        before the command runs.
        """
        command = self._build_exec_script(argv, env=env, remote_dir=remote_dir)
        return self.remote_exec(request, command=command, dry_run=dry_run)

    def remote_exec(
        self,
        request: VmRequest,
        *,
        command: str,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Run a shell command string on the VM over SSH and return its result."""
        return self._shell_run(
            [
                "ssh",
                *self._ssh_options(),
                self._ssh_target(request, dry_run=dry_run),
                shlex.join(("bash", "-lc", command)),
            ],
            dry_run=dry_run,
        )

    @staticmethod
    def _local_path(path: Path) -> str:
        """Return an absolute local path for scp to read.

        scp runs from the workspace, not from wherever the caller stands, so a
        relative path would quietly mean a different file. It cost a silent bug:
        load-test results "transferred" successfully into the checkout under
        test, named after the run directory, while the run directory stayed
        empty.
        """
        return str(Path(path).resolve())

    def transfer_to(
        self,
        request: VmRequest,
        *,
        source: Path,
        destination: str,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Copy a local file to the VM and return the scp result.

        The local source is made absolute so scp reads the file the caller
        meant regardless of the workspace scp runs from.
        """
        return self._shell_run(
            [
                "scp",
                *self._ssh_options(),
                self._local_path(source),
                f"{self._ssh_target(request, dry_run=dry_run)}:{destination}",
            ],
            dry_run=dry_run,
        )

    def transfer_from(
        self,
        request: VmRequest,
        *,
        source: str,
        destination: Path,
        dry_run: bool = False,
    ) -> ShellExecutionResult:
        """Copy a remote file from the VM to a local destination.

        The local destination is made absolute so scp writes where the caller
        meant, and the scp result is returned.
        """
        return self._shell_run(
            [
                "scp",
                *self._ssh_options(),
                f"{self._ssh_target(request, dry_run=dry_run)}:{source}",
                self._local_path(destination),
            ],
            dry_run=dry_run,
        )
