"""Run k6 load tests and interpret their exported summary measurements."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, override

from sonata_engine import Task, TaskInputs, TaskOutcome
from sonata_tasks.core.fingerprint import fingerprint_digest
from sonata_tasks.execution.models import CommandOptions, CommandTaskSpec
from sonata_tasks.execution.ports import CommandTaskExecutor
from sonata_tasks.k6_models import K6Config, K6RunResult
from sonata_tasks.metrics import finite_number

K6ConfigSource = K6Config | Callable[[TaskInputs], K6Config]


def k6_argv(config: K6Config) -> tuple[str, ...]:
    """Build the ``k6 run`` argv for ``config``.

    A summary is always exported to ``config.summary_output_path`` with the
    usual trend stats. ``vus``/``duration`` add the constant-load flags, or
    ``stages`` add one ``--stage duration:target`` per stage when neither is
    set; ``env`` adds ``-e key=value`` pairs.
    """
    argv = [
        "k6",
        "run",
        "--summary-export",
        str(config.summary_output_path),
        "--summary-trend-stats",
        "avg,min,med,max,p(50),p(90),p(95),p(99)",
    ]
    if config.vus is not None:
        argv.extend(("--vus", str(config.vus)))
    if config.duration is not None:
        argv.extend(("--duration", config.duration))
    if config.vus is None and config.duration is None:
        for stage in config.stages:
            argv.extend(("--stage", f"{stage.duration}:{stage.target}"))
    for key, value in config.env.items():
        argv.extend(("-e", f"{key}={value}"))
    argv.append(str(config.script_path))
    return tuple(argv)


class K6Task(Task[K6RunResult]):
    """Run k6 through an executor and translate its exit code to a result.

    k6 exits 0 when all thresholds pass and 99 when they fail. Either is a
    completed run and yields a :class:`K6RunResult` whose ``passed`` reflects
    the code; any other code raises ``RuntimeError``. Set ``require_pass`` to
    turn a threshold failure into an error as well.
    """

    def __init__(
        self,
        config: K6ConfigSource,
        *,
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
        title: str = "Run k6",
        semantic_key: str | None = None,
        require_pass: bool = False,
    ) -> None:
        """Configure the run.

        ``config`` is a fixed :class:`K6Config` or a callable deriving one from
        the task inputs; because a callable cannot be fingerprinted from its
        result, the callable form requires its own ``semantic_key``. This task
        owns the expected exit codes, so ``options`` must not narrow them.

        Raises:
            ValueError: If ``config`` is callable without a ``semantic_key``, or
                if ``options`` sets expected exit codes other than ``{0}``.

        """
        if callable(config) and not semantic_key:
            raise ValueError("semantic_key is required for a dynamic k6 config")
        current = options or CommandOptions()
        if current.expected_exit_codes != frozenset({0}):
            raise ValueError("K6Task owns expected exit codes {0, 99}")
        self.title = title
        self._config = config
        self._executor = executor
        self._role = role
        self._options = replace(current, expected_exit_codes=frozenset({0, 99}))
        self._require_pass = require_pass
        self._semantic_key = semantic_key
        self._binding_key = executor.binding_key(role)

    @override
    def run(self, inputs: TaskInputs) -> TaskOutcome[K6RunResult]:
        config = self._config(inputs) if callable(self._config) else self._config
        started_at = datetime.now(UTC)
        result = self._executor.run(
            CommandTaskSpec(
                task_id="",
                summary=self.title,
                argv=k6_argv(config),
                role=self._role,
                options=self._options,
            )
        )
        ended_at = datetime.now(UTC)
        if result.return_code not in (0, 99):
            detail = result.stderr.strip() or result.stdout.strip() or "no output"
            raise RuntimeError(
                f"{self.title} failed (exit {result.return_code}): {detail}"
            )
        if self._require_pass and result.return_code == 99:
            raise RuntimeError(f"{self.title} failed: k6 thresholds failed")
        return TaskOutcome(
            value=K6RunResult(
                config.summary_output_path,
                started_at,
                ended_at,
                result.return_code == 0,
            )
        )

    @override
    def _fingerprint_payload(self) -> object:
        config: object
        if callable(self._config):
            config = {"dynamic": self._semantic_key}
        else:
            config = {
                "script_path": self._config.script_path,
                "summary_output_path": self._config.summary_output_path,
                "stages": tuple(
                    (stage.duration, stage.target) for stage in self._config.stages
                ),
                "env": self._config.env,
                "vus": self._config.vus,
                "duration": self._config.duration,
            }
        return {
            "schema": 1,
            "digest": fingerprint_digest(
                {
                    "config": config,
                    "role": self._role,
                    "options": {
                        "cwd": self._options.cwd,
                        "env": self._options.env,
                        "remote_dir": self._options.remote_dir,
                        "expected_exit_codes": self._options.expected_exit_codes,
                        "timeout_seconds": self._options.timeout_seconds,
                    },
                    "require_pass": self._require_pass,
                    "binding_key": self._binding_key,
                }
            ),
        }


def k6_values(metrics: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """Read summary-export's flat values or handleSummary's nested values."""
    entry = metrics.get(name, {})
    if not isinstance(entry, Mapping):
        raise ValueError(f"k6.{name} must be a mapping")
    values = entry.get("values", entry)
    if not isinstance(values, Mapping):
        raise ValueError(f"k6.{name} must be a mapping")
    return values


def k6_value(values: Mapping[str, Any], name: str, *keys: str) -> float:
    """Require a finite nonnegative k6 value; first present alias wins."""
    for key in keys:
        if key not in values:
            continue
        value = values[key]
        try:
            # k6 writes JSON numbers. Numeric strings indicate malformed evidence.
            if not isinstance(value, (int, float)):
                raise ValueError("k6 measurement must be numeric")
            number = finite_number(value, nonnegative=True)
            if (
                name in {"http_req_failed", "checks"}
                and key in {"rate", "value"}
                and number > 1
            ):
                raise ValueError("rate must be between 0 and 1")
            return number
        except ValueError as error:
            raise ValueError(
                f"invalid required k6 metric {name}.{key}: {error}"
            ) from error
    raise ValueError(f"missing required k6 metric {name}: {'/'.join(keys)}")
