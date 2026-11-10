"""Real CLI lifecycle using the built-in provider; no cloud account required."""

from __future__ import annotations

import json
import shutil

import pytest

from sonata_engine import TaskInputs
from sonata_tasks.execution.local import LocalCommandTaskExecutor
from sonata_tasks.terraform import TerraformSpec, terraform_stack_resource


@pytest.mark.parametrize("binary", ["tofu", "terraform"])
@pytest.mark.parametrize("fail", [False, True])
def test_real_owned_stack_lifecycle(tmp_path, binary, fail):
    executable = shutil.which(binary)
    if executable is None:
        pytest.skip(f"{binary} is not installed")
    provisioner = 'provisioner "local-exec" { command = "exit 1" }' if fail else ""
    (tmp_path / "main.tf").write_text(
        'resource "terraform_data" "example" {\n input = "owned-value"\n'
        + provisioner
        + '\n}\noutput "value" { value = terraform_data.example.output }\n'
    )
    spec = TerraformSpec(
        tmp_path,
        owner="real-cli",
        inputs=("main.tf",),
        outputs=("value",),
        binary=executable,
    )
    resource = terraform_stack_resource(spec, executor=LocalCommandTaskExecutor())
    inputs = TaskInputs.empty()
    if fail:
        with pytest.raises(RuntimeError, match="apply failed"):
            resource.acquire.run(inputs)
    else:
        try:
            first = resource.acquire.run(inputs).value
            again = resource.acquire.run(inputs).value
            assert first is not None and again is not None
            assert first.outputs == again.outputs == {"value": "owned-value"}
        finally:
            resource.release.run(inputs)
        resource.release.run(inputs)
    state = json.loads((tmp_path / "terraform.tfstate").read_text())
    assert state["resources"] == []
