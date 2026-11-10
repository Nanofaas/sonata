from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from sonata_tasks.core.fingerprint import fingerprint_digest, semantic_key


def test_canonical_fingerprint_is_stable_for_reordered_nested_values() -> None:
    first = {
        "mapping": {"b": 2, "a": 1},
        "set": {"beta", "alpha"},
        "sequence": [Path("a/b"), bytearray(b"payload")],
    }
    second = {
        "sequence": [Path("a/b"), b"payload"],
        "set": {"alpha", "beta"},
        "mapping": {"a": 1, "b": 2},
    }

    assert fingerprint_digest(first) == fingerprint_digest(second)


def test_semantic_key_is_stable_in_a_distinct_process() -> None:
    expected = semantic_key("http-verify:v1", {"labels": {"b": "2", "a": "1"}})
    script = (
        "from sonata_tasks.core.fingerprint import semantic_key; "
        "print(semantic_key('http-verify:v1', {'labels': {'a': '1', 'b': '2'}}))"
    )
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (source_root, environment.get("PYTHONPATH")))
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.stdout.strip() == expected


def test_semantic_key_is_opaque() -> None:
    key = semantic_key("request:v1", {"authorization": "Bearer super-secret"})

    assert key.startswith("request:v1:sha256:")
    assert "super-secret" not in key


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"value": float("inf")}, ValueError),
        ({1: "not-a-string-key"}, TypeError),
        ({"value": object()}, TypeError),
    ],
)
def test_fingerprint_rejects_noncanonical_values(
    payload: dict, error: type[Exception]
) -> None:
    with pytest.raises(error):
        fingerprint_digest(payload)


def test_semantic_key_requires_a_namespace() -> None:
    with pytest.raises(ValueError, match="namespace"):
        semantic_key("", {"value": 1})


@pytest.mark.parametrize(
    "change", ["reuse_key", "pre_clean", "build", "ready_url", "helm"]
)
def test_wrapped_resource_fingerprint_rejects_changed_resume(tmp_path, change):
    from sonata_engine import JournalConfig, ReusableTask, TaskOutcome, Workflow
    from sonata_engine.errors import WorkflowTopologyMismatchError
    from sonata_tasks.command import CommandTask
    from sonata_tasks.compensation import compensated_resource
    from sonata_tasks.compose import DockerComposeProject, docker_compose_resource
    from sonata_tasks.helm import HelmReleaseSpec, helm_release_resource
    from sonata_tasks.tasks.models import TaskResult

    class Executor:
        def binding_key(self, role):
            return role

        def run(self, task, *, dry_run=False):
            return TaskResult(task_id="", status="passed", return_code=0)

    class KeyedAcquire(ReusableTask):
        title = "Acquire"

        def __init__(self, key):
            self.key = key

        @property
        def reuse_key(self):
            return self.key

        def run(self, inputs):
            return TaskOutcome()

    def workflow(changed):
        executor = Executor()
        if change == "reuse_key":
            resource = compensated_resource(
                title="owned",
                acquire=KeyedAcquire("new" if changed else "old"),
                compensate=CommandTask(
                    title="Release", argv=("true",), executor=executor
                ),
            )
        elif change == "helm":
            resource = helm_release_resource(
                HelmReleaseSpec(
                    release="test",
                    namespace="test",
                    chart="chart",
                    values=("--set", f"image.tag={'v2' if changed else 'v1'}"),
                ),
                executor=executor,
            )
        else:
            resource = docker_compose_resource(
                DockerComposeProject(
                    name="test",
                    file=Path("compose.yml"),
                    build=not (changed and change == "build"),
                    ready_url="http://new/ready"
                    if changed and change == "ready_url"
                    else "http://old/ready",
                ),
                executor=executor,
                pre_clean=changed and change == "pre_clean",
            )
        result = Workflow("wrapped")
        result.add(
            CommandTask(title="Consume", argv=("true",), executor=executor),
            requires=(resource,),
        )
        return result

    original = workflow(False)
    assert original.compile().fingerprint == workflow(False).compile().fingerprint
    assert original.compile().fingerprint != workflow(True).compile().fingerprint
    journal = JournalConfig(path=tmp_path / "journal.jsonl")
    original.run(journal=journal)
    with pytest.raises(WorkflowTopologyMismatchError):
        workflow(True).run(journal=journal, resume=True)
