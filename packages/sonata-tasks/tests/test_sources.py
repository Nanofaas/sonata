"""Builds consume one identified tree, including local SDK changes."""

import shutil
import subprocess

import pytest


def _capture(repo_root, destination, *, max_bytes):
    from sonata_tasks.artifacts import ArtifactWriter
    from sonata_tasks.sources import capture_source_snapshot

    if destination.exists() or destination.is_symlink():
        raise FileExistsError("snapshot destination already exists")
    writer = ArtifactWriter(destination, 16 * 1024 * 1024)
    try:
        return capture_source_snapshot(repo_root, writer, max_bytes=max_bytes)
    finally:
        writer.close()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    (root / ".gitignore").write_text("build/\n")
    (root / "sdk.txt").write_text("original SDK")
    subprocess.run(("git", "-C", str(root), "add", "."), check=True)
    return root


def test_snapshot_includes_modified_and_untracked_sources(source, tmp_path):
    from sonata_tasks.sources import verify_snapshot

    (source / "sdk.txt").write_text("fixed SDK")
    (source / "new.txt").write_text("new input")
    (source / "build").mkdir()
    (source / "build/stale.jar").write_bytes(b"not an input")
    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    assert (snapshot.root / "sdk.txt").read_text() == "fixed SDK"
    assert (snapshot.root / "new.txt").read_text() == "new input"
    assert not (snapshot.root / "build/stale.jar").exists()
    assert not (snapshot.root / ".git").exists()
    assert snapshot.dirty
    verify_snapshot(snapshot)
    assert (source / "sdk.txt").read_text() == "fixed SDK"


def test_snapshot_fingerprint_changes_with_sdk_not_ignored_output(source, tmp_path):

    first = _capture(source, tmp_path / "first", max_bytes=100000)
    (source / "build").mkdir()
    (source / "build/stale.jar").write_text("ignored")
    second = _capture(source, tmp_path / "second", max_bytes=100000)
    assert first.fingerprint == second.fingerprint
    (source / "sdk.txt").write_text("fixed SDK")
    third = _capture(source, tmp_path / "third", max_bytes=100000)
    assert third.fingerprint != first.fingerprint


def test_snapshot_retains_deletion_and_executable_mode(source, tmp_path):

    (source / "sdk.txt").unlink()
    script = source / "gradlew"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    assert not (snapshot.root / "sdk.txt").exists()
    assert (snapshot.root / "gradlew").stat().st_mode & 0o111


def test_source_change_during_copy_invalidates_snapshot(source, tmp_path, monkeypatch):
    from sonata_tasks.sources import SourceChangedError

    copyfile = shutil.copyfile

    def changing_copy(src, dst, **kwargs):
        result = copyfile(src, dst, **kwargs)
        if str(src).endswith("sdk.txt"):
            (source / "sdk.txt").write_text("concurrent edit")
        return result

    monkeypatch.setattr(shutil, "copyfile", changing_copy)
    with pytest.raises(SourceChangedError, match=r"source changed while creating its"):
        _capture(source, tmp_path / "snapshot", max_bytes=100000)


def test_changed_snapshot_is_not_accepted_for_build(source, tmp_path):
    from sonata_tasks.sources import (
        SourceChangedError,
        verify_snapshot,
    )

    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    (snapshot.root / "sdk.txt").write_text("tampered")
    with pytest.raises(
        SourceChangedError, match=r"snapshot no longer matches its frozen"
    ):
        verify_snapshot(snapshot)


def test_snapshot_does_not_follow_external_symlinks(source, tmp_path):

    outside = tmp_path / "outside"
    outside.write_text("not a build input")
    (source / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="source symlink escapes the snapshot"):
        _capture(source, tmp_path / "snapshot", max_bytes=100000)


def test_snapshot_preserves_safe_relative_symlinks(source, tmp_path):
    from sonata_tasks.sources import verify_snapshot

    (source / "sdk-link").symlink_to("sdk.txt")
    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    assert (snapshot.root / "sdk-link").is_symlink()
    assert (snapshot.root / "sdk-link").read_text() == "original SDK"
    verify_snapshot(snapshot)


def test_snapshot_rejects_oversized_input_and_existing_destination(source, tmp_path):

    with pytest.raises(ValueError, match=r"source snapshot exceeds the byte"):
        _capture(source, tmp_path / "too-small", max_bytes=1)
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_text("evidence")
    with pytest.raises(FileExistsError, match="snapshot destination already exists"):
        _capture(source, output, max_bytes=100000)
    assert (output / "keep").read_text() == "evidence"


def test_build_workspace_does_not_mutate_snapshot(source, tmp_path):
    from sonata_tasks.sources import (
        materialize_snapshot,
        verify_snapshot,
    )

    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    workspace = materialize_snapshot(snapshot, tmp_path / "build-workspace")
    (workspace / "sdk.txt").write_text("build-side edit")
    verify_snapshot(snapshot)
    assert (snapshot.root / "sdk.txt").read_text() == "original SDK"


def test_internal_dangling_symlink_survives_capture_verify_and_materialize(
    source, tmp_path
):
    from sonata_tasks.sources import (
        materialize_snapshot,
        verify_snapshot,
    )

    (source / "scripts").mkdir()
    link = source / "scripts/ansible"
    link.symlink_to("../ops/ansible")
    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    verify_snapshot(snapshot)
    work = materialize_snapshot(snapshot, tmp_path / "workspace")
    for root in (source, snapshot.root, work):
        copied = root / "scripts/ansible"
        assert copied.is_symlink()
        assert str(copied.readlink()) == "../ops/ansible"
        assert not copied.exists()
    entry = next(entry for entry in snapshot.entries if entry.path == "scripts/ansible")
    assert entry.kind == "symlink"
    assert entry.link_target == "../ops/ansible"
    verify_snapshot(snapshot)


def test_existing_ignored_symlink_target_is_still_rejected(source, tmp_path):

    (source / "build").mkdir()
    (source / "build/input.txt").write_text("existing ignored input")
    (source / "input-link").symlink_to("build/input.txt")
    with pytest.raises(ValueError, match="source symlink target is not captured"):
        _capture(source, tmp_path / "snapshot", max_bytes=100000)


@pytest.mark.parametrize("target", ["../missing/input", "/missing/input"])
def test_missing_external_symlink_target_is_still_rejected(source, tmp_path, target):

    (source / "escape").symlink_to(target)
    with pytest.raises(ValueError, match="source symlink escapes the snapshot"):
        _capture(source, tmp_path / "snapshot", max_bytes=100000)


def test_symlink_cycle_is_not_treated_as_missing(source, tmp_path):

    (source / "cycle-a").symlink_to("cycle-b")
    (source / "cycle-b").symlink_to("cycle-a")
    with pytest.raises((RuntimeError, OSError)):
        _capture(source, tmp_path / "snapshot", max_bytes=100000)


def test_symlink_target_access_error_is_not_treated_as_missing(
    source, tmp_path, monkeypatch
):
    from pathlib import Path

    target = source / "missing"
    (source / "input-link").symlink_to("missing")
    original_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("target access denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    with pytest.raises(PermissionError, match="target access denied"):
        _capture(source, tmp_path / "snapshot", max_bytes=100000)


@pytest.mark.parametrize("target_name", ["appeared.txt", "build/appeared.txt"])
def test_dangling_target_appearing_during_capture_invalidates_snapshot(
    source,
    tmp_path,
    monkeypatch,
    target_name,
):

    (source / "input-link").symlink_to(target_name)
    original_copy = shutil.copyfile
    appeared = []

    def changing_copy(src, dst, **kwargs):
        result = original_copy(src, dst, **kwargs)
        if str(src).endswith("sdk.txt"):
            target = source / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("appeared during capture")
            appeared.append(target)
        return result

    monkeypatch.setattr(shutil, "copyfile", changing_copy)
    with pytest.raises(ValueError, match=r"source changed while|source symlink target"):
        _capture(source, tmp_path / "snapshot", max_bytes=100000)
    assert appeared == [source / target_name]


def test_retargeted_dangling_symlink_invalidates_snapshot(source, tmp_path):
    from sonata_tasks.sources import (
        SourceChangedError,
        verify_snapshot,
    )

    (source / "input-link").symlink_to("missing-a")
    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    link = snapshot.root / "input-link"
    link.unlink()
    link.symlink_to("missing-b")
    with pytest.raises(
        SourceChangedError, match=r"snapshot no longer matches its frozen"
    ):
        verify_snapshot(snapshot)


def test_capture_leaves_writer_open_for_caller_receipt(source, tmp_path):
    from sonata_tasks.artifacts import ArtifactWriter
    from sonata_tasks.sources import capture_source_snapshot, verify_snapshot

    writer = ArtifactWriter(tmp_path / "snapshot", 100000)
    try:
        snapshot = capture_source_snapshot(source, writer, max_bytes=100000)
        assert not (writer.root / "snapshot.json").exists()
        writer.write_json("receipt.json", {"identity": snapshot.fingerprint})
        verify_snapshot(snapshot)
        with pytest.raises(FileExistsError):
            capture_source_snapshot(source, writer, max_bytes=100000)
        verify_snapshot(snapshot)
    finally:
        writer.close()


def test_empty_repository_has_sealed_empty_manifest(tmp_path):
    from sonata_tasks.sources import materialize_snapshot, verify_snapshot

    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    assert snapshot.entries == ()
    assert snapshot.manifest_path.read_bytes() == b""
    assert snapshot.revision is None
    assert not snapshot.dirty
    verify_snapshot(snapshot)
    workspace = materialize_snapshot(snapshot, tmp_path / "workspace")
    assert list(workspace.iterdir()) == []


def test_manifest_tampering_prevents_materialization(source, tmp_path):
    from sonata_tasks.sources import SourceChangedError, materialize_snapshot

    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    snapshot.manifest_path.write_text('{"path":"invented.txt"}\n')
    with pytest.raises(SourceChangedError):
        materialize_snapshot(snapshot, tmp_path / "workspace")
    assert not (tmp_path / "workspace").exists()


@pytest.mark.parametrize("max_bytes", [0, -1, True, 1.5])
def test_invalid_input_budget_creates_no_source_tree(source, tmp_path, max_bytes):
    from sonata_tasks.artifacts import ArtifactWriter
    from sonata_tasks.sources import capture_source_snapshot

    writer = ArtifactWriter(tmp_path / "snapshot", 100000)
    try:
        with pytest.raises(ValueError, match="positive integer"):
            capture_source_snapshot(source, writer, max_bytes=max_bytes)
        assert not (writer.root / "tree").exists()
        writer.write_json("failure.json", {"failed": True})
    finally:
        writer.close()


def test_capture_refuses_owner_inside_repository(source):
    from sonata_tasks.artifacts import ArtifactWriter
    from sonata_tasks.sources import capture_source_snapshot

    writer = ArtifactWriter(source / "output", 100000)
    try:
        with pytest.raises(ValueError, match="outside the source checkout"):
            capture_source_snapshot(source, writer, max_bytes=100000)
        assert not (writer.root / "tree").exists()
    finally:
        writer.close()


def test_unreadable_snapshot_is_not_accepted_as_empty(tmp_path, monkeypatch):
    import os

    from sonata_tasks.sources import verify_snapshot

    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(("git", "init", "-q", str(source)), check=True)
    snapshot = _capture(source, tmp_path / "snapshot", max_bytes=100000)
    scandir = os.scandir

    def denied(path):
        if str(path) == str(snapshot.root):
            raise PermissionError("unreadable snapshot")
        return scandir(path)

    monkeypatch.setattr(os, "scandir", denied)
    with pytest.raises(PermissionError, match="unreadable snapshot"):
        verify_snapshot(snapshot)
