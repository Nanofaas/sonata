"""Capture and verify Git working-tree inputs for independent build workspaces."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from sonata_tasks.artifacts import describe_artifact, encode_record

_MAX_FILES = 100000


class SourceChangedError(ValueError):
    """Source inputs or the identified snapshot changed across a build boundary."""


@dataclass(frozen=True)
class SourceEntry:
    """One source path, its content identity and build-relevant filesystem mode."""

    path: str
    kind: str
    mode: int
    size_bytes: int
    sha256: str
    link_target: str | None = None


@dataclass(frozen=True)
class SourceSnapshot:
    """An identified plain source tree, distinct from mutable build workspaces."""

    root: Path
    fingerprint: str
    revision: str | None
    dirty: bool
    entries: tuple[SourceEntry, ...]
    manifest_path: Path
    manifest_sha256: str


class SnapshotWriter(Protocol):
    """Caller-owned artifact storage required to publish a snapshot manifest."""

    @property
    def root(self) -> Path:
        """Return the caller's exclusively owned output directory."""
        ...

    def append(self, stream: str, record: dict[str, object]) -> None:
        """Append a bounded canonical manifest record."""
        ...

    def write_file(self, name: str, body: bytes) -> Path:
        """Publish an immutable file, including an empty repository manifest."""
        ...


def _git(root: Path, *args: str, optional: bool = False) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(root), *args), capture_output=True, check=False, timeout=30
    )
    if result.returncode:
        if optional:
            return b""
        raise ValueError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def source_entry(root: Path, name: str, paths: set[str]) -> SourceEntry:
    """Inspect an input without following external or excluded symlink targets.

    ``paths`` names the complete inventory, including deleted inputs. The caller
    controls the tree and serializes mutation while inspecting its entries.
    """
    root = root.resolve()
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
        raise ValueError(f"unsafe source input path: {name}")
    path = root / relative
    if path.is_symlink():
        target = str(path.readlink())
        resolved = path.resolve()
        if Path(target).is_absolute() or not resolved.is_relative_to(root):
            raise ValueError(f"source symlink escapes the snapshot: {name}")
        resolved_name = resolved.relative_to(root).as_posix()
        included = resolved_name in paths or any(
            item.startswith(resolved_name + "/") for item in paths
        )
        try:
            resolved.stat()
        except FileNotFoundError:
            # Preserve an already-dangling internal link verbatim. Only ENOENT
            # means absence; access errors, cycles and invalid paths still fail.
            pass
        else:
            if not included:
                raise ValueError(f"source symlink target is not captured: {name}")
        encoded = os.fsencode(target)
        return SourceEntry(
            name,
            "symlink",
            stat.S_IMODE(path.lstat().st_mode),
            len(encoded),
            hashlib.sha256(encoded).hexdigest(),
            target,
        )
    if not path.exists():
        return SourceEntry(name, "deleted", 0, 0, "")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise ValueError(f"unsupported source input: {name}")
    identity = describe_artifact(path)
    return SourceEntry(
        name,
        "file",
        stat.S_IMODE(path.stat().st_mode),
        int(str(identity["size_bytes"])),
        str(identity["sha256"]),
    )


def _inventory(root: Path, max_bytes: int) -> tuple[SourceEntry, ...]:
    stages = _git(root, "ls-files", "--stage", "-z").split(b"\0")
    if any(record.startswith(b"160000 ") for record in stages):
        raise ValueError("submodules are not supported by source snapshots")
    paths = {
        os.fsdecode(item)
        for item in _git(
            root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
        ).split(b"\0")
        if item
    }
    if len(paths) > _MAX_FILES:
        raise ValueError("source snapshot exceeds the input file budget")
    entries = []
    size = 0
    for path in sorted(paths):
        entry = source_entry(root, path, paths)
        size += entry.size_bytes
        if size > max_bytes:
            raise ValueError("source snapshot exceeds the byte budget")
        entries.append(entry)
    return tuple(entries)


def _raise_walk_error(error: OSError) -> None:
    raise error


def _fingerprint(entries: tuple[SourceEntry, ...]) -> str:
    body = encode_record({"entries": [asdict(entry) for entry in entries]})
    return hashlib.sha256(body[:-1]).hexdigest()


def _tree_entries(root: Path) -> tuple[SourceEntry, ...]:
    paths: set[str] = set()
    for parent, directories, files in os.walk(
        root, followlinks=False, onerror=_raise_walk_error
    ):
        directory = Path(parent)
        for name in directories:
            path = directory / name
            if path.is_symlink():
                paths.add(path.relative_to(root).as_posix())
        for name in files:
            paths.add((directory / name).relative_to(root).as_posix())
        if len(paths) > _MAX_FILES:
            raise SourceChangedError("snapshot tree exceeds the input file budget")
    return tuple(source_entry(root, name, paths) for name in sorted(paths))


def capture_source_snapshot(
    repo_root: Path, writer: SnapshotWriter, *, max_bytes: int
) -> SourceSnapshot:
    """Copy tracked/nonignored inputs through caller-owned bounded storage.

    The writer owns a directory outside the checkout. Capture publishes ``tree``
    and ``source-manifest.jsonl`` there, without closing the writer or creating a
    receipt. The caller controls the checkout/output namespace and must serialize
    concurrent mutation; repeated inventories detect ordinary source changes.
    Failure retains partial evidence. Limits cover input bytes (including link
    target bytes), 100000 entries, and the writer's separate manifest budget.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("source byte budget must be a positive integer")
    root = repo_root.resolve()
    output = writer.root.absolute()
    if output.resolve().is_relative_to(root):
        raise ValueError("snapshot destination must be outside the source checkout")
    manifest = output / "source-manifest.jsonl"
    if manifest.exists() or manifest.is_symlink():
        raise FileExistsError(f"snapshot manifest already exists: {manifest}")
    top = Path(
        os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()
    ).resolve()
    if top != root:
        raise ValueError("source must name the repository root")
    revision = (
        _git(root, "rev-parse", "--verify", "HEAD", optional=True).decode().strip()
        or None
    )
    state = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = _inventory(root, max_bytes)
    digest = _fingerprint(entries)
    tree = output / "tree"
    tree.mkdir()
    for entry in entries:
        target = tree / entry.path
        if entry.kind != "deleted":
            target.parent.mkdir(parents=True, exist_ok=True)
        if entry.kind == "file":
            shutil.copyfile(root / entry.path, target, follow_symlinks=False)
            if not stat.S_ISREG(target.lstat().st_mode):
                raise SourceChangedError("copied source is no longer a regular file")
            target.chmod(entry.mode, follow_symlinks=False)
        elif entry.kind == "symlink":
            if entry.link_target is None:
                raise ValueError("symlink source entry is missing its target")
            target.symlink_to(entry.link_target)
        writer.append("source-manifest", asdict(entry))
    after_revision = (
        _git(root, "rev-parse", "--verify", "HEAD", optional=True).decode().strip()
        or None
    )
    after_state = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if (
        revision != after_revision
        or state != after_state
        or entries != _inventory(root, max_bytes)
    ):
        raise SourceChangedError("source changed while creating its snapshot")
    expected = tuple(entry for entry in entries if entry.kind != "deleted")
    if _tree_entries(tree) != expected:
        raise SourceChangedError("copied source does not match its input manifest")
    # An empty repository still has a complete manifest to identify.
    if not entries:
        writer.write_file("source-manifest.jsonl", b"")
    return SourceSnapshot(
        tree,
        digest,
        revision,
        bool(state),
        entries,
        manifest,
        str(describe_artifact(manifest)["sha256"]),
    )


def verify_snapshot(snapshot: SourceSnapshot) -> None:
    """Reject a changed manifest or source tree before a build consumes it."""
    expected = tuple(entry for entry in snapshot.entries if entry.kind != "deleted")
    if (
        snapshot.root.is_symlink()
        or not snapshot.root.is_dir()
        or snapshot.manifest_path.is_symlink()
        or _tree_entries(snapshot.root) != expected
        or _fingerprint(snapshot.entries) != snapshot.fingerprint
        or str(describe_artifact(snapshot.manifest_path)["sha256"])
        != snapshot.manifest_sha256
    ):
        raise SourceChangedError("snapshot no longer matches its frozen identity")


def materialize_snapshot(snapshot: SourceSnapshot, destination: Path) -> Path:
    """Create an independent workspace; never compile inside the snapshot."""
    verify_snapshot(snapshot)
    output = destination.absolute()
    if output.resolve().is_relative_to(snapshot.root.resolve()):
        raise ValueError("build workspace must be outside the frozen source tree")
    shutil.copytree(snapshot.root, output, symlinks=True)
    expected = tuple(entry for entry in snapshot.entries if entry.kind != "deleted")
    if _tree_entries(output) != expected:
        raise SourceChangedError("build workspace does not match the frozen source")
    verify_snapshot(snapshot)
    return output
