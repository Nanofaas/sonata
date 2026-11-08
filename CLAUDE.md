# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

This repository is a uv workspace of two packages, and most checks run per
package rather than over the whole tree:

- `sonata-engine` (`src/sonata_engine/`) — the engine, no runtime dependencies.
- `sonata-tasks` (`packages/sonata-tasks/`) — the task catalogue, with optional
  integrations for shellcraft, proxmox-sdk, azure-vm-sdk and multipass-sdk.

```bash
uv sync --all-packages --all-groups --all-extras   # install everything

# Engine
uv run pytest -c pyproject.toml tests
uv run ruff check src tests
uv run basedpyright --project .

# Catalogue
uv run pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
uv run ruff check --config packages/sonata-tasks/pyproject.toml packages/sonata-tasks
uv run basedpyright --project packages/sonata-tasks
uv run lint-imports --config packages/sonata-tasks/.importlinter --no-cache

# Everything CI runs, in one go
uv run pre-commit run --all-files
```

## Tooling

The same stack as the sibling projects: ruff for lint and format, basedpyright
for types, bandit for security, pytest with a coverage gate, all wired into
pre-commit so local and CI cannot drift.

| Concern | Tool | Config |
| --- | --- | --- |
| Lint + format | ruff (88 cols) | `[tool.ruff]` in each package's `pyproject.toml` |
| Types | basedpyright | `[tool.basedpyright]`, standard mode |
| Security | bandit | `[tool.bandit]` |
| Import layers | import-linter | `packages/sonata-tasks/.importlinter` |
| Coverage | pytest-cov | `[tool.coverage.report]`, `fail_under = 90` |

Two deliberate differences from the 3.11 projects: `reportImplicitOverride` is
**on** here (the engine requires 3.12, so `typing.override` is always available),
and `ANN` is part of the ruff rule set, because this codebase already required
annotations everywhere and dropping the rule would have been a loosening.

The coverage threshold is declared once, in `[tool.coverage.report]`. It is not
repeated as a `--cov-fail-under` flag on the pytest command line: stating it in
two places is how the two come to disagree, with the flag silently winning.

**Watch out for `build/`:** ruff's built-in exclude list has `dist` but not
`build`, so a leftover setuptools build tree gets linted as if it were source.
Both packages set `extend-exclude = ["build", "dist"]` for that reason.

## What this is

Sonata (`sonata-engine` distribution, `sonata_engine` import) is a product-independent Python workflow engine extracted from `miciav/nanofaas`. It has **zero runtime dependencies** — keep it that way.

**Hard boundary:** Sonata must never import anything from `nanofaas`, `controlplane_tool`, VM providers, Ansible, load-test, release, or remote-execution code. Product-specific tasks stay in nanoFaaS; workflow assembly stays in `controlplane-tool`. Both depend on Sonata, never the reverse.

## Architecture

Two layers under `src/sonata_engine/`:

- **`core/`** — execution. `Task[T]` is a generic ABC whose `run()` returns
  `TaskOutcome[T]`. `Workflow.add()` builds a sequential workflow;
  `Workflow.compile()` assigns canonical `NNN.slug` IDs and inserts acquire and
  release units for declared `Resource`s (a pair of tasks; `compose` builds
  one resource from an ordered sequence of them); `Workflow.run()` executes the compiled
  graph, releases resources on success or failure, and optionally journals for
  resume. `Steps` assembles ordinary tasks into one compiled unit with
  individually journalled child steps and in-process upstream values.

- **`workflow/`** — observability. Frozen-dataclass `WorkflowEvent`s are emitted
  to a `WorkflowSink` Protocol (`emit`/`status`). The sink and current
  `WorkflowContext` live in ContextVars with a shared fallback for worker-thread
  visibility. The runner owns task lifecycle events; hand-written tasks may use
  `workflow_log`, `status`, and the reporting-only `subtask` context manager.
  Emission is a no-op when no sink is bound.

The core runner calls the private lifecycle helpers in `workflow/reporting.py`;
tasks never emit their own top-level lifecycle events.

Tests mirror the source layout (`tests/core/`, `tests/workflow/`).

## Design documents

The v2 execution design is recorded in
`docs/plans/2026-07-24-release-on-workflow-engine.md`. Composite-task behavior
and implementation are documented in
`docs/specs/2026-07-28-composite-task-design.md` and
`docs/plans/2026-07-28-composite-task.md`. Read the relevant document before
changing public APIs or journal semantics.
