"""Select the prepared native entry for Codex hooks and MCP startup.

[Role]
Resolves the canonical installed release (or first-run bootstrap, or MCP
migration source) and dispatches `runpy` into that entry. `hooks.json` and
`.mcp.json` invoke this module instead of inlining the selector as `python -c`.

[Invariants]
- Hooks never pass `--seed`, so they never migrate; only MCP/startup roles with
  a newer seed may route to the migration bootstrap.
- `stop` never prepares an unconfigured installation.
- Prepared `runtime.json` identity is compared exactly; a mismatch is not a
  first install and does not fall back to plugin cache.

[Timeouts & Locks]
- No locks or subprocesses; the selected entry owns timeouts.

[Side-Effect Policy]
- Importing this module is side-effect free. Running it as `__main__` or via
  `launch()` executes the selected entry.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import runpy
import sys
import tempfile
import time
from pathlib import Path

MCP_ROLES = ("remote-mcp", "tools-mcp", "knowledge-mcp")
VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!-]{0,79}")
NUMERIC_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_GESTURE = re.compile(
    r"^[\s\ufeff]*(?:"
    r"/mindie-agent"
    r"|\$mindie-agent(?::mindie-agent)?"
    r"|\[\$mindie-agent(?::mindie-agent)?\]\([^\r\n)]+\)"
    r"|\[@mindie-agent\]\(plugin://mindie-agent@[^\r\n)]+\)"
    r")(?=\s|$)"
)


def _same_interpreter(left, right, *, windows=None):
    """Compare runtime interpreter paths across Windows extended-path aliases."""
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False
    if os.name == "nt" if windows is None else windows:
        def _norm(value):
            text = value.replace("/", "\\")
            if text[:8].upper() == "\\\\?\\UNC\\":
                text = "\\\\" + text[8:]
            elif text.startswith("\\\\?\\"):
                text = text[4:]
            return ntpath.normcase(ntpath.abspath(text))
        return _norm(left) == _norm(right)
    return os.path.abspath(left) == os.path.abspath(right)


def read(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("MindIE metadata must be regular")
    with path.open("rb") as handle:
        raw = handle.read(65537)
    if len(raw) > 65536:
        raise ValueError("MindIE metadata exceeds its byte bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("MindIE metadata must be an object")
    return value


def version_key(value):
    match = NUMERIC_VERSION.fullmatch(value)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def parse_argv(argv):
    name = "mindie-agent"
    marketplace = "mindie-agent"
    seed = None
    index = 0
    while index < len(argv):
        current = argv[index]
        if current == "--name" and index + 1 < len(argv):
            name = argv[index + 1]
            index += 2
        elif current == "--marketplace" and index + 1 < len(argv):
            marketplace = argv[index + 1]
            index += 2
        elif current == "--seed" and index + 1 < len(argv):
            seed = argv[index + 1]
            index += 2
        elif current == "--":
            return name, marketplace, seed, list(argv[index + 1 :])
        else:
            return name, marketplace, seed, list(argv[index:])
    return name, marketplace, seed, []


def _plugin_source(home, marketplace, name, seed):
    default = home / "plugins/cache" / marketplace / name / (seed or "unprepared")
    return Path(os.environ.get("PLUGIN_ROOT", str(default)))


def _prepared_entry(adapter, data):
    validation = adapter.get("product_validation")
    if not isinstance(validation, dict):
        raise ValueError("MindIE release identity is invalid")
    version = validation.get("native_version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError("MindIE release identity is invalid")
    comp_rel = adapter.get("component_release")
    if comp_rel is not None and (not isinstance(comp_rel, str) or not os.path.isabs(comp_rel)):
        raise ValueError("MindIE release identity is invalid")
    release = Path(comp_rel) if comp_rel else data / "releases" / version
    receipt = read(release / "runtime.json")
    entry = release / "entry.py"
    if (not _same_interpreter(receipt.get("python"), adapter.get("python"))
            or receipt.get("validation") != validation
            or validation.get("status") != "validated"
            or validation.get("schema") != "mindie-candidate-check/1"):
        raise ValueError("MindIE installed runtime receipt differs")
    return entry


def select_entry(*, name="mindie-agent", marketplace="mindie-agent", seed=None, args=None):
    args = [] if args is None else list(args)
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    data = Path(os.environ.get("PLUGIN_DATA", str(home / "plugins/data" / (name + "-" + marketplace))))
    config = Path(os.environ.get(
        "MINDIE_AGENT_CONFIG", str(Path.home() / ".config/mindie-agent/codex.json"))).expanduser().absolute()
    role = args[0] if args else ""
    if config.exists():
        adapter = read(config)
        migrate = False
        if seed and (role in MCP_ROLES or (role == "bridge" and args[1:2] == ["mcp"])):
            validation = adapter.get("product_validation")
            installed = validation.get("native_version") if isinstance(validation, dict) else None
            if not isinstance(installed, str) or not VERSION_PATTERN.fullmatch(installed):
                raise ValueError("MindIE release identity is invalid")
            current, target = version_key(installed), version_key(seed)
            migrate = current is not None and target is not None and target > current
        if migrate:
            source = _plugin_source(home, marketplace, name, seed)
            bootstrap = source / "scripts/bootstrap.py"
            if bootstrap.is_file():
                return bootstrap, [str(data), "--source", str(source), "migrate", *args]
            entry = data / "releases" / seed / "entry.py"
            if not entry.is_file():
                raise RuntimeError("MindIE migration source is unavailable")
            return entry, ["migrate", *args]
        return _prepared_entry(adapter, data), args
    fixed = data / "releases" / seed / "entry.py" if seed else None
    if fixed is not None and fixed.is_file():
        return fixed, args
    if args[:1] == ["stop"]:
        raise RuntimeError("MindIE runtime is not prepared; start a new native session")
    source = _plugin_source(home, marketplace, name, seed)
    entry = source / "scripts/bootstrap.py"
    return entry, [str(data), "--source", str(source), *args]


def launch(argv=None):
    name, marketplace, seed, args = parse_argv(sys.argv[1:] if argv is None else argv)
    entry, forwarded = select_entry(name=name, marketplace=marketplace, seed=seed, args=args)
    if not entry.is_file():
        raise RuntimeError("MindIE installed entry is unavailable")
    sys.path.insert(0, str(entry.parent))
    sys.argv = [str(entry), *forwarded]
    runpy.run_path(str(entry), run_name="__main__")


def _record_launcher_failure(session_id, error_type):
    config = Path(os.environ.get(
        "MINDIE_AGENT_CONFIG", str(Path.home() / ".config/mindie-agent/codex.json"))).expanduser().absolute()
    target = config.with_suffix(".entry-status.json")
    if not target.parent.is_dir():
        return
    digest = (
        hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        if isinstance(session_id, str) and _IDENTITY.fullmatch(session_id)
        else None
    )
    entry = dict(
        updated_at=time.time(),
        stage="hook-launcher",
        status="failed",
        error_type=error_type,
    )
    store = {"schema": "mindie-manual-entry/2", "sessions": {}}
    try:
        if target.is_file():
            raw = target.read_bytes()
            if len(raw) <= 16384:
                loaded = json.loads(raw.decode("utf-8"))
                if (isinstance(loaded, dict)
                        and loaded.get("schema") == "mindie-manual-entry/2"
                        and isinstance(loaded.get("sessions"), dict)):
                    store["sessions"] = dict(loaded["sessions"])
                    if isinstance(loaded.get("unscoped_failure"), dict):
                        store["unscoped_failure"] = loaded["unscoped_failure"]
    except (OSError, ValueError, TypeError):
        pass
    if digest is not None:
        store["sessions"][digest] = entry
        if len(store["sessions"]) > 16:
            ordered = sorted(
                store["sessions"].items(),
                key=lambda pair: pair[1].get("updated_at", 0) if isinstance(pair[1], dict) else 0,
            )
            store["sessions"] = dict(ordered[-16:])
    else:
        store["unscoped_failure"] = entry
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(store, stream, sort_keys=True)
            stream.flush()
        os.replace(temporary, target)
    except TimeoutError:
        raise
    except OSError:
        return
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def _report_prompt_launcher_failure(exc):
    try:
        raw = sys.stdin.buffer.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            return
        event = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return
    explicit = (
        isinstance(event, dict)
        and event.get("hook_event_name") == "UserPromptSubmit"
        and not event.get("agent_id")
        and isinstance(event.get("prompt"), str)
        and _GESTURE.match(event["prompt"]) is not None
    )
    if not explicit:
        # Passive Default (P2.1): ordinary prompts remain completely silent and context-free.
        print("{}")
        raise SystemExit(0)
    error_type = type(exc).__name__
    print(f"MindIE hook launcher failed: {error_type}", file=sys.stderr)
    try:
        _record_launcher_failure(event.get("session_id"), error_type)
    except Exception:
        pass
    print(json.dumps(dict(
        systemMessage=(
            f"MindIE session authorization failed at hook-launcher ({error_type}); "
            "inspect local status/permissions and do not repeat /mindie-agent in a loop."
        ),
        hookSpecificOutput=dict(
            hookEventName="UserPromptSubmit",
            additionalContext=(
                f"MindIE UserPromptSubmit failed at stage 'hook-launcher' ({error_type}) "
                "before session authorization completed. Do NOT ask the user to resend "
                "/mindie-agent in a loop; inspect local status/permissions if needed and "
                "continue independent task work with native or deterministic tools."
            ),
        ),
    )))
    raise SystemExit(0)


if __name__ == "__main__":
    try:
        launch()
    except Exception as exc:
        _, _, _, _forwarded = parse_argv(sys.argv[1:])
        if _forwarded[:1] == ["user-prompt-submit"]:
            _report_prompt_launcher_failure(exc)
        raise
