"""Tests for AzureVmProvider."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.providers.azure import AzureVmProvider


def _make_provider() -> AzureVmProvider:
    return AzureVmProvider(repo_root=Path("/repo"), remote_project_name="project")


def _make_request(**kwargs: Any) -> VmRequest:
    defaults: dict[str, Any] = {
        "lifecycle": "azure",
        "name": "test-vm",
        "user": "ubuntu",
        "azure_resource_group": "rg-test",
        "azure_location": "eastus",
        "azure_ssh_key_path": "/home/user/.ssh/id_ed25519",
    }
    defaults.update(kwargs)
    return VmRequest(**defaults)


def _make_azure_client_mock() -> tuple[MagicMock, MagicMock]:
    client = MagicMock()
    vm = MagicMock()
    vm.wait_for_ip.return_value = "10.0.0.1"
    client.get_vm.return_value = vm
    return client, vm


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_remote_home_default(mock_client_cls) -> None:
    provider = _make_provider()
    req = _make_request(user="ubuntu")
    assert provider.remote_home(req) == "/home/ubuntu"


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_remote_home_root(mock_client_cls) -> None:
    provider = _make_provider()
    req = _make_request(user="root")
    assert provider.remote_home(req) == "/root"


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_remote_home_custom(mock_client_cls) -> None:
    provider = _make_provider()
    req = _make_request(home="/custom/home")
    assert provider.remote_home(req) == "/custom/home"


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_remote_project_dir(mock_client_cls) -> None:
    provider = _make_provider()
    req = _make_request(user="ubuntu")
    assert provider.remote_project_dir(req) == "/home/ubuntu/project"


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_vm_name_uses_name_field(mock_client_cls) -> None:
    provider = _make_provider()
    req = _make_request(name="custom-vm")
    assert provider._vm_name(req) == "custom-vm"


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_vm_name_default(mock_client_cls) -> None:
    provider = _make_provider()
    req = VmRequest(lifecycle="azure", azure_resource_group="rg")
    with pytest.raises(ValueError, match="explicit name"):
        provider._vm_name(req)


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_ssh_key_from_request(mock_client_cls) -> None:
    provider = _make_provider()
    req = _make_request(azure_ssh_key_path="/home/user/.ssh/id_ed25519")
    key = provider._ssh_key(req)
    assert key == Path("/home/user/.ssh/id_ed25519")


@patch("sonata_tasks.vm.providers.azure.AzureClient")
@patch(
    "sonata_tasks.vm.providers.azure.find_ssh_private_key_path",
    return_value=Path("/home/user/.ssh/id_rsa"),
)
def test_ssh_key_fallback(mock_find, mock_client_cls) -> None:
    provider = _make_provider()
    req = _make_request(azure_ssh_key_path=None)
    key = provider._ssh_key(req)
    assert key == Path("/home/user/.ssh/id_rsa")


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_ssh_private_key_path_public(mock_client_cls, tmp_path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("x")
    provider = _make_provider()
    req = _make_request(azure_ssh_key_path=str(key))
    assert provider.ssh_private_key_path(req) == key


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_ssh_private_key_path_strips_pub_suffix(mock_client_cls, tmp_path) -> None:
    # ssh_key_path configures the tofu PUBLIC key; ansible/scp need the
    # matching private key next to it.
    provider = _make_provider()
    req = _make_request(azure_ssh_key_path=str(tmp_path / "id_rsa.pub"))
    assert provider.ssh_private_key_path(req) == tmp_path / "id_rsa"


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_client_uses_private_key_for_ssh(mock_client_cls, tmp_path) -> None:
    provider = _make_provider()
    req = _make_request(azure_ssh_key_path=str(tmp_path / "id_ed25519.pub"))

    provider._client(req)

    mock_client_cls.assert_called_once_with(
        resource_group="rg-test",
        location="eastus",
        ssh_key_path=str(tmp_path / "id_ed25519"),
        ssh_username="ubuntu",
    )


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_connection_host(mock_client_cls) -> None:
    client_mock, _vm_mock = _make_azure_client_mock()
    mock_client_cls.return_value = client_mock
    provider = _make_provider()
    req = _make_request()
    host = provider.connection_host(req)
    assert host == "10.0.0.1"


@patch(
    "sonata_tasks.vm.providers.azure.AzureVmProvider._exists_in_azure",
    return_value=False,
)
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_teardown_success(mock_client_cls, mock_exists) -> None:
    client_mock, vm_mock = _make_azure_client_mock()
    mock_client_cls.return_value = client_mock
    provider = _make_provider()
    req = _make_request()
    result = provider.teardown(req)
    vm_mock.close.assert_called_once_with()
    vm_mock.delete.assert_called_once()
    assert result.return_code == 0


@patch(
    "sonata_tasks.vm.providers.azure.AzureVmProvider._exists_in_azure",
    return_value=False,
)
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_teardown_vm_not_found_is_ignored(mock_client_cls, mock_exists) -> None:
    from azure_vm.exceptions import VmNotFoundError

    client_mock, vm_mock = _make_azure_client_mock()
    vm_mock.delete.side_effect = VmNotFoundError("gone")
    mock_client_cls.return_value = client_mock
    provider = _make_provider()
    req = _make_request()
    result = provider.teardown(req)
    assert result.return_code == 0


@patch(
    "sonata_tasks.vm.providers.azure.AzureVmProvider._exists_in_azure",
    return_value=True,
)
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_ensure_running(mock_client_cls, mock_exists) -> None:
    client_mock = MagicMock()
    mock_client_cls.return_value = client_mock
    provider = _make_provider()
    req = _make_request()
    result = provider.ensure_running(req)
    client_mock.ensure_running.assert_called_once()
    assert result.return_code == 0


@patch(
    "sonata_tasks.vm.providers.azure.AzureVmProvider._exists_in_azure",
    return_value=True,
)
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_ensure_running_forwards_request_disk_as_gibibytes(
    mock_client_cls, mock_exists
) -> None:
    client_mock = MagicMock()
    mock_client_cls.return_value = client_mock
    provider = _make_provider()

    provider.ensure_running(_make_request(disk="128G"))

    assert client_mock.ensure_running.call_args.kwargs["disk_size_gb"] == 128


@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_release_facts_are_read_from_authoritative_azure_state(mock_run) -> None:
    process = MagicMock()
    process.returncode = 0
    process.stdout = json.dumps(
        {
            "location": "westeurope",
            "vmSize": "Standard_D4s_v5",
            "diskSizeGb": 128,
            "imagePublisher": "Canonical",
            "imageOffer": "ubuntu-24_04-lts",
            "imageSku": "server",
            "imageVersion": "24.04.202505280",
        }
    )
    process.stderr = ""
    mock_run.return_value = process
    provider = _make_provider()
    request = _make_request(
        name="nanofaas-azure-release",
        azure_resource_group="nanofaas-rg",
        azure_location="westeurope",
    )

    facts = provider.release_vm_facts(request)

    assert facts.location == "westeurope"
    assert facts.vm_size == "Standard_D4s_v5"
    assert facts.disk_size_gb == 128
    assert facts.image_urn == ("Canonical:ubuntu-24_04-lts:server:24.04.202505280")
    command = mock_run.call_args.args[0]
    assert command[:6] == [
        "az",
        "vm",
        "show",
        "--resource-group",
        "nanofaas-rg",
        "--name",
    ]
    assert command[6] == "nanofaas-azure-release"
    assert command[-2:] == ["--output", "json"]


@patch.object(
    AzureVmProvider,
    "_attached_nsgs",
    return_value=(("nanofaas-rg", "nanofaas-azure-release-nsg"),),
)
@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_release_nsg_rules_are_restricted_to_explicit_sources(
    mock_run, mock_nsgs
) -> None:
    sources = ("198.51.100.42/32", "203.0.113.0/24")
    process = MagicMock()
    process.returncode = 0
    process.stdout = json.dumps(list(sources))
    process.stderr = ""
    mock_run.return_value = process
    provider = _make_provider()
    request = _make_request(
        name="nanofaas-azure-release",
        azure_resource_group="nanofaas-rg",
    )

    provider.restrict_inbound_sources(
        request,
        ports=(30080, 30081, 30090),
        source_cidrs=sources,
    )

    assert mock_run.call_count == 3
    for index, (call, port) in enumerate(
        zip(mock_run.call_args_list, (30080, 30081, 30090), strict=True)
    ):
        command = call.args[0]
        assert command[:5] == ["az", "network", "nsg", "rule", "create"]
        assert command[command.index("--nsg-name") + 1] == "nanofaas-azure-release-nsg"
        assert command[command.index("--name") + 1] == f"Port{port}"
        assert command[command.index("--destination-port-ranges") + 1] == str(port)
        assert command[command.index("--priority") + 1] == str(1010 + index)
        source_index = command.index("--source-address-prefixes")
        assert tuple(command[source_index + 1 : source_index + 3]) == sources
        assert "*" not in command
        # Single-source rules echo back via the singular field: the query
        # must carry the fallback or the verification always fails.
        assert command[command.index("--query") + 1] == (
            "sourceAddressPrefixes || [sourceAddressPrefix]"
        )


@patch.object(
    AzureVmProvider,
    "_attached_nsgs",
    return_value=(("nanofaas-rg", "nanofaas-azure-release-nsg"),),
)
@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_release_nsg_restriction_fails_closed_on_mismatched_azure_response(
    mock_run,
    mock_nsgs,
) -> None:
    process = MagicMock()
    process.returncode = 0
    process.stdout = json.dumps(["*"])
    process.stderr = ""
    mock_run.return_value = process
    provider = _make_provider()

    with pytest.raises(RuntimeError, match="NSG source restriction mismatch"):
        provider.restrict_inbound_sources(
            _make_request(),
            ports=(30080,),
            source_cidrs=("198.51.100.42/32",),
        )


@pytest.mark.parametrize("source", ["0.0.0.0/0", "::/0"])
@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_release_nsg_restriction_rejects_unbounded_source(
    mock_run, source: str
) -> None:
    provider = _make_provider()

    with pytest.raises(ValueError, match="must be bounded"):
        provider.restrict_inbound_sources(
            _make_request(),
            ports=(30080,),
            source_cidrs=(source,),
        )
    mock_run.assert_not_called()


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_exec_argv(mock_client_cls) -> None:
    client_mock, vm_mock = _make_azure_client_mock()
    exec_result = MagicMock()
    exec_result.returncode = 0
    exec_result.stdout = "output"
    exec_result.stderr = ""
    vm_mock.exec_structured.return_value = exec_result
    mock_client_cls.return_value = client_mock
    provider = _make_provider()
    req = _make_request()
    result = provider.exec_argv(req, ["echo", "hello"])
    assert result.return_code == 0
    assert result.stdout == "output"


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_exec_argv_reuses_the_vm_handle(mock_client_cls) -> None:
    client_mock, vm_mock = _make_azure_client_mock()
    exec_result = MagicMock(returncode=0, stdout="", stderr="")
    vm_mock.exec_structured.return_value = exec_result
    mock_client_cls.return_value = client_mock
    provider = _make_provider()
    req = _make_request()

    provider.exec_argv(req, ["true"])
    provider.exec_argv(req, ["true"])

    client_mock.get_vm.assert_called_once_with("test-vm")


@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_transfer_to(mock_client_cls) -> None:
    client_mock, vm_mock = _make_azure_client_mock()
    mock_client_cls.return_value = client_mock
    provider = _make_provider()
    req = _make_request()
    result = provider.transfer_to(
        req, source=Path("/local/file"), destination="/remote/file"
    )
    vm_mock.transfer.assert_called_once_with("/local/file", "/remote/file")
    assert result.return_code == 0


@patch("sonata_tasks.vm.providers.azure.subprocess.run")
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_transfer_from(mock_client_cls, mock_subproc) -> None:
    client_mock, _vm_mock = _make_azure_client_mock()
    mock_client_cls.return_value = client_mock
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = ""
    proc.stderr = ""
    mock_subproc.return_value = proc
    provider = _make_provider()
    req = _make_request()
    result = provider.transfer_from(
        req, source="/remote/file", destination=Path("/local/file")
    )
    assert result.return_code == 0
    assert "scp" in result.command


@patch("sonata_tasks.vm.providers.azure.subprocess.run")
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_transfer_from_no_ssh_key(mock_client_cls, mock_subproc) -> None:
    client_mock, _vm_mock = _make_azure_client_mock()
    mock_client_cls.return_value = client_mock
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = ""
    proc.stderr = ""
    mock_subproc.return_value = proc
    provider = _make_provider()
    req = _make_request(azure_ssh_key_path=None)
    # patch find_ssh_private_key_path to return None so no -i flag
    with patch(
        "sonata_tasks.vm.providers.azure.find_ssh_private_key_path", return_value=None
    ):
        result = provider.transfer_from(
            req, source="/remote/file", destination=Path("/local")
        )
    assert result.return_code == 0
    assert "-i" not in result.command


@patch("sonata_tasks.vm.providers.azure.subprocess.run")
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_ensure_running_recreates_a_vm_deleted_outside_tofu(
    mock_client_cls, mock_run
) -> None:
    """Recreate the VM when Azure no longer has it, despite a "running" SDK state.

    The SDK reports "running" from the local tofu workspace, whose vm_state
    output only echoes the desired_state variable, so a VM deleted out of band
    looks running forever. Azure is the authority.
    """
    client_mock = MagicMock()
    mock_client_cls.return_value = client_mock
    missing = MagicMock(returncode=1, stdout="", stderr="(ResourceNotFound)")
    mock_run.return_value = missing

    result = _make_provider().ensure_running(_make_request())

    client_mock.launch.assert_called_once()
    client_mock.ensure_running.assert_not_called()
    assert result.return_code == 0


@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_azure_existence_probe_does_not_hide_cli_failures(mock_run) -> None:
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="ERROR: Please run 'az login' to setup account.",
    )

    with pytest.raises(RuntimeError, match="az login"):
        _make_provider()._exists_in_azure(_make_request())


@patch("sonata_tasks.vm.providers.azure.subprocess.run")
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_ensure_running_keeps_the_fast_path_for_a_live_vm(
    mock_client_cls, mock_run
) -> None:
    client_mock = MagicMock()
    mock_client_cls.return_value = client_mock
    mock_run.return_value = MagicMock(returncode=0, stdout='"Succeeded"\n', stderr="")

    _make_provider().ensure_running(_make_request())

    client_mock.ensure_running.assert_called_once()
    client_mock.launch.assert_not_called()


@patch(
    "sonata_tasks.vm.providers.azure.AzureVmProvider._exists_in_azure",
    return_value=True,
)
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_teardown_fails_when_the_vm_survives_in_azure(
    mock_client_cls, mock_exists
) -> None:
    """Fail teardown when Azure still has the VM, even if the local SDK does not.

    `AzureClient.get_vm` raises VmNotFoundError from a check of the LOCAL
    ~/.azure-vm-sdk workspace, never of Azure. Swallowing it reported success
    while the VM kept billing -- on any machine or CI runner without that
    workspace. Azure is the authority.
    """
    from azure_vm.exceptions import VmNotFoundError

    client_mock, vm_mock = _make_azure_client_mock()
    vm_mock.delete.side_effect = VmNotFoundError("no local workspace")
    mock_client_cls.return_value = client_mock

    result = _make_provider().teardown(_make_request(name="stack-vm"))

    assert result.return_code != 0
    assert "stack-vm" in result.stderr
    assert "rg-test" in result.stderr


@patch(
    "sonata_tasks.vm.providers.azure.AzureVmProvider._exists_in_azure",
    return_value=True,
)
@patch("sonata_tasks.vm.providers.azure.AzureClient")
def test_teardown_fails_when_tofu_destroy_left_the_vm_behind(
    mock_client_cls, mock_exists
) -> None:
    """Fail teardown when a clean `tofu destroy` leaves the VM behind in Azure.

    Not only the missing-workspace path: a `tofu destroy` that exits clean but
    leaves the VM must fail too.
    """
    client_mock, vm_mock = _make_azure_client_mock()
    mock_client_cls.return_value = client_mock

    result = _make_provider().teardown(_make_request(name="stack-vm"))

    vm_mock.delete.assert_called_once()
    assert result.return_code != 0


@pytest.mark.parametrize("attachment", ["nic", "subnet", "both"])
@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_ingress_uses_actual_associated_nsgs(mock_run, attachment: str) -> None:
    prefix = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network"
    nic = prefix + "/networkInterfaces/nic"
    subnet = prefix + "/virtualNetworks/net/subnets/default"
    groups = {
        "nic": prefix.replace("/rg/", "/shared/") + "/networkSecurityGroups/reused",
        "subnet": prefix + "/networkSecurityGroups/subnet-policy",
    }
    sources = ["198.51.100.1/32"]

    def run(argv, **kwargs):
        if argv[1:3] == ["vm", "show"]:
            value = [nic]
        elif argv[1:4] == ["network", "nic", "show"]:
            value = {
                "nsg": groups["nic"] if attachment in {"nic", "both"} else None,
                "subnets": [subnet],
            }
        elif argv[1:5] == ["network", "vnet", "subnet", "show"]:
            value = groups["subnet"] if attachment in {"subnet", "both"} else None
        else:
            assert argv[1:5] == ["network", "nsg", "rule", "create"]
            value = sources
        return SimpleNamespace(
            returncode=0, stdout="" if value is None else json.dumps(value), stderr=""
        )

    mock_run.side_effect = run
    _make_provider().restrict_inbound_sources(
        _make_request(), ports=(30080,), source_cidrs=tuple(sources)
    )
    writes = [
        call.args[0] for call in mock_run.call_args_list if "create" in call.args[0]
    ]
    expected = {"nic", "subnet"} if attachment == "both" else {attachment}
    assert {argv[argv.index("--nsg-name") + 1] for argv in writes} == {
        groups[key].rsplit("/", 1)[-1] for key in expected
    }
    for argv in writes:
        name = argv[argv.index("--nsg-name") + 1]
        assert argv[argv.index("--resource-group") + 1] == (
            "shared" if name == "reused" else "rg"
        )


@pytest.mark.parametrize("ports", [(), (0,), (65536,)])
@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_ingress_validates_all_ports_before_azure(mock_run, ports) -> None:
    provider = _make_provider()
    if ports:
        with pytest.raises(ValueError, match="invalid Azure NSG port"):
            provider.restrict_inbound_sources(
                _make_request(), ports=ports, source_cidrs=("198.51.100.1/32",)
            )
    else:
        provider.restrict_inbound_sources(
            _make_request(), ports=ports, source_cidrs=("198.51.100.1/32",)
        )
    mock_run.assert_not_called()


@pytest.mark.parametrize(
    ("response", "message"), [("[]", "no attached NSG"), ("{", "invalid JSON")]
)
@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_ingress_refuses_missing_or_invalid_topology(
    mock_run, response, message
) -> None:
    mock_run.return_value = SimpleNamespace(returncode=0, stdout=response, stderr="")
    with pytest.raises(RuntimeError, match=message):
        _make_provider().restrict_inbound_sources(
            _make_request(), ports=(30080,), source_cidrs=("198.51.100.1/32",)
        )
    assert all("create" not in call.args[0] for call in mock_run.call_args_list)


_ASSOCIATED_NSG_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Network/"
    "networkSecurityGroups/policy"
)


@pytest.mark.parametrize(
    ("details", "subnet_response"),
    [
        ({"nsg": _ASSOCIATED_NSG_ID}, None),
        ({"nsg": _ASSOCIATED_NSG_ID, "subnets": {}}, None),
        ({"nsg": _ASSOCIATED_NSG_ID, "subnets": None}, None),
        (
            {
                "nsg": _ASSOCIATED_NSG_ID.replace(
                    "Microsoft.Network", "Microsoft.Compute"
                ),
                "subnets": [],
            },
            None,
        ),
        ({"nsg": _ASSOCIATED_NSG_ID.replace("/rg/", "//"), "subnets": []}, None),
        ({"nsg": None, "subnets": ["subnet"]}, {}),
    ],
)
@patch("sonata_tasks.vm.providers.azure.subprocess.run")
def test_malformed_associations_never_write_rules(
    mock_run, details, subnet_response
) -> None:
    def run(argv, **kwargs):
        if argv[1:3] == ["vm", "show"]:
            value = ["nic"]
        elif argv[1:4] == ["network", "nic", "show"]:
            value = details
        elif argv[1:5] == ["network", "vnet", "subnet", "show"]:
            value = subnet_response
        else:
            value = ["198.51.100.1/32"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr="")

    mock_run.side_effect = run
    with pytest.raises(RuntimeError, match="invalid"):
        _make_provider().restrict_inbound_sources(
            _make_request(), ports=(30080,), source_cidrs=("198.51.100.1/32",)
        )
    assert all("create" not in call.args[0] for call in mock_run.call_args_list)
