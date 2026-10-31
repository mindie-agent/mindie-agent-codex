"""Exercise a real generation build and rebind in a disposable native profile.

[Role] Validate Windows long paths through the production component updater.
[Invariants] Selection uses the real baseline inventory and immutable pins;
only the caller's disposable profile is changed, with no model or NPU work.
[Timeouts & Locks] The parent owns a 900s budget; production builders and
activation retain their own locks and deadlines.
[Side-Effect Policy] Writes the explicit disposable profile and an owned
temporary generation. Does not query or change the operator's installation.
"""
import json
from pathlib import Path
import sys
import shutil
import tempfile
from unittest.mock import patch

config, source = (Path(value) for value in sys.argv[1:3])
external_python = sys.argv[3]
sys.path.insert(0, str(source / "scripts"))

from component_update import check
from mindie_knowledge.component_runtime import read_inventory
from release_activation import _atomic
from update_lock import update_lock

with update_lock(config, exclusive=True):
    adapter = json.loads(config.read_text(encoding="utf-8"))
    adapter["component_commands"] = {
        "diagnostics": [external_python, "-I", "-X", "utf8", "-m", "mindie_diagnostics.cli"],
    }
    _atomic(config, adapter)

# Keep this native regression deterministic while exercising the real builder,
# isolated API probe and activation against the installed formal baseline.
inventory = read_inventory(adapter["python"], timeout=15)
from bootstrap import native_path
from mindie_knowledge.filesystem import path_is_within

# Match the default plugin-data depth without making uv's own project URI
# longer than the native host's unrelated pytest profile directory.
generation_parent = Path(__file__).resolve().parents[3]
with tempfile.TemporaryDirectory(prefix="native-", dir=generation_parent) as directory:
    data = Path(directory)
    try:
        with patch("mindie_knowledge.component_runtime.discover",
                   return_value=dict(changed=False, runtime=inventory)):
            result = check(config, source, data)
        current = json.loads(config.read_text(encoding="utf-8"))
        assert result["status"] == "active", result
        assert current["component_release"]
        assert current["component_commands"]["diagnostics"][0] == current["python"]
        assert all(Path(path).is_file() for path in current["domain_configs"].values())
        assert (current["product_validation"]["native_version"]
                == adapter["product_validation"]["native_version"])
        print(json.dumps(dict(status="active", component_rebound=True)))
    finally:
        assert path_is_within(data, generation_parent)
        shutil.rmtree(native_path(data))
