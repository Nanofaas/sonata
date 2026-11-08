from __future__ import annotations

import hashlib
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import override

import pytest
from resource_stubs import acquired

from sonata_engine import Resource, TaskInputs
from sonata_tasks.archive import source_archive_resource


@dataclass
class ShellResult:
    return_code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class RecordingProvider:
    exec_seen: list[tuple[str, ...]] = field(default_factory=list)
    transfer_seen: list[tuple[Path, str]] = field(default_factory=list)

    def exec_argv(self, request: object, argv: tuple[str, ...]) -> ShellResult:
        self.exec_seen.append(argv)
        # sha256sum: compute and return checksum from the local file
        if argv[0] == "sha256sum":
            path = Path(argv[1])
            if path.exists():
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                return ShellResult(stdout=f"{checksum}  {argv[1]}")
            return ShellResult(return_code=1, stderr="file not found")
        # tar -xf: extract the archive to the target dir
        if argv[0] == "tar" and "-xf" in argv:
            # argv: ("tar", "-xf", <archive>, "-C", <target>)
            archive = Path(argv[2])
            target = Path(argv[4])
            target.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["tar", "-xf", str(archive), "-C", str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
        # mkdir -p: argv is ("mkdir", "-p", <dir>)
        if argv[0] == "mkdir":
            Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        # rm: remove file(s)/dir(s) - supports -rf and -f flags
        if argv[0] == "rm":
            paths = [Path(a) for a in argv[1:] if not a.startswith("-")]
            for path in paths:
                if path.exists():
                    subprocess.run(
                        ["rm", "-rf", str(path)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
        return ShellResult()

    def transfer_to(
        self, request: object, *, source: Path, destination: str
    ) -> ShellResult:
        self.transfer_seen.append((source, destination))
        # Simulate: copy source content to destination on "remote"
        Path(destination).write_bytes(source.read_bytes())
        return ShellResult()


@pytest.fixture
def git_repo() -> Path:
    """Create a real git repo with one commit containing a file."""
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init"], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"],
        cwd=str(tmp),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(tmp),
        check=True,
        capture_output=True,
    )
    readme = tmp / "README.md"
    readme.write_text("# hello\n")
    subprocess.run(["git", "add", "."], cwd=str(tmp), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(tmp),
        check=True,
        capture_output=True,
    )
    return tmp


def test_acquire_archives_transfers_verifies_and_extracts(
    git_repo: Path,
) -> None:
    """Run the acquire sequence and return the remote source dir.

    The steps run in order and the extracted README matches what was committed.
    """
    remote_dir = Path(tempfile.mkdtemp())
    remote_archive = str(remote_dir / "source.tar")
    remote_source_dir = str(remote_dir / "extracted")
    provider = RecordingProvider()

    resource = source_archive_resource(
        repo_root=git_repo,
        commit="HEAD",
        remote_source_dir=remote_source_dir,
        remote_archive=remote_archive,
        provider=provider,
        request=object(),
    )

    state = acquired(resource)

    assert state == remote_source_dir
    assert len(provider.transfer_seen) == 1
    assert provider.transfer_seen[0][1] == remote_archive

    assert len(provider.exec_seen) == 4
    assert provider.exec_seen[0] == ("mkdir", "-p", str(Path(remote_archive).parent))
    assert provider.exec_seen[1] == ("sha256sum", remote_archive)
    assert provider.exec_seen[2] == ("mkdir", "-p", remote_source_dir)
    assert provider.exec_seen[3] == (
        "tar",
        "-xf",
        remote_archive,
        "-C",
        remote_source_dir,
    )

    # Verify the extracted content
    extracted_readme = Path(remote_source_dir) / "README.md"
    assert extracted_readme.read_text() == "# hello\n"

    # Clean up
    Path(remote_archive).unlink()


def test_release_removes_source_dir(git_repo: Path) -> None:
    """Release removes the remote source dir."""
    remote_dir = Path(tempfile.mkdtemp())
    remote_archive = str(remote_dir / "source.tar")
    remote_source_dir = str(remote_dir / "extracted")
    provider = RecordingProvider()

    resource = source_archive_resource(
        repo_root=git_repo,
        commit="HEAD",
        remote_source_dir=remote_source_dir,
        remote_archive=remote_archive,
        provider=provider,
        request=object(),
    )

    state = acquired(resource)
    assert Path(remote_source_dir).exists()

    resource.release.run(replace(TaskInputs.empty(), _upstream=state))

    assert not Path(remote_source_dir).exists()
    assert provider.exec_seen[-1] == ("rm", "-rf", remote_source_dir)
    Path(remote_archive).unlink()


def test_release_propagates_programming_errors(git_repo: Path) -> None:
    class BrokenProvider(RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> ShellResult:
            raise ValueError("bad provider contract")

    resource = source_archive_resource(
        repo_root=git_repo,
        commit="HEAD",
        remote_source_dir="/tmp/source",
        remote_archive="/tmp/source.tar",
        provider=BrokenProvider(),
        request=object(),
    )

    with pytest.raises(ValueError, match="bad provider contract"):
        resource.release.run(replace(TaskInputs.empty(), _upstream="/tmp/source"))


def test_acquire_cleans_up_archive_on_verify_failure(git_repo: Path) -> None:
    """When sha256sum fails, the remote archive is cleaned up."""
    remote_dir = Path(tempfile.mkdtemp())
    remote_archive = str(remote_dir / "source.tar")
    remote_source_dir = str(remote_dir / "extracted")

    class FailVerifyProvider(RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> ShellResult:
            if argv[0] == "sha256sum":
                self.exec_seen.append(argv)
                return ShellResult(return_code=1, stderr="checksum error")
            # Delegate to parent for cleanup commands
            return super().exec_argv(request, argv)

    provider = FailVerifyProvider()

    resource = source_archive_resource(
        repo_root=git_repo,
        commit="HEAD",
        remote_source_dir=remote_source_dir,
        remote_archive=remote_archive,
        provider=provider,
        request=object(),
    )

    with pytest.raises(RuntimeError, match="remote command failed"):
        resource.acquire.run(TaskInputs.empty())

    # The archive should have been cleaned up
    assert not Path(remote_archive).exists()
    # The source dir should never have been created
    assert not Path(remote_source_dir).exists()


def test_acquire_cleans_up_on_extract_failure(git_repo: Path) -> None:
    """When tar -xf fails, both the source dir and archive are cleaned up."""
    remote_dir = Path(tempfile.mkdtemp())
    remote_archive = str(remote_dir / "source.tar")
    remote_source_dir = str(remote_dir / "extracted")

    class FailExtractProvider(RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> ShellResult:
            # sha256sum must succeed so we reach the tar step
            if argv[0] == "sha256sum":
                self.exec_seen.append(argv)
                path = Path(argv[1])
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                return ShellResult(stdout=f"{checksum}  {argv[1]}")
            if argv[0] == "tar":
                self.exec_seen.append(argv)
                return ShellResult(return_code=1, stderr="tar error")
            # Delegate to parent for cleanup commands
            return super().exec_argv(request, argv)

    provider = FailExtractProvider()

    resource = source_archive_resource(
        repo_root=git_repo,
        commit="HEAD",
        remote_source_dir=remote_source_dir,
        remote_archive=remote_archive,
        provider=provider,
        request=object(),
    )

    with pytest.raises(RuntimeError, match="remote command failed"):
        resource.acquire.run(TaskInputs.empty())

    # Archive should be cleaned up
    assert not Path(remote_archive).exists()
    assert not Path(remote_source_dir).exists()


def test_release_tolerates_missing_source_dir() -> None:
    """Release does not raise when the source dir is already gone."""
    provider = RecordingProvider()

    resource = source_archive_resource(
        repo_root=Path("/nonexistent"),
        commit="HEAD",
        remote_source_dir="/tmp/some-dir-that-does-not-exist",
        remote_archive="/tmp/some-archive.tar",
        provider=provider,
        request=object(),
    )

    # Should not raise
    resource.release.run(
        replace(TaskInputs.empty(), _upstream="/tmp/some-dir-that-does-not-exist")
    )


def test_release_tolerates_provider_error() -> None:
    """Release does not raise when the provider's exec_argv raises."""
    provider = RecordingProvider()

    class FailRmProvider(RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> ShellResult:
            self.exec_seen.append(argv)
            if argv[0] == "rm":
                msg = "rm failed"
                raise RuntimeError(msg)
            return super().exec_argv(request, argv)

    provider = FailRmProvider()

    resource = source_archive_resource(
        repo_root=Path(),
        commit="HEAD",
        remote_source_dir="/tmp/src",
        remote_archive="/tmp/src.tar",
        provider=provider,
        request=object(),
    )

    # Should not raise despite exec_argv failing
    resource.release.run(replace(TaskInputs.empty(), _upstream="/tmp/src"))


def test_transfer_failure_raises(git_repo: Path) -> None:
    """A failed transfer raises RuntimeError."""
    remote_dir = Path(tempfile.mkdtemp())
    remote_archive = str(remote_dir / "source.tar")
    remote_source_dir = str(remote_dir / "extracted")

    class FailTransferProvider(RecordingProvider):
        @override
        def transfer_to(
            self, request: object, *, source: Path, destination: str
        ) -> ShellResult:
            self.transfer_seen.append((source, destination))
            return ShellResult(return_code=1, stderr="scp failed")

    provider = FailTransferProvider()

    resource = source_archive_resource(
        repo_root=git_repo,
        commit="HEAD",
        remote_source_dir=remote_source_dir,
        remote_archive=remote_archive,
        provider=provider,
        request=object(),
    )

    with pytest.raises(RuntimeError, match="transfer failed"):
        resource.acquire.run(TaskInputs.empty())


def test_sha256sum_mismatch_cleans_up(git_repo: Path) -> None:
    """When checksums differ, the archive is cleaned up."""
    remote_dir = Path(tempfile.mkdtemp())
    remote_archive = str(remote_dir / "source.tar")
    remote_source_dir = str(remote_dir / "extracted")

    class MismatchProvider(RecordingProvider):
        @override
        def exec_argv(self, request: object, argv: tuple[str, ...]) -> ShellResult:
            self.exec_seen.append(argv)
            if argv[0] == "sha256sum":
                return ShellResult(
                    stdout=(
                        "0000000000000000000000000000000000000000000000000000000000000000"
                        "  source.tar"
                    )
                )
            return super().exec_argv(request, argv)

    provider = MismatchProvider()

    resource = source_archive_resource(
        repo_root=git_repo,
        commit="HEAD",
        remote_source_dir=remote_source_dir,
        remote_archive=remote_archive,
        provider=provider,
        request=object(),
    )

    with pytest.raises(RuntimeError, match="sha256sum mismatch"):
        resource.acquire.run(TaskInputs.empty())

    assert not Path(remote_archive).exists()
    assert not Path(remote_source_dir).exists()


def test_resource_is_resource_of_str() -> None:
    """The returned object is a Resource[str]."""
    provider = RecordingProvider()

    resource = source_archive_resource(
        repo_root=Path(),
        commit="HEAD",
        remote_source_dir="/tmp/src",
        remote_archive="/tmp/src.tar",
        provider=provider,
        request=object(),
    )

    assert isinstance(resource, Resource)
