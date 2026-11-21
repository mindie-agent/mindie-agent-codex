import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import rsi_plan  # noqa: E402


def base_plan():
    return {
        "schema": rsi_plan.SCHEMA,
        "task_id": "decode-latency",
        "endpoint": {"host": "<remote-host>", "port": 22, "user": "root"},
        "source_root": "/home/user/model",
        "rsi_root": "/home/user/RSI",
        "startup_script": "/home/user/model/start.sh",
        "test_script": "/home/user/model/benchmark.sh",
        "baseline": "baseline-control",
        "objective": "maximize throughput",
        "operations": ["doctor", "profile", "accuracy", "performance"],
        "parallel_workers": 2,
        "devices": ["0", "1"],
        "payload": {},
    }


def test_validate_normalizes_defaults_and_render_requires_lease():
    plan = rsi_plan.validate(base_plan())
    assert plan["endpoint"]["user"] == "root"
    rendered = rsi_plan.render(plan)
    assert rendered["steps"][0]["tool"] == "remote_write"
    assert json.loads(rendered["steps"][0]["arguments"]["content"])["source_root"] == "/home/user/model"
    rendered_payload = json.loads(rendered["steps"][0]["arguments"]["content"])
    assert rendered_payload["startup_script"] == "/home/user/model/start.sh"
    assert rendered_payload["test_script"] == "/home/user/model/benchmark.sh"
    assert rendered["steps"][2]["requires"] == ["coordinator.lease"]
    assert rendered_payload["defaults"]["profile_binding"]["tool"] == "msprof"
    assert rendered_payload["defaults"]["optimization_scope"]["parameters"]["num_speculative_tokens"] == "adaptive"
    assert rendered_payload["defaults"]["optimization_scope"]["strategy"] == "trend_bound"
    assert rendered_payload["defaults"]["candidate_order"] == [
        "runtime_parameters", "startup_features"
    ]
    assert rendered_payload["algorithm_exploration_authorized"] is False


@pytest.mark.parametrize("field", ["source_root", "rsi_root"])
def test_remote_paths_cannot_escape_root(field):
    plan = base_plan()
    plan[field] = "/home/user/../secret"
    with pytest.raises(rsi_plan.PlanError, match="absolute remote path"):
        rsi_plan.validate(plan)


def test_shell_payload_is_rejected():
    plan = base_plan()
    plan["payload"] = {"command": "rm -rf /"}
    with pytest.raises(rsi_plan.PlanError, match="shell fields"):
        rsi_plan.validate(plan)


def test_startup_and_test_inputs_are_required():
    plan = base_plan()
    del plan["startup_script"]
    del plan["test_script"]
    with pytest.raises(rsi_plan.MissingInputError) as exc:
        rsi_plan.validate(plan)
    assert exc.value.fields == ("startup_script", "test_script or test_config or test_description or test_command")


def test_test_config_can_replace_test_script():
    plan = base_plan()
    del plan["test_script"]
    plan["test_config"] = "/home/user/model/benchmark.json"
    normalized = rsi_plan.validate(plan)
    assert normalized["test_config"] == "/home/user/model/benchmark.json"
    assert normalized["test_script"] is None


def test_test_description_can_replace_file_inputs():
    plan = base_plan()
    del plan["test_script"]
    plan["test_description"] = "Run vLLM serving benchmark with batch size 8 and 128 input tokens"
    normalized = rsi_plan.validate(plan)
    assert normalized["test_description"].startswith("Run vLLM")
    assert normalized["test_command"] is None


def test_test_command_is_direct_argv_and_renders():
    plan = base_plan()
    del plan["test_script"]
    plan["test_command"] = ["vllm", "bench", "serve", "--model", "/models/foo",
                            "--num-prompts", "128"]
    normalized = rsi_plan.validate(plan)
    assert normalized["test_command"][0:2] == ["vllm", "bench"]
    rendered = rsi_plan.render(normalized)
    payload = json.loads(rendered["steps"][0]["arguments"]["content"])
    assert payload["test_command"][-1] == "128"


def test_test_command_accepts_shell_free_string_and_rejects_shell_syntax():
    plan = base_plan()
    del plan["test_script"]
    plan["test_command"] = "vllm bench serve --model /models/foo"
    normalized = rsi_plan.validate(plan)
    assert normalized["test_command"][:2] == ["vllm", "bench"]
    plan["test_command"] += "; rm -rf /"
    with pytest.raises(rsi_plan.PlanError, match="shell-free"):
        rsi_plan.validate(plan)


def test_cli_render_is_machine_readable(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(base_plan()), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPTS / "rsi_plan.py"), "render", str(path)],
                            check=True, capture_output=True, text=True)
    body = json.loads(result.stdout)
    assert body["status"] == "valid"
    assert body["plan"]["steps"][1]["tool"] == "remote_exec_command"


def test_cli_questions_returns_defaults_and_followups(tmp_path):
    path = tmp_path / "plan.json"
    value = base_plan()
    value["payload"] = {"optimization_request": "优化投机解码"}
    path.write_text(json.dumps(value), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPTS / "rsi_plan.py"), "questions", str(path)],
                            check=True, capture_output=True, text=True)
    body = json.loads(result.stdout)
    assert body["status"] == "needs_input"
    fields = {item["field"] for item in body["questions"]}
    assert fields == {"speculative_method", "speculative_weights"}
    assert body["defaults"]["profile_binding"]["tool"] == "msprof"
    assert body["defaults"]["performance_policy"] == "default"
    assert body["defaults"]["accuracy_policy"] == "default"
    assert body["defaults"]["optimization_scope"]["strategy"] == "trend_bound"
