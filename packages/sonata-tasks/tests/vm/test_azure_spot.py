"""Spot requests must reach the SDK and never reuse an ordinary VM."""

import json
from unittest.mock import Mock

import pytest
from azure_vm import AzureClient
from azure_vm.testing import CommandResult, FakeBackend

from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.providers.azure import AzureVmProvider


def _request(**overrides):
    return VmRequest(
        **{
            "lifecycle": "azure",
            "name": "spot-test",
            "azure_resource_group": "rg",
            "azure_location": "westeurope",
            "azure_priority": "Spot",
            "azure_eviction_policy": "Deallocate",
            "azure_max_bid_price": -1,
            **overrides,
        }
    )


@pytest.mark.parametrize("exists", [False, True])
def test_spot_settings_reach_real_sdk_workspace(tmp_path, monkeypatch, exists):
    backend = FakeBackend()
    backend.set_default(CommandResult(args=[], returncode=0, stdout="{}", stderr=""))
    client = AzureClient(
        resource_group="rg",
        location="westeurope",
        work_dir=str(tmp_path / "sdk"),
        backend=backend,
    )
    if exists:
        client.launch("spot-test", priority="Spot", eviction_policy="Deallocate")
    provider = AzureVmProvider(tmp_path)
    monkeypatch.setattr(provider, "_client", lambda request: client)
    monkeypatch.setattr(provider, "_exists_in_azure", lambda request: exists)
    monkeypatch.setattr(
        provider,
        "_az_json",
        lambda command: {
            "priority": "Spot",
            "evictionPolicy": "Deallocate",
            "maxPrice": -1,
        },
    )

    assert provider.ensure_running(_request()).return_code == 0

    hcl = (tmp_path / "sdk" / "spot-test" / "main.tf").read_text()
    assert 'priority = "Spot"' in hcl
    assert 'eviction_policy = "Deallocate"' in hcl
    assert "max_bid_price = -1" in hcl


@pytest.mark.parametrize(
    "facts",
    [
        {"priority": "Regular", "evictionPolicy": None, "maxPrice": None},
        {"priority": "Spot", "evictionPolicy": "Delete", "maxPrice": -1},
        {"priority": "Spot", "evictionPolicy": "Deallocate", "maxPrice": 0.1},
        {},
        [],
    ],
)
def test_existing_vm_with_wrong_spot_policy_is_rejected_before_sdk_mutation(
    tmp_path,
    monkeypatch,
    facts,
):
    provider = AzureVmProvider(tmp_path)
    client = Mock()
    monkeypatch.setattr(provider, "_client", lambda request: client)
    monkeypatch.setattr(provider, "_exists_in_azure", lambda request: True)
    # Keep the Azure response parser real.
    monkeypatch.setattr(
        "sonata_tasks.vm.providers.azure.subprocess.run",
        lambda *a, **kw: Mock(returncode=0, stdout=json.dumps(facts), stderr=""),
    )

    with pytest.raises(RuntimeError, match="Spot"):
        provider.ensure_running(_request())
    client.ensure_running.assert_not_called()
    client.launch.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"azure_priority": "Regular"},
        {"azure_priority": "spoot"},
        {"azure_eviction_policy": "Reallocate"},
        {"azure_max_bid_price": -0.5},
        {"azure_max_bid_price": float("inf")},
        {"azure_max_bid_price": float("nan")},
        {"azure_max_bid_price": True},
    ],
)
def test_invalid_spot_request_is_rejected(overrides):
    with pytest.raises(ValueError, match=r"Spot|azure_|max_bid_price"):
        _request(**overrides)
