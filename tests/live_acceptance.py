"""Opt-in actual Codex A -> organizer -> B -> judge -> C acceptance.

Run with the knowledge runtime Python. Requires the local marketplace installed.
Uses a synthetic CPU fixture; this is not an NPU validation or a quality benchmark.
All services, task files and logs belong to --output. No raw artifacts are published.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from mindie_knowledge.loop.engine import Engine
from mindie_knowledge.loop.store import Store, canonical
from mindie_knowledge.loop.transport import Service

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins/mindie-agent"


def wait_for(predicate, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise RuntimeError("acceptance timed out; inspect local service and task logs")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    services = []

    def service(name, upstream=None):
        root = output / name
        root.mkdir()
        store = Store(root / "data", "vllm-ascend")
        engine = Engine(
            store,
            agent_command=[sys.executable, str(PLUGIN / "scripts/agent_worker.py")],
            auto_publish=upstream is None,
        )
        instance = Service(
            engine, connection_path=store.root / "connection.json", upstream=upstream
        )
        thread = threading.Thread(target=instance.serve, daemon=True)
        thread.start()
        services.append((instance, thread))
        wait_for(lambda: (store.root / "connection.json").is_file(), 10)
        engine_config = root / "engine.json"
        engine_config.write_text(
            canonical(
                dict(
                    root=str(store.root.parent),
                    domain=store.domain,
                    agent_command=engine.agent_command,
                )
            )
        )
        config = root / "codex.json"
        config.write_text(
            canonical(dict(python=sys.executable, engine_config=str(engine_config)))
        )
        config.chmod(0o600)
        engine_config.chmod(0o600)
        return instance, config

    def task(name, config, prompt):
        root = output / name
        root.mkdir()
        package = root / "npu_fixture"
        package.mkdir()
        (package / "__init__.py").write_text("import torch_npu\n")
        (package / "shape.py").write_text(
            'def validate_shape(shape, rank):\n    if len(shape) != rank or any(n <= 0 for n in shape):\n        raise ValueError("invalid shape")\n    return tuple(shape)\n'
        )
        command = [
            "codex",
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--dangerously-bypass-hook-trust",
            "-C",
            str(root),
            "-c",
            "plugins.mindie-agent@mindie-agent.enabled=true",
            "-c",
            'plugins.mindie-agent@mindie-agent.mcp_servers.mindie-knowledge.default_tools_approval_mode="approve"',
            "-c",
            'marketplaces.mindie-agent={source_type="local",source='
            + json.dumps(str(REPO))
            + "}",
            "-c",
            "features.hooks=true",
            "--json",
            "--output-last-message",
            str(root / "result.txt"),
            "-",
        ]
        with (
            (root / "events.jsonl").open("w") as stdout,
            (root / "stderr.log").open("w") as stderr,
        ):
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                stdout=stdout,
                stderr=stderr,
                timeout=240,
                env={**os.environ, "MINDIE_AGENT_CONFIG": str(config)},
            )
        if completed.returncode:
            raise RuntimeError(f"task {name} exited {completed.returncode}")
        return root

    try:
        authority, ac = service("authority")
        a = task(
            "task-a",
            ac,
            """Use the MindIE Agent skill and its knowledge_query with your SessionStart session_id first.
This is a CPU-only acceptance fixture for an Ascend development workflow. Inspect npu_fixture/shape.py.
Demonstrate that importing the package normally fails because torch_npu is unavailable, then find a way
to run three checks directly against the pure helper: a valid (2,3) rank 2 shape, a wrong-rank shape,
and a nonpositive dimension. Do not edit files or install packages. Run actual Python checks using
the standard library. Summarize the reusable approach and actual results. Make clear this is a
synthetic CPU fixture, not NPU validation. Do not access files outside this fixture.""",
        )
        wait_for(lambda: authority.store.status()["captures"], 10)
        wait_for(lambda: authority.store.snapshot()["entries"])
        b_service, bc = service("replica-b", authority.connection)
        wait_for(lambda: b_service.sync().get("status") == "synced", 15)
        query = "CPU helper torch_npu import shape"
        before = b_service.store.query(query)["results"]
        if not before:
            raise RuntimeError("organized experience was not retrievable")
        b = task(
            "task-b",
            bc,
            """Use the MindIE Agent skill. Query its domain experience for 'CPU helper torch_npu import shape'
using the exact SessionStart session_id, and explain a relevant result. Independently use the retrieved
approach on npu_fixture/shape.py to check valid (2,3) rank 2, wrong rank, and nonpositive dimensions
without importing unavailable torch_npu. Run actual Python checks, no package installs or file edits.
After applying the experience, call knowledge_use with its ref, your session_id, the approach applied,
and concrete observed output. Finish with actual results and validation limits. Do not claim NPU
execution: this is a synthetic CPU fixture. Do not access files outside this fixture.""",
        )
        wait_for(lambda: b_service.store.status()["uses"] > 0, 10)
        wait_for(lambda: b_service.sync().get("status") == "synced", 15)
        wait_for(
            lambda: (
                authority.store.status()["feedback"]
                or authority.store.status()["failed_judges"]
            )
        )
        c_service, cc = service("replica-c", authority.connection)
        wait_for(lambda: c_service.sync().get("status") == "synced", 15)
        after = c_service.store.query(query)["results"]
        feedback = authority.store.status()["feedback"]
        summary = dict(
            fixture="synthetic CPU shape helper; no NPU execution",
            native_tasks=[str(a), str(b)],
            before=before,
            after=after,
            feedback=feedback,
            errors=authority.engine.errors,
            captures=authority.store.status()["captures"],
        )
        (output / "result.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2)
        )
        assert feedback and feedback[0]["verdict"] == "helpful", summary
        ref = authority.store.ref(feedback[0]["entry_id"])
        old = next(r for r in before if r["ref"] == ref)
        new = next(r for r in after if r["ref"] == ref)
        assert new["score"] > old["score"]
        print(
            json.dumps(
                dict(
                    passed=True,
                    before=old["score"],
                    after=new["score"],
                    result=str(output / "result.json"),
                )
            )
        )
    finally:
        for instance, thread in reversed(services):
            instance.close()
            thread.join(5)
            instance.engine.thread.join(160)
            instance.store.close()


if __name__ == "__main__":
    main()
