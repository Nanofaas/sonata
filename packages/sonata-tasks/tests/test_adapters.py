from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from sonata_engine import Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.errors import UnsupportedCommandOptionError
from sonata_tasks.execution.adapters import (
    HostCommandTaskExecutor,
    VmCommandTaskExecutor,
)
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec


@dataclass(frozen=True)
class _Result:
    return_code: int = 0
    stdout: str = "ok"
    stderr: str = ""


class _Host:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def binding_key(self, role: str) -> str:
        return f"test:{role}"

    def run(
        self,
        command: list[str],
        /,
        *,
        cwd: Path | None,
        env: dict[str, str],
        dry_run: bool,
    ) -> _Result:
        self.calls.append((command, cwd, env, dry_run))
        return _Result()


class _Vm:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def run_vm_command(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        remote_dir: str | None,
        dry_run: bool,
    ) -> _Result:
        self.calls.append((argv, env, remote_dir, dry_run))
        return _Result()


def test_host_adapter_forwards_supported_options() -> None:
    runner = _Host()
    executor = HostCommandTaskExecutor(runner, target_key="workstation")
    spec = CommandTaskSpec(
        "x",
        "X",
        ("echo", "ok"),
        role="builder",
        options=CommandOptions(cwd=Path("/repo"), env={"A": "B"}),
    )
    assert executor.run(spec, dry_run=True).ok
    assert runner.calls == [(["echo", "ok"], Path("/repo"), {"A": "B"}, True)]
    assert executor.binding_key("builder") == "workstation"


@pytest.mark.parametrize(
    "options",
    [CommandOptions(remote_dir="/srv/app"), CommandOptions(timeout_seconds=1)],
)
def test_host_adapter_rejects_unsupported_options_before_running(
    options: CommandOptions,
) -> None:
    runner = _Host()
    executor = HostCommandTaskExecutor(runner)
    with pytest.raises(UnsupportedCommandOptionError):
        executor.run(CommandTaskSpec("x", "X", ("true",), options=options))
    assert runner.calls == []


def test_vm_adapter_forwards_remote_options() -> None:
    runner = _Vm()
    executor = VmCommandTaskExecutor(runner, target_key="vm:builder")
    options = CommandOptions(env={"A": "B"}, remote_dir="/srv/app")
    spec = CommandTaskSpec("x", "X", ("make",), role="builder", options=options)
    assert executor.run(spec).ok
    assert runner.calls == [(("make",), {"A": "B"}, "/srv/app", False)]


@pytest.mark.parametrize(
    "options",
    [CommandOptions(cwd=Path("/repo")), CommandOptions(timeout_seconds=1)],
)
def test_vm_adapter_rejects_unsupported_options_before_running(
    options: CommandOptions,
) -> None:
    runner = _Vm()
    executor = VmCommandTaskExecutor(runner, target_key="vm:builder")
    with pytest.raises(UnsupportedCommandOptionError):
        executor.run(CommandTaskSpec("x", "X", ("true",), options=options))
    assert runner.calls == []


@pytest.mark.parametrize("remote_root", ["/srv/app with spaces", "work/app"])
@pytest.mark.parametrize("relative", [False, True])
def test_vm_maps_project_cwd_and_preserves_command_options(
    tmp_path: Path, remote_root: str, relative: bool
) -> None:
    runner = _Vm()
    executor = VmCommandTaskExecutor(
        runner, target_key="vm:builder", local_root=tmp_path, remote_root=remote_root
    )
    cwd = Path("deploy/chart") if relative else tmp_path / "deploy/chart"
    options = CommandOptions(
        cwd=cwd, env={"A": "B"}, expected_exit_codes=frozenset({0, 3})
    )
    spec = CommandTaskSpec("check", "Check", ("pwd",), options=options)

    result = executor.run(spec, dry_run=True)

    assert result.task_id == "check"
    assert result.ok
    assert result.expected_exit_codes == {0, 3}
    assert runner.calls == [(("pwd",), {"A": "B"}, f"{remote_root}/deploy/chart", True)]
    assert spec.options.cwd == cwd


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("escape", ["absolute", "parent", "symlink"])
def test_vm_mapping_refuses_cwd_outside_project_before_backend_effects(
    tmp_path: Path, dry_run: bool, escape: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    cwd = {"absolute": outside, "parent": Path("../outside"), "symlink": Path("link")}[
        escape
    ]
    runner = _Vm()
    executor = VmCommandTaskExecutor(
        runner, target_key="vm", local_root=root, remote_root="/srv/app"
    )
    with pytest.raises(ValueError, match="outside project root"):
        executor.run(
            CommandTaskSpec("x", "X", ("pwd",), options=CommandOptions(cwd=cwd)),
            dry_run=dry_run,
        )
    assert runner.calls == []


@pytest.mark.parametrize("remote_dir", [None, "/explicit"])
def test_configured_vm_preserves_commands_without_local_cwd(
    tmp_path: Path, remote_dir: str | None
) -> None:
    runner = _Vm()
    executor = VmCommandTaskExecutor(
        runner, target_key="vm", local_root=tmp_path, remote_root="/srv/app"
    )
    executor.run(
        CommandTaskSpec(
            "x", "X", ("true",), options=CommandOptions(remote_dir=remote_dir)
        )
    )
    assert runner.calls == [(("true",), {}, remote_dir, False)]


def test_vm_mapping_rejects_conflicting_directory_and_unsupported_timeout(
    tmp_path: Path,
) -> None:
    runner = _Vm()
    executor = VmCommandTaskExecutor(
        runner, target_key="vm", local_root=tmp_path, remote_root="/srv/app"
    )
    with pytest.raises(ValueError, match="both cwd and remote_dir"):
        executor.run(
            CommandTaskSpec(
                "x",
                "X",
                ("true",),
                options=CommandOptions(cwd=tmp_path, remote_dir="/other"),
            )
        )
    with pytest.raises(UnsupportedCommandOptionError, match="timeout_seconds"):
        executor.run(
            CommandTaskSpec(
                "x",
                "X",
                ("true",),
                options=CommandOptions(cwd=tmp_path, timeout_seconds=1),
            )
        )
    assert runner.calls == []


@pytest.mark.parametrize(
    ("local_root", "remote_root"),
    [(None, "/srv/app"), (Path("/app"), None), (Path("/app"), "")],
)
def test_vm_mapping_requires_paired_roots(
    local_root: Path | None, remote_root: str | None
) -> None:
    with pytest.raises(ValueError, match="root"):
        VmCommandTaskExecutor(
            _Vm(), target_key="vm", local_root=local_root, remote_root=remote_root
        )


def test_vm_mapping_changes_command_fingerprint_when_roots_or_target_change(
    tmp_path: Path,
) -> None:
    def fingerprint(local_root: Path, remote_root: str, target_key: str = "vm") -> str:
        executor = VmCommandTaskExecutor(
            _Vm(), target_key=target_key, local_root=local_root, remote_root=remote_root
        )
        workflow = Workflow("mapping")
        workflow.add(
            CommandTask(
                title="Check",
                argv=("pwd",),
                executor=executor,
                options=CommandOptions(cwd=Path("deploy")),
            )
        )
        return workflow.compile().fingerprint

    initial = fingerprint(tmp_path, "/srv/app")
    assert initial == fingerprint(tmp_path / ".", "/srv/app/")
    assert initial != fingerprint(tmp_path, "/srv/release")
    assert initial != fingerprint(tmp_path / "other", "/srv/app")
    assert initial != fingerprint(tmp_path, "/srv/app", "other-vm")
    assert (
        VmCommandTaskExecutor(_Vm(), target_key="legacy").binding_key("host")
        == "legacy"
    )
