"""Forward a remote port to a registry upstream through a systemd socat unit."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks._resource_tasks import resource_of
from sonata_tasks.compensation import best_effort
from sonata_tasks.transfer import RemoteCommandProvider, RemoteOperationResult


def _require_tunnel_result(
    result: RemoteOperationResult,
    *,
    action: str,
    absent_ok: bool,
) -> None:
    return_code = result.return_code
    if not isinstance(return_code, int) or isinstance(return_code, bool):
        raise RuntimeError(f"registry tunnel {action} returned no integer return_code")
    if return_code == 0:
        return
    detail = result.stderr or result.stdout
    absent_markers = ("not loaded", "not found", "does not exist")
    if absent_ok and any(marker in detail.lower() for marker in absent_markers):
        return
    suffix = f": {detail}" if detail else ""
    raise RuntimeError(f"registry tunnel {action} failed (exit {return_code}){suffix}")


def _tunnel_title(
    title: str | None,
    registry_upstream: str | Callable[[], str],
    upstream_port: int,
) -> str:
    if title is not None:
        return title
    if callable(registry_upstream):
        return "Acquire registry tunnel"
    return f"Acquire registry tunnel to {registry_upstream}:{upstream_port}"


def registry_tunnel_resource[RequestT](
    *,
    registry_upstream: str | Callable[[], str],
    provider: RemoteCommandProvider[RequestT],
    request: RequestT,
    unit_name: str,
    listen_port: int,
    upstream_port: int,
    title: str | None = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[None]:
    """Forward ``listen_port`` on the remote host to a registry upstream.

    Acquiring stops and resets any unit left by an earlier run, then starts a
    transient ``systemd-run`` unit named ``unit_name`` that runs ``socat``
    forwarding ``listen_port`` to ``registry_upstream:upstream_port``. A failed
    acquire stops and resets the unit again before re-raising. Releasing stops
    the unit best-effort and always resets it, so a failed stop cannot leave it
    in a failed state.

    ``registry_upstream`` may be a callable, in which case it is resolved at
    acquire time and ``title`` defaults to a generic label.

    Raises:
        ValueError: If ``unit_name`` is empty.

    """
    if not unit_name:
        raise ValueError("unit_name must not be empty")
    stop = ("sudo", "systemctl", "stop", unit_name)
    reset = ("sudo", "systemctl", "reset-failed", unit_name)

    def run(argv: tuple[str, ...], *, action: str, absent_ok: bool = False) -> None:
        result = provider.exec_argv(request, argv)
        _require_tunnel_result(result, action=action, absent_ok=absent_ok)

    def quiet(argv: tuple[str, ...]) -> None:
        with contextlib.suppress(RuntimeError):
            _ = provider.exec_argv(request, argv)

    def stop_and_reset() -> None:
        quiet(stop)
        quiet(reset)

    def acquire(_inputs: TaskInputs) -> None:
        try:
            run(stop, action="acquire", absent_ok=True)
            run(reset, action="acquire", absent_ok=True)
            upstream = (
                registry_upstream()
                if callable(registry_upstream)
                else registry_upstream
            )
            run(
                (
                    "sudo",
                    "systemd-run",
                    "--unit",
                    unit_name,
                    "socat",
                    f"TCP-LISTEN:{listen_port},fork,reuseaddr",
                    f"TCP:{upstream}:{upstream_port}",
                ),
                action="acquire",
            )
        except BaseException as error:
            best_effort(error, stop_and_reset, what="registry tunnel failed acquire")
            raise

    def release(_inputs: TaskInputs, _state: None) -> None:
        try:
            run(stop, action="release", absent_ok=True)
        finally:
            quiet(reset)

    tunnel_title = _tunnel_title(title, registry_upstream, upstream_port)
    return resource_of(
        tunnel_title,
        acquire,
        release,
        requires=requires,
        always_release=True,
    )
