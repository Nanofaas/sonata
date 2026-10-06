from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import pytest

from sonata_engine import Resource, Task, TaskInputs, TaskOutcome, Workflow
from sonata_tasks.command import CommandTask
from sonata_tasks.compose import DockerComposeProject, docker_compose_resource
from sonata_tasks.execution.models import CommandOptions
from sonata_tasks.tasks.models import CommandTaskSpec, TaskResult


@dataclass
class RecordingExecutor:
    def binding_key(self, role: str) -> str:
        return f"test-recording:{role}"

    seen: list[CommandTaskSpec] = field(default_factory=list)

    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        return TaskResult(task_id="", status="passed", return_code=0)


@dataclass
class FailingReadinessExecutor(RecordingExecutor):
    @override
    def binding_key(self, role: str) -> str:
        return f"test:{role}"

    @override
    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        if task.argv[0] == "curl":
            return TaskResult(
                task_id="",
                status="failed",
                return_code=7,
                stderr="not ready",
            )
        return TaskResult(task_id="", status="passed", return_code=0)


def test_compose_resource_builds_deploys_and_tears_down_the_project() -> None:
    executor = RecordingExecutor()
    project = DockerComposeProject(
        name="example",
        file=Path("deploy/compose/compose.yaml"),
        ready_url="http://127.0.0.1:8081/actuator/health/readiness",
    )
    resource = docker_compose_resource(
        project,
        executor=executor,
        options=CommandOptions(cwd=Path("/workspace")),
    )
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(resource,),
    )

    workflow.run()

    down = (
        "docker",
        "compose",
        "-f",
        "deploy/compose/compose.yaml",
        "-p",
        "example",
        "down",
    )
    assert [task.argv for task in executor.seen] == [
        (
            "docker",
            "compose",
            "-f",
            "deploy/compose/compose.yaml",
            "-p",
            "example",
            "up",
            "-d",
            "--build",
            "--wait",
        ),
        (
            "curl",
            "-fsS",
            "--retry",
            "60",
            "--retry-delay",
            "1",
            "--retry-connrefused",
            "--retry-all-errors",
            "http://127.0.0.1:8081/actuator/health/readiness",
        ),
        ("true",),
        down,
    ]
    assert executor.seen[0].options.cwd == Path("/workspace")
    assert executor.seen[-1].options.cwd == Path("/workspace")


def test_compose_resource_passes_project_environment_to_compose() -> None:
    executor = RecordingExecutor()
    project = DockerComposeProject(
        name="example-env",
        file=Path("compose.yaml"),
        ready_url="http://127.0.0.1:8081/actuator/health/readiness",
    )
    options = CommandOptions(env={"APP_MODE": "test"})
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(
            docker_compose_resource(project, executor=executor, options=options),
        ),
    )

    workflow.run()

    assert executor.seen[0].options.env == {"APP_MODE": "test"}


def test_a_prebuilt_image_is_deployed_without_rebuilding_it() -> None:
    """Deploy the declared image instead of rebuilding it with `up --build`.

    The compose service declares both `image:` and `build:`, so `up --build`
    rebuilds from the Dockerfile and tags the result with the image name. For a
    natively-compiled control plane that silently substitutes a JVM build and
    then reports its memory as the native figure.
    """
    executor = RecordingExecutor()
    project = DockerComposeProject(
        name="example",
        file=Path("compose.yaml"),
        ready_url="http://127.0.0.1:8081/actuator/health/readiness",
        build=False,
    )
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(docker_compose_resource(project, executor=executor),),
    )

    workflow.run()

    up = next(task.argv for task in executor.seen if "up" in task.argv)
    assert "--build" not in up
    assert "--wait" in up


def test_compose_resource_is_named_as_one_lifecycle_in_the_plan() -> None:
    executor = RecordingExecutor()
    resource = docker_compose_resource(
        DockerComposeProject(
            name="example",
            file=Path("compose.yaml"),
            ready_url="http://127.0.0.1:8081/actuator/health/readiness",
        ),
        executor=executor,
    )
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(resource,),
    )

    assert [task.task_id for task in workflow.compile().tasks] == [
        "001.acquire-docker-compose-project-example",
        "002.use-deployment",
        "003.release-docker-compose-project-example",
    ]


def test_compose_resource_tears_down_when_readiness_fails() -> None:
    executor = FailingReadinessExecutor()
    resource = docker_compose_resource(
        DockerComposeProject(
            name="example-validate",
            file=Path("compose.yaml"),
            ready_url="http://127.0.0.1:8081/actuator/health/readiness",
        ),
        executor=executor,
    )
    workflow = Workflow("compose")
    workflow.add(
        CommandTask(title="Use deployment", argv=("true",), executor=executor),
        requires=(resource,),
    )

    with pytest.raises(RuntimeError, match="not ready"):
        workflow.run()

    assert executor.seen[-1].argv == (
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-p",
        "example-validate",
        "down",
    )


@dataclass(frozen=True, slots=True)
class TaggedProject(DockerComposeProject):
    tag: str = "consumer-value"


def test_opt_in_pre_clean_preserves_subclass_value_dependencies_and_options() -> None:
    executor = RecordingExecutor()
    project = TaggedProject(
        "isolated", Path("compose.yaml"), "http://ready", build=False
    )
    events: list[str] = []
    dependency = Resource(
        title="Prepare",
        acquire=lambda _inputs: events.append("prepare"),
        release=lambda _inputs, _value: events.append("release"),
    )
    resource: Resource[TaggedProject] = docker_compose_resource(
        project,
        executor=executor,
        pre_clean=True,
        role="builder",
        options=CommandOptions(cwd=Path("/app"), env={"MODE": "isolated"}),
        remove_volumes=True,
        remove_orphans=True,
        requires=(dependency,),
    )

    class UseProject(Task[None]):
        title = "Use"

        @override
        def run(self, inputs: TaskInputs) -> TaskOutcome[None]:
            assert inputs.resource(resource) is project
            assert inputs.resource(resource).tag == "consumer-value"
            events.append("use")
            return TaskOutcome(value=None)

    workflow = Workflow("isolated")
    workflow.add(UseProject(), requires=(resource,))
    workflow.run()

    assert events == ["prepare", "use", "release"]
    assert [
        "curl" if task.argv[0] == "curl" else task.argv[6] for task in executor.seen
    ] == ["down", "up", "curl", "down"]
    assert executor.seen[0].argv == executor.seen[-1].argv
    assert executor.seen[0].argv[-2:] == ("--volumes", "--remove-orphans")
    assert executor.seen[0].summary == "Clear any previous isolated state"
    assert "--build" not in executor.seen[1].argv
    assert all(
        task.role == "builder"
        and task.options.cwd == Path("/app")
        and task.options.env == {"MODE": "isolated"}
        for task in executor.seen
    )


@dataclass
class FailingPhaseExecutor(RecordingExecutor):
    phase: str = "up"
    fail_cleanup: bool = False

    @override
    def run(self, task: CommandTaskSpec, *, dry_run: bool = False) -> TaskResult:
        self.seen.append(task)
        phase = "curl" if task.argv[0] == "curl" else task.argv[6]
        if phase == self.phase or (
            self.fail_cleanup and phase == "down" and len(self.seen) > 1
        ):
            return TaskResult(
                task_id=task.task_id,
                status="failed",
                return_code=1,
                stderr=f"failed {phase}",
            )
        return TaskResult(task_id=task.task_id, status="passed", return_code=0)


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("down", ["down", "down"]),
        ("up", ["down", "up", "down"]),
        ("curl", ["down", "up", "curl", "down"]),
    ],
)
@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_pre_clean_compensates_each_acquisition_failure_preserving_primary_error(
    phase: str, expected: list[str], fail_cleanup: bool
) -> None:
    executor = FailingPhaseExecutor(phase=phase, fail_cleanup=fail_cleanup)
    resource = docker_compose_resource(
        DockerComposeProject("isolated", Path("compose.yaml"), "http://ready"),
        executor=executor,
        pre_clean=True,
    )
    workflow = Workflow("failure")
    workflow.add(
        CommandTask(title="Use", argv=("true",), executor=executor),
        requires=(resource,),
    )
    with pytest.raises(RuntimeError, match=f"failed {phase}") as failure:
        workflow.run()
    assert [
        "curl" if task.argv[0] == "curl" else task.argv[6] for task in executor.seen
    ] == expected
    if fail_cleanup or phase == "down":
        assert any("failed down" in note for note in failure.value.__notes__)
