from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sonata_engine import TaskInputs
from sonata_tasks import archive as shared


@dataclass
class ShellResult:
    return_code: Any = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class LocalTarget:
    """Execute real target commands; inject only external transport failures."""

    fail: str = ""
    bad_status: Any = 0
    checksum: str | None = None
    cleanup_fails: bool = False
    calls: list[tuple[str, ...]] = field(default_factory=list)
    transfers: int = 0

    def exec_argv(self, request: object, argv: tuple[str, ...]):
        self.calls.append(argv)
        if self.fail == argv[0]:
            self.fail = ""
            raise RuntimeError("target unavailable")
        if self.cleanup_fails and argv[0] == "rm" and len(argv) == 5:
            return ShellResult(return_code=1, stdout="", stderr="cleanup denied")
        if self.checksum is not None and argv[0] == "sha256sum":
            return ShellResult(return_code=0, stdout=self.checksum, stderr="")
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        return ShellResult(
            return_code=result.returncode, stdout=result.stdout, stderr=result.stderr
        )

    def transfer_to(self, request: object, *, source: Path, destination: str):
        self.transfers += 1
        shutil.copyfile(source, destination)
        if self.fail == "transfer":
            self.fail = ""
            raise RuntimeError("connection died after partial upload")
        if self.fail == "interrupt":
            self.fail = ""
            raise KeyboardInterrupt
        return ShellResult(return_code=self.bad_status, stdout="", stderr="")


@pytest.fixture
def frozen(tmp_path):
    path = tmp_path / "frozen.tar"
    with tarfile.open(path, "w") as bundle:
        directory = tarfile.TarInfo("bin")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o700
        bundle.addfile(directory)
        file = tarfile.TarInfo("bin/app")
        file.size = 6
        file.mode = 0o755
        bundle.addfile(file, io.BytesIO(b"hello\n"))
    return path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def locations(tmp_path, target="one") -> dict[str, Any]:
    root = tmp_path / target
    return {
        "remote_archive": str(root / "source.tar"),
        "remote_source_dir": str(root / "source"),
    }


def test_frozen_resource_reuses_original_bytes_on_two_targets(tmp_path, frozen):
    local, digest = frozen
    original = local.read_bytes()
    provider = LocalTarget()
    for target in ("one", "two"):
        paths = locations(tmp_path, target)
        resource = shared.source_archive_resource(
            archive=local,
            expected_digest=digest,
            provider=provider,
            request=object(),
            strict_cleanup=True,
            **paths,
        )
        assert resource.always_release
        output = resource.acquire(TaskInputs.empty())
        assert (Path(output) / "bin/app").read_bytes() == b"hello\n"
        assert (Path(output) / "bin").stat().st_mode & 0o777 == 0o755
        assert (Path(output) / "bin/app").stat().st_mode & 0o777 == 0o755
        assert Path(paths["remote_archive"]).read_bytes() == original
        resource.release(TaskInputs.empty(), output)
        assert not Path(output).exists()
        assert not Path(paths["remote_archive"]).exists()
    assert local.read_bytes() == original
    assert provider.transfers == 2


@pytest.mark.parametrize(
    "digest", ["bad", "sha256:", "a" * 63, "x" * 64, "sha512:" + "a" * 64]
)
def test_invalid_expected_digest_never_touches_target(tmp_path, frozen, digest):
    provider = LocalTarget()
    with pytest.raises(ValueError, match=r"digest|SHA-256|paths|overlap|repo_root"):
        shared.stage_source_archive(
            provider,
            object(),
            archive=frozen[0],
            expected_digest=digest,
            **locations(tmp_path),
        )
    assert not provider.calls
    assert provider.transfers == 0


def test_changed_local_archive_never_touches_target(tmp_path, frozen):
    path, digest = frozen
    path.write_bytes(b"modified")
    provider = LocalTarget()
    with pytest.raises(RuntimeError, match="changed"):
        shared.stage_source_archive(
            provider,
            object(),
            archive=path,
            expected_digest=digest,
            **locations(tmp_path),
        )
    assert not provider.calls
    assert provider.transfers == 0


@pytest.mark.parametrize(
    ("source", "archive"),
    [
        ("/", "/tmp/source.tar"),
        ("/tmp/source", "/"),
        ("relative/source", "/tmp/source.tar"),
        ("/tmp/source", "-rf"),
        ("/tmp/source/../source", "/tmp/source.tar"),
        ("/tmp/source", "/tmp/source"),
        ("/tmp/source", "/tmp/source/archive.tar"),
        ("/tmp/source", "/tmp"),
        ("/tmp/source/", "/tmp/source.tar"),
    ],
)
def test_unsafe_paths_fail_before_staging_or_cleanup(tmp_path, frozen, source, archive):
    provider = LocalTarget()
    paths = {"remote_source_dir": source, "remote_archive": archive}
    with pytest.raises(ValueError, match=r"digest|SHA-256|paths|overlap|repo_root"):
        shared.stage_source_archive(provider, object(), archive=frozen[0], **paths)
    with pytest.raises(ValueError, match=r"digest|SHA-256|paths|overlap|repo_root"):
        shared.remove_source_archive(provider, object(), **paths)
    assert not provider.calls
    assert provider.transfers == 0


@pytest.mark.parametrize(
    "failure", ["rm", "mkdir", "transfer", "sha256sum", "python3", "interrupt"]
)
def test_partial_acquisition_cleans_both_paths(tmp_path, frozen, failure):
    paths = locations(tmp_path)
    source = Path(paths["remote_source_dir"])
    source.mkdir(parents=True)
    (source / "stale").write_text("stale")
    provider = LocalTarget(fail=failure)
    error = KeyboardInterrupt if failure == "interrupt" else RuntimeError
    with pytest.raises(error):
        shared.stage_source_archive(
            provider, object(), archive=frozen[0], expected_digest=frozen[1], **paths
        )
    assert not source.exists()
    assert not Path(paths["remote_archive"]).exists()
    assert frozen[0].exists()


@pytest.mark.parametrize("checksum", ["", "garbage", "0" * 64 + "  source.tar\n"])
def test_bad_remote_checksum_cleans_without_extracting(tmp_path, frozen, checksum):
    provider = LocalTarget(checksum=checksum)
    paths = locations(tmp_path)
    with pytest.raises(RuntimeError, match="checksum"):
        shared.stage_source_archive(provider, object(), archive=frozen[0], **paths)
    assert not any(argv[0] == "python3" for argv in provider.calls)
    assert not Path(paths["remote_source_dir"]).exists()
    assert not Path(paths["remote_archive"]).exists()


@pytest.mark.parametrize("status", [None, False, True, 0.0, "0", 1])
def test_invalid_transfer_status_compensates_partial_upload(tmp_path, frozen, status):
    provider = LocalTarget(bad_status=status)
    paths = locations(tmp_path)
    with pytest.raises(RuntimeError):
        shared.stage_source_archive(provider, object(), archive=frozen[0], **paths)
    assert not Path(paths["remote_archive"]).exists()
    assert not Path(paths["remote_source_dir"]).exists()


@pytest.mark.parametrize("member", ["../escaped", "/escaped", "link"])
def test_safe_extract_rejects_traversal_and_symlink_escape(tmp_path, member):
    path = tmp_path / "unsafe.tar"
    with tarfile.open(path, "w") as bundle:
        entry = tarfile.TarInfo(member)
        if member == "link":
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../../escaped"
        else:
            entry.size = 3
        bundle.addfile(entry, None if member == "link" else io.BytesIO(b"bad"))
    provider = LocalTarget()
    paths = locations(tmp_path)
    # An absolute member is normalized by the data filter to a safe relative name.
    if member == "/escaped":
        shared.stage_source_archive(provider, object(), archive=path, **paths)
        assert (Path(paths["remote_source_dir"]) / "escaped").read_bytes() == b"bad"
        shared.remove_source_archive(provider, object(), **paths)
    else:
        with pytest.raises(RuntimeError):
            shared.stage_source_archive(provider, object(), archive=path, **paths)
        assert not Path(paths["remote_source_dir"]).exists()
    assert not (tmp_path / "escaped").exists()


def test_cleanup_failure_is_noted_without_hiding_acquire_error(tmp_path, frozen):
    provider = LocalTarget(fail="transfer", cleanup_fails=True)
    with pytest.raises(RuntimeError, match="connection died") as error:
        shared.stage_source_archive(
            provider, object(), archive=frozen[0], **locations(tmp_path)
        )
    assert any("cleanup denied" in note for note in error.value.__notes__)


def test_strict_release_reports_cleanup_status(tmp_path, frozen):
    provider = LocalTarget()
    paths = locations(tmp_path)
    resource = shared.source_archive_resource(
        archive=frozen[0],
        expected_digest=frozen[1],
        provider=provider,
        request=object(),
        strict_cleanup=True,
        **paths,
    )
    value = resource.acquire(TaskInputs.empty())
    provider.cleanup_fails = True
    with pytest.raises(RuntimeError, match="cleanup denied"):
        resource.release(TaskInputs.empty(), value)


def test_frozen_default_release_retains_archive_and_suppresses_runtime_failure(
    tmp_path, frozen
):
    provider = LocalTarget()
    paths = locations(tmp_path)
    resource = shared.source_archive_resource(
        archive=frozen[0],
        expected_digest=frozen[1],
        provider=provider,
        request=object(),
        **paths,
    )
    value = resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), value)
    assert not Path(value).exists()
    assert Path(paths["remote_archive"]).exists()
    provider.fail = "rm"
    resource.release(TaskInputs.empty(), value)


def test_archive_modes_require_expected_evidence_or_git_source(tmp_path, frozen):
    args: dict[str, Any] = {
        "provider": LocalTarget(),
        "request": object(),
        **locations(tmp_path),
    }
    with pytest.raises(ValueError, match=r"digest|SHA-256|paths|overlap|repo_root"):
        shared.source_archive_resource(archive=frozen[0], **args)
    with pytest.raises(ValueError, match=r"digest|SHA-256|paths|overlap|repo_root"):
        shared.source_archive_resource(**args)


def test_export_mode_rejects_unused_expected_digest(tmp_path, frozen):
    with pytest.raises(ValueError, match="expected_digest"):
        shared.source_archive_resource(
            repo_root=tmp_path,
            commit="HEAD",
            expected_digest=frozen[1],
            provider=LocalTarget(),
            request=object(),
            **locations(tmp_path),
        )
