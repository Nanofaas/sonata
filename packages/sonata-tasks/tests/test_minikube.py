from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sonata_engine import TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult
from sonata_tasks.minikube import minikube_target_resource


@dataclass
class Executor:
    outputs: list[str] = field(default_factory=list)
    seen: list[tuple[str, ...]] = field(default_factory=list)

    def binding_key(self, role: str) -> str:
        return role

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task.argv)
        return TaskResult(
            task_id="", status="passed", return_code=0, stdout=self.outputs.pop(0)
        )


def test_minikube_target_checks_context_and_captures_only_selected_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sonata_tasks.minikube.shutil.which", lambda _name: "/bin/tool")
    monkeypatch.setattr("sonata_tasks.minikube.platform.machine", lambda: "x86_64")
    executor = Executor(
        outputs=[
            "demo\n",
            json.dumps(
                {
                    "valid": [
                        {
                            "Name": "demo",
                            "ActiveKubeContext": True,
                            "Config": {"Driver": "docker"},
                            "Status": "OK",
                        }
                    ]
                }
            ),
            json.dumps(
                {"Host": "Running", "Kubelet": "Running", "APIServer": "Running"}
            ),
            json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "demo"},
                            "status": {"nodeInfo": {"architecture": "amd64"}},
                        }
                    ]
                }
            ),
            json.dumps(
                {
                    "contexts": [
                        {
                            "name": "demo",
                            "context": {"cluster": "cluster", "user": "user"},
                        }
                    ],
                    "clusters": [{"name": "cluster"}],
                    "users": [{"name": "user"}, {"name": "unused"}],
                }
            ),
        ]
    )
    kubeconfig = tmp_path / "private" / "config.json"
    resource = minikube_target_resource(
        executor=executor,
        kubeconfig=kubeconfig,
        required_tools=("minikube", "kubectl"),
        driver="docker",
    )

    target = resource.acquire(TaskInputs.empty())

    assert (target.profile, target.context, target.nodes) == ("demo", "demo", ("demo",))
    assert [row["name"] for row in json.loads(kubeconfig.read_text())["users"]] == [
        "user"
    ]
    assert kubeconfig.stat().st_mode & 0o777 == 0o600
    resource.release(TaskInputs.empty(), target)
    assert not kubeconfig.exists()


def test_minikube_target_rejects_unselected_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sonata_tasks.minikube.shutil.which", lambda _name: "/bin/tool")
    executor = Executor(outputs=["demo\n", '{"valid": []}'])
    resource = minikube_target_resource(executor=executor)
    with pytest.raises(RuntimeError, match="not an active Minikube"):
        resource.acquire(TaskInputs.empty())
    assert len(executor.seen) == 2
