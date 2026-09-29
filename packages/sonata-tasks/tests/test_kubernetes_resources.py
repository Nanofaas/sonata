from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sonata_engine import Resource, TaskInputs
from sonata_tasks.execution.models import CommandTaskSpec, TaskResult
from sonata_tasks.kubectl import (
    PinnedKubeconfigExecutor,
    kubectl_port_forward_resource,
    owned_namespace_resource,
)


@dataclass
class RecordingExecutor:
    outputs: list[str] = field(default_factory=list)
    seen: list[CommandTaskSpec] = field(default_factory=list)

    def binding_key(self, role: str) -> str:
        return f"recording:{role}"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(
            task_id="",
            status="passed",
            return_code=0,
            stdout=self.outputs.pop(0) if self.outputs else "",
        )


def test_pinned_kubeconfig_only_changes_kubernetes_commands(tmp_path: Path) -> None:
    delegate = RecordingExecutor()
    pinned = PinnedKubeconfigExecutor(delegate, tmp_path / "selected.yaml")
    from sonata_tasks.command import CommandTask

    for argv in (("kubectl", "get", "pods"), ("helm", "list"), ("docker", "ps")):
        CommandTask(title="check", argv=argv, executor=pinned).run(TaskInputs.empty())

    assert pinned.binding_key("host") != delegate.binding_key("host")
    assert delegate.seen[0].options.env["KUBECONFIG"] == str(tmp_path / "selected.yaml")
    assert delegate.seen[1].options.env["KUBECONFIG"] == str(tmp_path / "selected.yaml")
    assert "KUBECONFIG" not in delegate.seen[2].options.env


def test_owned_namespace_uses_selected_context_and_deletes_only_its_own() -> None:
    executor = RecordingExecutor(outputs=['{"items":[]}', "", ""])
    cluster = Resource(
        title="cluster", acquire=lambda _inputs: "selected", release=lambda *_: None
    )
    resource = owned_namespace_resource(
        "one-run",
        executor=executor,
        role="stack",
        context=lambda inputs: inputs.resource(cluster),
        requires=(cluster,),
    )
    inputs = TaskInputs._for_resources({cluster: "selected"}, {cluster})

    assert resource.acquire(inputs) == "one-run"
    resource.release(inputs, "one-run")

    assert resource.requires == (cluster,)
    assert [task.argv for task in executor.seen] == [
        ("kubectl", "--context", "selected", "get", "namespaces", "-o", "json"),
        ("kubectl", "--context", "selected", "create", "namespace", "one-run"),
        (
            "kubectl",
            "--context",
            "selected",
            "delete",
            "namespace",
            "one-run",
            "--wait=true",
        ),
    ]


def test_owned_namespace_refuses_collision_without_deleting() -> None:
    executor = RecordingExecutor(
        outputs=['{"items":[{"metadata":{"name":"one-run"}}]}']
    )
    resource = owned_namespace_resource("one-run", executor=executor)

    with pytest.raises(RuntimeError, match="existing namespace"):
        resource.acquire(TaskInputs.empty())

    assert len(executor.seen) == 1


class FakeProcess:
    def __init__(self, line: str) -> None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, line.encode())
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd)
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_port_forward_returns_loopback_url_and_stops_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    process = FakeProcess("Forwarding from 127.0.0.1:40321 -> 8080\n")
    seen: list[tuple[str, ...]] = []

    def spawn(argv: tuple[str, ...], **_kwargs: object) -> FakeProcess:
        seen.append(argv)
        return process

    monkeypatch.setattr("sonata_tasks.kubectl.subprocess.Popen", spawn)
    resource = kubectl_port_forward_resource(
        namespace="one-run",
        resource="service/api",
        remote_port=8080,
        context="selected",
        log_path=tmp_path / "run" / "forward.log",
    )

    url = resource.acquire(TaskInputs.empty())
    resource.release(TaskInputs.empty(), url)

    assert url == "http://127.0.0.1:40321"
    assert seen[0] == (
        "kubectl",
        "--context",
        "selected",
        "-n",
        "one-run",
        "port-forward",
        "--address",
        "127.0.0.1",
        "service/api",
        "0:8080",
    )
    assert process.terminated
    assert (tmp_path / "run" / "forward.log").read_text().strip().endswith("-> 8080")
