import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import rsi_trend  # noqa: E402
from rsi_workload import discover_startup_parameter  # noqa: E402


def test_origin_is_discovered_from_startup_script():
    assert discover_startup_parameter("vllm --num-speculative-tokens 7") == 7
    assert discover_startup_parameter('{"num_speculative_tokens": 9}') == 9


def test_trend_uses_default_origin_when_no_observation_is_available():
    result = rsi_trend.assess_bound([])
    assert result["origin"] == 5
    assert result["next_values"] == [3, 7]


def test_trend_bound_detects_compute_saturation():
    result = rsi_trend.assess_bound([
        {"num_speculative_tokens": 2, "throughput_tokens_s": 100, "compute_utilization": 92},
        {"num_speculative_tokens": 4, "throughput_tokens_s": 100.5, "compute_utilization": 94},
        {"num_speculative_tokens": 8, "throughput_tokens_s": 100.2, "compute_utilization": 95},
    ])
    assert result["bound"] is True
    assert result["reason"] in {"compute_bound_upward", "throughput_plateau"}


def test_trend_bound_keeps_expanding_when_improving():
    result = rsi_trend.assess_bound([
        {"num_speculative_tokens": 2, "throughput_tokens_s": 100},
        {"num_speculative_tokens": 4, "throughput_tokens_s": 115},
        {"num_speculative_tokens": 8, "throughput_tokens_s": 130},
    ])
    assert result["status"] == "expand"
    assert result["next_action"] == "probe_adjacent_by_two"


def test_higher_k_loss_with_high_compute_stops_upward_branch():
    result = rsi_trend.assess_bound([
        {"num_speculative_tokens": 5, "throughput_tokens_s": 105, "pipe_utilization": 80},
        {"num_speculative_tokens": 7, "throughput_tokens_s": 100, "pipe_utilization": 95},
        {"num_speculative_tokens": 9, "throughput_tokens_s": 96, "pipe_utilization": 95},
    ], origin=7)
    assert result["direction_up"] == "stop"
    assert 11 not in result["next_values"]


def test_downward_improvement_reaches_zero_only_at_smallest_positive_k():
    result = rsi_trend.assess_bound([
        {"num_speculative_tokens": 1, "throughput_tokens_s": 120},
        {"num_speculative_tokens": 3, "throughput_tokens_s": 110},
        {"num_speculative_tokens": 7, "throughput_tokens_s": 100},
    ], origin=7)
    assert result["test_zero"] is True
    assert 0 in result["next_values"]
