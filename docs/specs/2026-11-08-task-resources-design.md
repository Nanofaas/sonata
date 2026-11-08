# Resources made of tasks

Design record for Sonata 0.7.0.

## Decided

1. **The task is the reusable unit.** A resource is a pair of tasks: an
   acquire `Task` producing the value, a release `Task` consuming it. Acquire
   and release therefore run through the normal runner: journalled, emitting
   the normal workflow events, resumable.
2. **Callable resources are removed.** `Resource(acquire=callable,
   release=callable)` goes away; there is one way to declare a resource.
   Released as a single breaking Sonata version (engine and tasks under one
   tag); `sonata-tasks`' 9 callable resources are converted in the same tag.
3. **Composite resource.** (Superseded in part: see "Settled while planning"
   and "As built".) A resource can be declared as an ordered sequence
   of elements, each either a plain `Task` (runs once during acquire: render a
   config, verify ingestion) or a task resource (started during acquire,
   stopped during release). Semantics:
   - acquire runs the elements in order, like `Steps`; each step journalled;
     the composite's value is the `Members` mapping (see "Settled while
     planning");
   - release first runs an optional ordered sequence of **collect** tasks
     (Prometheus snapshot, log export), then the release task of each member
     resource **that was acquired**, in reverse order;
   - acquire failing at step *k* releases only the member resources acquired
     before *k*, in reverse; the journal records which, so a teardown in a
     later process does the same;
   - resume skips completed steps with the same rules as `Steps`.
   Consumers are unchanged: `Workflow.add(task, requires=(platform,))`.

## Settled while planning (author's calls)

- The composite's members are task resources; plain tasks in the sequence
  are not needed for the first milestone (a "render config" step belongs
  inside the member's own acquire task) — dropped, add when a real case needs
  one.
- The composite's value is a `Members` mapping (member title → member value)
  with a typed accessor `members.of(resource)`; consumers that require the
  platform read `inputs.resource(platform).of(prometheus)`.
- A failing composite acquire unwinds its own already-acquired members
  before re-raising (the engine never releases an acquire that did not
  complete — the same rule `sonata_tasks.process` follows today).
- A composite is `always_release` when any member is, so `keep` never strands
  a member that refuses to be kept.
- A resource that is a member of a composite may not also be required
  directly by a task: compile refuses it, since it would be acquired twice.

## Kept, to be carried onto task resources

(Superseded by "As built" where they differ.)

`requires`, `keep` / `always_release`, `revive` (a release in a later process
still rebuilds the value from the journal's JSON), `acquire_idempotent`
(becomes the acquire task's own `idempotent`), identity semantics (`eq=False`,
keyed by `id()`), the compiler splicing acquire before the first consumer and
release after the last.

## Journal compatibility (decided)

No compatibility code: the new version need not read journals written by
the callable version. Every journal feature is kept for task resources:
resuming an interrupted run, retaining a resource with `keep`, and reviving
and releasing it from a later process. Before bumping nanolab, kept
resources are released and interrupted runs finished or discarded with the
old version; the plan includes that one-off drain step.

## As built

- `Resource(title, acquire: Task[T], release: Task[None], requires=(),
  always_release=False, revive=None)`. Callable resources and
  `acquire_idempotent` are gone; idempotence is the acquire task's own
  `idempotent`.
- The release task reads the acquired value through `inputs.upstream()`.
  `release_retained` runs the release task with the revived value as upstream.
- Acquire and release may be `Steps`; each step is journalled.
- `compose(title, members, *, collect=()) -> Resource[Members]`;
  `Members.of(resource)` is the typed accessor. Members are acquired in order
  and released in reverse, after the collect tasks. A failing composite acquire
  unwinds the members already acquired. The composite is `always_release` if
  any member is. A member also required directly, or shared by two
  composites, raises `CompositeMemberRequiredError`; a member requiring a
  later member is a `ValueError` from `compose`. Member releases (the unwind
  and the composite's release) never resume and survive journal failures.
- `sonata-tasks` resources are converted; `compensated_resource(*, title,
  acquire, compensate, release=None, requires=(), always_release=False,
  revive=None)` builds one whose failed acquire is compensated. The shared
  acquire/release tasks live in the package-private
  `sonata_tasks/_resource_tasks.py`.
- Released as 0.7.0, engine and tasks together.
