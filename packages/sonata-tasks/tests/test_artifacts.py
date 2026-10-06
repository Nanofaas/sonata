"""Bounded audit storage preserves immutable results and partial evidence."""

from __future__ import annotations

import importlib
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest


def storage():
    assert importlib.util.find_spec("sonata_tasks.artifacts") is not None, (
        "shared artifact storage API is missing"
    )
    return importlib.import_module("sonata_tasks.artifacts")


def publish(writer, method):
    if method == "write_json":
        return writer.write_json("result.json", {"value": 1})
    if method == "write_blob":
        return writer.write_blob("raw", "result.bin", b"x" * 12)
    return writer.write_file("result.json", b"x" * 12)


def writer_for(path, limit=65536):
    from sonata_tasks.artifacts import ArtifactWriter

    return ArtifactWriter(path, limit_bytes=limit)


def test_events_exist_before_writer_close(tmp_path):
    writer = writer_for(tmp_path)
    writer.append("events", {"phase": "drain"})
    assert json.loads((tmp_path / "events.jsonl").read_text()) == {"phase": "drain"}
    writer.close()


def test_existing_evidence_is_not_overwritten(tmp_path):
    evidence = tmp_path / "events.jsonl"
    evidence.write_text("important evidence\n")
    with pytest.raises(FileExistsError, match=r"run directory already contains"):
        writer_for(tmp_path)
    assert evidence.read_text() == "important evidence\n"


def test_two_writers_cannot_own_the_same_run(tmp_path):
    writer = writer_for(tmp_path)
    with pytest.raises(FileExistsError, match=r"run directory already contains"):
        writer_for(tmp_path)
    writer.close()
    with pytest.raises(FileExistsError, match=r"run directory already contains"):
        writer_for(tmp_path)


def test_prior_evaluation_is_immutable(tmp_path):
    writer = writer_for(tmp_path)
    writer.write_json("evaluation-1.json", {"status": "INCONCLUSIVE"})
    with pytest.raises(FileExistsError, match="Errno 17"):
        writer.write_json("evaluation-1.json", {"status": "PASS"})
    writer.write_json("evaluation-2.json", {"status": "FAIL"})
    assert (
        json.loads((tmp_path / "evaluation-1.json").read_text())["status"]
        == "INCONCLUSIVE"
    )
    writer.close()


@pytest.mark.parametrize(
    "name", ["../outside", "/tmp/outside", "a/b", "a\\b", ".", ".."]
)
def test_stream_names_cannot_escape_the_run(tmp_path, name):
    writer = writer_for(tmp_path)
    with pytest.raises(ValueError, match=r"artifact name must be a single safe"):
        writer.append(name, {"value": 1})
    writer.close()


def test_symlink_target_cannot_redirect_writes(tmp_path):
    root = tmp_path / "run"
    outside = tmp_path / "outside.jsonl"
    outside.write_text("unchanged")
    writer = writer_for(root)
    (root / "events.jsonl").symlink_to(outside)
    with pytest.raises(OSError, match="Errno 40"):
        writer.append("events", {"value": 1})
    assert outside.read_text() == "unchanged"
    writer.close()


def test_concurrent_records_are_individually_readable(tmp_path):
    from sonata_tasks.artifacts import read_records

    writer = writer_for(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda value: writer.append("samples", {"value": value}), range(100)
            )
        )
    writer.close()
    records = list(read_records(tmp_path / "samples.jsonl"))
    assert sorted(record["value"] for record in records) == list(range(100))


def test_corrupt_complete_record_is_not_silently_skipped(tmp_path):
    from sonata_tasks.artifacts import ArtifactCorruptionError, read_records

    path = tmp_path / "samples.jsonl"
    path.write_text('{"value": 1}\ninvalid\n{"value": 3}\n')
    records = read_records(path)
    assert next(records) == {"value": 1}
    with pytest.raises(ArtifactCorruptionError, match="malformed record"):
        next(records)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_values_cannot_be_persisted_as_observations(tmp_path, value):
    writer = writer_for(tmp_path)
    with pytest.raises(ValueError, match="Out of range float values are not JSON c"):
        writer.append("samples", {"value": value})
    writer.close()


def test_disk_failure_is_visible_and_preserves_prior_records(tmp_path, monkeypatch):
    import errno
    import os

    writer = writer_for(tmp_path)
    writer.append("events", {"phase": "steady"})
    real_open = os.open

    def full_disk(path, flags, *args, **kwargs):
        if str(path).endswith("events.jsonl"):
            raise OSError(errno.ENOSPC, "disk full")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", full_disk)
    with pytest.raises(OSError, match="Errno 28"):
        writer.append("events", {"phase": "drain"})
    assert json.loads((tmp_path / "events.jsonl").read_text()) == {"phase": "steady"}
    writer.close()


def test_closed_writer_rejects_new_observations(tmp_path):
    writer = writer_for(tmp_path)
    writer.close()
    writer.close()
    with pytest.raises(RuntimeError, match="artifact writer is closed"):
        writer.append("events", {"phase": "drain"})


def test_large_artifact_identity_includes_size_and_sha256(tmp_path):
    from sonata_tasks.artifacts import describe_artifact

    path = tmp_path / "dump"
    path.write_bytes(b"abc")
    identity = describe_artifact(path)
    assert identity["size_bytes"] == 3
    assert (
        identity["sha256"]
        == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_default_audit_writer_uses_its_full_quota_without_terminal_policy(tmp_path):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 12)
    writer.append("commands", {"value": 1})
    assert (tmp_path / "commands.jsonl").read_bytes() == b'{"value":1}\n'
    with pytest.raises(api.ArtifactLimitExceededError):
        writer.write_file("extra.txt", b"x")
    assert (tmp_path / ".artifact-owner").is_file()
    assert not (tmp_path / ".soak-owner").exists()


def test_reserve_and_marker_are_explicit_inputs(tmp_path):
    api = storage()
    writer = api.ArtifactWriter(
        tmp_path, 20, owner_marker=".audit-owner", reserve_bytes=8
    )
    writer.append("commands", {"value": 1})
    with pytest.raises(api.ArtifactLimitExceededError):
        writer.write_json("terminal.json", {})
    writer.write_json("summary.json", {}, use_reserve=True)
    assert (tmp_path / ".audit-owner").is_file()
    assert (tmp_path / "summary.json").read_bytes() == b"{}\n"
    assert not (tmp_path / "terminal.json").exists()


def test_usage_callback_counts_direct_and_sibling_writes(tmp_path):
    api = storage()
    sibling = tmp_path / "direct.log"
    sibling.write_bytes(b"x" * 12)
    root = tmp_path / "audit"
    writer = api.ArtifactWriter(
        root,
        20,
        reserve_bytes=5,
        measure_usage=lambda: sum(
            p.stat().st_size for p in tmp_path.rglob("*") if p.is_file()
        ),
    )
    with pytest.raises(api.ArtifactLimitExceededError):
        writer.write_json("ordinary.json", {"x": 1})
    writer.write_json("report.json", {"x": 1}, use_reserve=True)
    with pytest.raises(api.ArtifactLimitExceededError):
        writer.write_file("over.txt", b"x")
    assert sibling.read_bytes() == b"x" * 12
    assert (root / "report.json").read_bytes() == b'{"x":1}\n'


def test_workspace_named_blob_is_charged_by_the_generic_writer(tmp_path):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 10)
    writer.write_blob("workspace-data", "body.bin", b"x" * 9)
    with pytest.raises(api.ArtifactLimitExceededError):
        writer.write_file("extra.bin", b"xx")


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "128"])
def test_invalid_quota_cannot_acquire_a_directory(tmp_path, limit):
    api = storage()
    root = tmp_path / "audit"
    with pytest.raises(ValueError, match="artifact limit"):
        api.ArtifactWriter(root, limit)
    assert not root.exists()


@pytest.mark.parametrize("reserve", [-1, True, 1.5, 20, 21])
def test_invalid_reserve_cannot_acquire_a_directory(tmp_path, reserve):
    api = storage()
    root = tmp_path / "audit"
    with pytest.raises(ValueError, match="artifact reserve"):
        api.ArtifactWriter(root, 20, reserve_bytes=reserve)
    assert not root.exists()


@pytest.mark.parametrize("marker", ["", ".", "..", "../owner", "/owner", "a/b", "a\\b"])
def test_marker_cannot_escape_its_directory(tmp_path, marker):
    api = storage()
    root = tmp_path / "audit"
    with pytest.raises(ValueError, match="owner marker"):
        api.ArtifactWriter(root, 128, owner_marker=marker)
    assert not root.exists()


@pytest.mark.parametrize("marker", [None, False, 123, b".owner"])
def test_non_string_marker_cannot_acquire_a_directory(tmp_path, marker):
    api = storage()
    root = tmp_path / "audit"
    with pytest.raises(ValueError, match="owner marker"):
        api.ArtifactWriter(root, 128, owner_marker=marker)
    assert not root.exists()


def test_non_callable_usage_cannot_acquire_a_directory(tmp_path):
    api = storage()
    root = tmp_path / "audit"
    with pytest.raises(ValueError, match="usage"):
        api.ArtifactWriter(root, 128, measure_usage=123)
    assert not root.exists()


@pytest.mark.parametrize("used", [-1, False, 1.5, "0"])
def test_invalid_measured_usage_cannot_bypass_the_budget(tmp_path, used):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 128, measure_usage=lambda: used)
    with pytest.raises(ValueError, match="usage"):
        writer.write_file("body.bin", b"body")
    assert not (tmp_path / "body.bin").exists()


def test_symlink_root_cannot_redirect_acquisition(tmp_path):
    api = storage()
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "audit"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        api.ArtifactWriter(root, 128)
    assert not list(outside.iterdir())


@pytest.mark.parametrize("second_marker", [".first-owner", ".second-owner"])
def test_directory_ownership_is_exclusive_across_marker_choices(
    tmp_path, monkeypatch, second_marker
):
    api = storage()
    root = tmp_path / "audit"
    entered = Event()
    release = Event()
    original = Path.iterdir

    def synchronized_empty_check(path):
        entries = tuple(original(path))
        if path == root:
            if entered.is_set():
                release.set()
            else:
                entered.set()
            if not release.wait(5):
                raise TimeoutError("concurrent acquisition did not finish")
        return iter(entries)

    def acquire(marker):
        try:
            return api.ArtifactWriter(root, 128, owner_marker=marker)
        except FileExistsError as error:
            release.set()
            return error

    monkeypatch.setattr(Path, "iterdir", synchronized_empty_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(acquire, ".first-owner")
        assert entered.wait(5)
        second = pool.submit(acquire, second_marker)
        outcomes = [first.result(), second.result()]
    assert sum(isinstance(item, api.ArtifactWriter) for item in outcomes) == 1
    assert sum(isinstance(item, FileExistsError) for item in outcomes) == 1


@pytest.mark.parametrize("method", ["write_file", "write_blob", "write_json"])
def test_immutable_publication_does_not_replace_a_symlink(tmp_path, method):
    api = storage()
    writer = api.ArtifactWriter(tmp_path / "audit", 1024)
    outside = tmp_path / "outside"
    outside.write_bytes(b"original")
    if method == "write_blob":
        parent = writer.root / "raw"
        parent.mkdir()
        target = parent / "result.bin"
    else:
        target = writer.root / "result.json"
    target.symlink_to(outside)
    with pytest.raises(FileExistsError):
        publish(writer, method)
    assert outside.read_bytes() == b"original"
    assert not list(writer.root.rglob(".pending-*"))


def test_raw_parent_symlink_cannot_redirect_publication(tmp_path):
    api = storage()
    writer = api.ArtifactWriter(tmp_path / "audit", 1024)
    outside = tmp_path / "outside"
    outside.mkdir()
    (writer.root / "raw").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        writer.write_blob("raw", "body.bin", b"body")
    assert not list(outside.iterdir())


def test_raw_publication_bypasses_record_cap_but_is_charged(tmp_path):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 3 * 1024 * 1024)
    body = b"x" * (2 * 1024 * 1024)
    target = writer.write_blob("raw", "body.bin", body)
    assert target.read_bytes() == body
    with pytest.raises(api.ArtifactLimitExceededError):
        writer.write_file("extra.bin", body)


def test_json_record_cap_accepts_the_boundary_and_rejects_the_next_byte(tmp_path):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 3 * 1024 * 1024)
    target = writer.write_json("boundary.json", {"x": "a" * (1024 * 1024 - 9)})
    assert target.stat().st_size == 1024 * 1024
    with pytest.raises(
        api.ArtifactLimitExceededError, match="individual evidence record"
    ):
        writer.write_json("oversized.json", {"x": "a" * (1024 * 1024 - 8)})
    assert not (tmp_path / "oversized.json").exists()


class ShortAppend:
    def __init__(self, output, *, failure=False, no_progress=False):
        self.output = output
        self.failure = failure
        self.no_progress = no_progress
        self.calls = 0

    def __enter__(self):
        self.output.__enter__()
        return self

    def __exit__(self, *args):
        return self.output.__exit__(*args)

    def fileno(self):
        return self.output.fileno()

    def write(self, body):
        self.calls += 1
        if self.no_progress:
            return 0
        if self.failure and self.calls > 1:
            raise OSError("synthetic partial write failure")
        return self.output.write(body[:5])


def test_short_appends_finish_a_complete_record(tmp_path, monkeypatch):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 20)
    original = api.os.fdopen
    monkeypatch.setattr(
        api.os, "fdopen", lambda *a, **k: ShortAppend(original(*a, **k))
    )
    writer.append("events", {"value": 1})
    assert (tmp_path / "events.jsonl").read_bytes() == b'{"value":1}\n'


def test_partial_append_failure_remains_charged(tmp_path, monkeypatch):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 16)
    original = api.os.fdopen
    monkeypatch.setattr(
        api.os, "fdopen", lambda *a, **k: ShortAppend(original(*a, **k), failure=True)
    )
    with pytest.raises(OSError, match="partial write failure"):
        writer.append("events", {"value": 1})
    assert (tmp_path / "events.jsonl").read_bytes() == b'{"val'
    with pytest.raises(api.ArtifactLimitExceededError):
        writer.write_file("extra.bin", b"x" * 12)


def test_append_without_progress_fails_without_charging_bytes(tmp_path, monkeypatch):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 12)
    original = api.os.fdopen
    monkeypatch.setattr(
        api.os,
        "fdopen",
        lambda *a, **k: ShortAppend(original(*a, **k), no_progress=True),
    )
    with pytest.raises(OSError, match="made no progress"):
        writer.append("events", {"value": 1})
    assert (tmp_path / "events.jsonl").read_bytes() == b""
    monkeypatch.undo()
    writer.write_file("all.bin", b"x" * 12)


@pytest.mark.parametrize("method", ["write_file", "write_blob", "write_json"])
def test_failed_publication_leaves_quota_available(tmp_path, monkeypatch, method):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 12)

    def fail_link(*args, **kwargs):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(api.os, "link", fail_link)
    with pytest.raises(OSError, match="publication failure"):
        publish(writer, method)
    assert not list(tmp_path.rglob(".pending-*"))
    monkeypatch.undo()
    writer.write_file("all.bin", b"x" * 12)


@pytest.mark.parametrize("method", ["write_file", "write_json"])
def test_fsync_failure_does_not_publish_an_immutable_result(
    tmp_path, monkeypatch, method
):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 12)

    def fail_sync(fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(api.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="fsync failure"):
        publish(writer, method)
    assert not (tmp_path / "result.json").exists()
    assert not list(tmp_path.rglob(".pending-*"))
    monkeypatch.undo()
    writer.write_file("all.bin", b"x" * 12)


@pytest.mark.parametrize("method", ["write_file", "write_json"])
def test_published_bytes_stay_charged_when_temporary_cleanup_fails(
    tmp_path, monkeypatch, method
):
    api = storage()
    writer = api.ArtifactWriter(tmp_path, 12)
    original = Path.unlink

    def fail_cleanup(path, *args, **kwargs):
        if path.name.startswith(".pending-"):
            raise OSError("synthetic cleanup failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with pytest.raises(OSError, match="cleanup failure"):
        publish(writer, method)
    assert (tmp_path / "result.json").stat().st_size == 12
    with pytest.raises(api.ArtifactLimitExceededError):
        writer.write_file("extra.bin", b"x")


def test_torn_tail_yields_the_valid_prefix_then_a_neutral_error(tmp_path):
    api = storage()
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"value":1}\n{"value":')
    records = api.read_records(path)
    assert next(records) == {"value": 1}
    with pytest.raises(api.IncompleteRecordError) as caught:
        next(records)
    assert caught.value.path == path
    assert caught.value.line_number == 2


@pytest.mark.parametrize(
    "body",
    [
        b"invalid\n",
        b"\n",
        b'{"value":"\xff"}\n',
        b'{"value":NaN}\n',
        b'{"value":Infinity}\n',
    ],
)
def test_complete_malformed_records_raise_corruption(tmp_path, body):
    api = storage()
    path = tmp_path / "events.jsonl"
    path.write_bytes(body)
    with pytest.raises(api.ArtifactCorruptionError, match="malformed record"):
        list(api.read_records(path))


@pytest.mark.parametrize("body", [b"[1]\n", b"null\n", b"42\n", b"true\n"])
def test_complete_non_object_records_raise_corruption(tmp_path, body):
    api = storage()
    path = tmp_path / "events.jsonl"
    path.write_bytes(body)
    with pytest.raises(api.ArtifactCorruptionError, match="must be an object"):
        list(api.read_records(path))


def test_decoder_accepts_exact_cap_and_rejects_oversized_record(tmp_path):
    api = storage()
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"x":"' + b"a" * (1024 * 1024 - 9) + b'"}\n')
    assert len(next(api.read_records(path))["x"]) == 1024 * 1024 - 9
    path.write_bytes(b'{"x":"' + b"a" * (1024 * 1024 - 8) + b'"}\n')
    with pytest.raises(api.ArtifactCorruptionError, match="size limit"):
        list(api.read_records(path))


@pytest.mark.parametrize("body", [b'{"value":1e999}\n', b'{"nested":[-1e999]}\n'])
def test_overflowing_json_floats_are_complete_corruption(tmp_path, body):
    api = storage()
    path = tmp_path / "events.jsonl"
    path.write_bytes(body)
    with pytest.raises(api.ArtifactCorruptionError, match="malformed record"):
        list(api.read_records(path))


def test_finite_nested_json_floats_remain_valid(tmp_path):
    api = storage()
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"nested":[1.25,-0.0]}\n')
    assert list(api.read_records(path)) == [{"nested": [1.25, -0.0]}]


def test_unsupported_locking_fails_before_acquiring_a_directory(tmp_path, monkeypatch):
    import sys

    api = storage()
    root = tmp_path / "audit"
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(ValueError, match="directory locking"):
        api.ArtifactWriter(root, 128)
    assert not root.exists()


def test_failed_marker_creation_releases_the_directory_lock(tmp_path, monkeypatch):
    api = storage()
    root = tmp_path / "audit"
    original = api.os.open

    def fail_marker(path, *args, **kwargs):
        if Path(path).name == ".artifact-owner":
            raise OSError("synthetic marker failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(api.os, "open", fail_marker)
    with pytest.raises(OSError, match="marker failure"):
        api.ArtifactWriter(root, 128)
    monkeypatch.undo()
    writer = api.ArtifactWriter(root, 128)
    writer.write_file("body.bin", b"body")
    assert (root / "body.bin").read_bytes() == b"body"
