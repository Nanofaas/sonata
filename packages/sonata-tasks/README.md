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
