from __future__ import annotations

import importlib
import os
import shutil
import signal
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import override

import pytest

PAYLOAD = b"synthetic-private\x00\xff\n"


def _api() -> ModuleType:
    return importlib.import_module("sonata_tasks.credentials")


@dataclass
class _Result:
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


class _Provider:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.sources: list[Path] = []
        self.fail: str | None = None
        self.bad_result: object | None = None
        self.bad_at = "mktemp"
        self.output = "/tmp/sonata-credentials.ABC123\n"

    def exec_argv(self, request: object, argv: tuple[str, ...]) -> object:
        assert request == "target"
        self.commands.append(argv)
        phase = argv[0]
        if self.fail == phase:
            raise OSError("synthetic-private provider detail")
        if phase == self.bad_at and self.bad_result is not None:
            return self.bad_result
        return _Result(stdout=self.output if phase == "mktemp" else "")

    def transfer_to(self, request: object, *, source: Path, destination: str) -> object:
        assert request == "target"
        assert destination.startswith(self.output.strip() + "/")
        self.sources.append(source)
        assert source.read_bytes() == PAYLOAD
        assert source.stat().st_mode & 0o777 == 0o600
        assert source.parent.stat().st_mode & 0o777 == 0o700
        if self.fail == "transfer":
            raise RuntimeError("synthetic-private transfer detail")
        if self.bad_at == "transfer" and self.bad_result is not None:
            return self.bad_result
        return _Result()


@pytest.fixture
def private_file(tmp_path: Path) -> Path:
    path = tmp_path / "input"
    path.write_bytes(PAYLOAD)
    path.chmod(0o600)
    return path


@pytest.mark.parametrize("mode", [0o400, 0o600, 0o700])
def test_validate_private_file_accepts_owner_only_readable_regular_file(
    private_file, mode
):
    private_file.chmod(mode)
    assert _api().validate_private_file(private_file) == private_file


@pytest.mark.parametrize(
    "kind",
    ["missing", "directory", "symlink", "empty", "shared", "unreadable", "foreign"],
)
def test_validate_private_file_rejects_unusable_sources(
    private_file, monkeypatch, kind
):
    api = _api()
    if kind == "missing":
        private_file.unlink()
    elif kind == "directory":
        private_file.unlink()
        private_file.mkdir()
    elif kind == "symlink":
        target = private_file.with_name("target")
        private_file.rename(target)
        private_file.symlink_to(target)
    elif kind == "empty":
        private_file.write_bytes(b"")
    elif kind == "shared":
        private_file.chmod(0o640)
    elif kind == "unreadable":
        private_file.chmod(0o200)
    else:
        monkeypatch.setattr(api.os, "getuid", lambda: private_file.stat().st_uid + 1)
    with pytest.raises((ValueError, PermissionError)):
        api.validate_private_file(private_file)


def test_validate_private_file_requires_path():
    with pytest.raises(TypeError, match="path"):
        _api().validate_private_file("do-not-open")


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"../escape": "file"},
        {"/absolute": "file"},
        {".": "file"},
        {"..": "file"},
        {"": "file"},
        {"a/b": "file"},
        {"a\\b": "file"},
        {1: "file"},
    ],
)
def test_unsafe_names_fail_before_remote_action(private_file, files):
    provider = _Provider()
    mapped = dict.fromkeys(files, private_file)
    with (
        pytest.raises((ValueError, TypeError)),
        _api().stage_private_files(provider, "target", mapped),
    ):
        pytest.fail("invalid names acquired a lease")
    assert provider.commands == []
    assert provider.sources == []


@pytest.mark.parametrize(
    "prefix", ["../escape", "/tmp/other", ".", "..", "", "x y", "x\x00y", 1]
)
def test_unsafe_prefix_fails_before_remote_action(private_file, prefix):
    provider = _Provider()
    with (
        pytest.raises((ValueError, TypeError)),
        _api().stage_private_files(
            provider, "target", {"token": private_file}, prefix=prefix
        ),
    ):
        pytest.fail("invalid prefix acquired a lease")
    assert provider.commands == []


@pytest.mark.parametrize(
    "output",
    [
        "/etc",
        "/tmp/sonata-credentials.A",
        "/tmp/sonata-credentials.ABC123/../other",
        "/tmp/other.ABC123",
        "/tmp/sonata-credentials.ABC123\n/tmp/other.ABC123",
        None,
    ],
)
def test_unsafe_remote_directory_is_never_transferred_to_or_removed(
    private_file, output
):
    provider = _Provider()
    provider.output = output
    with (
        pytest.raises(RuntimeError),
        _api().stage_private_files(provider, "target", {"token": private_file}),
    ):
        pytest.fail("unsafe remote path acquired a lease")
    assert provider.sources == []
    assert not any(command[0] == "rm" for command in provider.commands)


@pytest.mark.parametrize("status", [None, False, True, 0.0, "0", 3])
@pytest.mark.parametrize("phase", ["mktemp", "chmod", "transfer"])
def test_malformed_or_failed_remote_results_do_not_acquire_lease(
    private_file, status, phase
):
    provider = _Provider()
    provider.bad_at = phase
    provider.bad_result = (
        SimpleNamespace(
            stdout=provider.output, stderr="synthetic-private status detail"
        )
        if status is None
        else SimpleNamespace(
            return_code=status,
            stdout=provider.output,
            stderr="synthetic-private status detail",
        )
    )
    with (
        pytest.raises(RuntimeError) as caught,
        _api().stage_private_files(provider, "target", {"token": private_file}),
    ):
        pytest.fail("failed operation acquired a lease")
    assert "synthetic-private" not in "".join(traceback.format_exception(caught.value))
    assert all(not source.exists() for source in provider.sources)
    assert any(command[0] == "rm" for command in provider.commands) == (
        phase != "mktemp"
    )


def test_private_binary_copy_and_both_locations_cleaned_after_success(private_file):
    provider = _Provider()
    with _api().stage_private_files(
        provider, "target", {"token": private_file, "key": private_file}
    ) as (directory, paths):
        assert directory == "/tmp/sonata-credentials.ABC123"
        assert paths == {"token": f"{directory}/token", "key": f"{directory}/key"}
        assert all(source.exists() for source in provider.sources)
        assert private_file.read_bytes() == PAYLOAD
        assert "synthetic-private" not in repr(provider.commands)
        assert provider.sources[0].parent == provider.sources[1].parent
    assert all(not source.parent.exists() for source in provider.sources)
    assert provider.commands[-1] == ("rm", "-rf", "--", directory)
    assert private_file.read_bytes() == PAYLOAD


@pytest.mark.parametrize("phase", ["chmod", "transfer"])
def test_partial_acquisition_cleans_both_locations(private_file, phase):
    provider = _Provider()
    provider.fail = phase
    with (
        pytest.raises(RuntimeError) as caught,
        _api().stage_private_files(provider, "target", {"token": private_file}),
    ):
        pytest.fail("failed acquisition yielded")
    assert "synthetic-private" not in "".join(traceback.format_exception(caught.value))
    assert provider.commands[-1][0] == "rm"
    assert all(not source.parent.exists() for source in provider.sources)


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, KeyboardInterrupt])
def test_body_error_retains_identity_after_successful_cleanup(private_file, error_type):
    provider = _Provider()
    error = error_type("synthetic-private body detail")
    with (
        pytest.raises(error_type) as caught,
        _api().stage_private_files(provider, "target", {"token": private_file}),
    ):
        raise error
    assert caught.value is error
    assert provider.commands[-1][0] == "rm"
    assert all(not source.parent.exists() for source in provider.sources)


@pytest.mark.parametrize("body_fails", [False, True])
@pytest.mark.parametrize("location", ["remote", "local"])
def test_cleanup_failure_hides_all_private_exception_details(
    private_file, monkeypatch, body_fails, location
):
    api = _api()
    provider = _Provider()
    original = shutil.rmtree
    if location == "remote":
        provider.fail = "rm"
    else:

        def fail_cleanup(path):
            raise OSError("synthetic-private local cleanup")

        monkeypatch.setattr(api.shutil, "rmtree", fail_cleanup)
    try:

        def use_lease():
            with api.stage_private_files(provider, "target", {"token": private_file}):
                if body_fails:
                    raise ValueError("synthetic-private body detail")

        with pytest.raises(api.CredentialCleanupError) as caught:
            use_lease()
        assert caught.value.operation_type == (
            "ValueError"
            if body_fails
            else ("RuntimeError" if location == "remote" else "OSError")
        )
        assert "synthetic-private" not in "".join(
            traceback.format_exception(caught.value)
        )
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__
        assert provider.commands[-1][0] == "rm"
    finally:
        for source in provider.sources:
            if source.parent.exists():
                original(source.parent)


@pytest.mark.parametrize("replacement", ["symlink", "fifo", "regular"])
def test_source_replaced_at_open_never_copies_or_blocks(
    private_file, monkeypatch, replacement
):
    api = _api()
    provider = _Provider()
    original = os.open
    target = private_file.with_name("replacement")
    target.write_bytes(b"must-not-transfer")
    target.chmod(0o600)

    def replace_and_open(path, flags, *args, **kwargs):
        if path == private_file:
            private_file.unlink()
            if replacement == "symlink":
                private_file.symlink_to(target)
            elif replacement == "fifo":
                os.mkfifo(private_file, 0o600)
            else:
                target.rename(private_file)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(api.os, "open", replace_and_open)

    def timeout(signum, frame):
        raise AssertionError("source open blocked")

    previous = signal.signal(signal.SIGALRM, timeout)
    signal.alarm(2)
    try:
        with (
            pytest.raises(ValueError, match="private file"),
            api.stage_private_files(provider, "target", {"token": private_file}),
        ):
            pytest.fail("replacement acquired a lease")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert provider.commands == []
    assert provider.sources == []


@pytest.mark.parametrize("failure", ["chmod", "copy"])
def test_local_acquisition_failure_removes_directory(
    private_file, monkeypatch, failure
):
    api = _api()
    provider = _Provider()
    directories = []
    original_mkdtemp = api.mkdtemp

    def track_directory(*args, **kwargs):
        directory = original_mkdtemp(*args, **kwargs)
        directories.append(Path(directory))
        return directory

    monkeypatch.setattr(api, "mkdtemp", track_directory)
    if failure == "copy":

        def fail_copy(*args, **kwargs):
            raise OSError("synthetic-private copy error")

        monkeypatch.setattr(api.shutil, "copyfileobj", fail_copy)
    else:
        original_chmod = Path.chmod

        def fail_chmod(path, mode, **kwargs):
            if path in directories:
                raise OSError("synthetic-private hardening error")
            return original_chmod(path, mode, **kwargs)

        monkeypatch.setattr(Path, "chmod", fail_chmod)
    with (
        pytest.raises((ValueError, RuntimeError)),
        api.stage_private_files(provider, "target", {"token": private_file}),
    ):
        pytest.fail("local acquisition failure yielded")
    assert len(directories) == 1
    assert not directories[0].exists()
    assert provider.commands == []


def test_provider_programming_error_preserved_after_cleanup(private_file):
    class BrokenProvider(_Provider):
        @override
        def transfer_to(
            self, request: object, *, source: Path, destination: str
        ) -> object:
            self.sources.append(source)
            raise TypeError("programming error")

    provider = BrokenProvider()
    with (
        pytest.raises(TypeError, match="programming error"),
        _api().stage_private_files(provider, "target", {"token": private_file}),
    ):
        pytest.fail("broken provider yielded")
    assert provider.commands[-1][0] == "rm"
    assert not provider.sources[0].parent.exists()
