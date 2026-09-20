"""Expose the optional Azure VM provider, or explain the missing extra.

Importing this module without the ``azure`` extra raises a ModuleNotFoundError
that names the extra to install rather than the underlying missing dependency.
"""

try:
    from sonata_tasks.vm.providers.azure import AzureVmFacts, AzureVmProvider
except ModuleNotFoundError as error:
    if error.name in {"azure_vm", "subprocess_toolkit"}:
        raise ModuleNotFoundError(
            "Install sonata-tasks[azure] to use AzureVmProvider"
        ) from error
    raise

__all__ = ["AzureVmFacts", "AzureVmProvider"]
