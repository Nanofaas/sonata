from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import override

import pytest

from sonata_engine import TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult
from sonata_tasks.terraform import (
    TerraformSpec,
    TerraformTask,
    terraform_stack_resource,
)


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
        if task.argv[1] == "apply":
            (task.options.cwd / "terraform.tfstate").write_text(
                json.dumps(
                    {
                        "version": 4,
                        "lineage": "test-owned",
                        "resources": [{"type": "terraform_data"}],
                    }
                )
            )
        if task.argv[1] == "destroy" and code == 0:
            (task.options.cwd / "terraform.tfstate").write_text(
                json.dumps({"version": 4, "lineage": "test-owned", "resources": []})
            )
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
    assert result is not None
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
    assert result is not None
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


def stack_spec(module):
    return TerraformSpec(module, owner="run-1", inputs=("main.tf",), outputs=("nodes",))


OUTPUT = json.dumps(
    {
        "nodes": {
            "sensitive": False,
            "type": ["map", "string"],
            "value": {"a": "10.0.0.2"},
        }
    }
)


def test_owned_stack_applies_and_releases_only_after_consumer(module):
    executor = Executor([(0, ""), (2, ""), (0, ""), (0, OUTPUT), (0, "")])
    resource = terraform_stack_resource(stack_spec(module), executor=executor)
    result = resource.acquire.run(TaskInputs.empty()).value
    assert result is not None and result.outputs["nodes"] == {"a": "10.0.0.2"}
    assert [x.argv[1] for x in executor.seen] == ["init", "plan", "apply", "output"]
    resource.release.run(TaskInputs.empty())
    resource.release.run(TaskInputs.empty())
    assert [x.argv[1] for x in executor.seen].count("destroy") == 1


def test_foreign_state_never_triggers_cleanup(module):
    (module / "terraform.tfstate").write_text('{"lineage":"foreign"}')
    executor = Executor()
    resource = terraform_stack_resource(stack_spec(module), executor=executor)
    with pytest.raises(RuntimeError, match="foreign"):
        resource.acquire.run(TaskInputs.empty())
    assert executor.seen == []


def test_partial_apply_compensates_and_keeps_primary_failure(module):
    executor = Executor(
        [(0, ""), (2, ""), (1, "original secret"), (1, "cleanup secret")]
    )
    resource = terraform_stack_resource(stack_spec(module), executor=executor)
    with pytest.raises(RuntimeError, match="apply failed") as caught:
        resource.acquire.run(TaskInputs.empty())
    assert executor.seen[-1].argv[1] == "destroy"
    assert "secret" not in str(caught.value)
    assert any("destroy failed" in n for n in caught.value.__notes__)
    assert (
        json.loads((module / ".sonata-terraform" / "owner.json").read_text())["phase"]
        == "uncertain"
    )


def test_retention_blocks_destroy_and_survives_resource_reconstruction(module):
    executor = Executor([(0, ""), (2, ""), (0, ""), (0, OUTPUT)])
    spec = stack_spec(module)
    resource = terraform_stack_resource(
        spec, executor=executor, retain=lambda: "archive failed"
    )
    resource.acquire.run(TaskInputs.empty())
    with pytest.raises(RuntimeError, match="retained"):
        resource.release.run(TaskInputs.empty())
    reconstructed = terraform_stack_resource(spec, executor=executor)
    with pytest.raises(RuntimeError, match="retained"):
        reconstructed.release.run(TaskInputs.empty())
    assert all(x.argv[1] != "destroy" for x in executor.seen)


def test_resume_refreshes_outputs_without_apply_and_refuses_drift(module):
    executor = Executor(
        [(0, ""), (2, ""), (0, ""), (0, OUTPUT), (0, ""), (0, ""), (0, OUTPUT)]
    )
    resource = terraform_stack_resource(stack_spec(module), executor=executor)
    resource.acquire.run(TaskInputs.empty())
    resource.acquire.run(TaskInputs.empty())
    assert [x.argv[1] for x in executor.seen].count("apply") == 1
    executor.responses = [(0, ""), (2, "")]
    with pytest.raises(RuntimeError, match="drift"):
        resource.acquire.run(TaskInputs.empty())
    assert [x.argv[1] for x in executor.seen].count("apply") == 1
    assert all(x.argv[1] != "destroy" for x in executor.seen)


@pytest.mark.parametrize(
    "backend",
    [
        {"type": "s3", "config": {}},
        {"type": "local", "config": {"path": "/foreign.tfstate"}},
    ],
)
def test_custom_backend_is_refused_before_mutation(module, backend):
    metadata = module / ".terraform"
    metadata.mkdir()
    (metadata / "terraform.tfstate").write_text(json.dumps({"backend": backend}))
    executor = Executor()
    with pytest.raises(RuntimeError, match="backend"):
        terraform_stack_resource(stack_spec(module), executor=executor).acquire.run(
            TaskInputs.empty()
        )
    assert executor.seen == []


def test_interrupted_apply_keeps_uncertain_state_without_destroy(module):
    class TimeoutExecutor(Executor):
        @override
        def run(self, task, *, dry_run=False):
            if task.argv[1] == "apply":
                self.seen.append(task)
                raise TimeoutError("sensitive timeout detail")
            return super().run(task, dry_run=dry_run)

    executor = TimeoutExecutor([(0, ""), (2, "")])
    resource = terraform_stack_resource(stack_spec(module), executor=executor)
    with pytest.raises(RuntimeError, match="apply failed"):
        resource.acquire.run(TaskInputs.empty())
    with pytest.raises(RuntimeError, match="uncertain"):
        resource.acquire.run(TaskInputs.empty())
    assert all(x.argv[1] != "destroy" for x in executor.seen)


def test_parallelism_is_explicit_for_multipass_launch_safety(module):
    spec = TerraformSpec(module, owner="run-1", inputs=("main.tf",), parallelism=1)
    executor = Executor([(2, ""), (0, "")])
    TerraformTask(spec, "plan", executor=executor).run(TaskInputs.empty())
    TerraformTask(spec, "apply", executor=executor).run(TaskInputs.empty())
    assert "-parallelism=1" in executor.seen[-1].argv


@pytest.mark.parametrize("value", [0, -1, True])
def test_invalid_parallelism_is_refused(module, value):
    with pytest.raises(ValueError, match="parallelism"):
        TerraformSpec(module, owner="run-1", inputs=("main.tf",), parallelism=value)


def test_failed_recovery_record_does_not_replace_apply_error(module, monkeypatch):
    from sonata_tasks import _terraform_stack

    original = _terraform_stack._write

    def failing_write(path, record):
        if record.get("phase") == "uncertain":
            raise OSError("disk full")
        original(path, record)

    monkeypatch.setattr(_terraform_stack, "_write", failing_write)
    executor = Executor([(0, ""), (2, ""), (1, "apply error"), (1, "destroy error")])
    with pytest.raises(RuntimeError, match="apply failed") as caught:
        terraform_stack_resource(stack_spec(module), executor=executor).acquire.run(
            TaskInputs.empty()
        )
    assert any("disk full" in note for note in caught.value.__notes__)


def test_failed_apply_never_claims_no_untracked_resources_after_cleanup(module):
    executor = Executor([(0, ""), (2, ""), (1, "provider failure"), (0, "")])
    resource = terraform_stack_resource(stack_spec(module), executor=executor)
    with pytest.raises(RuntimeError, match="apply failed"):
        resource.acquire.run(TaskInputs.empty())
    assert json.loads((module / "terraform.tfstate").read_text())["resources"] == []
    with pytest.raises(RuntimeError, match="uncertain"):
        resource.acquire.run(TaskInputs.empty())


@pytest.mark.parametrize("key", ["TF_VAR_count", "TF_CLI_CONFIG_FILE"])
def test_undeclared_inherited_configuration_cannot_change_stack(
    module, monkeypatch, key
):
    spec = stack_spec(module)
    monkeypatch.setenv(key, "unexpected")
    executor = Executor()
    with pytest.raises(ValueError, match="override"):
        TerraformTask(spec, "plan", executor=executor).run(TaskInputs.empty())
    assert not executor.seen


def test_explicit_cli_config_bytes_are_frozen(module):
    config = module / "provider.tfrc"
    config.write_text("# original configuration")
    spec = TerraformSpec(
        module,
        owner="test",
        inputs=("main.tf",),
        env={"TF_CLI_CONFIG_FILE": str(config)},
    )
    before = TerraformTask(spec, "plan", executor=Executor())._fingerprint_payload()
    config.write_text("provider_installation { dev_overrides {} }")
    replacement = TerraformSpec(
        module,
        owner="test",
        inputs=("main.tf",),
        env={"TF_CLI_CONFIG_FILE": str(config)},
    )
    assert (
        TerraformTask(replacement, "plan", executor=Executor())._fingerprint_payload()
        != before
    )
    with pytest.raises(ValueError, match="changed"):
        TerraformTask(spec, "plan", executor=Executor()).run(TaskInputs.empty())


def test_default_cli_config_is_explicit_and_ignores_home_configuration(module):
    import os

    executor = Executor()
    TerraformTask(stack_spec(module), "init", executor=executor).run(TaskInputs.empty())
    assert executor.seen[0].options.env["TF_CLI_CONFIG_FILE"] == os.devnull


def test_failed_uncertain_write_after_compensation_cannot_allow_reapply(
    module, monkeypatch
):
    from sonata_tasks import _terraform_stack

    original = _terraform_stack._write

    def failing_write(path, record):
        if record.get("phase") == "uncertain":
            raise OSError("disk full")
        original(path, record)

    monkeypatch.setattr(_terraform_stack, "_write", failing_write)
    executor = Executor([(0, ""), (2, ""), (1, "apply failure"), (0, "")])
    resource = terraform_stack_resource(stack_spec(module), executor=executor)
    with pytest.raises(RuntimeError, match="apply failed"):
        resource.acquire.run(TaskInputs.empty())
    commands = len(executor.seen)
    with pytest.raises(RuntimeError, match="recovery"):
        resource.acquire.run(TaskInputs.empty())
    assert len(executor.seen) == commands


def test_retention_callback_failure_is_durable(module):
    executor = Executor([(0, ""), (2, ""), (0, ""), (0, OUTPUT)])

    def broken_guard():
        raise OSError("receipt unavailable")

    spec = stack_spec(module)
    resource = terraform_stack_resource(spec, executor=executor, retain=broken_guard)
    resource.acquire.run(TaskInputs.empty())
    with pytest.raises(OSError, match="receipt unavailable"):
        resource.release.run(TaskInputs.empty())
    resumed = terraform_stack_resource(spec, executor=executor, retain=lambda: None)
    with pytest.raises(RuntimeError, match="recovery"):
        resumed.release.run(TaskInputs.empty())
    assert all(task.argv[1] != "destroy" for task in executor.seen)


def test_failed_retention_record_cannot_allow_destroy_on_restart(module, monkeypatch):
    from sonata_tasks import _terraform_stack

    original = _terraform_stack._write

    def failing_write(path, record):
        if record.get("phase") == "retained":
            raise OSError("disk full")
        original(path, record)

    executor = Executor([(0, ""), (2, ""), (0, ""), (0, OUTPUT)])
    spec = stack_spec(module)
    resource = terraform_stack_resource(
        spec, executor=executor, retain=lambda: "archive missing"
    )
    resource.acquire.run(TaskInputs.empty())
    monkeypatch.setattr(_terraform_stack, "_write", failing_write)
    with pytest.raises(OSError, match="disk full"):
        resource.release.run(TaskInputs.empty())
    with pytest.raises(RuntimeError, match="recovery"):
        terraform_stack_resource(spec, executor=executor).release.run(
            TaskInputs.empty()
        )
    assert all(task.argv[1] != "destroy" for task in executor.seen)


def test_active_stack_cannot_drop_required_retention_guard(module):
    executor = Executor([(0, ""), (2, ""), (0, ""), (0, OUTPUT)])
    spec = stack_spec(module)
    terraform_stack_resource(spec, executor=executor, retain=lambda: None).acquire.run(
        TaskInputs.empty()
    )
    with pytest.raises(RuntimeError, match="retained"):
        terraform_stack_resource(spec, executor=executor).release.run(
            TaskInputs.empty()
        )
    assert all(task.argv[1] != "destroy" for task in executor.seen)
