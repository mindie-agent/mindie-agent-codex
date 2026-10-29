"""Component MCP calls run the real parsers and preserve their business output."""
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import mcp_gate


def source_root(package):
    spec = importlib.util.find_spec(package)
    if spec is None:
        return None
    return Path(spec.origin).resolve().parents[1]


@pytest.fixture
def component_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDIE_COORDINATOR_STATE_DIR", str(tmp_path / "coordinator"))
    packages = ["mindie_profiler", "mindie_sim", "mindie_coordinator",
                "mindie_diagnostics", "remote_dev"]
    roots = {package: root for package in packages if (root := source_root(package)) is not None}
    paths = json.dumps([str(root) for root in roots.values()])
    entry = Path(__file__).parent / "fixtures/component_source_cli.py"
    modules = dict(profiler="mindie_profiler", sim="mindie_sim",
                   coordinator="mindie_coordinator.cli", diagnostics="mindie_diagnostics.cli")
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, runtime_scripts=str(SCRIPTS),
        engine_config="/must-not-read/knowledge.json",
        component_commands={name: [sys.executable, "-I", "-X", "utf8", str(entry), paths, module]
                            for name, module in modules.items()},
    )))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    return mcp_gate.Gate("components"), roots


def call(gate, name, arguments):
    return gate.call(dict(id=1, params=dict(name=name, arguments=arguments, _meta={
        "threadId": "ordinary-task", "x-codex-turn-metadata": dict(
            thread_id="ordinary-task", session_id="ordinary-task", turn_id="turn-1")})))


@pytest.fixture
def sim_gate(tmp_path, monkeypatch):
    runtime = os.environ.get("MINDIE_SIM_CONTRACT_PYTHON")
    root = source_root("mindie_sim")
    if root is None and runtime is None:
        pytest.skip("sim contract requires mindie_sim")
    if runtime is None:
        import importlib.metadata
        from packaging.version import Version
        if Version(importlib.metadata.version("mindie-sim")) < Version("0.8.0"):
            pytest.skip("new sim contract requires MINDIE_SIM_CONTRACT_PYTHON (sim >= 0.8)")
    entry = Path(__file__).parent / "fixtures/component_source_cli.py"
    config = tmp_path / "sim-adapter.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, runtime_scripts=str(SCRIPTS),
        engine_config="/must-not-read/knowledge.json",
        component_commands={"sim": ([runtime, "-I", "-m", "mindie_sim"] if runtime else
                                   [sys.executable, "-I", "-X", "utf8", str(entry), json.dumps([str(root)]), "mindie_sim"])},
    )))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    return mcp_gate.Gate("components")


@pytest.fixture
def sim_case(tmp_path):
    path = tmp_path / "case.json"
    path.write_text(json.dumps({
        "machine": {
            "name": "synthetic-cli-control", "num_devices": 1,
            "device": {"memory_bytes": 1024**3, "compute_units": 4,
                       "compute_peaks": {"bf16": 16e9, "fp32": 8e9},
                       "rates": {"memory": 8e9, "vector": 4e9}},
        },
        "model": {"name": "control", "layers": 1, "hidden_size": 32,
                  "num_heads": 4, "num_kv_heads": 2, "head_dim": 8,
                  "intermediate_size": 64, "vocab_size": 128},
        "workload": {"requests": [{"id": "r", "prompt_tokens": 4, "output_tokens": 2}]},
        "runtime": {"max_num_batched_tokens": 8, "max_model_len": 128, "graph_mode": "none"},
    }))
    return path


def test_profiler_inventory_reads_the_real_capture_without_knowledge(component_gate, tmp_path):
    gate, roots = component_gate
    if "mindie_profiler" not in roots:
        pytest.skip("profiler contract requires its optional component installation")
    capture = Path(__file__).parent / "fixtures/profiler/rank_0"
    result = call(gate, "profiler", {"argv": ["inventory", "--root", str(capture)]})
    assert not result["isError"], result
    inventory = result["structuredContent"]["result"]
    assert inventory["rank_count"] == 1
    assert inventory["ranks"][0]["files"]
    output = tmp_path / "inventory.json"
    saved = call(gate, "profiler", {"argv": ["inventory", "--root", str(capture), "--output", str(output)]})
    assert not saved["isError"], saved
    assert json.loads(output.read_text())["rank_count"] == inventory["rank_count"]
    invalid = call(gate, "profiler", {"argv": ["invalid-component-operation"]})
    assert invalid["isError"]
    assert invalid["structuredContent"]["exit_code"] == 2
    assert "unknown command 'invalid-component-operation'" in invalid["structuredContent"]["stderr"]


def test_sim_installed_cli_keeps_operator_graph_and_serving_boundaries(sim_gate, sim_case, tmp_path):
    operators = call(sim_gate, "sim", {"argv": [
        "operators", str(sim_case), "--phase", "prefill", "--tokens", "4", "--scope", "model",
    ]})
    assert not operators["isError"], operators
    isolated = operators["structuredContent"]["result"]
    assert isolated["boundary"] == "sum_of_isolated_device_programs"
    graph_path = tmp_path / "graph.json"
    graph = call(sim_gate, "sim", {"argv": [
        "graph", str(sim_case), "--phase", "prefill", "--tokens", "4",
        "--scope", "model", "--output", str(graph_path),
    ]})
    assert not graph["isError"], graph
    assert graph["structuredContent"]["stdout"] == ""
    result = json.loads(graph_path.read_text())
    assert result["boundary"] == "device_graph"
    assert result["operator_occurrences"] == isolated["operator_occurrences"]
    assert result["isolated"]["operator_sum_ms"] == pytest.approx(isolated["operator_sum_ms"])
    prediction_path = tmp_path / "prediction.json"
    serving = call(sim_gate, "sim", {"argv": [
        "predict", str(sim_case), "--output", str(prediction_path),
    ]})
    assert not serving["isError"], serving
    prediction = json.loads(prediction_path.read_text())
    assert prediction["metric_boundary"] == "device_token_commit"
    assert prediction["requests"][0]["generated_tokens"] == 2


def test_sim_preserves_real_cli_error_and_exit_status(sim_gate, tmp_path):
    bad = call(sim_gate, "sim", {"argv": [
        "graph", str(tmp_path / "absent.json"), "--phase", "prefill", "--tokens", "4",
    ]})
    assert bad["isError"]
    value = bad["structuredContent"]
    assert value["exit_code"] == 2
    assert value["stdout"] == ""
    error = json.loads(value["stderr"])
    assert error["status"] == "failed"
    assert str(tmp_path / "absent.json") in error["error"]


def test_sim_partial_study_keeps_success_and_failure_in_the_nonzero_cli_result(
    sim_gate, sim_case, tmp_path,
):
    manifest = tmp_path / "study.json"
    manifest.write_text(json.dumps({
        "candidates": [
            {"id": "ordinary", "case": sim_case.name},
            {"id": "unavailable", "case": sim_case.name, "overrides": {
                "runtime": {"kernels": {"lowerings": {
                    "gqa_attention": "unavailable_source_implementation",
                }}},
            }},
        ],
        "probes": [{"id": "prefill", "phase": "prefill", "tokens": 4}],
    }))
    response = call(sim_gate, "sim", {"argv": ["study", str(manifest)]})
    assert response["isError"]
    value = response["structuredContent"]
    assert value["exit_code"] == 2
    report = value["result"]
    assert report["status"] == "partial"
    evaluations = {row["candidate"]: row for row in report["evaluations"]}
    assert evaluations["ordinary"]["status"] == "modeled"
    assert evaluations["ordinary"]["operators"]["operator_sum_ms"] > 0
    assert evaluations["unavailable"]["status"] == "failed"
    assert "unavailable_source_implementation" in evaluations["unavailable"]["error"]
    assert {row["candidate"] for ranking in report["rankings"]
            for row in ranking["candidates"]} == {"ordinary"}


def test_coordinator_local_host_inventory_without_activation(component_gate):
    gate, roots = component_gate
    if "mindie_coordinator" not in roots:
        pytest.skip("coordinator contract requires its optional component installation")
    hosts = call(gate, "coordinator", {"argv": ["hosts", "--action", "list"]})
    assert not hosts["isError"], hosts
    assert hosts["structuredContent"]["result"]["outcome"] == "success"


def test_local_diagnostics_without_activation(component_gate, tmp_path):
    gate, _ = component_gate
    adapter = json.loads(gate.config.read_text())
    adapter["component_commands"].pop("diagnostics")
    gate.config.write_text(json.dumps(adapter))
    status = call(gate, "diagnostics_status", {})
    assert not status["isError"], status
    assert status["structuredContent"]["result"]["status"] == "not_configured"
    logs = tmp_path / "logs"
    logs.mkdir()
    bundle = call(gate, "diagnostics_bundle", {"root": str(logs), "output": str(tmp_path / "support.zip")})
    assert not bundle["isError"], bundle
    assert (tmp_path / "support.zip").exists()


def test_missing_component_is_an_explicit_failure_before_execution(component_gate, tmp_path):
    gate, _ = component_gate
    adapter = json.loads(gate.config.read_text())
    adapter["component_commands"]["sim"] = [str(tmp_path / "not-installed")]
    gate.config.write_text(json.dumps(adapter))
    result = call(gate, "sim", {"argv": ["--help"]})
    assert result["isError"]
    assert result["process"]["execution"] == "not_started"
    assert result["process"]["automatic_retry"] is False
