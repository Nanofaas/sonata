from dataclasses import dataclass, field, replace
from typing import override

import pytest

from sonata_engine import TaskInputs
from sonata_tasks.registry_tunnel import registry_tunnel_resource

TUNNEL_OPTIONS = {
    "unit_name": "registry-tunnel",
    "listen_port": 5000,
    "upstream_port": 5000,
}


@dataclass(frozen=True)
class _Result:
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


def _result(*, return_code: int = 0, stdout: str = "", stderr: str = "") -> _Result:
    return _Result(return_code=return_code, stdout=stdout, stderr=stderr)


@dataclass
class _RecordingProvider:
    calls: list[tuple[object, tuple[str, ...]]] = field(default_factory=list)
    fail_on: int | None = None

    def exec_argv(self, request: object, argv: tuple[str, ...]) -> _Result:
        self.calls.append((request, argv))
        if self.fail_on is not None and len(self.calls) - 1 == self.fail_on:
            return _result(return_code=1, stderr="command failed")
        return _result(return_code=0)


def test_acquire_runs_socat_tunnel_with_upstream_host():
    provider = _RecordingProvider()
    request = object()
    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.42",
        provider=provider,
        request=request,
        **TUNNEL_OPTIONS,
    )

    resource.acquire.run(TaskInputs.empty())

    assert len(provider.calls) == 3
    assert all(seen_request is request for seen_request, _argv in provider.calls)
    assert provider.calls[0][1] == ("sudo", "systemctl", "stop", "registry-tunnel")
    assert provider.calls[1][1] == (
        "sudo",
        "systemctl",
        "reset-failed",
        "registry-tunnel",
    )
    assert provider.calls[2][1] == (
        "sudo",
        "systemd-run",
        "--unit",
        "registry-tunnel",
        "socat",
        "TCP-LISTEN:5000,fork,reuseaddr",
        "TCP:10.0.0.42:5000",
    )
    assert resource.always_release is True


def test_acquire_tolerates_an_absent_previous_transient_unit():
    class Provider(_RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> _Result:
            self.calls.append((request, argv))
            if argv[:2] == ("sudo", "systemctl"):
                return _result(
                    return_code=5, stderr="Unit nanofaas-registry-tunnel not loaded"
                )
            return _result()

    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.42",
        provider=Provider(),
        request=object(),
        **TUNNEL_OPTIONS,
    )

    resource.acquire.run(TaskInputs.empty())


def test_release_stops_the_tunnel():
    provider = _RecordingProvider()
    resource = registry_tunnel_resource(
        registry_upstream="stack-registry.nanofaas.svc.cluster.local",
        provider=provider,
        request=object(),
        **TUNNEL_OPTIONS,
    )

    resource.acquire.run(TaskInputs.empty())
    resource.release.run(replace(TaskInputs.empty(), _upstream=None))

    assert len(provider.calls) == 5
    _req, argv = provider.calls[3]
    assert "systemctl" in argv
    assert "stop" in argv
    assert "registry-tunnel" in argv


@pytest.mark.parametrize(("fail_on", "calls"), [(0, 3), (1, 4), (2, 5)])
def test_acquire_raises_and_compensates_on_each_failed_step(fail_on: int, calls: int):
    provider = _RecordingProvider(fail_on=fail_on)
    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=provider,
        request=object(),
        **TUNNEL_OPTIONS,
    )

    with pytest.raises(RuntimeError, match="registry tunnel acquire failed"):
        resource.acquire.run(TaskInputs.empty())

    assert len(provider.calls) == calls
    assert provider.calls[-2][1] == ("sudo", "systemctl", "stop", "registry-tunnel")
    assert provider.calls[-1][1] == (
        "sudo",
        "systemctl",
        "reset-failed",
        "registry-tunnel",
    )


def test_release_raises_on_provider_failure():
    provider = _RecordingProvider(fail_on=3)
    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=provider,
        request=object(),
        **TUNNEL_OPTIONS,
    )

    resource.acquire.run(TaskInputs.empty())
    with pytest.raises(RuntimeError, match="registry tunnel release failed"):
        resource.release.run(replace(TaskInputs.empty(), _upstream=None))


def test_failed_acquire_propagates_programming_errors_from_cleanup():
    class BrokenProvider(_RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> _Result:
            self.calls.append((request, argv))
            if len(self.calls) == 1:
                return _result(return_code=1, stderr="command failed")
            raise ValueError("bad provider contract")

    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=BrokenProvider(),
        request=object(),
        **TUNNEL_OPTIONS,
    )

    with pytest.raises(ValueError, match="bad provider contract"):
        resource.acquire.run(TaskInputs.empty())


def test_failed_acquire_ignores_operational_cleanup_errors():
    class FailingCleanupProvider(_RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> _Result:
            self.calls.append((request, argv))
            if len(self.calls) == 1:
                return _result(return_code=1, stderr="command failed")
            raise RuntimeError("cleanup unavailable")

    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=FailingCleanupProvider(),
        request=object(),
        **TUNNEL_OPTIONS,
    )

    with pytest.raises(RuntimeError, match="registry tunnel acquire failed"):
        resource.acquire.run(TaskInputs.empty())


def test_release_ignores_operational_reset_error():
    class FailingResetProvider(_RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> _Result:
            self.calls.append((request, argv))
            if argv == ("sudo", "systemctl", "reset-failed", "registry-tunnel"):
                raise RuntimeError("reset unavailable")
            return _result()

    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=FailingResetProvider(),
        request=object(),
        **TUNNEL_OPTIONS,
    )

    resource.release.run(replace(TaskInputs.empty(), _upstream=None))


def test_release_propagates_programming_error_from_reset():
    class BrokenResetProvider(_RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> _Result:
            self.calls.append((request, argv))
            if argv == ("sudo", "systemctl", "reset-failed", "registry-tunnel"):
                raise ValueError("bad reset contract")
            return _result()

    resource = registry_tunnel_resource(
        registry_upstream="10.0.0.1",
        provider=BrokenResetProvider(),
        request=object(),
        **TUNNEL_OPTIONS,
    )

    with pytest.raises(ValueError, match="bad reset contract"):
        resource.release.run(replace(TaskInputs.empty(), _upstream=None))
