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
