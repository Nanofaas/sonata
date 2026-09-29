"""Run kubectl commands and wait for a deployment to become ready."""

from __future__ import annotations

import json
import re
import select
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, override

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome
from sonata_tasks.command import CommandTask
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor


class KubectlTask(CommandTask):
    """Run one kubectl subcommand, optionally scoped to a namespace."""

    def __init__(
        self,
        *args: str,
        executor: CommandTaskExecutor,
        role: str = "host",
        namespace: str | None = None,
        options: CommandOptions | None = None,
        title: str | None = None,
        verify: Callable[[TaskResult], None] | None = None,
        semantic_key: str | None = None,
    ) -> None:
        """Build ``kubectl [-n namespace] <args>`` as the command to run.

        ``title`` defaults to one echoing the arguments.
        """
        scope = ("-n", namespace) if namespace is not None else ()
        super().__init__(
            title=title or f"kubectl {' '.join(args)}",
            argv=("kubectl", *scope, *args),
            executor=executor,
            role=role,
            options=options,
            verify=verify,
            semantic_key=semantic_key,
        )


class ClusterIpEndpointTask(Task[str]):
    """Turn a ClusterIP read by the previous step into an ``http://`` URL."""

    def __init__(self, *, service: str, port: int, title: str | None = None) -> None:
        """Record the service and port to build the URL from.

        ``title`` defaults to one naming the service.
        """
        self.title = title or f"Resolve where {service} answers"
        self._service = service
        self._port = port

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        result = inputs.upstream()
        if not isinstance(result, TaskResult):
            raise RuntimeError(
                f"{self.title}: expected the previous step's command result, got "
                f"{type(result).__name__}"
            )
        address = result.stdout.strip()
        if not address:
            raise RuntimeError(f"service {self._service} reported no ClusterIP")
        return TaskOutcome(value=f"http://{address}:{self._port}")  # NOSONAR

    @override
    def _fingerprint_payload(self) -> object:
        return {"service": self._service, "port": self._port}


class PinnedKubeconfigExecutor:
    """Pin kubectl and Helm commands to one captured kubeconfig."""

    def __init__(self, delegate: CommandTaskExecutor, kubeconfig: Path) -> None:
        """Wrap a command executor without changing non-Kubernetes commands."""
        self.delegate = delegate
        self.kubeconfig = kubeconfig

    def binding_key(self, role: str) -> str:
        """Distinguish bindings that target different kubeconfigs."""
        return f"{self.delegate.binding_key(role)}:kubeconfig:{self.kubeconfig}"

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        """Inject KUBECONFIG for kubectl and Helm, then delegate execution."""
        if task.argv and task.argv[0] in {"kubectl", "helm"}:
            task = replace(
                task,
                options=replace(
                    task.options,
                    env={**task.options.env, "KUBECONFIG": str(self.kubeconfig)},
                ),
            )
        return self.delegate.run(task, dry_run=dry_run)


def owned_deployment_pods(
    deployment: dict[str, Any],
    replica_sets: list[dict[str, Any]],
    pods: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Find live Pods owned by a Deployment through its ReplicaSets."""
    uid = deployment.get("metadata", {}).get("uid")
    if not uid:
        raise ValueError("Deployment has no UID")
    replica_uids = {
        row.get("metadata", {}).get("uid")
        for row in replica_sets
        if any(
            owner.get("uid") == uid and owner.get("kind") == "Deployment"
            for owner in row.get("metadata", {}).get("ownerReferences", [])
        )
    }
    replica_uids.discard(None)
    return [
        pod
        for pod in pods
        if not pod.get("metadata", {}).get("deletionTimestamp")
        and any(
            owner.get("uid") in replica_uids and owner.get("kind") == "ReplicaSet"
            for owner in pod.get("metadata", {}).get("ownerReferences", [])
        )
    ]


def pod_image_digest(image_id: str) -> str:
    """Parse known Kubernetes Pod imageID forms into an OCI digest."""
    if image_id.startswith("sha256:"):
        return image_id
    if image_id.startswith("containerd://sha256:"):
        return image_id.removeprefix("containerd://")
    if "@sha256:" in image_id and not image_id.startswith("@"):
        return "sha256:" + image_id.rsplit("@sha256:", 1)[1]
    raise ValueError(f"Unknown Kubernetes Pod imageID: {image_id!r}")


KubernetesContext = str | Callable[[TaskInputs], str] | None


def _context_args(context: KubernetesContext, inputs: TaskInputs) -> tuple[str, ...]:
    selected = context(inputs) if callable(context) else context
    return ("--context", selected) if selected else ()


def owned_namespace_resource(
    namespace: str,
    *,
    executor: CommandTaskExecutor,
    role: str = "host",
    context: KubernetesContext = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[str]:
    """Create a new namespace and remove only one acquired by this resource."""

    def run(inputs: TaskInputs, title: str, argv: tuple[str, ...]) -> str:
        result = (
            KubectlTask(
                *argv,
                executor=executor,
                role=role,
                title=title,
            )
            .run(inputs)
            .value
        )
        if result is None:
            raise RuntimeError(f"{title} returned no result")
        return result.stdout

    def acquire(inputs: TaskInputs) -> str:
        prefix = _context_args(context, inputs)
        data = json.loads(
            run(
                inputs,
                f"Check namespace {namespace}",
                (*prefix, "get", "namespaces", "-o", "json"),
            )
        )
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("Kubernetes namespace list is malformed")
        if any(
            row.get("metadata", {}).get("name") == namespace for row in data["items"]
        ):
            raise RuntimeError(f"Refusing to acquire existing namespace {namespace}")
        run(
            inputs,
            f"Create namespace {namespace}",
            (*prefix, "create", "namespace", namespace),
        )
        return namespace

    def release(inputs: TaskInputs, _value: str) -> None:
        run(
            inputs,
            f"Delete namespace {namespace}",
            (
                *_context_args(context, inputs),
                "delete",
                "namespace",
                namespace,
                "--wait=true",
            ),
        )

    return Resource(
        title=f"Own namespace {namespace}",
        acquire=acquire,
        release=release,
        requires=requires,
    )


def kubectl_port_forward_resource(
    *,
    namespace: str,
    resource: str,
    remote_port: int,
    log_path: Path,
    context: KubernetesContext = None,
    requires: tuple[Resource[Any], ...] = (),
) -> Resource[str]:
    """Forward one Kubernetes service to an ephemeral host loopback port."""
    process: subprocess.Popen[str] | None = None

    def stop() -> None:
        nonlocal process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        process = None

    def acquire(inputs: TaskInputs) -> str:
        nonlocal process
        log_path.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            (
                "kubectl",
                *_context_args(context, inputs),
                "-n",
                namespace,
                "port-forward",
                "--address",
                "127.0.0.1",
                resource,
                f"0:{remote_port}",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if process.stdout is None:
            stop()
            raise RuntimeError("Kubernetes port-forward has no output stream")
        deadline = time.monotonic() + 30
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        "Kubernetes port-forward exited before readiness"
                    )
                readable, _, _ = select.select([process.stdout], [], [], 0.2)
                if not readable:
                    continue
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError("Kubernetes port-forward closed its output")
                with log_path.open("a") as stream:
                    stream.write(line)
                match = re.search(
                    rf"Forwarding from 127\.0\.0\.1:(\d+) -> {remote_port}\b", line
                )
                if match:
                    return f"http://127.0.0.1:{match.group(1)}"
            raise RuntimeError("Kubernetes port-forward did not become ready")
        except BaseException:
            stop()
            raise

    return Resource(
        title=f"Forward {resource} from namespace {namespace}",
        acquire=acquire,
        release=lambda _inputs, _value: stop(),
        requires=requires,
        always_release=True,
    )


def k8s_deployment_readiness(
    *,
    deployment: str,
    namespace: str,
    executor: CommandTaskExecutor,
    role: str = "host",
    timeout_seconds: int = 120,
    options: CommandOptions | None = None,
) -> tuple[CommandTask, CommandTask]:
    """Build the pair of tasks that carry a deployment rollout to completion.

    The first polls for the deployment object every two seconds — for roughly
    ``timeout_seconds``, at least once — and fails with a descriptive message if
    it never appears; waiting for the object before ``rollout status`` avoids
    the latter failing outright when the deployment has not been created yet.
    The second then runs ``kubectl rollout status`` with a matching timeout.

    Returns:
        The poll task followed by the rollout-status task.

    """
    attempts = max(1, timeout_seconds // 2)
    timed_out = shlex.quote(
        f"deployment {deployment} did not appear within {timeout_seconds}s"
    )
    appeared = (
        f"for _ in $(seq 1 {attempts}); do "
        f"kubectl -n {shlex.quote(namespace)} get deployment/{shlex.quote(deployment)} "
        ">/dev/null 2>&1 && exit 0; sleep 2; done; "
        "echo "
        f"{timed_out} "
        ">&2; "
        "exit 1"
    )
    return (
        CommandTask(
            title=f"Wait for deployment/{deployment}",
            argv=("bash", "-lc", appeared),
            executor=executor,
            role=role,
            options=options,
        ),
        KubectlTask(
            "rollout",
            "status",
            f"deployment/{deployment}",
            f"--timeout={timeout_seconds}s",
            executor=executor,
            role=role,
            namespace=namespace,
            title=f"Roll out deployment/{deployment}",
            options=options,
        ),
    )
