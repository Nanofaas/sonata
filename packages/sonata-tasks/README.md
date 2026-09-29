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
