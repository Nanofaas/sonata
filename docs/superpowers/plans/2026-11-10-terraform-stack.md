# Terraform stacks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Native execution was selected and the design approved with “procedi”.

**Goal:** Ship reusable Terraform/OpenTofu stack tasks in Sonata and use them to provision Nanolab experiment fleets.

**Architecture:** One Sonata Resource owns one local-state stack. Nanolab prepares one Multipass module per site and makes node setup resources depend on its outputs; only the stack deletes VMs.

**Tech Stack:** Python, existing Sonata executor/Resource, OpenTofu/Terraform CLI, Nanofaas Multipass provider, pytest.

**Spec:** `docs/superpowers/specs/2026-11-10-terraform-stack-design.md` (approved).

## Global Constraints

- Work in existing isolated Sonata and Nanolab worktrees; preserve remediation changes.
- No new Python dependencies; explicit CLI selection, argv execution, finite timeouts.
- Owned resources use default workspace and local state only. Foreign state is never adopted.
- State, plans and diagnostics stay private; journal outputs contain selected non-sensitive facts only.
- After consumers start, resume validates and refuses infrastructure changes. Recovery retention survives restart.
- No publication or public pins to unavailable versions. Record candidates used by integration.

## Review Focus

- Environment variables redirecting state/CLI flags: reject unsafe inherited overrides before mutation (Task 1).
- Config changes after constructing a task: fingerprint and execution must detect altered inputs (Task 2).
- Timeout after remote creation but before state write: retain uncertain ownership, never claim successful rollback (Task 2).
- Process restart during collection: missing success evidence retains the whole stack (Task 3).
- Partial outputs, stale addresses and duplicate node mapping: fail before bootstrap (Task 3).

## Task 1: Reusable CLI operations

**Files:** create `packages/sonata-tasks/src/sonata_tasks/terraform.py`, `packages/sonata-tasks/tests/test_terraform.py`.

**Interfaces:** `TerraformSpec(directory: Path, owner: str, inputs: tuple[str, ...], outputs: tuple[str, ...] = (), binary: str = 'tofu', timeout_seconds: float = 600, lock_timeout_seconds: int = 60, env: Mapping[str,str] = {})`; `TerraformTask(spec, action, *, executor, role='host')`; result has `changed` and selected `outputs`, never raw CLI text. Actions are init/plan/apply/output/destroy.

- [ ] Write failure-first tests for both executable names, plan exit 0/2/1, saved-plan application, JSON output membership/types/sensitive rejection, unsafe environment overrides and sanitized errors/timeouts.
- [ ] Run targeted tests; expect missing module/behavior failures, then implement using existing executor options. Capture diagnostics privately; avoid the generic CommandTask exception path which interpolates raw stdout.
- [ ] Run targeted tests and lint/types; expect all pass. Commit the independently usable operations.

## Task 2: Owned stack resource

**Files:** same module/tests; create `packages/sonata-tasks/tests/test_terraform_integration.py`; update README.

**Interfaces:** `terraform_stack_resource(spec, *, executor, role='host', requires=(), retain: Callable[[], str | None] | None=None) -> Resource[TerraformResult]`. Spec input paths are relative files under its prepared directory, including the provider lock file when providers are used. A durable private ownership record stores identity, input digest and phase; writes are atomic and fsynced.

- [ ] Add tests for foreign state/no destroy, custom backend/workspace rejection, input mutation, partial apply compensation, failed cleanup preserving the primary cause, uncertain timeout, retention, repeated destroy, changed config and drift on resume. Assert effects on real ownership records and emitted CLI commands at the executor boundary.
- [ ] Implement ownership before init, backend validation before plan/apply, and guarded compensation. Initial/retry acquisition may apply only before consumers have used the stack; a previously delivered stack requires a no-change plan. Use Resource revival to validate real state rather than trust serialized outputs. Do not automatically recover an interrupted uncertain apply.
- [ ] Run a provider-free `terraform_data` module with the installed OpenTofu CLI: acquire, refresh/no-change resume, output, destroy twice, partial apply failure. Run Terraform too when available and explicitly record absence otherwise.
- [ ] Run both Sonata suites, wheel smoke, lint/types; expect green. Commit resource and documentation.

## Task 3: Nanolab fleet integration

**Files:** modify `packages/nanolab/src/nanolab/config/environment.py`, `one_shot/infrastructure.py`, `one_shot/multisite.py`; create `one_shot/terraform.py`, packaged `assets/terraform/multipass` module and lock; tests under `packages/nanolab/tests/one_shot/`; update example environment/docs.

**Interfaces:** `EnvironmentConfig.provisioning: Literal['direct','opentofu']='direct'`; `EnvironmentConfig.opentofu` config contains executable and optional CLI config file (local provider mirror). `build_multipass_stack(...)` receives existing nodes/requests, provider transport, site, generator and run directory, and returns a fleet Resource plus per-node VM dependencies. Existing `NodeResource` remains the downstream contract.

- [ ] Test config compatibility, exact output mapping and missing/malformed addresses, grouped site stacks, and existing direct/Azure behavior.
- [ ] Extract node setup/teardown around an injected acquired-VM Resource from `_owned_node`; direct provisioning retains its refusal to adopt and current VM cleanup. Terraform node dependencies never delete VMs. Reuse pinning, tunnels and live facts.
- [ ] Prepare pinned module/variables from scenario nodes; inject existing VM SSH key, reuse provider command/transfer transport. Validate existing VM-name collisions and CPU/RAM/disk admission before apply. Resolve provider SSH settings from the existing site without disabling host verification.
- [ ] Persist consumer/collection phase for retention; a node's recovery flag or missing completion after restart blocks stack destruction. Node cleanup continues to close tunnels and stop retained VMs where supported. Tests prove collector-before-stack release and archive failure retention.
- [ ] Run Nanolab affected groups then full Linux suite against candidate Sonata. Commit integration and docs.

## Task 4: Real provider and cross-repository acceptance

**Files:** integration tests/fixtures and evidence under Nanolab `docs/testing/2026-11-10-terraform-stack/`.

- [ ] Build or verify the pinned Multipass provider release in an isolated local mirror, record commit/version/hash and run provider preflight on `.35`.
- [ ] Through the actual Sonata Resource, create two uniquely named small VMs; verify actual CPU/RAM, guest commands, outputs and no duplicates on re-entry; collect distinct guest files and checksums; destroy only the owned state. Repeat with controlled partial failure/recovery. Export evidence first.
- [ ] Run smallest supported Nanolab experiment through OpenTofu and candidate collection/wheels. Check load, export, coverage, reports and cleanup, plus retained evidence on collection fault.
- [ ] Ten-VM acceptance is separately gated by compatible freeze/reference and host capacity; never infer it from smoke tests or overcommit root disk.
- [ ] Run final fresh-context branch review required by native execution; fix important findings with RED→GREEN tests. Prepare release notes/artifacts, retain reproducible evidence, commit remaining changes. Do not publish.

## Self-review

All approved spec sections map to Tasks 1–4. Interfaces deliberately reuse the current NodeResource transport and executor rather than introduce a VM adapter per Terraform resource. Retention gates both release and resume. Real CLI and actual VM tests are distinct from mocked command tests. Existing review/remediation evidence and its outstanding Linux checks remain tracked separately.
