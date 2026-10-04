"""Installation-level entry point with an OS-owned generation lease.

Native tasks keep this stable path, not a path into an old virtualenv. Reading
the current pointer and taking its lease happen under the update lock. Once
selected, the process retains that generation until it exits; elapsed time
never proves it unused. This file and update_lock.py are installed together.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import runpy
import sys

from update_lock import generation_lease, update_lock

ROLES = {"knowledge-mcp": ("bridge.py", ["mcp"]),
         "remote-mcp": ("remote_bridge.py", []),
         "stop": ("bridge.py", ["stop"])}


def selected(config, root, lifetime):
    with update_lock(config):
        value = json.loads(config.read_text(encoding="utf-8"))
        scripts = Path(value["runtime_scripts"]).resolve(strict=True)
        generation = scripts.parent.parent
        if generation.parent != (root / "generations").resolve() or scripts != generation / "plugin/scripts":
            raise ValueError("runtime pointer is outside the managed generations")
        marker = json.loads((generation / "ownership.json").read_text(encoding="utf-8"))
        if marker != {"schema": "mindie-runtime-generation/2", "revision": generation.name}:
            raise ValueError("runtime generation ownership does not match")
        locks = root / "generation-locks"
        locks.mkdir(exist_ok=True)
        lifetime.enter_context(generation_lease(locks / (generation.name + ".lock")))
        return scripts


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3 or argv[0] != "--config" or argv[2] not in ROLES:
        raise ValueError("runtime launcher requires --config PATH and a declared role")
    root = Path(__file__).resolve().parent
    role = argv[2]
    stage = 'selection'
    try:
        config = Path(argv[1]).expanduser().resolve(strict=True)
        with ExitStack() as lifetime:
            scripts = selected(config, root, lifetime)
            name, arguments = ROLES[role]
            entry = scripts / name
            stage = 'entry'
            if not entry.is_file():
                raise FileNotFoundError("selected runtime entry is missing")
            os.environ["MINDIE_AGENT_CONFIG"] = str(config)
            sys.path.insert(0, str(scripts))
            sys.argv = [str(entry), *arguments]
            runpy.run_path(str(entry), run_name="__main__")
    except Exception as exc:
        if role != 'stop':
            raise
        from diagnostic_support import failure
        failure('capture.stop', stage, 'generation_busy' if isinstance(exc, BlockingIOError)
                else 'generation_unavailable', exception=exc, reportable=False)
        print('{}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
