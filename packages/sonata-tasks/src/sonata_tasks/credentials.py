"""Private file validation and temporary staging on trusted POSIX targets."""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from tempfile import mkdtemp

from sonata_tasks.transfer import RemoteOperationResult, RemoteProvider


class CredentialCleanupError(RuntimeError):
    """A failed cleanup exposing only the interrupted operation's type name."""

    def __init__(self, operation_type: str) -> None:
        """Record the operation type without its potentially private message."""
        self.operation_type = operation_type
        super().__init__(f"credential cleanup failed after {operation_type}")


def _require_posix() -> None:
    if not all(hasattr(os, name) for name in ("getuid", "O_NOFOLLOW", "O_NONBLOCK")):
        raise NotImplementedError("private file staging requires POSIX file access")


def _validate_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("private file must be a regular file")
    if metadata.st_uid != os.getuid():
        raise PermissionError("private file must be owned by the current user")
    if metadata.st_mode & 0o077:
        raise PermissionError(
            "private file permissions must deny group and world access"
        )
    if not metadata.st_mode & stat.S_IRUSR:
        raise PermissionError("private file must be owner-readable")
    if metadata.st_size == 0:
        raise ValueError("private file must not be empty")


def validate_private_file(path: Path) -> Path:
    """Require a nonempty, current-user-owned, owner-only readable regular file.

    The source's parent namespace must be controlled by the caller. Validation
    does not reserve the path; staging rechecks it through a no-follow descriptor.
    """
    if not isinstance(path, Path):
        raise TypeError("private file must be provided as a file path")
    _require_posix()
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError("private file must be a regular file") from None
    _validate_metadata(metadata)
    return path


def _copy_private_file(source: Path, destination: Path) -> None:
    try:
        before = source.lstat()
    except OSError:
        raise ValueError("private file must be a regular file") from None
    _validate_metadata(before)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError:
        raise ValueError("private file must be a regular file") from None
    with os.fdopen(source_fd, "rb") as source_stream:
        opened = os.fstat(source_stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("private file changed while being staged")
        _validate_metadata(opened)
        destination_fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(destination_fd, "wb") as destination_stream:
            shutil.copyfileobj(source_stream, destination_stream)
    destination.chmod(0o600)


def _require_success(result: RemoteOperationResult) -> RemoteOperationResult:
    status = getattr(result, "return_code", None)
    if not isinstance(status, int) or isinstance(status, bool):
        raise RuntimeError("private file operation returned no integer return_code")
    if status != 0:
        raise RuntimeError("private file operation failed")
    return result


def _command[RequestT](
    provider: RemoteProvider[RequestT], request: RequestT, argv: tuple[str, ...]
) -> RemoteOperationResult:
    try:
        result = provider.exec_argv(request, argv)
    except (OSError, RuntimeError):
        raise RuntimeError("remote private file command failed") from None
    return _require_success(result)


def _transfer[RequestT](
    provider: RemoteProvider[RequestT],
    request: RequestT,
    source: Path,
    destination: str,
) -> None:
    try:
        result = provider.transfer_to(request, source=source, destination=destination)
    except (OSError, RuntimeError):
        raise RuntimeError("private file transfer failed") from None
    _require_success(result)


@contextmanager
def _cleanup_after(cleanup: Callable[[], None]) -> Iterator[None]:
    try:
        yield
    except BaseException as operation_error:
        try:
            cleanup()
        except BaseException:
            raise CredentialCleanupError(type(operation_error).__name__) from None
        raise
    else:
        try:
            cleanup()
        except BaseException as cleanup_error:
            raise CredentialCleanupError(type(cleanup_error).__name__) from None


def _validate_component(value: str) -> None:
    if not isinstance(value, str):
        raise TypeError("private file name and prefix must be strings")
    if value in {".", ".."} or re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None:
        raise ValueError("private file name and prefix must be safe basenames")


@contextmanager
def stage_private_files[RequestT](
    provider: RemoteProvider[RequestT],
    request: RequestT,
    files: Mapping[str, Path],
    *,
    prefix: str = "sonata-credentials",
) -> Iterator[tuple[str, dict[str, str]]]:
    """Yield remote private file paths, then remove local and remote copies.

    Use trusted providers and caller-controlled filesystem namespaces on POSIX.
    Names and prefix are public metadata, not credential contents. The remote
    target must support mktemp, chmod and rm. Copies are 0600 under 0700 directories.
    Operational provider failures are sanitized; body/programming exceptions
    survive successful cleanup. Failed cleanup raises CredentialCleanupError
    without private exception messages. An invalid mktemp response is never
    used as a guessed cleanup destination.
    """
    _require_posix()
    _validate_component(prefix)
    if not files:
        raise ValueError("private file staging requires at least one file")
    validated: dict[str, Path] = {}
    for name, source in files.items():
        _validate_component(name)
        validated[name] = validate_private_file(source)
    local_dir = Path(mkdtemp(prefix=f"{prefix}-"))
    with _cleanup_after(lambda: shutil.rmtree(local_dir)):
        try:
            local_dir.chmod(0o700)
            staged: dict[str, Path] = {}
            for name, source in validated.items():
                destination = local_dir / name
                _copy_private_file(source, destination)
                staged[name] = destination
        except OSError:
            raise RuntimeError("local private file staging failed") from None
        # mktemp creates this remote directory atomically; this is not a local
        # hard-coded temporary path (B108).
        template = f"/tmp/{prefix}.XXXXXX"  # nosec B108
        result = _command(provider, request, ("mktemp", "-d", template))
        output = getattr(result, "stdout", None)
        remote_dir = output.strip() if isinstance(output, str) else ""
        pattern = re.escape(f"/tmp/{prefix}.") + r"[A-Za-z0-9]{6}"  # nosec B108
        if re.fullmatch(pattern, remote_dir) is None:
            raise RuntimeError("remote private directory returned an unsafe path")

        def remove_remote() -> None:
            _command(provider, request, ("rm", "-rf", "--", remote_dir))

        with _cleanup_after(remove_remote):
            _command(provider, request, ("chmod", "700", remote_dir))
            paths: dict[str, str] = {}
            for name, source in staged.items():
                destination = f"{remote_dir}/{name}"
                _transfer(provider, request, source, destination)
                _command(provider, request, ("chmod", "600", destination))
                paths[name] = destination
            yield remote_dir, paths
