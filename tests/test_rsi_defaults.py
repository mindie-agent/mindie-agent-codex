import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import rsi_defaults  # noqa: E402


def test_default_profile_and_scope_are_explicit():
    defaults = rsi_defaults.default_settings({})
    assert defaults["profile_binding"]["tool"] == "msprof"
    assert defaults["optimization_scope"]["parameters"]["num_speculative_tokens"] == "adaptive"
    assert defaults["optimization_scope"]["strategy"] == "trend_bound"


def test_defaults_do_not_produce_questions():
    assert rsi_defaults.questions({}) == []


def test_default_settings_fill_policy_values():
    defaults = rsi_defaults.default_settings({})
    assert defaults["performance_policy"] == "default"
    assert defaults["accuracy_policy"] == "default"
    assert defaults["runtime_parameter_policy"] == "discover_from_startup"
    assert defaults["startup_feature_policy"] == "allow_existing_features"
    assert defaults["source_change_policy"] == "parameters_only"
    assert defaults["algorithm_exploration_policy"] == "disabled"
    assert defaults["algorithm_exploration_authorized"] is False
    assert defaults["candidate_order"] == ["runtime_parameters", "startup_features"]


def test_speculative_request_asks_for_method_and_weights():
    items = rsi_defaults.questions({"optimization_request": "优化投机解码"})
    fields = {item["field"] for item in items}
    assert {"speculative_method", "speculative_weights"} <= fields


def test_selected_method_and_weights_remove_followup_questions():
    items = rsi_defaults.questions({
        "optimization_request": "优化 dflash",
        "speculative_method": "dflash",
        "speculative_weights": "/models/dflash",
    })
    fields = {item["field"] for item in items}
    assert "speculative_method" not in fields
    assert "speculative_weights" not in fields
