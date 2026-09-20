# Sonata

Sonata is a small workflow engine for Python. You describe work as tasks; Sonata
orders them, gives each task typed access to the values and resources it
declared, and records enough to resume a run — or to run only a slice of it —
without repeating work that already succeeded.

It is product-independent: no CLI, no cloud provider, no configuration files, and
no opinion about what your tasks do. The engine is stdlib-only.

`sonata_engine` · Python 3.12+ · zero runtime dependencies · typed (`py.typed`)

## Install

```bash
pip install sonata-engine
```

## Quickstart

A task is a `Task[T]` whose `run` returns a `TaskOutcome[T]`. Inside a composite,
each step receives the value the step before it produced:

```python
from sonata_engine import Steps, Task, TaskInputs, TaskOutcome, Workflow


class Build(Task[str]):
    title = "Build image"

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        return TaskOutcome(value="registry.example/api:v1")


class Deploy(Task[str]):
    title = "Deploy"

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        image = inputs.upstream()
        return TaskOutcome(value=f"{image} -> staging")


workflow = Workflow(workflow_id="release")
workflow.add(Steps(title="Release", steps=(Build(), Deploy())))

result = workflow.run()
unit = result.tasks[0]
print(unit.task_id, "->", unit.outcome.value)
```

```
001.release -> registry.example/api:v1 -> staging
```

## The mental model

**Tasks have no IDs of their own.** `Workflow.run()` compiles the workflow and
assigns each unit a deterministic ordinal and slug — `001.release`. Results,
journal records and events are all keyed by that compiled ID, so nothing in your
code has to invent, carry or hardcode one.

**A composite is one unit.** `Steps` runs its steps in order and is itself a
single compiled unit: one ordinal, one journal entry, one fate. Its value is the
last step's value. The steps are journalled individually, which is what lets a
resumed run skip the ones that already finished.

**Values flow between steps, not between units.** Each task passed to
`workflow.add(...)` is an independent unit: it receives no value from whatever
ran before it, and calling `inputs.upstream()` there raises
`NoUpstreamValueError`. Values move along the steps of a composite; units share
resources and the journal, not values.

**`TaskInputs` is capability-limited.** It is a task's access to the values it
declared — its upstream value and its resource values, and nothing else. It is
not `WorkflowContext`, which carries flow and task identifiers for reporting and
event correlation. A task receives `TaskInputs`, never `WorkflowContext`.

**Outcomes carry evidence.** Besides its value, a `TaskOutcome` holds `evidence`,
which is what makes a task skippable on a later run. See *Reusing work*.

## Watching a run

Bind a sink and the engine reports progress through it. The contract is small:
`emit` for events, and a `status` context manager for whatever progress display
you already have. Nothing is bound by default, and with no sink bound `subtask`
is a no-op.

```python
from contextlib import contextmanager

from sonata_engine import WorkflowEvent, bind_workflow_sink


class ConsoleSink:
    def emit(self, event: WorkflowEvent) -> None:
        depth = 1 if event.parent_task_id else 0
        print(f"{'    ' * depth}{event.task_id:<32} {event.kind}")

    @contextmanager
    def status(self, label: str):
        yield


with bind_workflow_sink(ConsoleSink()):
    workflow.run()
```

A composite of three steps reports as:

```
001.release                      task.started
    001.release/build-image          task.started
    001.release/build-image          task.passed
    001.release/scan-image           task.started
    001.release/scan-image           task.passed
    001.release/deploy               task.started
    001.release/deploy               task.passed
001.release                      task.passed
```

The steps are named by the engine, from their titles, under the compiled unit, so
you choose titles and nothing else.

## Assembling a task from steps

`Steps` is for when the operations *are* the task. A task made of steps needs no
`run()` of its own: `Steps` takes them and runs them in order, feeding each the
value the one before produced.

```python
from sonata_engine import Steps

workflow.add(
    Steps(
        title="Deploy the chart",
        steps=(HelmInstall(chart), WaitRollout(), ResolveEndpoint()),
    )
)
```

Each step is an ordinary `Task`, so anything already written serves as one, and a
step that needs no input simply never calls `inputs.upstream()`.

Because its steps are journalled individually, a resumed run skips the ones
already finished — what may be skipped is decided exactly as for any other unit,
by a `ReusableTask` whose evidence still verifies. Build five images, have the
fifth fail, resume, and only the fifth runs again. A step's only legal value is
`None`, which the engine reconstructs when the step is skipped.

## Reporting progress inside one task

A hand-written `run()` can report progress without turning that work into
journalled steps. `subtask` emits the same events a compiled step does, nested
under whichever task is running:

```python
class PublishImages(Task[str]):
    title = "Publish images"

    def __init__(self, slug: str, images: tuple[str, ...]) -> None:
        self._slug = slug
        self._images = images

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        for image in self._images:
            with subtask(task_id=f"{self._slug}/build/{image}", title=f"Build {image}"):
                ...  # build it

        with subtask(task_id=f"{self._slug}/scan", title="Scan for vulnerabilities"):
            ...  # scan everything built above

        with subtask(task_id=f"{self._slug}/push", title="Push the tags"):
            digest = ...  # push, and keep what the registry answered

        return TaskOutcome(value=digest)
```

Subtasks need not come from a loop and need not resemble each other — those are
three different kinds of step, and the last one produces the value the whole task
returns.

The step stays one compiled unit: one ordinal, one journal entry, one thing a
`Selection` can name. If the scan fails the whole unit fails, because sharing one
fate is what makes these one unit rather than four. Subtasks exist in the event
stream only, so a consumer's UI can show progress through a long step, and a
resumed run restarts that step from its beginning. If the reported operations are
the task's actual sequential steps, prefer `Steps` above: it names and journals
them for you.

Pick `task_id` yourself and keep it unique within the run. A consumer keys child
phases by it, so a repeat merges two steps into one. Do not imitate the engine's
`NNN.slug` IDs: those are the engine's, and a task is not told its own. `slug` is
a constructor argument rather than a hardcoded literal because two instances of
the same task class (two `PublishImages` in one workflow) need something to tell
their subtask IDs apart.

Open subtasks sequentially, on the thread running the task. The parent is
resolved through a context shared as a fallback for worker threads, which start
with none of their own; subtasks opened concurrently from worker threads — or one
left open past its `with` block while another opens — nest under each other
instead of under the unit, silently.

## Resources

A `Resource[T]` acquires a runtime value of type `T` and receives that same typed
value back when it is released. Consumers declare what they need; the compiler
splices acquire and release units around them, and cleanup runs in reverse
acquisition order after success or failure.

```python
from sonata_engine import Resource, TaskInputs


def start_builder(inputs: TaskInputs) -> Builder:
    return Builder()


def stop_builder(inputs: TaskInputs, builder: Builder) -> None:
    builder.stop()


builder: Resource[Builder] = Resource(
    title="Acquire builder",
    acquire=start_builder,
    release=stop_builder,
    acquire_idempotent=True,
)

workflow.add(Build(), requires=(builder,))
```

Inside `Build.run`, `inputs.resource(builder)` returns the acquired value. A
resource may itself declare `requires=(other_resource,)`; those dependencies are
acquired first, stay available to their lifecycle callbacks, and are released
after their dependents. Declare only the resources you use directly. A cycle
fails compilation with `ResourceDependencyCycleError`.

`acquire_idempotent=False` is the safe default: a failed or interrupted acquire is
ambiguous, and resume refuses to retry it automatically.

### Retention

With `Workflow(workflow_id=..., keep=True)`, every resource is retained except
those declaring `always_release=True`. Retention is opt-out because the two
questions it used to conflate are unrelated: whether a resource is expensive to
rebuild, and whether it is safe to leave behind. A resource holding a secret — a
staged token, a signing key, an open credential lease — declares
`always_release`, so leaving one behind stops being something a caller can cause
by forgetting to classify it.

Each retained resource is written to the journal with its acquired value, and a
later process releases them with:

```python
from sonata_engine import release_retained

release_retained({vm.title: vm}, JournalConfig(path))
```

Values are journaled as JSON, so a resource whose value cannot be encoded is
released rather than retained — a retention the journal cannot record is a
promise it cannot keep. `Resource.revive` rebuilds a dataclass value from its
record, since a release written against one would break on a dict.

## Reusing work

A `ReusableTask` may be skipped only when it returned non-empty evidence and every
evidence item has a successful verifier. It must also expose a deterministic
`reuse_key` that changes whenever its semantic inputs change; that key
participates in the workflow fingerprint. Reusable tasks cannot return a runtime
value.

Sonata ships one generic verifier, `file-digest`. Domain evidence — an OCI
artifact, a deployed release — must be verified by a verifier you inject:

```python
result = workflow.run(
    journal=JournalConfig(Path("run/journal.jsonl")),
    resume=True,
    verifiers={"oci-image": verify_oci_image},
)
```

## Running a slice

`Selection` narrows a run to some of its consumer tasks, addressed by title slug:

```python
from sonata_engine import Selection

workflow.run(select=Selection(only="build-image"))
workflow.run(select=Selection(start="build-image", until="publish-manifest"))
```

Selection names tasks by slug rather than by compiled `task_id`, because ordinals
renumber over the survivors — an ID is not a stable handle for the very operation
that changes it. Resources are not selectable: the compiler re-splices acquire and
release around whichever consumers survive, retaining every transitive resource
dependency those consumers need, so a slice keeps its setup and cleanup. That
renumbering makes a sliced run a different topology, so `resume` across one fails
closed.

## Journal and resume

Passing `journal=` writes an optional JSON Lines journal, created from the full
compiled topology. Each task starts at attempt `0`, status `pending`; later
attempts append lifecycle records. Every record carries a deterministic workflow
fingerprint, and resume fails if the ordered task topology or a task's type
changed. A torn final line is removed before continuing, while any complete
malformed record raises `CorruptJournalError`.

Runtime values — `TaskOutcome.value` and acquired resource values — are in-process
only: they are not journaled and are not reconstructed by resume. Make resumed
work depend on durable, verifier-backed evidence rather than on a value from a
previous run. Resource acquire callbacks run again on resume, yielding fresh
in-process values for the resumed run.

## Upgrading

**A Sonata upgrade invalidates existing journals.** The fingerprint is derived
from the compiled topology, which includes the engine's own internal shape: for
example, adding resource-dependency edges in `0.2.0` changed the fingerprint of
every workflow, even ones that declare no resources. With `resume=True` a
fingerprint mismatch raises `WorkflowTopologyMismatchError` — loud and correct.
Without `resume` (a plain `journal=` run), old records for a different fingerprint
are simply ignored and a new topology is appended to the *same file*; this emits a
`UserWarning` (Sonata adds no logging dependency) but the run itself proceeds.
Start a fresh journal file after upgrading if you don't want mixed topologies
accumulating in one file.

**`0.2.0` changed two call signatures with no compatibility shim.** Pre-existing
code must be migrated before upgrading:

- `Task.run(self)` → `Task.run(self, inputs: TaskInputs)`. Every concrete task's
  `run` now takes the workflow's `TaskInputs` as its one argument.
- `Resource(acquire=lambda: ..., release=lambda: ...)` →
  `Resource(acquire=lambda inputs: ..., release=lambda inputs, value: ...)`.
  `acquire` now takes `TaskInputs` and returns the resource's runtime value;
  `release` now takes `TaskInputs` and that same value.

Old-shape code raises `TypeError` at call time (`run() takes 1 positional argument
but 2 were given`) rather than silently misbehaving, so the break is loud. It is
what makes `TaskInputs`/`Resource` dependencies possible at all.

## Related

This repository also builds **`sonata-tasks`**, a reusable task catalogue layered
on the engine. Its public contracts, extras and journal migration rules are
documented in
[`docs/task-catalog.md`](https://github.com/miciav/sonata/blob/main/docs/task-catalog.md);
an independent executable consumer lives in
[`examples/shared_tasks_client.py`](https://github.com/miciav/sonata/blob/main/examples/shared_tasks_client.py).

## Development

The repository is a uv workspace of two packages, so most checks run per package
rather than over the whole tree:

```bash
uv sync --all-packages --all-groups --all-extras   # install the whole workspace

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

ruff, basedpyright, bandit and import-linter are all wired into pre-commit, so
local and CI results cannot drift, and `pytest` enforces the coverage gate
declared in `[tool.coverage.report]`.

The v2 design and the migration sequence it came from are documented in
[`docs/plans/2026-07-24-release-on-workflow-engine.md`](https://github.com/miciav/sonata/blob/main/docs/plans/2026-07-24-release-on-workflow-engine.md).
