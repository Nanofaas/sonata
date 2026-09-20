"""Azure VM provider: lifecycle, remote execution, and file transfer."""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure_vm import AzureClient, AzureVM
from azure_vm.exceptions import VmNotFoundError
from subprocess_toolkit.backend import ShellExecutionResult

from sonata_tasks.vm.models import VmRequest, vm_remote_home
from sonata_tasks.vm.results import successful_result
from sonata_tasks.vm.ssh import find_ssh_private_key_path

_MISSING = object()


def _required_value(payload: Mapping[object, object], key: str) -> Any:  # noqa: ANN401
    value = payload.get(key, _MISSING)
    if value is _MISSING:
        raise KeyError(key)
    return value


@dataclass(frozen=True, slots=True)
class AzureVmFacts:
    """The live VM facts read back from Azure.

    Captures where the VM runs, how big it is, and which image it was built
    from, so a run can be reproduced from what was actually provisioned rather
    than what was requested.
    """

    location: str
    vm_size: str
    disk_size_gb: int
    image_urn: str


class AzureVmProvider:
    """Generic Azure VM provider: lifecycle, command execution, file transfer."""

    def __init__(
        self, repo_root: Path, *, remote_project_name: str | None = None
    ) -> None:
        """Store the repository root and the optional remote project name.

        ``_vms`` caches the AzureVM handles this provider has touched, keyed by
        the request attributes that identify one.
        """
        self.repo_root = Path(repo_root)
        self._remote_project_name = remote_project_name
        self._vms: dict[
            tuple[str, str | None, str | None, str, str | None], AzureVM
        ] = {}

    def _client(self, request: VmRequest) -> AzureClient:
        private_key = self.ssh_private_key_path(request)
        return AzureClient(
            resource_group=request.azure_resource_group,
            location=request.azure_location,
            ssh_key_path=str(private_key) if private_key is not None else None,
            ssh_username=request.user,
        )

    def _vm_name(self, request: VmRequest) -> str:
        if not request.name:
            raise ValueError("a managed Azure VM requires an explicit name")
        return request.name

    def _vm_key(
        self, request: VmRequest
    ) -> tuple[str, str | None, str | None, str, str | None]:
        key = self.ssh_private_key_path(request)
        return (
            self._vm_name(request),
            request.azure_resource_group,
            request.azure_location,
            request.user,
            str(key) if key is not None else None,
        )

    def _vm(self, request: VmRequest) -> AzureVM:
        key = self._vm_key(request)
        if key not in self._vms:
            self._vms[key] = self._client(request).get_vm(self._vm_name(request))
        return self._vms[key]

    def _ssh_key(self, request: VmRequest) -> Path | None:
        if request.azure_ssh_key_path:
            return Path(request.azure_ssh_key_path)
        return find_ssh_private_key_path()

    def ssh_private_key_path(self, request: VmRequest) -> Path | None:
        """Return the private key matching the configured SSH key, or None.

        The request may name the public key instead; its private half sits
        beside it under the same stem.
        """
        key = self._ssh_key(request)
        if key is None:
            return None
        # ssh_key_path configures the tofu PUBLIC key (SDK default
        # ~/.ssh/id_rsa.pub); the matching private key sits next to it.
        if key.suffix == ".pub":
            return key.with_suffix("")
        return key

    def remote_home(self, request: VmRequest) -> str:
        """Return the remote user's home directory for the request."""
        return vm_remote_home(request)

    def remote_project_dir(self, request: VmRequest) -> str:
        """Return the project directory under the remote home.

        Raises ValueError when no remote project name was configured.
        """
        if not self._remote_project_name:
            raise ValueError("remote_project_name is required for remote_project_dir")
        return f"{self.remote_home(request)}/{self._remote_project_name}"

    def connection_host(self, request: VmRequest) -> str:
        """Return the VM's public IP address as reported by Azure."""
        return self._vm(request).wait_for_ip()

    def teardown(self, request: VmRequest) -> ShellExecutionResult:
        """Delete the VM and report whether it is really gone from Azure.

        Deletion is driven from the local tofu workspace, so a VM deleted
        outside tofu cannot be destroyed through the SDK. When the VM is still
        present in Azure afterwards -- and therefore still billing -- the
        returned result is a failure describing what is left and how to list
        it; otherwise the result is a success. A VM missing from the tofu
        workspace is not an error by itself.
        """
        name = self._vm_name(request)
        try:
            vm = self._vms.pop(self._vm_key(request), None) or self._client(
                request
            ).get_vm(name)
            vm.close()
            vm.delete()
        except VmNotFoundError:
            # `get_vm` raises this from the LOCAL ~/.azure-vm-sdk/<name> workspace,
            # never from Azure. The SDK can only destroy what it recorded, so a
            # missing workspace means "cannot", not "already gone".
            pass
        if not self._exists_in_azure(request):
            return successful_result(["azure", "delete", name])
        group = request.azure_resource_group or ""
        return ShellExecutionResult(
            command=["azure", "delete", name],
            return_code=1,
            stdout="",
            stderr=(
                f"{name} still exists in Azure (resource group {group}) after "
                f"teardown, so it is still billing. The local tofu workspace "
                f"~/.azure-vm-sdk/{name} is missing or incomplete, and `tofu destroy` "
                f"cannot delete what it did not record. List what is left with: "
                f"az resource list -g {group} "
                f"--query \"[?starts_with(name,'{name}')].id\" -o tsv"
            ),
        )

    def _exists_in_azure(self, request: VmRequest) -> bool:
        process = subprocess.run(
            [
                "az",
                "vm",
                "show",
                "--resource-group",
                request.azure_resource_group or "",
                "--name",
                self._vm_name(request),
                "--query",
                "provisioningState",
                "-o",
                "tsv",
            ],
            capture_output=True,
            text=True,
        )
        if process.returncode == 0:
            return True
        if "ResourceNotFound" in process.stderr:
            return False
        raise RuntimeError(process.stderr or "Azure CLI command failed")

    def ensure_running(self, request: VmRequest) -> ShellExecutionResult:
        """Launch or converge the VM and return the command that was run.

        Whether to launch or re-apply an existing deployment is decided by
        asking Azure, because the local tofu workspace can report a VM that no
        longer exists as running.
        """
        name = self._vm_name(request)
        client = self._client(request)
        parameters = {
            "vm_size": request.azure_vm_size,
            "image_urn": request.azure_image_urn,
            "ssh_key_path": request.azure_ssh_key_path,
            "open_ports": request.azure_open_ports,
            "disk_size_gb": _disk_size_gb(request.disk),
        }
        # The SDK decides "already running" from the local tofu workspace, whose
        # `vm_state` output merely echoes the `desired_state` input variable. A VM
        # deleted outside tofu -- a cleaned resource group, a console delete, an
        # interrupted teardown -- therefore reports RUNNING forever and
        # ensure_running returns without touching Azure. Ask Azure, which is the
        # authority, and force a converging apply when the workspace is lying.
        vm = (
            client.ensure_running(name, **parameters)
            if self._exists_in_azure(request)
            else client.launch(name, **parameters)
        )
        self._vms[self._vm_key(request)] = vm
        return successful_result(["azure", "ensure_running", name])

    def release_vm_facts(self, request: VmRequest) -> AzureVmFacts:
        """Read the deployed VM's location, size, disk, and image from Azure.

        Raises ValueError without a resource group and RuntimeError when the
        CLI call fails or its response is missing fields.
        """
        resource_group = request.azure_resource_group
        if not resource_group:
            raise ValueError("Azure resource group is required")
        payload = self._az_json(
            [
                "az",
                "vm",
                "show",
                "--resource-group",
                resource_group,
                "--name",
                self._vm_name(request),
                "--query",
                (
                    "{location:location,vmSize:hardwareProfile.vmSize,"
                    # az >= 2.88 returns the raw ARM casing (diskSizeGB); older
                    # versions normalized it to diskSizeGb. Accept both.
                    "diskSizeGb:storageProfile.osDisk.diskSizeGB || "
                    "storageProfile.osDisk.diskSizeGb,"
                    "imagePublisher:storageProfile.imageReference.publisher,"
                    "imageOffer:storageProfile.imageReference.offer,"
                    "imageSku:storageProfile.imageReference.sku,"
                    "imageVersion:storageProfile.imageReference.version}"
                ),
                "--output",
                "json",
            ]
        )
        if not isinstance(payload, dict):
            raise RuntimeError("Azure VM facts response is invalid")
        try:
            image_urn = ":".join(
                str(_required_value(payload, key))
                for key in ("imagePublisher", "imageOffer", "imageSku", "imageVersion")
            )
            return AzureVmFacts(
                location=str(_required_value(payload, "location")),
                vm_size=str(_required_value(payload, "vmSize")),
                disk_size_gb=int(_required_value(payload, "diskSizeGb")),
                image_urn=image_urn,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("Azure VM facts response is invalid") from error

    def restrict_inbound_sources(
        self,
        request: VmRequest,
        *,
        ports: tuple[int, ...],
        source_cidrs: tuple[str, ...],
        priority_base: int = 1010,
    ) -> None:
        """Allow the given ports from ``source_cidrs`` on the VM's NSG.

        One inbound TCP rule is created per port, named after it with priority
        ascending from ``priority_base``. Source CIDRs are normalized and
        deduplicated first; an unbounded CIDR (``/0``), an empty source list,
        or an out-of-range port raises ValueError, and a rule whose sources
        Azure did not store as requested raises RuntimeError.
        """
        resource_group = request.azure_resource_group
        if not resource_group:
            raise ValueError("Azure resource group is required")
        networks = tuple(
            ipaddress.ip_network(source, strict=False) for source in source_cidrs
        )
        if any(network.prefixlen == 0 for network in networks):
            raise ValueError("Azure NSG source CIDRs must be bounded")
        sources = tuple(dict.fromkeys(map(str, networks)))
        if not sources:
            raise ValueError("at least one Azure NSG source CIDR is required")
        for index, port in enumerate(sorted(set(ports))):
            if not 1 <= port <= 65535:
                raise ValueError(f"invalid Azure NSG port: {port}")
            payload = self._az_json(
                [
                    "az",
                    "network",
                    "nsg",
                    "rule",
                    "create",
                    "--resource-group",
                    resource_group,
                    "--nsg-name",
                    f"{self._vm_name(request)}-nsg",
                    "--name",
                    f"Port{port}",
                    "--priority",
                    str(priority_base + index),
                    "--direction",
                    "Inbound",
                    "--access",
                    "Allow",
                    "--protocol",
                    "Tcp",
                    "--destination-port-ranges",
                    str(port),
                    "--source-address-prefixes",
                    *sources,
                    "--query",
                    # A single source lands in the singular sourceAddressPrefix
                    # field and leaves the plural empty ([] is falsy in JMESPath).
                    "sourceAddressPrefixes || [sourceAddressPrefix]",
                    "--output",
                    "json",
                ]
            )
            if not isinstance(payload, list) or set(map(str, payload)) != set(sources):
                raise RuntimeError("Azure NSG source restriction mismatch")

    @staticmethod
    def _az_json(argv: list[str]) -> object:
        process = subprocess.run(argv, capture_output=True, text=True)
        if process.returncode != 0:
            raise RuntimeError(process.stderr or "Azure CLI command failed")
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("Azure CLI returned invalid JSON") from error

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

        ``remote_dir`` is the working directory of the command on the guest.
        ``dry_run`` is accepted for interface compatibility and has no effect.
        """
        del dry_run
        vm = self._vm(request)
        # The Azure client's `cwd` is the directory on the VM, same meaning.
        result = vm.exec_structured(list(argv), env=env, cwd=remote_dir)
        return ShellExecutionResult(
            command=list(argv),
            return_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    def transfer_to(
        self,
        request: VmRequest,
        *,
        source: Path,
        destination: str,
    ) -> ShellExecutionResult:
        """Copy a local file to the VM and return a successful result."""
        vm = self._vm(request)
        vm.transfer(str(source), destination)
        return successful_result(["scp", str(source), destination])

    def transfer_from(
        self,
        request: VmRequest,
        *,
        source: str,
        destination: Path,
    ) -> ShellExecutionResult:
        """Copy a remote file from the VM to a local destination.

        The copy runs over scp against the VM's public IP, trusting the
        disposable VM's host key; the process result is returned.
        """
        ip = self._vm(request).wait_for_ip()
        key = self.ssh_private_key_path(request)
        cmd: list[str] = ["scp"]
        if key:
            cmd.extend(["-i", str(key)])
        cmd.extend(
            [
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                f"{request.user}@{ip}:{source}",
                str(destination),
            ]
        )
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return ShellExecutionResult(
            command=cmd,
            return_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


def _disk_size_gb(disk: str) -> int:
    match = re.fullmatch(r"([1-9]\d*)G", disk)
    if match is None:
        raise ValueError(f"Azure disk must use whole gibibytes, got {disk!r}")
    return int(match.group(1))
