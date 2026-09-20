"""Expose the optional Multipass VM provider, or explain the missing extra.

Importing this module without the ``multipass`` extra raises a
ModuleNotFoundError that names the extra to install rather than the underlying
missing dependency.
"""

try:
    from sonata_tasks.vm.providers.multipass import (
        MultipassVmProvider,
        resolve_connection_host,
    )
except ModuleNotFoundError as error:
    if error.name in {"multipass_vm_sdk", "subprocess_toolkit"}:
        raise ModuleNotFoundError(
            "Install sonata-tasks[multipass] to use MultipassVmProvider"
        ) from error
    raise


def find_ssh_public_key() -> str:
    """Compatibility hook for callers that patch credential discovery."""
    from multipass_vm_sdk import find_ssh_public_key as discover

    key = discover()
    if key is None:
        raise RuntimeError("Multipass did not expose an SSH public key")
    return key


__all__ = ["MultipassVmProvider", "find_ssh_public_key", "resolve_connection_host"]
