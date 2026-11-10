"""Run Terraform/OpenTofu operations with private diagnostics and frozen inputs."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, override
from uuid import uuid4

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.core.fingerprint import fingerprint_digest
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec
from sonata_tasks.execution.ports import CommandTaskExecutor

TerraformAction = Literal["init", "plan", "apply", "output", "destroy"]
_ACTIONS = {"init", "plan", "apply", "output", "destroy"}


@dataclass(frozen=True, slots=True)
class TerraformSpec:
    """Identify a prepared local module and its immutable input files.

    Include all module files, variable files and the provider lock file in
    ``inputs``. The executor must capture output privately: Terraform JSON
    and diagnostic output can contain secrets.
    """

    directory: Path
    owner: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...] = ()
    binary: str = "tofu"
    timeout_seconds: float = 600
    lock_timeout_seconds: int = 60
    parallelism: int = 1
    var_files: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        """Freeze and validate paths, selection and command bounds."""
        directory = Path(self.directory).absolute()
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Terraform directory must be an existing real directory")
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise ValueError("Terraform owner must be non-empty")
        if not isinstance(self.binary, str) or not self.binary.strip():
            raise ValueError("Terraform executable must be non-empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Terraform timeout must be finite and positive")
        if type(self.lock_timeout_seconds) is not int or self.lock_timeout_seconds <= 0:
            raise ValueError("Terraform lock timeout must be a positive integer")
        if type(self.parallelism) is not int or self.parallelism <= 0:
            raise ValueError("Terraform parallelism must be a positive integer")
        for name in ("inputs", "outputs", "var_files"):
            values = tuple(getattr(self, name))
            if any(not isinstance(v, str) or not v for v in values) or len(
                set(values)
            ) != len(values):
                raise ValueError(
                    f"Terraform {name} must contain unique non-empty strings"
                )
            object.__setattr__(self, name, values)
        if not self.inputs or not set(self.var_files) <= set(self.inputs):
            raise ValueError("Terraform inputs must include its variable files")
        if any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in self.env.items()
        ):
            raise ValueError("Terraform environment must map strings to strings")
        object.__setattr__(self, "directory", directory)
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "digest", self.input_digest())

    def input_digest(self) -> str:
        """Hash declared files without allowing paths to escape the module."""
        hashes = {}
        for name in self.inputs:
            relative = Path(name)
            path = self.directory / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or any(
                    parent.is_symlink()
                    for parent in (path, *path.parents)
                    if parent != self.directory.parent
                )
            ):
                raise ValueError(
                    "Terraform inputs must be real files inside the module"
                )
            if not path.is_file():
                raise ValueError(f"Terraform input is missing: {name}")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if config := self.env.get("TF_CLI_CONFIG_FILE"):
            path = Path(config).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise ValueError("Terraform CLI configuration must be an absolute file")
            hashes["@cli-config"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return fingerprint_digest(hashes)

    def verify(self) -> None:
        """Reject changed or undeclared configuration before executing it."""
        if self.input_digest() != self.digest:
            raise ValueError("Terraform inputs changed after the stack was specified")
        patterns = (
            "*.tf",
            "*.tf.json",
            "*.tofu",
            "*.tofu.json",
            "*.auto.tfvars",
            "*.auto.tfvars.json",
            "terraform.tfvars",
            "terraform.tfvars.json",
            ".terraform.lock.hcl",
        )
        for pattern in patterns:
            if any(p.name not in self.inputs for p in self.directory.glob(pattern)):
                raise ValueError("Terraform configuration contains undeclared inputs")


@dataclass(frozen=True, slots=True)
class TerraformResult:
    """Safe workflow value: change status and selected non-sensitive outputs."""

    changed: bool = False
    outputs: dict[str, Any] = field(default_factory=dict)


class TerraformCommandError(RuntimeError):
    """A failed CLI operation whose details remain in a private diagnostic file."""

    def __init__(
        self, action: str, diagnostic: Path, *, uncertain: bool = False
    ) -> None:
        """Report a safe summary, retaining whether process completion is unknown."""
        self.uncertain = uncertain
        super().__init__(
            f"Terraform {action} failed; private diagnostics: {diagnostic}"
        )


def _private_dir(spec: TerraformSpec) -> Path:
    path = spec.directory / ".sonata-terraform"
    if path.is_symlink():
        raise ValueError("Terraform private directory must not be a symlink")
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def _environment(spec: TerraformSpec) -> dict[str, str]:
    effective = {**os.environ, **spec.env}
    for key, value in effective.items():
        if value and (
            (
                (key.startswith("TF_VAR_") or key == "TF_CLI_CONFIG_FILE")
                and key not in spec.env
            )
            or key == "TF_DATA_DIR"
            or key.startswith("TF_CLI_ARGS")
            or (key == "TF_WORKSPACE" and value != "default")
        ):
            raise ValueError(f"Terraform state/command override is unsupported: {key}")
    return {
        **spec.env,
        "TF_CLI_CONFIG_FILE": spec.env.get("TF_CLI_CONFIG_FILE") or os.devnull,
        "TF_INPUT": "0",
        "TF_IN_AUTOMATION": "1",
        "TF_WORKSPACE": "default",
        "TF_LOG": "",
        "TF_LOG_PATH": "",
    }


class TerraformTask(Task[TerraformResult]):
    """Execute one CLI operation; apply consumes the previously saved plan."""

    def __init__(
        self,
        spec: TerraformSpec,
        action: TerraformAction,
        *,
        executor: CommandTaskExecutor,
        role: str = "host",
    ) -> None:
        """Configure an explicit operation on the prepared module."""
        if action not in _ACTIONS:
            raise ValueError("unsupported Terraform action")
        self.spec = spec
        self.action = action
        self.executor = executor
        self.role = role
        self.title = f"Terraform {action}"

    @override
    def _fingerprint_payload(self) -> object:
        return {
            "digest": fingerprint_digest(
                {
                    "spec": (
                        self.spec.owner,
                        str(self.spec.directory),
                        self.spec.digest,
                        self.spec.binary,
                        self.spec.outputs,
                        self.spec.var_files,
                        dict(self.spec.env),
                        self.spec.timeout_seconds,
                        self.spec.lock_timeout_seconds,
                        self.spec.parallelism,
                        self.action,
                        self.executor.binding_key(self.role),
                    )
                }
            )
        }

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[TerraformResult]:
        """Run with bounded locking and keep raw output out of the journal."""
        del inputs
        self.spec.verify()
        env = _environment(self.spec)
        private = _private_dir(self.spec)
        plan = private / "plan"
        args: list[str] = [self.spec.binary, self.action]
        if self.action == "output":
            args += ["-json", "-no-color"]
        else:
            args += ["-input=false", "-no-color"]
            if self.action == "init":
                args += ["-lockfile=readonly"]
            else:
                args += [f"-lock-timeout={self.spec.lock_timeout_seconds}s"]
                args += [f"-parallelism={self.spec.parallelism}"]
            if self.action == "plan":
                args += ["-detailed-exitcode", f"-out={plan}"]
            elif self.action == "apply":
                if not plan.is_file() or plan.is_symlink():
                    raise ValueError("Terraform apply requires its saved plan")
                args += [str(plan)]
            elif self.action == "destroy":
                args += ["-auto-approve"]
            if self.action in ("plan", "destroy"):
                args += [
                    f"-var-file={self.spec.directory / p}" for p in self.spec.var_files
                ]
        expected = frozenset({0, 2}) if self.action == "plan" else frozenset({0})
        command = CommandTaskSpec(
            task_id="",
            summary=self.title,
            argv=tuple(args),
            role=self.role,
            options=CommandOptions(
                cwd=self.spec.directory,
                env=env,
                expected_exit_codes=expected,
                timeout_seconds=self.spec.timeout_seconds,
            ),
        )
        diagnostic = private / f"{self.action}-{uuid4().hex}.log"
        with os.fdopen(
            os.open(diagnostic, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as log:
            try:
                result = self.executor.run(command)
            except Exception as error:
                log.write(str(error))
                raise TerraformCommandError(
                    self.action, diagnostic, uncertain=True
                ) from None
            log.write(result.stdout + "\n" + result.stderr)
        if (
            type(result.return_code) is not int
            or result.return_code not in expected
            or result.status != "passed"
        ):
            raise TerraformCommandError(self.action, diagnostic)
        selected = {}
        if self.action == "output":
            try:
                payload = json.loads(result.stdout)
                if not isinstance(payload, dict):
                    raise ValueError
                for name in self.spec.outputs:
                    item = payload[name]
                    if (
                        not isinstance(item, dict)
                        or item.get("sensitive") is not False
                        or "value" not in item
                    ):
                        raise ValueError
                    selected[name] = item["value"]
            except (ValueError, KeyError, TypeError):
                raise ValueError(
                    "Terraform selected output is missing, malformed or sensitive"
                ) from None
        return TaskOutcome(
            value=TerraformResult(changed=result.return_code == 2, outputs=selected)
        )


def terraform_stack_resource(
    spec: TerraformSpec,
    *,
    executor: CommandTaskExecutor,
    role: str = "host",
    requires: tuple[Resource[Any], ...] = (),
    retain: Callable[[], str | None] | None = None,
) -> Resource[TerraformResult]:
    """Own one default-workspace local-state stack, retaining uncertain failures.

    ``retain`` supplies a reason to preserve the stack on release. Once
    retained or uncertain, automatic operations refuse destructive recovery.
    Inspect its private owner record and state before an explicit recovery.
    """
    from sonata_tasks._terraform_stack import owned_stack

    return owned_stack(
        spec, executor=executor, role=role, requires=requires, retain=retain
    )
