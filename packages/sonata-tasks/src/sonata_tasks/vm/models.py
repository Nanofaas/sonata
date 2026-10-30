"""Value objects describing the VM a task should run on and how to reach it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator


@dataclass(frozen=True, slots=True)
class VmConfig:
    """Sizing and naming requested for a VM to be created."""

    name: str
    cpus: int = 2
    memory: str = "2G"
    disk: str = "20G"


@dataclass(frozen=True, slots=True)
class VmInfo:
    """Connection details for a VM that exists and can be reached."""

    name: str
    host: str
    user: str
    home: str


VmLifecycle = Literal["multipass", "external", "azure", "proxmox"]


class VmRequest(BaseModel):
    """Validated request for a VM, carrying per-lifecycle connection settings.

    The ``lifecycle`` field selects which of the provider-specific fields below
    are meaningful.
    """

    lifecycle: VmLifecycle
    name: str | None = None
    host: str | None = None
    user: str = "ubuntu"
    home: str | None = None
    cpus: int = 4
    memory: str = "12G"
    disk: str = "30G"
    azure_vm_size: str = "Standard_D4s_v5"
    azure_resource_group: str | None = None
    azure_location: str | None = None
    azure_image_urn: str | None = None
    azure_ssh_key_path: str | None = None
    azure_open_ports: tuple[int, ...] | None = None
    azure_priority: Literal["Regular", "Spot"] = "Regular"
    azure_eviction_policy: Literal["Delete", "Deallocate"] | None = None
    azure_max_bid_price: float | None = Field(
        default=None, strict=True, allow_inf_nan=False
    )
    proxmox_host: str | None = None
    proxmox_node: str | None = None
    proxmox_user: str | None = None
    proxmox_password: str | None = None
    proxmox_template_id: int | None = None
    proxmox_ssh_key_path: str | None = None

    @model_validator(mode="after")
    def validate_lifecycle_requirements(self) -> Self:
        """Reject requests whose lifecycle is missing the fields it needs.

        Returns the request unchanged when valid, so it can be used as a model
        validator.
        """
        if self.lifecycle == "external" and not self.host:
            raise ValueError("host is required for external lifecycle")
        if self.azure_priority != "Spot" and (
            self.azure_eviction_policy is not None
            or self.azure_max_bid_price is not None
        ):
            raise ValueError("eviction_policy and max_bid_price require Spot priority")
        price = self.azure_max_bid_price
        if price is not None and price != -1 and price < 0:
            raise ValueError("max_bid_price must be -1 or non-negative")
        return self


def vm_remote_home(request: VmRequest) -> str:
    """Return the remote home directory to use for a request.

    Falls back to the conventional ``/root`` or ``/home/<user>`` path when the
    request does not name one explicitly.
    """
    if request.home:
        return request.home
    return "/root" if request.user == "root" else f"/home/{request.user}"
