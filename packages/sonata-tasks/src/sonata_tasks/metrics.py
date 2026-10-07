"""Interpret numeric observations and check Prometheus scrape output."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from itertools import pairwise
from typing import Any

from sonata_engine import TaskInputs
from sonata_tasks.command import CommandTask
from sonata_tasks.core.fingerprint import semantic_key as build_semantic_key
from sonata_tasks.execution.models import CommandOptions, TaskResult
from sonata_tasks.execution.ports import CommandTaskExecutor

MetricsEndpoint = str | Callable[[TaskInputs], str]
_METRIC_NAME = r"[A-Za-z_:][A-Za-z0-9_:]*"
_SAMPLE = re.compile(
    rf"^({_METRIC_NAME})(?:\{{([^}}]*)\}})?\s+([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)"
)
_LABEL = re.compile(r'([A-Za-z_]\w*)="((?:[^"\\]|\\.)*)"')


def metric_sum(scrape: str, name: str, labels: Mapping[str, str]) -> tuple[int, float]:
    """Sum the samples of one metric in a Prometheus text scrape.

    Only lines for metric ``name`` whose labels include every entry in
    ``labels`` are counted. Returns the number of matching samples and their
    total, which is ``(0, 0.0)`` when nothing matches.
    """
    matches, total = 0, 0.0
    for line in scrape.splitlines():
        match = _SAMPLE.match(line)
        if match is None or match.group(1) != name:
            continue
        actual = {
            key: value.replace(r"\"", '"').replace(r"\\", "\\")
            for key, value in _LABEL.findall(match.group(2) or "")
        }
        if all(actual.get(key) == value for key, value in labels.items()):
            matches += 1
            total += float(match.group(3))
    return matches, total


def _resolve(endpoint: MetricsEndpoint, inputs: TaskInputs) -> str:
    return endpoint(inputs) if callable(endpoint) else endpoint


def _require_metric_sum(
    url: object,
    scrape: str,
    names: tuple[str, ...],
    labels: Mapping[str, str],
    minimum: float,
) -> None:
    for name in names:
        matches, total = metric_sum(scrape, name, labels)
        if matches:
            if total < minimum:
                raise RuntimeError(
                    f"{url}: {name}{dict(labels)} sum was {total}, "
                    f"expected >= {minimum}"
                )
            return
    raise RuntimeError(f"{url}: none of {names} appeared with labels {dict(labels)}")


def _check_scrape(
    result: TaskResult,
    *,
    url: object,
    expect: tuple[str, ...],
    reject: tuple[str, ...],
) -> None:
    for sample in expect:
        if sample not in result.stdout:
            raise RuntimeError(f"{url}: expected sample not scraped: {sample}")
    for sample in reject:
        if sample in result.stdout:
            raise RuntimeError(f"{url}: sample present and should not be: {sample}")


def _check_minimums(
    result: TaskResult,
    *,
    url: object,
    minimums: tuple[tuple[str, Mapping[str, str], float], ...],
    any_minimums: tuple[tuple[tuple[str, ...], Mapping[str, str], float], ...],
) -> None:
    for name, labels, minimum in minimums:
        _require_metric_sum(url, result.stdout, (name,), labels, minimum)
    for names, labels, minimum in any_minimums:
        _require_metric_sum(url, result.stdout, names, labels, minimum)


class PrometheusScrapeCheckTask(CommandTask):
    """Scrape a metrics endpoint and assert which samples it must contain."""

    def __init__(
        self,
        *,
        url: MetricsEndpoint,
        executor: CommandTaskExecutor,
        role: str = "host",
        expect: tuple[str, ...] = (),
        reject: tuple[str, ...] = (),
        options: CommandOptions | None = None,
        title: str | None = None,
        semantic_key: str | None = None,
    ) -> None:
        """Configure the scrape and the substring checks over its output.

        ``expect`` entries must each appear in the scraped text and ``reject``
        entries must not; the first one that violates this fails the task.
        ``url`` may be a callable resolving the endpoint at run time, which is
        why such a call also needs its own ``semantic_key``. ``title`` defaults
        to one naming the endpoint.

        Raises:
            ValueError: If both ``expect`` and ``reject`` are empty, or if
                ``url`` is callable and no ``semantic_key`` was supplied.

        """
        if not expect and not reject:
            raise ValueError("a scrape check must expect or reject at least one sample")
        if callable(url) and not semantic_key:
            raise ValueError("semantic_key is required for a dynamic metrics endpoint")

        argv = (
            (lambda inputs: ("curl", "-fsS", _resolve(url, inputs)))
            if callable(url)
            else ("curl", "-fsS", url)
        )
        key = build_semantic_key(
            "metrics-scrape:v2",
            {"endpoint": semantic_key or url, "expect": expect, "reject": reject},
        )
        super().__init__(
            title=title or f"Check the metrics at {url}",
            argv=argv,
            executor=executor,
            role=role,
            options=options,
            verify=partial(_check_scrape, url=url, expect=expect, reject=reject),
            semantic_key=key,
        )


class PrometheusMinimumCheckTask(CommandTask):
    """Scrape a metrics endpoint and assert its metric sums reach a floor."""

    def __init__(
        self,
        *,
        url: MetricsEndpoint,
        minimums: tuple[tuple[str, Mapping[str, str], float], ...],
        any_minimums: tuple[tuple[tuple[str, ...], Mapping[str, str], float], ...] = (),
        executor: CommandTaskExecutor,
        role: str = "host",
        options: CommandOptions | None = None,
        title: str | None = None,
        semantic_key: str | None = None,
    ) -> None:
        """Configure the scrape and the value floors to check.

        Each ``minimums`` entry is a single metric name, its required labels,
        and the floor its sum must reach. Each ``any_minimums`` entry is instead
        a tuple of alternative names, of which the first that appears with those
        labels must reach the floor. ``url`` may be a callable resolving the
        endpoint at run time, which is why such a call also needs its own
        ``semantic_key``. ``title`` defaults to a generic description.

        Raises:
            ValueError: If no expectations were given, or if ``url`` is callable
                and no ``semantic_key`` was supplied.

        """
        if not minimums and not any_minimums:
            raise ValueError("a metric minimum check needs at least one expectation")
        if callable(url) and not semantic_key:
            raise ValueError("semantic_key is required for a dynamic metrics endpoint")

        argv = (
            (lambda inputs: ("curl", "-fsS", _resolve(url, inputs)))
            if callable(url)
            else ("curl", "-fsS", url)
        )
        key = build_semantic_key(
            "metrics-minimum:v2",
            {
                "endpoint": semantic_key or url,
                "minimums": minimums,
                "any_minimums": any_minimums,
            },
        )
        super().__init__(
            title=title or "Check metric values",
            argv=argv,
            executor=executor,
            role=role,
            options=options,
            verify=partial(
                _check_minimums,
                url=url,
                minimums=minimums,
                any_minimums=any_minimums,
            ),
            semantic_key=key,
        )


def finite_number(value: object, *, nonnegative: bool = False) -> float:
    """Read a finite int, float or numeric string, excluding booleans.

    Raises:
        ValueError: If the value is not finite/numeric, or is negative when
            ``nonnegative`` is required.

    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("measurement must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError("measurement must be a finite number") from error
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise ValueError("measurement must be a finite nonnegative number")
    return number


def counter_delta(points: Sequence[Mapping[str, Any]]) -> float | None:
    """Sum counter increments and resets independently for each publisher.

    Full ``labels`` identify a publisher. Each needs at least two samples;
    timestamped samples sort chronologically when every sample in that
    publisher has a timestamp, otherwise input order is used. The first
    sample establishes the baseline; a decrease contributes the new value.
    Empty, malformed, negative/nonfinite or overflowing evidence is
    unavailable (``None``), distinct from a valid zero increment.
    """
    if not points:
        return None
    series: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    try:
        for point in points:
            _ = finite_number(point.get("value"), nonnegative=True)
            key = tuple(sorted(point.get("labels", {}).items()))
            series.setdefault(key, []).append(point)
        total = 0.0
        for samples in series.values():
            if len(samples) < 2:
                return None
            if all("timestamp" in point for point in samples):
                samples = sorted(samples, key=lambda point: point["timestamp"])
            values = [finite_number(point["value"]) for point in samples]
            for previous, current in pairwise(values):
                total += current - previous if current >= previous else current
    except (AttributeError, TypeError, ValueError):
        return None
    return total if math.isfinite(total) else None


def point_stats(
    points: Sequence[Mapping[str, Any]], *, counter: bool = False
) -> dict[str, float | int]:
    """Summarize points without turning unavailable evidence into zero.

    Samples at equal timestamps are summed. Fully timestamped inputs sort
    by their keys, which must be consistently comparable; other inputs use
    insertion order and string-index keys for missing timestamps.
    Gauges can be negative/decrease. Counter deltas use publisher labels
    before aggregation. Invalid values/sums/keys retain counts but omit
    statistics; an overflowing delta alone is omitted. Dates and time units
    are not parsed or normalized.
    """
    merged: dict[Any, float] = {}
    invalid = 0
    for index, point in enumerate(points):
        try:
            value = finite_number(point.get("value"), nonnegative=counter)
            timestamp = point.get("timestamp", str(index))
            merged[timestamp] = finite_number(merged.get(timestamp, 0.0) + value)
        except (AttributeError, TypeError, ValueError):
            invalid += 1
    if invalid:
        return {"points": len(merged), "invalid_points": invalid}
    try:
        values = (
            [merged[key] for key in sorted(merged)]
            if all("timestamp" in point for point in points)
            else list(merged.values())
        )
    except TypeError:
        return {"points": len(merged), "invalid_points": len(points)}
    if not values:
        return {"points": 0}
    result: dict[str, float | int] = {
        "points": len(values),
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
    }
    delta = counter_delta(points) if counter else values[-1] - values[0]
    if delta is not None and math.isfinite(delta):
        result["delta"] = delta
    return result
