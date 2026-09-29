from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pytest

from sonata_tasks.vm.logged import VmFileFetcher, run_remote_logged
from sonata_tasks.vm.models import VmRequest


@dataclass
class Result:
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Provider:
    result: Result = field(default_factory=Result)
    transfer_result: Result = field(default_factory=Result)
    commands: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)
    transfers: list[tuple[str, Path]] = field(default_factory=list)

    def exec_argv(
        self,
        request: VmRequest,
        argv: tuple[str, ...] | list[str],
        *,
        env: dict[str, str] | None = None,
        remote_dir: str | None = None,
        dry_run: bool = False,
    ) -> Result:
        self.commands.append((tuple(argv), remote_dir))
        return self.result

    def transfer_to(
        self, request: VmRequest, *, source: Path, destination: str
    ) -> Result:
        return Result()

    def transfer_from(
        self, request: VmRequest, *, source: str, destination: Path
    ) -> Result:
        self.transfers.append((source, destination))
        if self.transfer_result.return_code == 0:
            destination.write_text("full remote log")
        return self.transfer_result


def test_vm_file_fetcher_creates_parent_and_checks_result(tmp_path: Path) -> None:
    provider = Provider()
    request = VmRequest(lifecycle="external", host="example.test")
    local = tmp_path / "nested" / "output.log"

    VmFileFetcher(provider, request).fetch_from("/tmp/output.log", local)

    assert local.read_text() == "full remote log"
    assert provider.transfers == [("/tmp/output.log", local)]

    provider.transfer_result = Result(7, stderr="permission denied")
    with pytest.raises(RuntimeError, match="permission denied"):
        VmFileFetcher(provider, request).fetch_from("/tmp/missing", local)


def test_logged_vm_commands_fetch_complete_log_on_success_and_failure(
    tmp_path: Path,
) -> None:
    provider = Provider()
    request = VmRequest(lifecycle="external", host="example.test")
    local = tmp_path / "nested" / "build.log"
    options = {
        "remote_dir": PurePosixPath("/tmp/build"),
        "remote_log": PurePosixPath("/tmp/build/build.log"),
        "local_log": local,
    }

    run_remote_logged(
        provider, request, (("echo", "hello world"), ("false",)), **options
    )

    script = provider.commands[0][0]
    assert script[:2] == ("sh", "-c")
    assert "echo 'hello world' && false" in script[2]
    assert "tail -c 65536" in script[2]
    assert provider.commands[0][1] == "/tmp/build"
    assert provider.transfers == [("/tmp/build/build.log", local)]

    provider.result = Result(1, stderr="failed")
    with pytest.raises(RuntimeError, match="VM command failed"):
        run_remote_logged(provider, request, (("false",),), **options)
    assert len(provider.transfers) == 2


def test_logged_vm_commands_reject_empty_list(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        run_remote_logged(
            Provider(),
            VmRequest(lifecycle="external", host="example.test"),
            (),
            remote_dir=PurePosixPath("/tmp"),
            remote_log=PurePosixPath("/tmp/log"),
            local_log=tmp_path / "log",
        )
