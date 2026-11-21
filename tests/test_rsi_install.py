import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import rsi_install  # noqa: E402


def test_install_creates_standard_wrapper_and_detects_nonstandard_runtime(tmp_path):
    runtime = tmp_path / "rsi-mindie/bin/mindie_agent_rsi_entry.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        "import json, sys\n"
        "print(json.dumps({'status': 'completed', 'operation': sys.argv[1]}))\n",
        encoding="utf-8",
    )
    result = rsi_install.install(tmp_path)
    assert result["entry_created"] is True
    assert result["runtime_entry_detected"] == "rsi-mindie/bin/mindie_agent_rsi_entry.py"
    wrapper = tmp_path / "bin/mindie_agent_rsi_entry.py"
    adapter = tmp_path / "bin/rsi_runtime_adapter.py"
    bindings = tmp_path / "config/live-runtime-bindings.json"
    assert wrapper.is_file()
    assert adapter.is_file()
    assert bindings.is_file()
    assert (tmp_path / "bin/mindie_rsi/rsi_workload.py").is_file()
    assert result["core_files_created"] > 0

    config = json.loads(bindings.read_text(encoding="utf-8"))
    config["default"]["runtime_python"] = sys.executable
    bindings.write_text(json.dumps(config), encoding="utf-8")
    process = subprocess.run(
        [sys.executable, str(wrapper), "performance", "--root", str(tmp_path)],
        input="{}",
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "RSI_BINDINGS": str(bindings)},
    )
    assert process.returncode == 0
    assert json.loads(process.stdout)["operation"] == "performance"


def test_install_falls_back_to_reference_adapter_when_runtime_is_absent(tmp_path):
    result = rsi_install.install(tmp_path)
    config = json.loads((tmp_path / "config/live-runtime-bindings.json").read_text(encoding="utf-8"))
    assert result["adapter_created"] is True
    assert config["default"]["runtime_entry"] == "bin/rsi_runtime_adapter.py"
    assert config["default"]["commands"] == {}
    assert config["default"]["profile_binding"]["tool"] == "msprof"

    process = subprocess.run(
        [sys.executable, str(tmp_path / "bin/mindie_agent_rsi_entry.py"),
         "prepare", "--root", str(tmp_path)],
        input=json.dumps({"candidate": {"candidate_id": "c1"}}),
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "RSI_BINDINGS": str(tmp_path / "config/live-runtime-bindings.json")},
    )
    assert process.returncode == 0
    assert json.loads(process.stdout)["status"] == "prepared"


def test_install_preserves_existing_standard_wrapper(tmp_path):
    wrapper = tmp_path / "bin/mindie_agent_rsi_entry.py"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("sentinel\n", encoding="utf-8")
    result = rsi_install.install(tmp_path)
    assert result["entry_created"] is False
    assert wrapper.read_text(encoding="utf-8") == "sentinel\n"
