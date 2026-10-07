# sonata-tasks

Reusable, product-independent tasks for `sonata-engine`. The base installation
contains command execution contracts and common command-line tool tasks.
Provider and transport integrations are installed through optional extras.

The base catalogue includes Kubernetes namespace ownership, port forwarding,
Pod and OCI image inspection, and Minikube profile preflight in
`sonata_tasks.kubectl`, `sonata_tasks.imagetools`, and `sonata_tasks.minikube`.
`sonata_tasks.vm.logged` provides bounded VM command output with a complete
log copied to the host. Callers supply their own executor, VM provider, and
product-specific image checks.

`sonata_tasks.containerd` inspects a caller-owned container and its image using
caller-supplied commands, then checks the runtime image reference and published
manifest digest. The caller controls namespace, rootless session, and evidence
storage; the task has no NanoLab or recipe dependency.

With the `shell` extra, `sonata_tasks.shell.SubprocessShell` streams stdout and
stderr to the active workflow sink, including when no output listener is
provided. An explicit listener receives each line as well; command results
still contain the complete output and exit code.

`sonata_tasks.process` also exports `OwnedCommandRunner`, `OwnedCommandResult`
and `run_owned_command` for bounded local Linux commands. They require procfs,
pidfds and subreaper support. An isolated supervisor owns and reaps descendants,
including children that create their own sessions, without changing the caller's
subreaper state or signaling unrelated processes. Output goes to an exclusive
log file; optional framed summaries share the same byte budget.

```python
import os
import sys
from pathlib import Path
from threading import Event

from sonata_tasks.process import run_owned_command

result = run_owned_command(
    [sys.executable, "-c", "print('ordinary application command')"],
    cwd=Path.cwd(),
    env=os.environ,
    log_path=Path("command.log"),
    timeout_s=30,
    cancelled=Event(),
    output_limit_bytes=1024 * 1024,
)
print(result)
```

The result records exit status, cancellation, timeout, quota exhaustion, forced
stops and confirmed reaping. A zero exit code alone is insufficient to declare
success. `OwnedCommandRunner.stop(timeout_s)` requires at least 0.1 seconds for
descendant cleanup; a runner executes only once. Linux restrictions apply to this
API, while the existing `managed_process_resource` remains portable.

The implementation in `sonata_tasks.owned_process` uses only the standard
library. A worker that cannot install the catalogue may copy that module from
its installed distribution, preserving the distribution version and verifying
the source digest. It does not need the workflow engine at worker runtime.

`sonata_tasks.artifacts` provides exclusive bounded artifact directories,
serialized JSONL append, immutable JSON/raw publication and streaming file
descriptors. Acquisition requires directory `flock`; publication uses hard links,
fsync and no-follow append flags. Configurable markers share the same directory
lock during acquisition and cannot create independent concurrent owners;
the filesystem must support those operations. A fresh writer refuses existing
contents, and its ownership marker remains after close. Default accounting
charges this writer's own bytes, including partially failed appends. No files
are excluded by name. The codec limits each JSON record to 1 MiB; raw writes
are bounded by the total quota.

An ordinary command audit can also account for a log written outside the writer:

```python
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from sonata_tasks.artifacts import ArtifactWriter, describe_artifact, read_records
from sonata_tasks.process import run_owned_command

with TemporaryDirectory() as temporary:
    root = Path(temporary) / "audit"
    writer = ArtifactWriter(
        root,
        1024 * 1024,
        measure_usage=lambda: sum(
            p.stat().st_size
            for p in root.rglob("*")
            if p.is_file() and not p.is_symlink()
        ),
    )
    result = run_owned_command(
        [sys.executable, "-c", "print('ordinary application command')"],
        cwd=root,
        env=os.environ,
        log_path=root / "command.log",
        timeout_s=30,
        cancelled=Event(),
        output_limit_bytes=1024,
    )
    writer.append(
        "commands",
        {
            "returncode": result.returncode,
            "reaped": result.reaped,
            "errors": list(result.errors),
            "log": describe_artifact(root / "command.log"),
        },
    )
    writer.close()
    print(list(read_records(root / "commands.jsonl")))
```

Callers must serialize producers sharing a `measure_usage` callback; a writer's
lock only protects its own writes. An explicit `reserve_bytes` is available to
`write_json("summary.json", value, use_reserve=True)`, with no privileged filename
or default reservation. `owner_marker` selects a safe hidden filename, defaulting to
`.artifact-owner`. `read_records` yields valid objects and raises a distinct
`IncompleteRecordError` with `path` and `line_number` for a final record without
a newline. Complete malformed, non-object, non-finite or oversized records
raise `ArtifactCorruptionError`; clients decide how to report missing evidence.

`sonata_tasks.sources` captures a Git working tree for repeatable local builds:
tracked files (including deletions), nonignored untracked files, executable modes
and safe relative symlinks. Already dangling internal links are preserved; links
outside the checkout or to existing excluded inputs, cycles and submodules fail.
This differs from the committed-source remote transfer in `sonata_tasks.archive`.

```python
from pathlib import Path

from sonata_tasks.artifacts import ArtifactWriter
from sonata_tasks.sources import capture_source_snapshot, materialize_snapshot

writer = ArtifactWriter(Path("/tmp/application-snapshot"), 16 * 1024 * 1024)
try:
    snapshot = capture_source_snapshot(
        Path("/path/to/application"), writer, max_bytes=128 * 1024 * 1024
    )
    writer.write_json("receipt.json", {"fingerprint": snapshot.fingerprint})
finally:
    writer.close()
workspace = materialize_snapshot(snapshot, Path("/tmp/application-build"))
```

The caller owns/closes storage and chooses its receipt schema. Capture creates
`tree` and `source-manifest.jsonl`; failed capture retains partial evidence.
`max_bytes` bounds input bytes, including symlink target bytes; the writer's
quota separately bounds the manifest, with a 100000-entry inventory limit.
`SourceSnapshot` seals the manifest and inventory with bare-hex SHA-256 identities.
`verify_snapshot` rejects changed inputs/manifest; `materialize_snapshot` verifies
before and after copying to a fresh independent workspace. `source_entry` supports
checking original inputs in a workspace that also contains generated outputs.
The caller must control its filesystem namespace and serialize concurrent writes;
repeated inventory checks detect ordinary concurrent changes, without making
capture atomic against hostile filesystem mutation. Verification ignores empty
directories and binds file/link modes, paths and contents, not directory metadata.

`sonata_tasks.buildx.buildx_builder_resource` can acquire a private application
builder with `exclusive=True`. It refuses existing names, assigns a unique owner
node, bootstraps the builder and returns its name. `use=False` leaves the client's
selected builder intact. Default reuse and optional replacement remain available.

```python
from sonata_engine import TaskInputs
from sonata_tasks.buildx import buildx_builder_resource
from sonata_tasks.execution.local import LocalCommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions

inputs = TaskInputs.empty()
builder = buildx_builder_resource(
    name="application-build",
    executor=LocalCommandTaskExecutor(),
    exclusive=True,
    use=False,
    options=CommandOptions(env={"DOCKER_CONFIG": "/tmp/application-docker"}),
)
name = builder.acquire(inputs)
try:
    print(f"Build application images with docker buildx build --builder {name}")
finally:
    builder.release(inputs, name)
```

Exclusive cleanup verifies the original single node and `docker-container`
driver before removing the builder. Failed creation and validation compensate
partial state; the original exception retains cleanup failures as notes. An
explicit `owner_node` lets a caller record the identity. After failed acquisition,
`release(inputs, "application-build")` can retry unresolved cleanup; it does
nothing after confirmed removal or absence. Foreign replacement, added nodes or
unavailable inspection prevent removal and report an error. Callers serialize
other mutations of the same Buildx client store during inspection and removal;
Docker's CLI has no atomic compare-and-delete operation. Emulation installation
and platform requirements remain caller policy.

A failed `buildx rm` may erase the client record while leaving the daemon alive.
After such a failure, record absence remains unresolved and requires operator
reconciliation. A retry succeeds if the same owned builder is still inspectable
and its removal succeeds; an empty listing alone cannot confirm daemon cleanup.


Compose resources can clear previous project state before deployment when the
caller explicitly owns an isolated project name:

```python
from pathlib import Path
from sonata_engine import Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.compose import DockerComposeProject, docker_compose_resource
from sonata_tasks.execution.local import LocalCommandTaskExecutor

project = DockerComposeProject(
    name="application-integration-test",
    file=Path("compose.yaml"),
    ready_url="http://127.0.0.1:8080/health",
    build=False,
)
resource = docker_compose_resource(
    project,
    executor=LocalCommandTaskExecutor(),
    pre_clean=True,
    remove_volumes=True,
    remove_orphans=True,
)
workflow = Workflow("application-integration-test")
workflow.add(
    CommandTask(
        title="Check application",
        argv=("curl", "-fsS", project.ready_url),
        executor=LocalCommandTaskExecutor(),
    ),
    requires=(resource,),
)
workflow.run()
```

By default `pre_clean=False`: acquisition only deploys and waits for readiness.
Both pre-clean and final teardown use the same optional volume/orphan flags.
Failed acquisition performs best-effort teardown and retains cleanup failures
as exception notes. The caller chooses the namespace and removal policy; the
resource does not verify exclusive ownership of existing Compose state. It
returns the original project object, including subclass fields, and accepts
resource dependencies through `requires`.

VM executors can translate local project directories to an explicitly selected
remote checkout. Supply both roots when configuring an injected VM runner:

```python
from pathlib import Path
from sonata_tasks.execution.adapters import VmCommandTaskExecutor
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec


def run_remote_build(vm_runner):
    executor = VmCommandTaskExecutor(
        vm_runner,
        target_key="application-build-vm",
        local_root=Path("/workspace/application"),
        remote_root="/srv/application/releases/test",
    )
    return executor.run(
        CommandTaskSpec(
            task_id="build",
            summary="Build application",
            argv=("make",),
            options=CommandOptions(cwd=Path("src"), env={"MODE": "test"}),
        )
    )
```

The runner receives `/srv/application/releases/test/src` as `remote_dir`.
Absolute local directories must remain under the resolved local root; relative
ones resolve against it. Parent-directory and symlink escapes, or simultaneous
`cwd` and `remote_dir`, fail before calling the backend, including in dry runs.
With no `cwd`, the original `remote_dir` passes through and the runner retains
its defaults. Without mapping roots, local `cwd` remains unsupported. Remote
roots are nonempty POSIX paths; relative roots retain the runner's meaning and
are not canonicalized on the remote filesystem. Timeouts remain unsupported.
The binding identity includes the target and both mapping roots, so changes to
the destination invalidate command fingerprints. Enabling mapping also changes
existing bindings once, which can cause previously journalled commands to rerun.


Pure measurement readers are available without optional integrations:

```python
from sonata_tasks.k6 import k6_value, k6_values
from sonata_tasks.metrics import counter_delta, point_stats

summary = {"http_reqs": {"values": {"count": 12}}}
requests = k6_value(k6_values(summary, "http_reqs"), "http_reqs", "count")
observations = [
    {"timestamp": 1, "value": "10", "labels": {"job": "application"}},
    {"timestamp": 2, "value": "14", "labels": {"job": "application"}},
]
assert requests == 12
assert counter_delta(observations) == 4
assert point_stats(observations, counter=True)["delta"] == 4
```

`k6_values` accepts flat summary exports and nested `handleSummary` values.
`k6_value` requires a finite nonnegative JSON number; the first present alias
wins, even when malformed. Missing/invalid values raise `ValueError`. The
standard `checks` and `http_req_failed` rate/value fields must be within 0..1.
`finite_number` also accepts numeric strings, excluding booleans and nonfinite
values, with optional nonnegative validation.

Counter deltas group complete string-to-string publisher labels and require two samples per
publisher. Timestamped publisher samples are sorted; incomplete timestamps
retain input order. A decrease contributes the new counter value. Invalid,
missing or overflowing evidence returns `None`, while a constant counter
returns zero. Statistics sum equal timestamps and allow negative gauges.
Invalid values, sums or timestamp keys produce counts and `invalid_points`
without statistics; an unavailable or overflowing delta is omitted. Timestamp
keys must be hashable and consistently comparable, non-null/non-boolean and
finite when numeric. Missing keys have distinct internal identities and cannot
collide with observed keys; dates and units are not parsed. Callers
choose metrics, counter classification, time windows and qualification policy.

`sonata_tasks.credentials` validates and stages private application files through
any existing `RemoteProvider`. This base-only API needs POSIX current-user
ownership, no-follow/nonblocking opens, and trusted providers with
caller-controlled filesystem namespaces. Source files must be nonempty regular
owner-readable files with no group/world permissions. Copies are 0600 inside
0700 directories; remote targets must support `mktemp`, `chmod` and `rm`.
Names and prefix are public basenames, never credential contents.

```python
from pathlib import Path

from sonata_tasks.credentials import stage_private_files, validate_private_file

source = validate_private_file(Path("service.key"))
with stage_private_files(provider, request, {"service-key": source}) as staged:
    directory, paths = staged
    configure_service(paths["service-key"])
```

The context removes both temporary locations on success and failure. Operational
provider failures omit command output and exception messages. Body/programming
exceptions retain their type after successful cleanup; `CredentialCleanupError`
reports only the interrupted operation's type when cleanup fails. An invalid
`mktemp` response is refused without guessing a directory to remove. The provider
and caller remain responsible for protecting the remote filesystem namespace.
