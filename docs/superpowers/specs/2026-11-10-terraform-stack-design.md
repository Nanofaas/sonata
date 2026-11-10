# Terraform/OpenTofu stacks in Sonata, consumed by Nanolab

Status: proposed for approval. This extends the ongoing review/remediation; it is not yet implemented.

## Intent and scope

Provide an engineered, reusable way to create a group of VMs through Terraform/OpenTofu in `sonata-tasks`, and use it in Nanolab's one-shot experiments. Sonata owns CLI execution and stack lifecycle; Nanolab supplies the experiment topology and consumes connection facts. The first real integration uses `Nanofaas/terraform-provider-multipass` against `michele@149.132.179.35`.

User requirements: reusable Sonata functionality, actual Nanolab integration, tests, native implementation. The details below are implementation proposals, not previously approved choices.

## Approaches

1. Individual command tasks only: useful building blocks, but every caller must implement ownership, partial failure and teardown correctly.
2. A provider-specific VM adapter invoking Terraform once per VM: fits the current per-node API but fragments state and does not model a fleet as one dependency.
3. Reusable Terraform operations plus a composed stack Resource: follows Sonata's Helm/Compose conventions, gives Nanolab one fleet dependency, and centralizes lifecycle checks. Recommended. Ordinary operations remain usable for explicit workflows; automatic teardown belongs only to the owned-stack Resource.

No new Python Terraform SDK, shell-based command assembly, custom HCL parser, replacement scheduler, or generic VM-provider registry is needed. Reuse the existing executor, resource dependency graph, compensation/error conventions, and VM connection objects.

## Sonata contract

Add `sonata_tasks.terraform` to the existing task distribution; no engine dependency on Terraform or Nanolab.

- A stack specification identifies a dedicated working directory, explicit executable (`tofu` by default; `terraform` selectable), module/configuration identity, variable-file paths, and finite command/lock timeouts. It is independent of Multipass and VM schemas.
- Expose init, plan, apply, output and destroy operations in the existing task style. `plan` distinguishes no changes, changes and failure; the saved plan is the one applied. Read machine-readable output, never parse human CLI prose.
- `terraform_stack_resource(...)` composes those operations and yields a handle containing the owned stack location/identity and validated, non-sensitive outputs. Its release destroys only that owned stack. Do not expose raw state or raw command output as workflow values.
- Use the injected command executor and argv tuples. Both binaries follow the same supported command contract and receive equivalent tests; do not silently switch executables after an error.
- Start with local state in a private per-run directory. The owned Resource rejects remote/custom backend configuration, an unrelated existing state, and implicit workspace switching. Supporting arbitrary backends is deferred; standalone operations do not imply ownership of their state.
- The prepared directory contains immutable module inputs and the provider lock file. Fingerprints include their content identity, variable-file contents via digests, executable selection and executor binding. Never put variable contents or secret values in a fingerprint payload or journal.
- A durable ownership record is created before the first mutation. Fresh acquisition checks that state is absent; recovery accepts only the same owner/configuration identity. State absence alone never proves that a failed provisioning left no external resources.
- On a partial apply failure, attempt cleanup only after ownership has been established, preserve the original failure, and retain the directory plus cleanup diagnostics if teardown fails. An init/validation/ownership failure must not trigger destruction of pre-existing infrastructure.
- Timeouts/cancellation stop the owned CLI process group using the existing executor. Preserve uncertain state and report it as recovery-required if the provider's external operation cannot be confirmed finished. No force-unlock or destructive retry on an unknown lock owner.
- Resume reads/refreshes real state and outputs; journal success alone must never resurrect stale VM addresses. After a stack has been handed to consumers, resume is validation-only: any plan requiring infrastructure changes fails with a recovery diagnostic, even if the input digest is unchanged. In particular, drift must not trigger replacement of a VM holding evidence. Changed configuration is rejected for continuation of the same frozen run. A new intended topology uses a new run identity. A fresh retry before any consumer ran is allowed only after prior cleanup is confirmed complete, or through an explicit recovery operation.
- Output JSON can contain secrets. Generic results expose only explicitly selected non-sensitive outputs; a requested sensitive output is rejected. Raw CLI diagnostics/state/plan files stay in the private directory, and public exceptions/journal records expose a bounded summary and diagnostic path, not raw payloads.
- The release path supports a caller-provided retention guard using existing Resource composition, rather than a Nanolab-specific condition in Sonata. Retention is recorded as an incomplete cleanup needing recovery, never a successful destroy.

Public type names and the smallest number of task classes/functions are left to implementation, following the catalog's conventions. No public extension to `sonata-engine` is planned.

## Nanolab integration

Keep the distinction between infrastructure provider and provisioning mechanism. Add an explicit one-shot infrastructure setting selecting the existing direct path or OpenTofu; preserve existing configurations. The implementation plan will settle its exact schema name beside existing environment settings. New experiment fixtures select OpenTofu explicitly.

Nanolab ships a small Multipass module with a map of nodes and `for_each`, keyed by stable node IDs. Existing scenario node CPU/RAM/disk settings and site placement generate variable JSON; do not maintain a second hand-written topology. Pin provider source/version/checksums; record the tested provider commit. Its current README requires a local provider mirror because registry registration is pending.

Create one independently owned stack per Multipass site (one stack for a local host). This permits ordinary provider configuration per site and preserves the existing multisite boundary. On the `.35` the provider can run Multipass over SSH directly from the operator; no daemon TCP exposure or new managed bridge is needed for the first integration.

Each stack returns a node-ID map of VM name, addresses and connection facts. Validate exact membership, types, nonempty addresses and the expected names before any bootstrap. Translate these into existing `VmInfo`/`NodeResource` objects and reuse the current command/transfer transport.

Refactor the node resource boundary to take an acquired VM dependency separately from node setup/teardown. Both provisioning paths reuse CPU pinning, forwarding, tunnels, live-VM bookkeeping and recovery checks. For the OpenTofu path, node teardown removes node-owned processes/tunnels but never calls provider VM deletion. The stack dependency is released last, after all consumers and collectors.

If any node in a stack has raw evidence requiring recovery, retain the entire affected stack and its state. This intentionally retains other VMs in that stack; selective `-target` destruction/state surgery is out of scope. Export/recovery status must be recorded before release can destroy anything. Ordinary workload failures still clean up when evidence collection succeeded.

Retention is conservative across process restarts: once consumers have started, missing collection completion records mean retention, not permission to destroy. Reuse the run's verified archive receipts/journal to establish completed collection; persist any additional stack phase marker before handing it to consumers. A failed write cannot clear retention. Neither resume nor acquisition compensation may bypass this evidence gate. This resolves the builder review's concern that drift reconciliation could otherwise delete evidence before release.

Existing Azure/proxmox/direct provisioning behavior remains supported. This first change does not migrate their implementation or broaden the current Azure placement rules. The Terraform task itself remains provider-neutral.

## Tests and acceptance

1. Sonata unit tests with the existing fake executor: arguments and exit codes; invalid specs; output validation/sensitive output handling; immutable fingerprints; foreign-state rejection; apply failure before/after partial creation; original failure plus failed cleanup; lock timeout; retention; changed-input resume; no stale journal-only output reuse.
2. Real local CLI tests with a provider-free `terraform_data` module: init/plan/apply/output, second apply without changes, destroy twice, deliberate failure after a resource enters state, restart/recovery. Run against OpenTofu and Terraform when available; explicitly report unavailable engine coverage. Never treat mocked calls as lifecycle acceptance.
3. Nanolab regression tests: all nodes appear exactly once; mapping/site isolation; no per-node VM deletion for a stack; consumers/collectors finish before stack destroy; failed archive retains the stack; CPU/tunnel cleanup still runs; original direct-path tests remain green.
4. Server smoke: two small VMs through the actual provider on `.35`, verify sizing and guest execution, rerun without duplicate VMs, export distinct files from both guests, then prove scoped destruction. Repeat with a controlled failed acquisition/recovery scenario. Record config, provider/CLI versions, plan summary, outputs, inventory and checksums without secrets.
5. Run the smallest supported real Nanolab experiment with the three candidate packages and OpenTofu provisioning, exercising export and recovery. Only then run the ten-VM scientific acceptance if a suitable freeze/reference and capacity are available.

The `.35` is reachable and has 20 logical CPUs, about 115 GiB available RAM and 80 GiB free root storage. Nanolab node disks default to 20 GiB, so ten such VM disks nominally require 200 GiB before ancillary data; sparse allocation is not an adequate storage admission policy. A successful small smoke does not establish capacity or scientific validity for the ten-VM run. A sampled existing server freeze has six nodes and no `environment.yaml`; it is not yet the ten-VM acceptance input.

## Delivery and remaining decisions

Implement natively with failure-first tests in the isolated checkouts. Preserve the current remediation commits and keep the new feature's commits identifiable. Prepare compatible Sonata/Nanolab release artifacts and notes; do not publish or change public dependency pins to unavailable artifacts. Candidate integration uses isolated local installations.

Acceptance requires both the reusable Sonata contract and a Nanolab consumer tested on real VMs. A generic command wrapper or standalone `.tf` example alone is insufficient. No new resources have been created on the server for this proposal.

References: [provider](https://github.com/Nanofaas/terraform-provider-multipass), [OpenTofu plan](https://opentofu.org/docs/cli/commands/plan/), [output and sensitive values](https://opentofu.org/docs/cli/commands/output/).
