"""Ownership and recovery for the public Terraform stack resource."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, override
from uuid import uuid4

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.compensation import best_effort
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.terraform import (
    TerraformAction,
    TerraformCommandError,
    TerraformResult,
    TerraformSpec,
    TerraformTask,
    _environment,
    _private_dir,
)


def _read(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError("Terraform state/ownership path must not be a symlink")
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, OSError):
        raise RuntimeError(
            "Terraform state/ownership record is unreadable; retain for recovery"
        ) from None


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".owner-{uuid4().hex}")
    try:
        with os.fdopen(
            os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


class _Stack:
    def __init__(
        self,
        spec: TerraformSpec,
        executor: CommandTaskExecutor,
        role: str,
        retain: Callable[[], str | None] | None,
    ) -> None:
        self.spec, self.executor, self.role, self.retain = spec, executor, role, retain
        self.identity = TerraformTask(
            spec, "init", executor=executor, role=role
        )._fingerprint_payload()

    def run(self, action: TerraformAction, inputs: TaskInputs) -> TerraformResult:
        value = (
            TerraformTask(self.spec, action, executor=self.executor, role=self.role)
            .run(inputs)
            .value
        )
        assert value is not None
        return value

    @contextmanager
    def locked(self) -> Iterator[Path]:
        self.spec.verify()
        _environment(self.spec)
        private = _private_dir(self.spec)
        self.spec.directory.chmod(0o700)
        with os.fdopen(
            os.open(private / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600),
            "w",
        ) as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Terraform stack is busy; retry after its owner finishes"
                ) from None
            try:
                self.local_backend()
                yield private / "owner.json"
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def local_backend(self) -> None:
        directory = self.spec.directory
        workspace = directory / ".terraform" / "environment"
        if workspace.exists() and workspace.read_text().strip() != "default":
            raise RuntimeError("Terraform stack requires the default workspace")
        metadata = directory / ".terraform" / "terraform.tfstate"
        if metadata.exists():
            backend = _read(metadata).get("backend")
            if backend:
                config = backend.get("config", {})
                if (
                    backend.get("type") != "local"
                    or config.get("path") not in (None, "", "terraform.tfstate")
                    or config.get("workspace_dir")
                ):
                    raise RuntimeError(
                        "Terraform owned stack requires the default local backend"
                    )
        if (directory / ".terraform").is_symlink():
            raise RuntimeError("Terraform backend directory must not be a symlink")

    def state(self) -> dict[str, Any] | None:
        path = self.spec.directory / "terraform.tfstate"
        return _read(path) if path.exists() or path.is_symlink() else None

    def load(self, path: Path) -> dict[str, Any]:
        record = _read(path)
        if record.get("identity") != self.identity:
            raise RuntimeError(
                "Terraform ownership/configuration changed; refusing foreign state"
            )
        if record.get("requires_retention") and self.retain is None:
            raise RuntimeError(
                "Terraform stack retained: original guard required for recovery"
            )
        state = self.state()
        if record.get("lineage") and (
            state is None or state.get("lineage") != record["lineage"]
        ):
            raise RuntimeError(
                "Terraform state lineage changed; refusing foreign state"
            )
        return record

    def destroy(
        self,
        path: Path,
        record: dict[str, Any],
        inputs: TaskInputs,
        *,
        compensating: bool = False,
    ) -> None:
        record["phase"] = "destroying"
        _write(path, record)
        try:
            self.run("destroy", inputs)
            state = self.state()
            if state is None or state.get("resources"):
                raise RuntimeError(
                    "Terraform destroy left unconfirmed state; recovery required"
                )
        except BaseException as error:
            record["phase"] = "uncertain"
            best_effort(
                error, lambda: _write(path, record), what="recording uncertain teardown"
            )
            raise
        record["phase"] = "uncertain" if compensating else "destroyed"
        _write(path, record)

    def acquire(self, inputs: TaskInputs) -> TerraformResult:
        with self.locked() as path:
            if path.exists():
                record = self.load(path)
            else:
                if (
                    self.state() is not None
                    or (self.spec.directory / "terraform.tfstate.backup").exists()
                ):
                    raise RuntimeError(
                        "Terraform refusing foreign state without ownership"
                    )
                record = {
                    "identity": self.identity,
                    "phase": "prepared",
                    "lineage": None,
                    "requires_retention": self.retain is not None,
                }
                _write(path, record)
            phase = record.get("phase")
            if phase not in ("prepared", "active", "destroyed"):
                raise RuntimeError(
                    f"Terraform stack is {phase}; retained for explicit recovery"
                )
            self.run("init", inputs)
            self.local_backend()
            plan = self.run("plan", inputs)
            if phase == "active":
                if plan.changed:
                    raise RuntimeError(
                        "Terraform drift requires changes; retained for recovery"
                    )
                return self.run("output", inputs)
            record["phase"] = "applying"
            _write(path, record)
            try:
                self.run("apply", inputs)
                state = self.state()
                if state is None or not state.get("lineage"):
                    raise TerraformCommandError("apply", path, uncertain=True)
                record["lineage"] = state["lineage"]
                result = self.run("output", inputs)
                record["phase"] = "active"
                _write(path, record)
                return result
            except BaseException as error:
                if (
                    isinstance(error, TerraformCommandError) and not error.uncertain
                ) or isinstance(error, ValueError):
                    try:
                        state = self.state()
                        if state is None:
                            raise RuntimeError(
                                "Terraform partial apply has no durable state; "
                                "retain for recovery"
                            )
                        record["lineage"] = state.get("lineage")
                        # Never authorize retry after potentially unrecorded creation.
                        self.destroy(path, record, inputs, compensating=True)
                    except BaseException as cleanup:
                        record["phase"] = "uncertain"
                        best_effort(
                            error,
                            lambda: _write(path, record),
                            what="recording failed compensation",
                        )
                        error.add_note(f"Terraform compensation failed: {cleanup}")
                else:
                    record["phase"] = "uncertain"
                    best_effort(
                        error,
                        lambda: _write(path, record),
                        what="recording uncertain apply",
                    )
                raise

    def release(self, inputs: TaskInputs) -> None:
        with self.locked() as path:
            record = self.load(path)
            if record.get("phase") == "destroyed":
                return
            if record.get("phase") != "active":
                raise RuntimeError("Terraform stack retained for explicit recovery")
            if self.retain is not None:
                record["phase"] = "checking_release"
                _write(path, record)
            reason = self.retain() if self.retain else None
            if reason:
                record["phase"] = "retained"
                _write(path, record)
                raise RuntimeError(
                    "Terraform stack retained: caller requires evidence recovery"
                )
            self.destroy(path, record, inputs)


class _Acquire(Task[TerraformResult]):
    idempotent = True

    def __init__(self, stack: _Stack) -> None:
        self.stack = stack
        self.title = f"Acquire Terraform stack {stack.spec.owner}"

    @override
    def _fingerprint_payload(self) -> object:
        return self.stack.identity

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[TerraformResult]:
        return TaskOutcome(value=self.stack.acquire(inputs))


class _Release(Task[None]):
    def __init__(self, stack: _Stack) -> None:
        self.stack = stack
        self.title = f"Release Terraform stack {stack.spec.owner}"

    @override
    def _fingerprint_payload(self) -> object:
        return self.stack.identity

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        self.stack.release(inputs)
        return TaskOutcome()


def owned_stack(
    spec: TerraformSpec,
    *,
    executor: CommandTaskExecutor,
    role: str,
    requires: tuple[Resource[Any], ...],
    retain: Callable[[], str | None] | None,
) -> Resource[TerraformResult]:
    """Build the public Resource without exporting the ownership implementation."""
    stack = _Stack(spec, executor, role, retain)
    acquire = _Acquire(stack)
    return Resource(acquire.title, acquire, _Release(stack), requires=requires)
