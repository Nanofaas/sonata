"""Ship a git commit's tree to a remote host as an extracted source archive."""

from __future__ import annotations

import contextlib
import hashlib
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import cast

from sonata_engine import Resource, TaskInputs
from sonata_tasks.compensation import best_effort
from sonata_tasks.transfer import RemoteProvider


def _exec[RequestT](
    provider: RemoteProvider[RequestT], request: RequestT, argv: tuple[str, ...]
) -> str:
    result = provider.exec_argv(request, argv=argv)
    if not isinstance(getattr(result, "return_code", None), int) or isinstance(
        result.return_code, bool
    ):
        raise RuntimeError("remote command returned no integer return_code")
    if result.return_code != 0:
        detail = result.stderr or result.stdout
        raise RuntimeError(
            f"remote command failed (exit {result.return_code})"
            + (f": {detail}" if detail else "")
        )
    return result.stdout


def _transfer_archive[RequestT](
    provider: RemoteProvider[RequestT],
    request: RequestT,
    archive_path: Path,
    remote_archive: str,
) -> None:
    result = provider.transfer_to(
        request, source=archive_path, destination=remote_archive
    )
    if not isinstance(getattr(result, "return_code", None), int) or isinstance(
        result.return_code, bool
    ):
        raise RuntimeError("transfer returned no integer return_code")
    if result.return_code != 0:
        detail = result.stderr or result.stdout
        raise RuntimeError(
            f"transfer failed (exit {result.return_code})"
            + (f": {detail}" if detail else "")
        )


def _verify_remote_archive[RequestT](
    provider: RemoteProvider[RequestT],
    request: RequestT,
    remote_archive: str,
    local_checksum: str,
) -> None:
    try:
        remote_checksum = _exec(
            provider, request, ("sha256sum", remote_archive)
        ).split()[0]
        if remote_checksum != local_checksum:
            raise RuntimeError(
                f"sha256sum mismatch: local={local_checksum}, remote={remote_checksum}"
            )
    except BaseException as error:
        best_effort(
            error,
            lambda: provider.exec_argv(request, argv=("rm", "-f", remote_archive)),
            what="cleanup remote archive after failed verify",
        )
        raise


def _extract_remote_archive[RequestT](
    provider: RemoteProvider[RequestT],
    request: RequestT,
    remote_archive: str,
    remote_source_dir: str,
) -> None:
    try:
        _exec(provider, request, ("mkdir", "-p", remote_source_dir))
        _exec(
            provider, request, ("tar", "-xf", remote_archive, "-C", remote_source_dir)
        )
    except BaseException as error:
        best_effort(
            error,
            lambda: provider.exec_argv(
                request, argv=("rm", "-rf", remote_source_dir, remote_archive)
            ),
            what="cleanup after failed extract",
        )
        raise


# Same extraction semantics on a planning host and a Python >=3.12 target.
SOURCE_ARCHIVE_EXTRACT_SCRIPT = """
import sys, tarfile
from pathlib import Path
archive, output = Path(sys.argv[1]), Path(sys.argv[2])
with tarfile.open(archive) as bundle:
    bundle.extractall(output, filter="data")
for path in output.rglob("*"):
    if path.is_dir() and not path.is_symlink():
        path.chmod(0o755)
"""


def _owned_paths(remote_source_dir: str, remote_archive: str) -> None:
    source, archive = PurePosixPath(remote_source_dir), PurePosixPath(remote_archive)
    for text, path in ((remote_source_dir, source), (remote_archive, archive)):
        if (
            not path.is_absolute()
            or text.startswith("//")
            or path == PurePosixPath("/")
            or ".." in path.parts
            or str(path) != text
            or "\x00" in text
        ):
            raise ValueError(
                "archive staging requires canonical absolute nonroot paths"
            )
    if source.is_relative_to(archive) or archive.is_relative_to(source):
        raise ValueError("archive and source paths must not overlap")


def _checksum(digest: str) -> str:
    value = digest.removeprefix("sha256:")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", value):
        raise ValueError("expected_digest must be a SHA-256 digest")
    return value.lower()


def remove_source_archive[RequestT](
    provider: RemoteProvider[RequestT],
    request: RequestT,
    *,
    remote_archive: str,
    remote_source_dir: str,
) -> None:
    """Remove both caller-owned remote paths, reporting unsuccessful cleanup.

    Parents must be caller-controlled. Paths must be canonical, absolute,
    nonroot and disjoint; ownership cannot be inferred by this helper.
    """
    _owned_paths(remote_source_dir, remote_archive)
    _exec(provider, request, ("rm", "-rf", "--", remote_source_dir, remote_archive))


def stage_source_archive[RequestT](
    provider: RemoteProvider[RequestT],
    request: RequestT,
    *,
    archive: Path,
    remote_archive: str,
    remote_source_dir: str,
    expected_digest: str | None = None,
) -> None:
    """Verify, transfer and safely extract an existing caller-owned archive.

    Check the expected digest before touching the target; without one, use
    the current local bytes. Accept bare or sha256:-prefixed SHA-256 values.
    Replace the owned source directory, verify remote bytes before extraction
    and compensate both remote paths after a failed acquisition. Normal
    release belongs to the caller via remove_source_archive.

    The target needs Python >=3.12, mkdir, rm and sha256sum. The data filter
    rejects escaping links/traversal; directories become0755 and file execute
    bits survive. Providers, parent namespaces and concurrent writers remain
    the caller's responsibility. Local archive bytes are never modified.
    """
    _owned_paths(remote_source_dir, remote_archive)
    expected = _checksum(expected_digest) if expected_digest is not None else None
    with archive.open("rb") as handle:
        local = hashlib.file_digest(handle, "sha256").hexdigest()
    if expected is not None and local != expected:
        raise RuntimeError("source archive changed before consumption")
    try:
        _exec(provider, request, ("rm", "-rf", "--", remote_source_dir))
        _exec(
            provider,
            request,
            (
                "mkdir",
                "-p",
                "--",
                remote_source_dir,
                str(PurePosixPath(remote_archive).parent),
            ),
        )
        _transfer_archive(provider, request, archive, remote_archive)
        output = _exec(provider, request, ("sha256sum", remote_archive))
        parts = output.split() if isinstance(output, str) else []
        if not parts or parts[0].lower() != local:
            raise RuntimeError("source archive checksum mismatch")
        _exec(
            provider,
            request,
            (
                "python3",
                "-c",
                SOURCE_ARCHIVE_EXTRACT_SCRIPT,
                remote_archive,
                remote_source_dir,
            ),
        )
    except BaseException as error:
        best_effort(
            error,
            lambda: remove_source_archive(
                provider,
                request,
                remote_archive=remote_archive,
                remote_source_dir=remote_source_dir,
            ),
            what="source archive acquisition cleanup",
        )
        raise


def source_archive_resource[RequestT](
    *,
    repo_root: Path | None = None,
    commit: str | None = None,
    remote_source_dir: str,
    remote_archive: str,
    provider: RemoteProvider[RequestT],
    request: RequestT,
    archive: Path | None = None,
    expected_digest: str | None = None,
    strict_cleanup: bool = False,
) -> Resource[str]:
    """Package ``commit`` locally and unpack it on the remote host.

    The commit is exported with ``git archive``, transferred, verified against
    its local SHA-256, and extracted into ``remote_source_dir``. Acquiring
    returns ``remote_source_dir``; releasing best-effort deletes that extracted
    directory on the remote host.

    Supply archive and expected_digest instead of repo_root/commit to reuse
    frozen local bytes through stage_source_archive without another Git export.
    Frozen resources always release. Opt in to strict_cleanup to remove both
    remote paths and report cleanup failures; the default release is unchanged.
    """
    if archive is None:
        if expected_digest is not None:
            raise ValueError("expected_digest requires a frozen archive")
        if repo_root is None or commit is None:
            raise ValueError("repo_root and commit are required for Git export")
    else:
        _owned_paths(remote_source_dir, remote_archive)
        if expected_digest is None:
            raise ValueError("expected_digest is required for a frozen archive")
        _checksum(expected_digest)
    if strict_cleanup:
        _owned_paths(remote_source_dir, remote_archive)

    def acquire(_inputs: TaskInputs) -> str:
        if archive is not None:
            stage_source_archive(
                provider,
                request,
                archive=archive,
                expected_digest=expected_digest,
                remote_archive=remote_archive,
                remote_source_dir=remote_source_dir,
            )
            return remote_source_dir
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "source.tar"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "archive",
                    "--format=tar",
                    cast(str, commit),
                    "-o",
                    str(archive_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            _exec(provider, request, ("mkdir", "-p", str(Path(remote_archive).parent)))
            _transfer_archive(provider, request, archive_path, remote_archive)
            _verify_remote_archive(provider, request, remote_archive, checksum)
            _extract_remote_archive(
                provider, request, remote_archive, remote_source_dir
            )
        return remote_source_dir

    def release(_inputs: TaskInputs, _state: str) -> None:
        if strict_cleanup:
            remove_source_archive(
                provider,
                request,
                remote_archive=remote_archive,
                remote_source_dir=remote_source_dir,
            )
            return
        with contextlib.suppress(RuntimeError):
            provider.exec_argv(request, argv=("rm", "-rf", remote_source_dir))

    return Resource(
        title=f"Acquire source archive at {remote_source_dir}",
        acquire=acquire,
        release=release,
        always_release=archive is not None,
    )
