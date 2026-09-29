"""Select and inspect an existing Minikube profile before using it."""

from __future__ import annotations

import json
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sonata_engine import Resource, TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.ports import CommandTaskExecutor


@dataclass(frozen=True, slots=True)
class MinikubeTarget:
    """Selected Kubernetes context, Minikube profile, and node names."""

    profile: str
    context: str
    nodes: tuple[str, ...]


def minikube_target_resource(
    *,
    executor: CommandTaskExecutor,
    kubeconfig: Path | None = None,
    required_tools: tuple[str, ...] = ("minikube", "kubectl"),
    driver: str | None = None,
    require_host_arch: bool = True,
) -> Resource[MinikubeTarget]:
    """Validate the active profile and optionally pin its credentials."""

    def command(inputs: TaskInputs, title: str, argv: tuple[str, ...]) -> str:
        result = (
            CommandTask(title=title, argv=argv, executor=executor).run(inputs).value
        )
        if result is None:
            raise RuntimeError(f"{title} returned no result")
        return result.stdout

    def json_command(
        inputs: TaskInputs, title: str, argv: tuple[str, ...]
    ) -> dict[str, Any]:
        data = json.loads(command(inputs, title, argv))
        if not isinstance(data, dict):
            raise ValueError(f"{title} returned no JSON object")
        return data

    def acquire(inputs: TaskInputs) -> MinikubeTarget:
        missing = [name for name in required_tools if not shutil.which(name)]
        if missing:
            raise RuntimeError(f"Missing local Kubernetes tools: {', '.join(missing)}")
        context = command(
            inputs,
            "Read current Kubernetes context",
            ("kubectl", "config", "current-context"),
        ).strip()
        profiles = json_command(
            inputs,
            "Read Minikube profiles",
            ("minikube", "profile", "list", "-o", "json"),
        ).get("valid", [])
        matches = [
            row
            for row in profiles
            if row.get("Name") == context and row.get("ActiveKubeContext") is True
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"Current context {context!r} is not an active Minikube profile"
            )
        profile = matches[0]
        if profile.get("Status") != "OK" or (
            driver is not None and profile.get("Config", {}).get("Driver") != driver
        ):
            raise RuntimeError(
                "Selected Minikube profile is not running with the required driver"
            )
        status = json_command(
            inputs,
            "Check selected Minikube profile",
            ("minikube", "status", "-p", context, "-o", "json"),
        )
        if any(
            status.get(key) != "Running" for key in ("Host", "Kubelet", "APIServer")
        ):
            raise RuntimeError("Selected Minikube profile is not ready")
        nodes = json_command(
            inputs,
            "Read selected Kubernetes nodes",
            ("kubectl", "--context", context, "get", "nodes", "-o", "json"),
        ).get("items", [])
        host_arch = (
            platform.machine()
            .lower()
            .replace("x86_64", "amd64")
            .replace("aarch64", "arm64")
        )
        names: list[str] = []
        for node in nodes:
            name = node.get("metadata", {}).get("name")
            arch = node.get("status", {}).get("nodeInfo", {}).get("architecture")
            if not isinstance(name, str) or (require_host_arch and arch != host_arch):
                raise RuntimeError(
                    "Minikube node architecture differs from host or "
                    "node metadata is missing"
                )
            names.append(name)
        if not names:
            raise RuntimeError("Selected Minikube profile has no nodes")
        if kubeconfig is not None:
            full = json_command(
                inputs,
                "Capture selected Kubernetes credentials",
                ("kubectl", "config", "view", "--flatten", "--raw", "-o", "json"),
            )
            contexts = [
                row for row in full.get("contexts", []) if row.get("name") == context
            ]
            if len(contexts) != 1:
                raise RuntimeError(
                    "Selected Kubernetes context is missing from kubeconfig"
                )
            cluster_name = contexts[0].get("context", {}).get("cluster")
            user_name = contexts[0].get("context", {}).get("user")
            selected_config = {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": context,
                "contexts": contexts,
                "clusters": [
                    row
                    for row in full.get("clusters", [])
                    if row.get("name") == cluster_name
                ],
                "users": [
                    row for row in full.get("users", []) if row.get("name") == user_name
                ],
            }
            if (
                len(selected_config["clusters"]) != 1
                or len(selected_config["users"]) != 1
            ):
                raise RuntimeError("Selected Kubernetes credentials are incomplete")
            kubeconfig.parent.mkdir(parents=True, exist_ok=True)
            kubeconfig.write_text(json.dumps(selected_config))
            kubeconfig.chmod(0o600)
        return MinikubeTarget(context, context, tuple(names))

    def release(_inputs: TaskInputs, _target: MinikubeTarget) -> None:
        if kubeconfig is not None:
            kubeconfig.unlink(missing_ok=True)

    return Resource(
        title="Preflight selected host Minikube",
        acquire=acquire,
        release=release,
    )
