"""Capture bounded VM command output and fetch the complete remote log."""

from __future__ import annotations

import contextlib
import shlex
from pathlib import Path, PurePosixPath

from sonata_tasks.vm.models import VmRequest
from sonata_tasks.vm.ports import VmCommandProvider


class VmFileFetcher:
    """Fetch a file using any VM command provider."""

    def __init__(self, vm: VmCommandProvider, request: VmRequest) -> None:
        """Store the provider and VM request used for transfers."""
        self._vm = vm
        self._request = request

    def fetch_from(self, remote: str, local: Path) -> None:
        """Copy a remote file to ``local``, creating its parent directory."""
        local.parent.mkdir(parents=True, exist_ok=True)
        result = self._vm.transfer_from(self._request, source=remote, destination=local)
        if result.return_code != 0:
            raise RuntimeError(
                result.stderr
                or result.stdout
                or f"transfer failed (exit {result.return_code})"
            )


def run_remote_logged(
    provider: VmCommandProvider,
    request: VmRequest,
    commands: tuple[tuple[str, ...], ...],
    *,
    remote_dir: PurePosixPath,
    remote_log: PurePosixPath,
    local_log: Path,
) -> None:
    """Run VM commands in order, bound returned output, and fetch the full log."""
    if not commands:
        raise ValueError("Remote logged command list is empty")
    script = (
        "{ "
        + " && ".join(shlex.join(argv) for argv in commands)
        + f"; }} > {shlex.quote(str(remote_log))} 2>&1; "
        "result=$?; "
        f"tail -c 65536 {shlex.quote(str(remote_log))}; "
        "exit $result"
    )
    fetcher = VmFileFetcher(provider, request)
    try:
        result = provider.exec_argv(
            request,
            ("sh", "-c", script),
            env=None,
            remote_dir=str(remote_dir),
            dry_run=False,
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"VM command failed ({result.return_code}): sh: "
                f"{result.stderr or result.stdout}"
            )
    except BaseException as error:
        with contextlib.suppress(Exception):
            fetcher.fetch_from(str(remote_log), local_log)
        if not local_log.is_file():
            local_log.parent.mkdir(parents=True, exist_ok=True)
            local_log.write_text(str(error) + "\n")
        raise
    fetcher.fetch_from(str(remote_log), local_log)
