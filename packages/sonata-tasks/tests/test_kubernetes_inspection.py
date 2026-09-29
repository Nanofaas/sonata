from __future__ import annotations

import pytest

from sonata_tasks.imagetools import select_platform_manifest_digest
from sonata_tasks.kubectl import owned_deployment_pods, pod_image_digest


def test_select_platform_manifest_ignores_attestation() -> None:
    index = {
        "manifests": [
            {
                "digest": "sha256:real",
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "digest": "sha256:attestation",
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
        ]
    }
    assert select_platform_manifest_digest(index, "linux", "amd64") == "sha256:real"
    with pytest.raises(ValueError, match="no unique"):
        select_platform_manifest_digest(index, "linux", "arm64")


def test_owned_deployment_pods_walks_owner_uids_and_excludes_terminating() -> None:
    deployment = {"metadata": {"uid": "deployment"}}
    replica_sets = [
        {
            "metadata": {
                "uid": "ours",
                "ownerReferences": [{"kind": "Deployment", "uid": "deployment"}],
            }
        },
        {
            "metadata": {
                "uid": "other",
                "ownerReferences": [{"kind": "Deployment", "uid": "someone-else"}],
            }
        },
    ]
    pods = [
        {
            "metadata": {
                "name": "ours",
                "ownerReferences": [{"kind": "ReplicaSet", "uid": "ours"}],
            }
        },
        {
            "metadata": {
                "name": "other",
                "ownerReferences": [{"kind": "ReplicaSet", "uid": "other"}],
            }
        },
        {
            "metadata": {
                "name": "terminating",
                "deletionTimestamp": "now",
                "ownerReferences": [{"kind": "ReplicaSet", "uid": "ours"}],
            }
        },
    ]
    assert [
        pod["metadata"]["name"]
        for pod in owned_deployment_pods(deployment, replica_sets, pods)
    ] == ["ours"]
    with pytest.raises(ValueError, match="UID"):
        owned_deployment_pods({}, replica_sets, pods)


@pytest.mark.parametrize(
    "image_id",
    [
        "sha256:abc",
        "containerd://sha256:abc",
        "registry.example/image@sha256:abc",
    ],
)
def test_pod_image_digest(image_id: str) -> None:
    assert pod_image_digest(image_id) == "sha256:abc"
    with pytest.raises(ValueError, match="Unknown"):
        pod_image_digest("opaque-id")
