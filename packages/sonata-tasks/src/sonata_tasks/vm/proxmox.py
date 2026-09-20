"""Expose the optional Proxmox VM provider, or explain the missing extra.

Importing this module without the ``proxmox`` extra raises a ModuleNotFoundError
that names the extra to install rather than the underlying missing dependency.
"""

try:
    from sonata_tasks.vm.providers.proxmox import ProxmoxVmProvider
except ModuleNotFoundError as error:
    if error.name in {"proxmox_vm_sdk", "subprocess_toolkit"}:
        raise ModuleNotFoundError(
            "Install sonata-tasks[proxmox] to use ProxmoxVmProvider"
        ) from error
    raise

__all__ = ["ProxmoxVmProvider"]
