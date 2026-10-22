"""Candidate-owned, model-free validation in the candidate interpreter.

[Role]
Validates candidate source integrity, installed runtime package commit pins (`direct_url.json`), adapter layout, `runtime_probe` execution, and the official publication contract commit.

[Invariants]
- Only protocol-owned fields (`stage`, `code`, `component`, `process`) leave this process on failure; provider output and filesystem paths are never echoed.
- Re-reads source identity after validation to reject concurrent local edits.

[Timeouts & Locks]
- `publication_git` enforces a 60.0s timeout per Git subprocess via `bounded_process.run`.

[Side-Effect Policy]
- Uses a temporary bare Git directory for `publication_contract` verification; modifies no installed state.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

# -I intentionally discards ambient imports; helpers belong to this candidate.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from product_contract import identity, native_version, read_json, validate_receipt
from runtime_probe import build_probe_script


class CheckFailure(ValueError):
    def __init__(self, stage, code, component=None, *, process=None):
        self.failure = dict(stage=stage, code=code)
        if component is not None:
            self.failure["component"] = component
        if process is not None:
            self.failure["process"] = process
        super().__init__(stage + ": " + code)


def checked(stage, code, operation):
    try:
        return operation()
    except CheckFailure:
        raise
    except Exception as exc:
        raise CheckFailure(stage, code) from exc


def adapter_check(source):
    """This candidate owns its adapter layout and safety checks."""
    plugin = source / "plugins/mindie-agent"
    if not plugin.is_dir():
        plugin = source
    if (
        sum(p.stat().st_size for p in plugin.rglob("*") if p.is_file())
        > 16 * 1024 * 1024
    ):
        raise ValueError("plugin package exceeds 16 MiB")
    if (
        "allow_implicit_invocation: true"
        not in (plugin / "skills/mindie-agent/agents/openai.yaml").read_text(encoding='utf-8')
    ):
        raise ValueError("informational skill must be available on demand")
    hooks = read_json(plugin / "hooks/hooks.json")[0]["hooks"]
    if set(hooks) != {"SessionStart", "Stop", "Interrupt"} or any(len(v) != 1 for v in hooks.values()):
        raise ValueError("one SessionStart, Stop and Interrupt hook group is required")
    entries = hooks["Stop"][0]["hooks"]
    if len(entries) != 1 or "timeout" in entries[0]:
        raise ValueError("Stop must not impose an execution deadline")
    expected_interrupt = [
        dict(type="mcp_tool", server=server, tool="mindie_interrupt",
             input=dict(thread_id="${session_id}", turn_id="${turn_id}"),
             timeout=3, statusMessage="MindIE cancellation")
        for server in ("mindie-knowledge", "mindie-remote-dev")
    ]
    if hooks["Interrupt"][0]["hooks"] != expected_interrupt:
        raise ValueError("Interrupt must target both exact native MCP turn controls")
    for name in (
        "session_gate.py",
        "mcp_gate.py",
        "receipt_layout.py",
        "update_lock.py",
        "bounded_process.py",
        "windows_process.py",
        "runtime_call.py",
        "candidate_validate.py",
        "product_contract.py",
        "admission_ops.py",
        "capture_errors.py",
        "codex_transcript.py",
        "history_import.py",
        "agent_worker.py",
        "organizer_distill.py",
        "capture_config.py",
        "entry.py",
        "bootstrap.py",
        "bridge.py",
        "bridge_commands.py",
        "service_client.py",
        "mcp_catalog.json",
        "agent_diagnostics.py",
    ):
        if not (plugin / "scripts" / name).is_file():
            raise ValueError("missing bounded runtime entry: " + name)
    catalog = read_json(plugin / "scripts" / "mcp_catalog.json")[0]
    names = {tool.get("name") for tool in catalog.get("knowledge") or []}
    if not {"knowledge_query", "knowledge_explain", "knowledge_feedback", "knowledge_distill"} <= names:
        raise ValueError("adapter knowledge catalogue is incomplete")
    if "knowledge_use" in names or "knowledge_judge" in names:
        raise ValueError("retired knowledge tools are advertised")
    release_check(plugin)


def release_check(plugin):
    """Validate protection declarations; component versions do not imply formats."""
    version = native_version(plugin)
    if re.fullmatch(r"\d+\.\d+\.\d+", version):
        from mindie_knowledge import state_layout
        import receipt_layout
        for layout in (state_layout, receipt_layout):
            if (type(layout.FORMAT) is not int or layout.FORMAT < 1
                    or not isinstance(layout.RELEASE_VERSION, str)
                    or not re.fullmatch(r"\d+\.\d+\.\d+", layout.RELEASE_VERSION)):
                raise CheckFailure('adapter', 'release_state_undeclared')


def installed_revisions(pins):
    for name, expected in pins.items():
        try:
            raw = importlib.metadata.distribution(name).read_text("direct_url.json")
        except importlib.metadata.PackageNotFoundError as exc:
            raise CheckFailure("runtime_pins", "package_missing", name) from exc
        except Exception as exc:
            raise CheckFailure("runtime_pins", "metadata_unavailable", name) from exc
        if not raw:
            raise CheckFailure("runtime_pins", "receipt_missing", name)
        try:
            actual = (json.loads(raw).get("vcs_info") or {}).get("commit_id")
        except (ValueError, TypeError, AttributeError) as exc:
            raise CheckFailure("runtime_pins", "receipt_invalid", name) from exc
        if actual != expected:
            raise CheckFailure("runtime_pins", "revision_mismatch", name)


def runtime_check(scripts):
    output = io.StringIO()
    with redirect_stdout(output):
        exec(build_probe_script(Path(scripts) / "codex_transcript.py"), {})
    if output.getvalue().strip() != "OK":
        raise ValueError(output.getvalue().strip() or "candidate runtime probe returned no result")


def publication_git(args, code, env, *, timeout: float | None = 60.0):
    """Project only execution facts from one owned Git process."""
    from bounded_process import run

    try:
        result = run(args, "", timeout=timeout, env=env, allowed_returncodes=None)
    except Exception as exc:
        result = getattr(exc, "process_result", None)
        execution = result.execution if result is not None else "unknown"
        returncode = result.returncode if result is not None else None
        cleanup_failed = bool(result.cleanup) if result is not None else False
        reason = "process_failed"
        if execution == "not_started":
            reason = "spawn_failed"
        elif execution == "completed" and returncode != 0:
            reason = "nonzero_exit"
        elif execution == "unknown" and isinstance(exc, TimeoutError):
            reason = "timeout"
        raise CheckFailure("publication_fetch", code, process=dict(
            execution=execution, reason=reason, returncode=returncode,
            cleanup_failed=cleanup_failed,
        )) from exc
    if result.execution != "completed" or result.returncode != 0 or result.cleanup:
        reason = ("process_failed" if result.execution != "completed"
                  else "nonzero_exit" if result.returncode != 0 else "cleanup_failed")
        raise CheckFailure("publication_fetch", code, process=dict(
            execution=result.execution, reason=reason, returncode=result.returncode,
            cleanup_failed=bool(result.cleanup),
        ))
    return result.stdout


def publication_contract(publication):
    """One bounded, read-only fetch; no checkout, Git hooks or retries."""
    from mindie_knowledge.publication_contract import read_git_contract
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull)
    with tempfile.TemporaryDirectory(prefix="mindie-publication-contract-") as directory:
        git = ["git", "-c", "core.hooksPath=" + os.devnull, "-C", directory]
        publication_git(git + ["init", "--bare", "--quiet"], "init_failed", env)
        publication_git(git + [
            "fetch", "--no-auto-maintenance", "--depth=1", "--no-tags",
            "https://github.com/" + publication["repository"] + ".git",
            publication["verified_commit"],
        ], "fetch_failed", env)
        observed = publication_git(
            git + ["rev-parse", "FETCH_HEAD"], "revision_read_failed", env,
        ).strip()
        if observed != publication["verified_commit"]:
            raise CheckFailure("publication_fetch", "revision_mismatch")
        try:
            return read_git_contract(
                directory, observed, publication["domain"], env=env,
            )
        except Exception as exc:
            code = "contract_mismatch" if getattr(exc, "code", None) == "contract_mismatch" else "read_failed"
            raise CheckFailure("publication_contract", code) from exc


def validate(source, expected, verified_receipt=None):
    actual = checked("source", "source_invalid", lambda: identity(source, expected.get("candidate_revision")))
    if actual != expected:
        raise CheckFailure("source", "source_changed")
    # Verify provenance before adapter checks import runtime state declarations.
    installed_revisions(actual["runtime"])
    checked("adapter", "adapter_incompatible", lambda: adapter_check(Path(source)))
    checked("runtime_api", "api_incompatible", lambda: runtime_check(Path(__file__).resolve().parent))
    if verified_receipt is None:
        # The runtime parser checks material formats and review semantics.
        # The verified Git revision still binds the trusted publication read;
        # current-format selection no longer depends on a fixed content digest.
        checked("publication_contract", "read_failed",
                lambda: publication_contract(actual["publication"]))
    else:
        # This baseline is immutable and already verified for these exact
        # source/product/runtime identities. Rechecking before installation
        # never repeats the network read; local checks above always rerun.
        checked("receipt", "receipt_mismatch", lambda: validate_receipt(json.dumps(verified_receipt), actual))
    # Re-read the source after validation to reject concurrent local edits.
    if checked("source", "source_invalid", lambda: identity(source, actual["candidate_revision"])) != actual:
        raise CheckFailure("source", "source_changed")
    return dict(actual, status="validated")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--verified-receipt")
    args = parser.parse_args()
    expected = {}
    try:
        expected = json.loads(args.identity)
        previous = json.loads(args.verified_receipt) if args.verified_receipt else None
        result = validate(args.source, expected, previous)
    except Exception as exc:
        # Only fixed protocol-owned fields leave this process. Provider output,
        # filesystem paths and arbitrary exception text are never echoed.
        failure = exc.failure if isinstance(exc, CheckFailure) else dict(stage="validation", code="internal_error")
        result = dict(expected, status="failed", failure=failure) if isinstance(expected, dict) else dict(status="failed", failure=failure)
        sys.stdout.buffer.write((json.dumps(result, sort_keys=True) + "\n").encode("utf-8"))
        return 1
    sys.stdout.buffer.write((json.dumps(result, sort_keys=True) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
