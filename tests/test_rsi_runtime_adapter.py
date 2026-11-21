import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import rsi_runtime_adapter  # noqa: E402
import rsi_install  # noqa: E402


def test_reference_adapter_never_uses_shell_and_parses_json(tmp_path):
    command = tmp_path / "command.py"
    command.write_text(
        "import json,sys\n"
        "payload=json.load(sys.stdin)\n"
        "print('diagnostic line')\n"
        "print(json.dumps({'status':'completed','passed':True,'benchmark_passed':True,\n"
        "'paired_speedup_ci_lower_bound':1.2,'request_count':4}))\n",
        encoding="utf-8",
    )
    payload = {
        "runtime_binding": {"commands": {"benchmark_cmd": [sys.executable, str(command)]}},
        "candidate": {"candidate_id": "c1"},
    }
    result = rsi_runtime_adapter.dispatch("performance", tmp_path, payload)
    assert result["passed"] is True
    assert result["paired_speedup_ci_lower_bound"] == 1.2


def test_plan_test_command_is_used_as_performance_argv(tmp_path):
    command = tmp_path / "command.py"
    command.write_text("import json; print(json.dumps({'status':'completed','passed':True}))\n",
                       encoding="utf-8")
    result = rsi_runtime_adapter.dispatch(
        "performance", tmp_path,
        {"test_command": [sys.executable, str(command)]},
    )
    assert result["passed"] is True


def test_reference_adapter_missing_evidence_fails_closed(tmp_path):
    result = rsi_runtime_adapter.dispatch("accuracy", tmp_path, {})
    assert result["status"] == "unavailable"
    assert result["passed"] is False
    assert result["reason"] == "accuracy_command_not_configured"


def test_reference_adapter_wrapper_round_trip(tmp_path):
    root = tmp_path
    rsi_install.install(root)
    bindings = root / "config/live-runtime-bindings.json"
    config = json.loads(bindings.read_text(encoding="utf-8"))
    config["default"]["runtime_python"] = sys.executable
    bindings.write_text(json.dumps(config), encoding="utf-8")
    process = subprocess.run(
        [sys.executable, str(root / "bin/mindie_agent_rsi_entry.py"), "prepare", "--root", str(root)],
        input=json.dumps({"candidate": {"candidate_id": "c1"}}),
        text=True, capture_output=True, check=False,
    )
    assert process.returncode == 0
    assert json.loads(process.stdout)["candidate_id"] == "c1"
