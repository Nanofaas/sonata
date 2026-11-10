from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from sonata_engine import TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult
from sonata_tasks.terraform import TerraformSpec, TerraformTask


@dataclass
class Executor:
    responses: list[tuple[int, str]] = field(default_factory=list)
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def binding_key(self, role):
        return f"local-test:{role}"

    def run(self, task, *, dry_run=False):
        self.seen.append(task)
        code, stdout = self.responses.pop(0) if self.responses else (0, "")
        if task.argv[1] == "plan" and code in (0, 2):
            (task.options.cwd / ".sonata-terraform" / "plan").write_bytes(b"plan")
        return TaskResult(
            task_id="",
            status="passed" if code in task.options.expected_exit_codes else "failed",
            return_code=code,
            stdout=stdout,
        )


@pytest.fixture
def module(tmp_path):
    (tmp_path / "main.tf").write_text('resource "terraform_data" "test" {}')
    return tmp_path


@pytest.mark.parametrize("binary", ["tofu", "terraform"])
@pytest.mark.parametrize(("code", "changed"), [(0, False), (2, True)])
def test_plan_exit_codes_and_saved_plan_application(module, binary, code, changed):
    spec = TerraformSpec(module, owner="run-1", inputs=("main.tf",), binary=binary)
    executor = Executor([(code, "human prose is not parsed"), (0, "")])
    result = (
        TerraformTask(spec, "plan", executor=executor).run(TaskInputs.empty()).value
    )
    assert result.changed is changed
    TerraformTask(spec, "apply", executor=executor).run(TaskInputs.empty())
    assert executor.seen[0].argv[:3] == (binary, "plan", "-input=false")
    assert "-detailed-exitcode" in executor.seen[0].argv
    assert executor.seen[1].argv[-1] == str(module / ".sonata-terraform" / "plan")
    assert executor.seen[1].options.timeout_seconds == 600


def test_failure_has_private_diagnostics_not_secret_in_exception(module):
    spec = TerraformSpec(module, owner="run-1", inputs=("main.tf",))
    with pytest.raises(RuntimeError) as caught:
        TerraformTask(spec, "plan", executor=Executor([(1, "password=hunter2")])).run(
            TaskInputs.empty()
        )
    assert "hunter2" not in str(caught.value)
    logs = list((module / ".sonata-terraform").glob("*.log"))
    assert logs and "hunter2" in logs[0].read_text()
    assert logs[0].stat().st_mode & 0o077 == 0


def test_output_selects_only_declared_non_sensitive_values(module):
    spec = TerraformSpec(module, owner="run-1", inputs=("main.tf",), outputs=("nodes",))
    payload = json.dumps(
        {
            "nodes": {
                "sensitive": False,
                "type": ["map", "string"],
                "value": {"a": "10.0.0.2"},
            },
            "password": {"sensitive": True, "type": "string", "value": "hunter2"},
        }
    )
    result = (
        TerraformTask(spec, "output", executor=Executor([(0, payload)]))
        .run(TaskInputs.empty())
        .value
    )
    assert result.outputs == {"nodes": {"a": "10.0.0.2"}}
    assert "hunter2" not in repr(result)


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "not json",
        "{}",
        '{"nodes":{"sensitive":true,"value":"secret"}}',
        '{"nodes":{"value":{}}}',
    ],
)
def test_output_rejects_malformed_missing_or_sensitive_selection(module, payload):
    spec = TerraformSpec(module, owner="run-1", inputs=("main.tf",), outputs=("nodes",))
    with pytest.raises(ValueError, match="Terraform"):
        TerraformTask(spec, "output", executor=Executor([(0, payload)])).run(
            TaskInputs.empty()
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("TF_WORKSPACE", "other"),
        ("TF_DATA_DIR", "/tmp/foreign"),
        ("TF_CLI_ARGS_apply", "-state=/tmp/foreign"),
    ],
)
def test_rejects_environment_state_redirects_before_commands(
    module, monkeypatch, key, value
):
    spec = TerraformSpec(module, owner="run-1", inputs=("main.tf",))
    executor = Executor()
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="Terraform"):
        TerraformTask(spec, "init", executor=executor).run(TaskInputs.empty())
    assert executor.seen == []


def test_input_mutation_is_rejected_before_execution(module):
    spec = TerraformSpec(module, owner="run-1", inputs=("main.tf",))
    task = TerraformTask(spec, "plan", executor=Executor())
    before = task._fingerprint_payload()
    (module / "main.tf").write_text('resource "terraform_data" "other" {}')
    with pytest.raises(ValueError, match="changed"):
        task.run(TaskInputs.empty())
    new = TerraformTask(
        TerraformSpec(module, owner="run-1", inputs=("main.tf",)),
        "plan",
        executor=Executor(),
    )
    assert new._fingerprint_payload() != before


def test_apply_without_saved_plan_cannot_mutate(module):
    spec = TerraformSpec(module, owner="run-1", inputs=("main.tf",))
    executor = Executor()
    with pytest.raises(ValueError, match="plan"):
        TerraformTask(spec, "apply", executor=executor).run(TaskInputs.empty())
    assert executor.seen == []


@pytest.mark.parametrize("inputs", [("../outside.tf",), ("/outside.tf",), ()])
def test_rejects_invalid_input_boundary(module, inputs):
    with pytest.raises(ValueError, match="Terraform"):
        TerraformSpec(module, owner="run-1", inputs=inputs)
