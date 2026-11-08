"""Small runnable demo of the Sonata workflow engine.

Run with:
    uv run python examples/demo_workflow.py
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import ClassVar

from sonata_engine import (
    Resource,
    Steps,
    Task,
    TaskInputs,
    TaskOutcome,
    Workflow,
    WorkflowEvent,
    bind_workflow_sink,
    workflow_log,
)

IMAGES = ("control-plane", "function-runtime")


class ConsoleSink:
    """The whole sink contract: `emit`, plus a `status` context manager.

    Without one bound, the engine reports nowhere and `subtask` is a no-op.
    """

    _MARKS: ClassVar[dict[str, str]] = {
        "task.started": "->",
        "task.passed": "ok",
        "task.failed": "XX",
    }

    def emit(self, event: WorkflowEvent) -> None:
        """Print one workflow event as a line, indented for nested subtasks.

        Log lines render as a dotted detail line; every other event renders as
        its mark from `_MARKS` followed by the task id.
        """
        if event.kind == "log.line":
            print(f"        . {event.line}")
            return
        indent = "    " if event.parent_task_id else ""
        print(f"  {indent}{self._MARKS.get(event.kind, event.kind)} {event.task_id}")

    @contextmanager
    def status(self, label: str) -> Generator[None, None, None]:
        """Run the wrapped block without rendering any progress display."""
        yield


class PrepareSource(Task[str]):
    """Produce the source tree the image builds consume.

    A stand-in for a real checkout: it reports `/tmp/src` without touching the
    filesystem.
    """

    title = "Prepare source"

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        """Log the preparation step and hand back the source path."""
        workflow_log("preparing source tree...")
        return TaskOutcome(value="/tmp/src")


class BuildImage(Task[tuple[str, ...]]):
    """One image build, reused at every position in the chain.

    The first instance starts the accumulated tuple; later instances extend
    the tuple their upstream step produced.
    """

    def __init__(self, image: str, *, first: bool = False) -> None:
        """Configure a build step for `image`.

        Pass `first=True` for the step that starts the image tuple instead of
        extending one.
        """
        self.title = f"Build {image}"
        self._image = image
        self._first = first

    def run(self, inputs: TaskInputs) -> TaskOutcome[tuple[str, ...]]:
        """Append this image's registry reference to the upstream tuple."""
        workflow_log(f"docker build {self._image}")
        built = () if self._first else inputs.upstream()
        return TaskOutcome(value=(*built, f"registry.example/{self._image}:v1"))


class ScanImages(Task[tuple[str, ...]]):
    """Report how many images the build steps produced.

    The tuple passes through unchanged; only the count is logged.
    """

    title = "Scan for vulnerabilities"

    def run(self, inputs: TaskInputs) -> TaskOutcome[tuple[str, ...]]:
        """Log the number of upstream images and pass the tuple through."""
        built = inputs.upstream()
        workflow_log(f"scanning {len(built)} images")
        return TaskOutcome(value=built)


class StartBuilderVm(Task[str]):
    """Acquire half of the builder VM resource: its value is the VM id."""

    title = "Acquire builder VM"

    def run(self, inputs: TaskInputs) -> TaskOutcome[str]:
        """Log the VM coming up and return its id."""
        workflow_log("[resource] builder VM up")
        return TaskOutcome(value="builder-vm-1")


class StopBuilderVm(Task[None]):
    """Release half: the acquired VM id arrives as the upstream value."""

    title = "Release builder VM"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Log the VM going down."""
        workflow_log(f"[resource] builder VM down ({inputs.upstream()})")
        return TaskOutcome()


class PushImages(Task[None]):
    """Simulate publishing the built images to the registry.

    Declared with the builder VM as a required resource, so the VM is live
    while this step runs.
    """

    title = "Push images"

    def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
        """Log the push and return an empty outcome."""
        workflow_log("pushing images (needs the builder VM)...")
        return TaskOutcome()


def main() -> None:
    builder_vm = Resource(
        title="Acquire builder VM",
        acquire=StartBuilderVm(),
        release=StopBuilderVm(),
    )

    workflow = Workflow(workflow_id="demo-release")
    workflow.add(PrepareSource())
    workflow.add(
        Steps(
            title="Build images",
            steps=(
                *(
                    BuildImage(image, first=index == 0)
                    for index, image in enumerate(IMAGES)
                ),
                ScanImages(),
            ),
        )
    )
    workflow.add(PushImages(), requires=(builder_vm,))

    print("Running workflow (indented lines are steps):")
    with bind_workflow_sink(ConsoleSink()):
        result = workflow.run()

    print("\nCompiled task IDs and outcomes:")
    for execution in result.tasks:
        # `outcome` is None exactly when the unit was skipped, so it is not
        # interchangeable with an outcome whose value happens to be None.
        outcome = execution.outcome if execution.outcome else "(skipped)"
        print(f"  {execution.task_id:<25} {execution.status:<8} {outcome}")

    print(
        f"\n{len(IMAGES)} images built as {len(IMAGES) + 1} reported steps, "
        "inside 1 compiled unit."
    )


if __name__ == "__main__":
    main()
