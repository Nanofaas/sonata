"""Containerd image identity is checked against the published manifest."""

from __future__ import annotations

import json

import pytest

from sonata_engine import TaskInputs, Workflow
from sonata_tasks.containerd import (
    ContainerdImageInspectTask,
    require_containerd_image_identity,
)
from sonata_tasks.tasks.models import TaskResult
from sonata_tasks.testing import RecordingExecutor

REFERENCE = "127.0.0.1:5000/nanofaas/word-stats:recipe-a"
DIGEST = "sha256:" + "a" * 64
REPO_DIGEST = "127.0.0.1:5000/nanofaas/word-stats@" + DIGEST


def _result(stdout: object) -> TaskResult:
    return TaskResult(
        task_id="inspect", status="passed", return_code=0, stdout=json.dumps(stdout)
    )


def _task(executor: RecordingExecutor):
    return ContainerdImageInspectTask(
        container_argv=("session", "inspect-owned", "word-stats", "1"),
        image_argv=("session", "image-inspect", REFERENCE),
        executor=executor,
        role="stack",
    )


def test_accepts_published_manifest_even_when_local_image_id_differs() -> None:
    executor = RecordingExecutor(
        results=[
            _result({"ID": "owned-1", "Image": REFERENCE}),
            _result([{"Id": "sha256:local-config", "RepoDigests": [REPO_DIGEST]}]),
        ]
    )

    outcome = _task(executor).run(TaskInputs.empty())

    assert outcome.value is not None
    assert outcome.value.container["ID"] == "owned-1"
    assert outcome.value.image["RepoDigests"] == [REPO_DIGEST]
    require_containerd_image_identity(outcome.value, reference=REFERENCE, digest=DIGEST)
    assert [item.argv for item in executor.seen] == [
        ("session", "inspect-owned", "word-stats", "1"),
        ("session", "image-inspect", REFERENCE),
    ]


def test_rejects_image_with_another_published_digest() -> None:
    executor = RecordingExecutor(
        results=[
            _result({"Image": REFERENCE}),
            _result(
                [{"RepoDigests": ["127.0.0.1:5000/nanofaas/word-stats@sha256:other"]}]
            ),
        ]
    )

    identity = _task(executor).run(TaskInputs.empty()).value
    assert identity is not None
    with pytest.raises(ValueError, match="manifest digest"):
        require_containerd_image_identity(identity, reference=REFERENCE, digest=DIGEST)


def test_rejects_container_using_another_tag() -> None:
    executor = RecordingExecutor(
        results=[
            _result({"Image": "127.0.0.1:5000/nanofaas/word-stats:stale"}),
            _result([{"RepoDigests": [REPO_DIGEST]}]),
        ]
    )

    identity = _task(executor).run(TaskInputs.empty()).value
    assert identity is not None
    with pytest.raises(ValueError, match="containerd image is"):
        require_containerd_image_identity(identity, reference=REFERENCE, digest=DIGEST)


def test_fingerprint_changes_with_execution_target() -> None:
    first = Workflow("inspect").add(_task(RecordingExecutor(target_key="vm-a")))
    second = Workflow("inspect").add(_task(RecordingExecutor(target_key="vm-b")))

    assert first.compile().fingerprint != second.compile().fingerprint
