from collections.abc import Mapping
from typing import Any

import pytest

from sonata_tasks import k6, metrics


@pytest.mark.parametrize("nested", [False, True])
def test_k6_readers_accept_both_exports_without_changing_values(nested: bool) -> None:
    values = {"count": 0, "rate": 0.25}
    exports = {"http_req_failed": {"values": values} if nested else values}
    read = k6.k6_values(exports, "http_req_failed")
    assert read is values
    assert k6.k6_value(read, "http_req_failed", "rate", "value") == 0.25
    assert k6.k6_value(read, "requests", "count") == 0
    assert k6.k6_values({}, "absent") == {}


@pytest.mark.parametrize("entry", [None, [], {"values": None}, {"values": []}])
def test_k6_readers_reject_malformed_metric_shapes(entry: object) -> None:
    with pytest.raises(ValueError, match=r"k6.requests must be a mapping"):
        k6.k6_values({"requests": entry}, "requests")


@pytest.mark.parametrize(
    "value", [None, True, "0.25", -1, float("nan"), float("inf"), 10**400]
)
def test_k6_invalid_first_alias_never_falls_back_to_a_valid_alias(
    value: object,
) -> None:
    with pytest.raises(
        ValueError, match=r"invalid required k6 metric http_req_failed.rate"
    ):
        k6.k6_value({"rate": value, "value": 0}, "http_req_failed", "rate", "value")


@pytest.mark.parametrize("name", ["checks", "http_req_failed"])
@pytest.mark.parametrize("key", ["rate", "value"])
def test_k6_failure_and_check_rates_are_bounded(name: str, key: str) -> None:
    with pytest.raises(ValueError, match="between 0 and 1"):
        k6.k6_value({key: 1.1}, name, key)
    assert k6.k6_value({key: 1}, name, key) == 1


def test_k6_absence_zero_and_ordinary_rates_remain_distinct() -> None:
    with pytest.raises(
        ValueError, match="missing required k6 metric requests: count/total"
    ):
        k6.k6_value({}, "requests", "count", "total")
    assert k6.k6_value({"rate": 25}, "http_reqs", "rate") == 25
    assert k6.k6_value({"count": 0}, "requests", "count") == 0


@pytest.mark.parametrize("value", [True, None, [], float("nan"), "Inf", 10**400])
def test_numeric_observations_require_finite_non_boolean_numbers(value: object) -> None:
    with pytest.raises(ValueError, match="finite"):
        metrics.finite_number(value)


def test_prometheus_numeric_strings_and_negative_gauges_are_valid() -> None:
    assert metrics.finite_number("-3.5") == -3.5
    with pytest.raises(ValueError, match="nonnegative"):
        metrics.finite_number(-1, nonnegative=True)


def test_counter_resets_are_detected_per_publisher_before_aggregation() -> None:
    points = [
        {"labels": {"worker": "b", "job": "ordinary"}, "timestamp": 2, "value": 210},
        {"labels": {"job": "ordinary", "worker": "a"}, "timestamp": 1, "value": 100},
        {"labels": {"worker": "b", "job": "ordinary"}, "timestamp": 1, "value": 100},
        {"labels": {"worker": "a", "job": "ordinary"}, "timestamp": 2, "value": 5},
    ]
    before = [dict(point) for point in points]
    assert metrics.counter_delta(points) == 115
    assert points == before


def test_counter_without_timestamps_uses_input_order_and_counts_multiple_resets() -> (
    None
):
    assert (
        metrics.counter_delta([{"value": value} for value in [10, 15, 2, 4, 0, 3]])
        == 12
    )
    assert (
        metrics.counter_delta([{"timestamp": 3, "value": "10"}, {"value": "12"}]) == 2
    )


def test_singleton_publishers_are_unavailable_and_constant_counters_are_zero() -> None:
    assert metrics.counter_delta([]) is None
    assert metrics.counter_delta([{"value": 0}]) is None
    assert metrics.counter_delta([{"value": 5}, {"value": 5}]) == 0
    assert (
        metrics.counter_delta(
            [
                {"labels": {"worker": "a"}, "value": 1},
                {"labels": {"worker": "a"}, "value": 2},
                {"labels": {"worker": "b"}, "value": 3},
            ]
        )
        is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"value": True},
        {"value": -1},
        {"value": "NaN"},
        {},
        {"value": 2, "labels": None},
        {"value": 2, "labels": {"worker": []}},
        {"value": 2, "timestamp": "later"},
    ],
)
def test_invalid_counter_evidence_is_unavailable(bad: Mapping[str, Any]) -> None:
    assert metrics.counter_delta([{"value": 1, "timestamp": 0}, bad]) is None


def test_gauges_sum_publishers_at_shared_timestamps_and_follow_time_order() -> None:
    result = metrics.point_stats(
        [
            {"timestamp": 2, "value": -2},
            {"timestamp": 1, "value": "6"},
            {"timestamp": 2, "value": "3"},
            {"timestamp": 1, "value": 2},
        ]
    )
    assert result == {
        "points": 2,
        "first": 8,
        "last": 1,
        "min": 1,
        "max": 8,
        "delta": -7,
    }
    assert metrics.point_stats([]) == {"points": 0}


@pytest.mark.parametrize("value", [True, None, "NaN", float("inf"), -1])
def test_invalid_counter_samples_never_become_summary_statistics(value: object) -> None:
    assert metrics.point_stats([{"value": 3}, {"value": value}], counter=True) == {
        "points": 1,
        "invalid_points": 1,
    }


def test_counter_point_statistics_keep_merged_view_but_compute_independent_resets() -> (
    None
):
    points = [
        {"labels": {"worker": "a"}, "timestamp": 1, "value": 100},
        {"labels": {"worker": "b"}, "timestamp": 1, "value": 100},
        {"labels": {"worker": "a"}, "timestamp": 2, "value": 5},
        {"labels": {"worker": "b"}, "timestamp": 2, "value": 210},
    ]
    assert metrics.point_stats(points, counter=True) == {
        "points": 2,
        "first": 200,
        "last": 215,
        "min": 200,
        "max": 215,
        "delta": 115,
    }


def test_overflow_retains_unavailable_evidence_without_nonfinite_statistics() -> None:
    assert (
        metrics.counter_delta([{"value": value} for value in [0, 1e308, 0, 1e308]])
        is None
    )
    assert metrics.point_stats([{"timestamp": 1, "value": 1e308}] * 2) == {
        "points": 1,
        "invalid_points": 1,
    }
    assert metrics.point_stats([{"value": -1e308}, {"value": 1e308}]) == {
        "points": 2,
        "first": -1e308,
        "last": 1e308,
        "min": -1e308,
        "max": 1e308,
    }


@pytest.mark.parametrize(
    "points",
    [
        [{"value": 1, "timestamp": []}],
        [{"value": 1, "timestamp": 0}, {"value": 2, "timestamp": "later"}],
    ],
)
def test_malformed_or_unorderable_point_stats_remain_unavailable(
    points: list[dict[str, Any]],
) -> None:
    result = metrics.point_stats(points)
    assert "first" not in result and "delta" not in result
    assert result["invalid_points"] >= 1
