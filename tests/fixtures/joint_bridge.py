"""Run bridge.py with the joint source visible to fixed-runtime helpers.

Production intentionally strips PYTHONPATH so helpers use the installed pin.
These tests run against the current checkout's shared core, so this fixture
keeps the test-supplied joint source for child helper processes only. It
patches the same ``runtime_env`` that production bridge imports.
"""
import os
import runpy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "plugins" / "mindie-agent" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import session_gate  # noqa: E402

_base_runtime_env = session_gate.runtime_env


def _joint_runtime_env(config=None, *, adapter=None):
    value = _base_runtime_env(config, adapter=adapter)
    joint = os.environ.get("PYTHONPATH")
    if joint:
        value["PYTHONPATH"] = joint
    else:
        value.pop("PYTHONPATH", None)
    return value


session_gate.runtime_env = _joint_runtime_env
runpy.run_path(str(SCRIPTS / "bridge.py"), run_name="__main__")
