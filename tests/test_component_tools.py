"""Component MCP calls run the real parsers and preserve their business output."""
import importlib.util
import json
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
        component_commands={name: [sys.executable, "-I", str(entry), paths, module]
                            for name, module in modules.items()},
    )))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    return mcp_gate.Gate("components"), roots


def call(gate, name, arguments):
    return gate.call(dict(id=1, params=dict(name=name, arguments=arguments, _meta={
        "threadId": "ordinary-task", "x-codex-turn-metadata": dict(
            thread_id="ordinary-task", session_id="ordinary-task", turn_id="turn-1")})))


def test_profiler_inventory_reads_the_real_capture_without_knowledge(component_gate):
    gate, roots = component_gate
    if "mindie_profiler" not in roots:
        pytest.skip("profiler contract requires its optional component installation")
    capture = roots["mindie_profiler"] / "tests/fixtures/profiler_core/rank_0"
    if not capture.exists():
        pytest.skip("profiler golden capture requires its source checkout")
    result = call(gate, "profiler", {"argv": ["inventory", "--root", str(capture)]})
    assert not result["isError"], result
    inventory = result["structuredContent"]["result"]
    assert inventory["rank_count"] == 1
    assert inventory["ranks"][0]["files"]


def test_sim_reads_hardware_input_and_preserves_invalid_input_failure(component_gate, tmp_path):
    gate, roots = component_gate
    if "mindie_sim" not in roots:
        pytest.skip("sim contract requires its optional component installation")
    fixture = roots["mindie_sim"] / "tests/fixtures/mindie_sim/Ascend910B4.ini"
    if not fixture.exists():
        pytest.skip("sim hardware capture requires its source checkout")
    good = call(gate, "sim", {"argv": ["hardware", "--ini", str(fixture)]})
    assert not good["isError"], good
    assert good["structuredContent"]["result"]["command"] == "hardware"
    bad = call(gate, "sim", {"argv": ["pipeline", "--graph", str(tmp_path / "absent.json")]})
    assert bad["isError"]
    assert bad["structuredContent"]["exit_code"] != 0
    assert "input_unreadable" in bad["structuredContent"]["stdout"]


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
