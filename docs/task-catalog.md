# Sonata task catalog

`sonata-tasks` contains reusable command, tool, transfer, HTTP, metrics and VM
tasks for `sonata-engine`. The base wheel depends only on the engine. Optional
integrations are installed with the `shell`, `prometheus`, `multipass`, `azure`
and `proxmox` extras.

Commands receive a `CommandOptions` value. `cwd` belongs to local launchers and
`remote_dir` belongs to remote commands; adapters reject unsupported combinations.
Every executor exposes a stable `binding_key(role)`, and callable argv or verify
functions require a versioned semantic key so journal fingerprints change when
their behavior changes.

Version 0.2.0 is incompatible with the former package embedded in nanolab.
Existing journals must be completed or torn down with the old installation.
Start new runs in a new run directory after upgrading. Rollback restores the
client, task catalog pin and lockfile together; it does not reinterpret journals
created by the other version.

See `examples/shared_tasks_client.py` for a consumer that records Docker and k6
commands and executes one harmless local command without importing nanolab.

`sonata_tasks.process` provides portable managed-process resources and a separate
Linux-only owned-command API (`OwnedCommandRunner`, `OwnedCommandResult`,
`run_owned_command`). The latter bounds deadlines and combined log/summary bytes,
reaps detached descendants through an isolated subreaper, and returns cleanup
evidence instead of treating an exit code as sufficient proof of success.
See the catalogue README for an ordinary Python command example and the
standalone `sonata_tasks.owned_process` worker module.
