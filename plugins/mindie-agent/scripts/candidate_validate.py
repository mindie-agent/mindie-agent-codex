"""Candidate-owned, model-free validation in the candidate interpreter.

[Role]
Validates candidate source integrity, installed runtime package commit pins (`direct_url.json`), adapter layout, `runtime_probe` execution, and the official publication contract commit.

[Invariants]
- Only protocol-owned fields (`stage`, `code`, `component`, `process`, and one
  closed transport `category`) leave this process on failure; provider output
  and filesystem paths are never echoed.
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
import time

# -I intentionally discards ambient imports; helpers belong to this candidate.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from product_contract import identity, native_version, read_json, validate_receipt
from runtime_probe import build_probe_script

CATALOG_MAX_BYTES = 1024 * 1024
FAILURE_CATEGORIES = frozenset({
    "temporary_network", "rate_limited", "certificate", "authentication",
    "resolver", "permission", "prerequisite",
})
_DNS_PHRASES = (
    "could not resolve host", "temporary failure in name resolution",
    "name or service not known", "nodename nor servname",
    "temporary failure resolving", "dns error", "getaddrinfo",
    "no address associated with hostname", "unknown host",
)


def read_catalog(path):
    """Read the generated discovery snapshot under its own bounded envelope.

    Contract metadata stays at the 64 KiB `read_json` bound; the Codex-profile
    catalog legitimately carries canonical plus native schemas and is larger.
    """
    path = Path(path)
    if not path.is_file():
        raise ValueError("required catalog is not a regular file: " + str(path))
    with path.open("rb") as stream:
        raw = stream.read(CATALOG_MAX_BYTES + 1)
    if len(raw) > CATALOG_MAX_BYTES:
        raise ValueError("catalog exceeds its metadata byte bound")
    return json.loads(raw.decode("utf-8"))


class CheckFailure(ValueError):
    def __init__(self, stage, code, component=None, *, process=None, category=None):
        self.failure = dict(stage=stage, code=code)
        if component is not None:
            self.failure["component"] = component
        if process is not None:
            self.failure["process"] = process
        if category in FAILURE_CATEGORIES:
            # Only a closed, non-secret category label is ever projected.
            self.failure["category"] = category
        super().__init__(stage + ": " + code)


def _transport_category(reason, exc, *streams):
    """Classify bounded Git text into one closed category; never retain it."""
    if reason == "spawn_failed" and isinstance(exc, FileNotFoundError):
        return "prerequisite"
    text = "\n".join(value for value in streams if isinstance(value, str) and value)
    lower = text.lower()
    if any(phrase in lower for phrase in _DNS_PHRASES):
        return "resolver"
    if text.strip():
        from bounded_process import classify_transport_text
        kind = classify_transport_text(text)
        if kind is not None and kind[0] in FAILURE_CATEGORIES:
            return kind[0]
    if reason == "timeout":
        return "temporary_network"
    return None


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
    import yaml
    skill = yaml.safe_load((plugin / "skills/mindie-agent/agents/openai.yaml").read_text(encoding='utf-8'))
    if skill.get("policy", {}).get("allow_implicit_invocation") is not False:
        raise ValueError("knowledge skill requires explicit user invocation")
    hooks = read_json(plugin / "hooks/hooks.json")[0]["hooks"]
    if set(hooks) != {"SessionStart", "UserPromptSubmit", "Stop", "Interrupt"} or any(len(v) != 1 for v in hooks.values()):
        raise ValueError("one SessionStart, UserPromptSubmit, Stop and Interrupt hook group is required")
    from native_entry import SESSION_START_TIMEOUT, STOP_TIMEOUT, USER_PROMPT_TIMEOUT
    required = (
        ("SessionStart", SESSION_START_TIMEOUT),
        ("UserPromptSubmit", USER_PROMPT_TIMEOUT),
        ("Stop", STOP_TIMEOUT),
    )
    for event, minimum in required:
        entries = hooks[event][0]["hooks"]
        if len(entries) != 1 or entries[0].get("type") != "command":
            raise ValueError("one native " + event + " command is required")
        timeout = entries[0].get("timeout")
        if not isinstance(timeout, int) or timeout < minimum:
            raise ValueError(event + " hook timeout must cover inner budgets and cleanup")
    expected_interrupt = [
        dict(type="mcp_tool", server=server, tool="mindie_interrupt",
             input=dict(thread_id="${session_id}", turn_id="${turn_id}"),
             timeout=3, statusMessage="MindIE cancellation")
        for server in ("mindie-remote-dev", "mindie-tools")
    ]
    if hooks["Interrupt"][0]["hooks"] != expected_interrupt:
        raise ValueError("Interrupt must target both exact native MCP turn controls")
    for name in (
        "session_gate.py",
        "mcp_gate.py",
        "receipt_layout.py",
        "update_lock.py",
        "bounded_process.py",
        "capture_spool.py",
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
        "manual_entry.py",
        "knowledge_cli.py",
        "component_tools.py",
        "bootstrap.py",
        "hook_launcher.py",
        "component_update.py",
        "bridge.py",
        "bridge_commands.py",
        "service_client.py",
        "mcp_catalog.json",
        "agent_diagnostics.py",
        "experiment_ledger.py",
        "rsi_loop.py",
    ):
        if not (plugin / "scripts" / name).is_file():
            raise ValueError("missing bounded runtime entry: " + name)
    catalog = read_catalog(plugin / "scripts" / "mcp_catalog.json")
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
    from bounded_process import run

    # Exercise the actual isolated CLI import boundary without discovery/builds.
    run([sys.executable, "-I", "-B", str(Path(scripts) / "component_update.py"),
         "--help"], "", timeout=5.0).checked_stdout()
    output = io.StringIO()
    with redirect_stdout(output):
        exec(build_probe_script(Path(scripts) / "codex_transcript.py"), {})
    if output.getvalue().strip() != "OK":
        raise ValueError(output.getvalue().strip() or "candidate runtime probe returned no result")


def windows_service_check(executable=None):
    """Validate the generated GUI launcher only when service capability is required.

    The shared PE reader owns the header contract; this adapter translates its
    known failures into the closed candidate-validation stage.
    """
    if os.name != "nt":
        return
    from mindie_knowledge.loop.service_host import ServiceHostError
    from mindie_knowledge.windows_interpreter import require_windowless

    launcher = Path(sys.executable if executable is None else executable).with_name("pythonw.exe")
    try:
        require_windowless(launcher)
    except TimeoutError:
        raise
    except ServiceHostError as exc:
        code = (exc.code if exc.code in {"windowless_interpreter_missing", "windowless_interpreter_invalid"}
                else "windowless_interpreter_invalid")
        raise CheckFailure("windows_service_interpreter", code) from exc


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
        if execution == "not_started" or (
                result is None and isinstance(exc, FileNotFoundError)):
            reason = "spawn_failed"
        elif execution == "completed" and returncode != 0:
            reason = "nonzero_exit"
        elif isinstance(exc, TimeoutError) and (result is None or execution == "unknown"):
            reason = "timeout"
        category = _transport_category(
            reason, exc,
            getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        raise CheckFailure("publication_fetch", code, process=dict(
            execution=execution, reason=reason, returncode=returncode,
            cleanup_failed=cleanup_failed,
        ), category=category) from exc
    if result.execution != "completed" or result.returncode != 0 or result.cleanup:
        reason = ("process_failed" if result.execution != "completed"
                  else "nonzero_exit" if result.returncode != 0 else "cleanup_failed")
        category = _transport_category(
            reason, None, getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        raise CheckFailure("publication_fetch", code, process=dict(
            execution=result.execution, reason=reason, returncode=result.returncode,
            cleanup_failed=bool(result.cleanup),
        ), category=category)
    return result.stdout


def publication_contract(publication):
    """One bounded, read-only fetch; no checkout, Git hooks or retries."""
    from mindie_knowledge.publication_contract import read_git_contract
    deadline = time.monotonic() + 150
    def remaining():
        value = deadline - time.monotonic() - 5
        if value <= 0:
            raise TimeoutError("publication validation budget exhausted")
        return min(60.0, value)
    # The user's Git configuration owns enterprise proxy and CA trust.
    # The temporary bare repository and disabled hooks isolate this read.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    with tempfile.TemporaryDirectory(prefix="mindie-publication-contract-") as directory:
        git = ["git", "-c", "core.hooksPath=" + os.devnull, "-C", directory]
        publication_git(git + ["init", "--bare", "--quiet"], "init_failed", env, timeout=remaining())
        publication_git(git + [
            "fetch", "--no-auto-maintenance", "--depth=1", "--no-tags",
            "https://github.com/" + publication["repository"] + ".git",
            publication["verified_commit"],
        ], "fetch_failed", env, timeout=remaining())
        observed = publication_git(
            git + ["rev-parse", "FETCH_HEAD"], "revision_read_failed", env, timeout=remaining(),
        ).strip()
        if observed != publication["verified_commit"]:
            raise CheckFailure("publication_fetch", "revision_mismatch")
        try:
            return read_git_contract(
                directory, observed, publication["domain"], env=env, deadline=deadline,
            )
        except Exception as exc:
            code = "contract_mismatch" if getattr(exc, "code", None) == "contract_mismatch" else "read_failed"
            raise CheckFailure("publication_contract", code) from exc


def publication_baseline(receipt, actual):
    """Whether a validated receipt still proves the same official publication."""
    return (
        isinstance(receipt, dict)
        and receipt.get("status") == "validated"
        and all(receipt.get(field) == actual.get(field)
                for field in ("schema", "source_sha256", "product_sha256", "publication"))
    )


def validate(source, expected, verified_receipt=None, *, verified_publication=None,
             require_windows_service=False):
    actual = checked("source", "source_invalid", lambda: identity(source, expected.get("candidate_revision"), components=expected.get("component_updates")))
    if actual != expected:
        raise CheckFailure("source", "source_changed")
    # Verify provenance before adapter checks import runtime state declarations.
    installed_revisions(actual["runtime"])
    if "component_updates" in actual:
        from mindie_knowledge.runtime_inventory import verify, verify_lock
        checked("runtime_pins", "receipt_invalid", lambda: verify_lock(actual["component_updates"]))
        checked("runtime_pins", "revision_mismatch", lambda: verify(actual["component_updates"]["runtime"]))
    checked("adapter", "adapter_incompatible", lambda: adapter_check(Path(source)))
    checked("runtime_api", "api_incompatible", lambda: runtime_check(Path(__file__).resolve().parent))
    # Service capability is required even when a generic immutable receipt is
    # reused: that receipt never proves this machine's generated launcher.
    if require_windows_service:
        windows_service_check()
    if verified_receipt is not None:
        try:
            validate_receipt(json.dumps(verified_receipt), actual)
        except (TypeError, ValueError):
            # A pure component update keeps the same source, product and
            # publication contract; only runtime pins/component_updates move.
            # That already-verified publication baseline still binds the
            # official commit, so only the redundant network read is skipped.
            if not publication_baseline(verified_receipt, actual):
                raise CheckFailure("receipt", "receipt_mismatch") from None
    elif verified_publication is not None:
        if not publication_baseline(verified_publication, actual):
            raise CheckFailure("receipt", "receipt_mismatch")
    else:
        # The runtime parser checks material formats and review semantics.
        # The verified Git revision still binds the trusted publication read;
        # current-format selection no longer depends on a fixed content digest.
        checked("publication_contract", "read_failed",
                lambda: publication_contract(actual["publication"]))
    # Re-read the source after validation to reject concurrent local edits.
    if checked("source", "source_invalid", lambda: identity(source, actual["candidate_revision"], components=actual.get("component_updates"))) != actual:
        raise CheckFailure("source", "source_changed")
    return dict(actual, status="validated")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--verified-receipt")
    parser.add_argument("--verified-publication")
    parser.add_argument("--require-windows-service", action="store_true")
    args = parser.parse_args()
    expected = {}
    try:
        expected = json.loads(args.identity)
        previous = json.loads(args.verified_receipt) if args.verified_receipt else None
        publication = (json.loads(args.verified_publication)
                       if args.verified_publication else None)
        options = (dict(verified_publication=publication) if publication is not None
                   else {})
        result = validate(args.source, expected, previous,
                          require_windows_service=args.require_windows_service,
                          **options)
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
