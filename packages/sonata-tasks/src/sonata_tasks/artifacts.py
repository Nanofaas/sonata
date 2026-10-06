"""Exclusive, bounded artifact storage and strict streaming JSONL decoding.

Published documents never replace an existing name. Append writes retain and
charge partial evidence. Ownership markers persist after close; this is a fresh
artifact directory, not a general store that resumes or adopts existing data.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Lock
from typing import Any, override

MAX_RECORD_BYTES = 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_MARKER = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_.-]*")


class ArtifactLimitExceededError(OSError):
    """Evidence could not be written without exceeding its declared disk budget."""


class ArtifactCorruptionError(ValueError):
    """A complete evidence record is malformed and cannot be silently discarded."""


class IncompleteRecordError(ArtifactCorruptionError):
    """A bounded final record lacks its newline; the valid prefix was yielded."""

    @override
    def __init__(self, path: Path, line_number: int) -> None:
        """Retain the location so callers can provide their own gap policy."""
        self.path = path
        self.line_number = line_number
        super().__init__(f"{path}:{line_number}: incomplete final record")


def encode_record(value: dict[str, object]) -> bytes:
    """Encode one record exactly as `ArtifactWriter` publishes it, newline included."""
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def describe_artifact(path: Path) -> dict[str, object]:
    """Hash an artifact with fixed memory usage, including its byte count."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(65536):
            size += len(chunk)
            digest.update(chunk)
    return {"path": str(path), "size_bytes": size, "sha256": digest.hexdigest()}


class ArtifactWriter:
    """Own a directory and persist bounded observations.

    Default accounting charges this writer's published and appended bytes.
    An explicit usage callback can include other producers and direct writes;
    callers must serialize those producers themselves. Reserved space is
    accessible only through ``write_json(..., use_reserve=True)``.
    """

    def __init__(
        self,
        root: Path,
        limit_bytes: int,
        *,
        owner_marker: str = ".artifact-owner",
        reserve_bytes: int = 0,
        measure_usage: Callable[[], int] | None = None,
    ) -> None:
        """Exclusively acquire an empty directory with an explicit byte budget."""
        if type(limit_bytes) is not int or limit_bytes <= 0:
            raise ValueError("artifact limit must be a positive integer")
        if type(reserve_bytes) is not int or not 0 <= reserve_bytes < limit_bytes:
            raise ValueError("artifact reserve must be an integer below the limit")
        if not isinstance(owner_marker, str) or _MARKER.fullmatch(owner_marker) is None:
            raise ValueError("owner marker must be a single safe hidden filename")
        if measure_usage is not None and not callable(measure_usage):
            raise ValueError("artifact usage measurement must be callable")
        try:
            import fcntl
        except ImportError as error:
            raise ValueError("artifact ownership requires directory locking") from error
        if root.is_symlink():
            raise ValueError("artifact root cannot be a symbolic link")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Lock the directory identity while checking and claiming it. Different
        # marker choices must contend on the same acquisition, not separate files.
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise FileExistsError(
                    f"artifact directory is being acquired: {root}"
                ) from error
            if any(root.iterdir()):
                raise FileExistsError(
                    f"run directory already contains evidence: {root}"
                )
            descriptor = os.open(
                root / owner_marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(descriptor)
        finally:
            os.close(directory)
        self.root = root
        self.limit_bytes = limit_bytes
        self._reserve = reserve_bytes
        self._measure_usage = measure_usage
        self._used_bytes = 0
        self._closed = False
        self._lock = Lock()

    def _target(self, name: str) -> Path:
        if _NAME.fullmatch(name) is None:
            raise ValueError("artifact name must be a single safe path component")
        return self.root / name

    def _check_budget(self, size: int, *, use_reserve: bool = False) -> None:
        if self._closed:
            raise RuntimeError("artifact writer is closed")
        budget = self.limit_bytes if use_reserve else self.limit_bytes - self._reserve
        used = (
            self._measure_usage()
            if self._measure_usage is not None
            else self._used_bytes
        )
        if type(used) is not int or used < 0:
            raise ValueError("artifact usage must be a nonnegative integer")
        if used + size > budget:
            raise ArtifactLimitExceededError(
                "artifact budget exhausted; space is reserved"
            )

    def _check_write(self, size: int, *, use_reserve: bool = False) -> None:
        if self._closed:
            raise RuntimeError("artifact writer is closed")
        if size > MAX_RECORD_BYTES:
            raise ArtifactLimitExceededError(
                "individual evidence record exceeds its size limit"
            )
        self._check_budget(size, use_reserve=use_reserve)

    def append(self, stream: str, record: dict[str, object]) -> None:
        """Append one complete JSONL record, accounting for partial writes."""
        target = self._target(stream)
        target = target.with_name(target.name + ".jsonl")
        payload = encode_record(record)
        with self._lock:
            self._check_write(len(payload))
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            with os.fdopen(descriptor, "ab", buffering=0) as output:
                before = os.fstat(output.fileno()).st_size
                try:
                    remaining = memoryview(payload)
                    while remaining:
                        written = output.write(remaining)
                        if written is None or written <= 0:
                            raise OSError("evidence append made no progress")
                        remaining = remaining[written:]
                finally:
                    self._used_bytes += max(
                        0, os.fstat(output.fileno()).st_size - before
                    )

    def write_json(
        self, name: str, value: dict[str, object], *, use_reserve: bool = False
    ) -> Path:
        """Publish a new immutable document, never replacing an evaluation."""
        target = self._target(name)
        if target.suffix != ".json":
            raise ValueError("complete JSON artifact names must end with .json")
        payload = encode_record(value)
        with self._lock:
            self._check_write(len(payload), use_reserve=use_reserve)
            descriptor, filename = tempfile.mkstemp(prefix=".pending-", dir=self.root)
            temporary = Path(filename)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                # A hard link publishes complete content atomically and fails if an
                # artifact (including a symbolic link) already owns the target name.
                os.link(temporary, target)
                self._used_bytes += len(payload)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    def write_blob(self, directory: str, name: str, body: bytes) -> Path:
        """Publish immutable raw evidence without imposing the JSON-record cap."""
        parent = self._target(directory)
        if _NAME.fullmatch(name) is None:
            raise ValueError("artifact name must be a single safe path component")
        return self._write_raw(parent / name, body)

    def write_file(self, name: str, body: bytes) -> Path:
        """Publish a raw input directly under the evidence owner."""
        return self._write_raw(self._target(name), body)

    def _write_raw(self, target: Path, body: bytes) -> Path:
        parent = target.parent
        with self._lock:
            self._check_budget(len(body))
            if parent.is_symlink():
                raise ValueError("raw evidence directory cannot be a symbolic link")
            parent.mkdir(exist_ok=True, mode=0o700)
            fd, filename = tempfile.mkstemp(prefix=".pending-", dir=parent)
            temporary = Path(filename)
            published = False
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, target)
                published = True
            finally:
                # A later cleanup error must not leave a published file uncharged.
                if published:
                    self._used_bytes += len(body)
                temporary.unlink(missing_ok=True)
        return target

    def close(self) -> None:
        """Idempotently stop future writes; evidence and ownership marker remain."""
        with self._lock:
            self._closed = True


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"non-finite JSON float: {value}")
    return result


def read_records(path: Path) -> Iterator[dict[str, Any]]:
    """Stream strict JSON objects; distinguish an incomplete final record."""
    with path.open("rb") as source:
        line_number = 0
        while payload := source.readline(MAX_RECORD_BYTES + 1):
            line_number += 1
            if len(payload) > MAX_RECORD_BYTES:
                raise ArtifactCorruptionError(
                    f"{path}:{line_number}: record exceeds size limit"
                )
            if not payload.endswith(b"\n"):
                raise IncompleteRecordError(path, line_number)
            try:
                record = json.loads(
                    payload, parse_constant=_reject_constant, parse_float=_finite_float
                )
            except (ValueError, UnicodeDecodeError) as error:
                raise ArtifactCorruptionError(
                    f"{path}:{line_number}: malformed record"
                ) from error
            if not isinstance(record, dict):
                raise ArtifactCorruptionError(
                    f"{path}:{line_number}: record must be an object"
                )
            yield record
