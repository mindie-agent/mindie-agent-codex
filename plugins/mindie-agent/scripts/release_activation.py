"""Prepare a complete runtime binding, then publish it after exact idle handoff.

[Role]
Activates a newly validated plugin release (`activate`) by preparing the target store layout, acquiring an exclusive `update_lock`, stopping any idle knowledge service (`idle_stop`), and atomically updating `engine.json` and `codex.json`.

[Invariants]
- Never overwrites an active installation if `update_lock` is busy (`status="deferred_busy"`) or if the knowledge service is actively serving a task.
- The activation child receives an immutable bounded stdin attempt snapshot; it refuses to prepare against a newer canonical binding and never rereads the replaceable pending receipt. The persisted copy is only parent recovery evidence; `activate` remains an idempotent canonical readback.
- Prepared engine files stay immutable; failed binding publication restores retired domain owners within the control budget.

[Timeouts & Locks]
- Uses non-blocking `update_lock(config, exclusive=True, blocking=False)` for atomic handoff and `timeout=30.0s` for `idle-stop`. Legacy runtime metadata shares 15s. Preparation accepts a caller startup deadline; the attempt snapshot is bounded to 64 KiB stdin.

[Side-Effect Policy]
- Publishes the prepared engine and atomically replaces the adapter binding via
  `os.replace()`. Existing runtime directories remain available to loaded callers.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from product_contract import official_feeds, read_json
from session_gate import runtime_binding
from update_lock import update_lock

CONTROL_SECONDS = 30.0
ATTEMPT_SCHEMA = "mindie-activation-attempt/1"


class InstallationChanged(ValueError):
    """The canonical binding is no longer the expected pre-activation binding."""


def _atomic(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _validate_legacy_runtime_floor(adapter):
    """An old parent snapshots after building; recheck its actual runtime floor."""
    from mindie_knowledge.component_runtime import read_inventory, read_versions
    from semantic_version import Version

    deadline = time.monotonic() + 15
    installed = read_inventory(adapter["python"], timeout=15)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("legacy runtime metadata budget exhausted")
    candidate = read_versions(sys.executable, list(installed), timeout=remaining)
    for name, component in installed.items():
        if Version(candidate[name]) < Version(component["version"]):
            raise ValueError(f"candidate runtime is below the current installed component: {name}")


def prepare(config, release, *, blocking=True, expected_binding=None, deadline=None):
    """No running service or canonical configuration is changed here.

    expected_binding is the immutable attempt snapshot supplied by the parent.
    A delayed child therefore cannot prepare against a newer canonical binding
    and later downgrade it, even if the parent's persisted receipt is replaced.
    """
    from capture_config import prepare as capture_prepare
    from native_context import service_launch_context
    from mindie_knowledge.loop.cli import config_at
    from mindie_knowledge.loop.service_context import validate
    from mindie_knowledge.state_layout import state_root
    from mindie_knowledge.git_layout import validate_config_feed_layout
    from summary_provider import required_environment

    config, release = Path(config).absolute(), Path(release).absolute()
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("activation preparation deadline expired")
    receipt = read_json(release / "runtime.json")[0]
    if receipt["python"] != sys.executable:
        raise ValueError("candidate preparation must run in its exact installed runtime")
    scripts = Path(__file__).resolve().parent
    legacy = expected_binding is not None and "component_commands" not in expected_binding
    with update_lock(config, blocking=blocking):
        old = read_json(config)[0]
        current_binding = runtime_binding(old)
        comparison = ({key: value for key, value in current_binding.items()
                       if key != "component_commands"} if legacy else current_binding)
        if expected_binding is not None and comparison != expected_binding:
            raise InstallationChanged("installation binding changed before activation")
        old_engine = config_at(old["engine_config"])
    if legacy:
        _validate_legacy_runtime_floor(old)
    if "component_updates" in receipt["validation"]:
        from mindie_knowledge.component_bindings import validate_command_floor
        validate_command_floor(old, receipt["validation"]["component_updates"]["runtime"])
    digest = hashlib.sha256(os.fsencode(str(config))).hexdigest()
    path = release / ("profile-" + digest + ".engine.json")
    engine = dict(old_engine)
    engine.update(capture_prepare(sys.executable, scripts))
    engine.update(transcript_adapter=str(scripts / "codex_transcript.py"),
                  product_validation=receipt["validation"])
    adapter = dict(old, python=sys.executable, runtime_scripts=str(scripts),
                   engine_config=str(path), product_validation=receipt["validation"])
    if "component_commands" in old and "component_updates" in receipt["validation"]:
        from mindie_knowledge.component_bindings import rebind_commands
        adapter["component_commands"] = rebind_commands(
            old, sys.executable, receipt["validation"].get("component_updates", {}).get("runtime", {}))
    adapter["component_release"] = (str(release) if "component_updates" in receipt["validation"] else None)
    engine["service_launch_context"] = service_launch_context(config, adapter)
    from capture_config import prepare_domains
    from consent import consent_path_for
    adapter['domain_configs'] = prepare_domains(
        sys.executable, adapter, engine, config_directory=str(release / ('profile-' + digest + '.domains')),
        authority_directory=str(config.parent / 'knowledge-domains'),
        consent_config=str(consent_path_for(config)), public_feeds=bool(old_engine.get('feeds')),
        domains=official_feeds())
    validate(engine)
    required_environment(engine["service_launch_context"]["paths"].get("CODEX_HOME"))
    state_root(engine["root"], engine["domain"])
    validate_config_feed_layout(engine)
    if path.exists():
        if read_json(path)[0] != engine:
            raise ValueError("immutable candidate engine differs from its prepared binding")
    else:
        from setup import write_private
        write_private(path, engine)
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("activation preparation deadline expired")
    return dict(adapter=adapter, expected_binding=current_binding)


def binding_matches_release(config, release, receipt, *, scripts=None):
    """True only when canonical already carries this exact prepared release."""
    config, release = Path(config).absolute(), Path(release).absolute()
    scripts = Path(__file__).resolve().parent if scripts is None else Path(scripts).absolute()
    adapter = read_json(config)[0]
    validation = receipt.get("validation")
    if not isinstance(validation, dict) or receipt.get("python") != sys.executable:
        return False
    digest = hashlib.sha256(os.fsencode(str(config))).hexdigest()
    expected_engine = release / ("profile-" + digest + ".engine.json")
    expected_component = str(release) if "component_updates" in validation else None
    matches = (adapter.get("python") == sys.executable
            and adapter.get("runtime_scripts") == str(scripts)
            and adapter.get("engine_config") == str(expected_engine)
            and adapter.get("product_validation") == validation
            and adapter.get("component_release") == expected_component
            and Path(expected_engine).is_file())
    if not matches or "component_updates" not in validation:
        return matches
    from mindie_knowledge.component_bindings import managed_commands, rebind_commands
    components = validation["component_updates"]["runtime"]
    if any(name not in components for name, _python, _module in managed_commands(adapter).values()):
        return False
    return adapter.get("component_commands", {}) == rebind_commands(adapter, sys.executable, components)


def activate(config, prepared, *, control_seconds=CONTROL_SECONDS):
    """Only the update path calls this; loaded facades never select a release."""
    from mindie_knowledge.loop.cli import config_at
    from mindie_knowledge.loop.handoff import _probe
    from mindie_knowledge.loop.lifecycle import inspect_retirement, retire_service

    if (type(control_seconds) not in (int, float) or not math.isfinite(control_seconds)
            or control_seconds <= 0):
        raise ValueError("activation control window must be finite and positive")
    config = Path(config)
    started = time.monotonic()
    deadline = started + control_seconds
    stage, state = "installation_lock", None
    retired = None

    def incomplete(**details):
        return dict(status="incomplete", stage=stage, service=state,
                    unavailable_seconds=time.monotonic() - started, **details)

    def probe(engine, value):
        return _probe(value, min(1., max(.01, deadline - time.monotonic())),
                      config_path=engine)

    try:
        with update_lock(config, exclusive=True):
            stage = "binding_read"
            current = read_json(config)[0]
            target = prepared["adapter"]
            if runtime_binding(current) == runtime_binding(target):
                state = probe(Path(target["engine_config"]), config_at(target["engine_config"]))
                return (dict(status="active", unavailable_seconds=0)
                        if state in {"ready", "absent"} else incomplete(binding_published=True))
            if runtime_binding(current) != prepared["expected_binding"]:
                return dict(status="deferred", reason="installation_changed", execution="not_started")
            old_engine, new_engine = Path(current["engine_config"]), Path(target["engine_config"])
            new_config = config_at(new_engine)
            retired = inspect_retirement(old_engine)
            if retired is not None:
                state = probe(new_engine, new_config)
                # A newly ready owner holds the same consumer lock: retiring
                # the previous owner again could remove the new discovery.
                if state not in {"ready", "absent"}:
                    stage = "new_owner_ready"
                    return incomplete()
            if state != "ready":
                stage = "old_owner_retirement"
                result = retire_service(old_engine, control_timeout=max(0, deadline - time.monotonic()))
                if result.get("idle") is False:
                    return dict(status="deferred", reason="service_busy", execution="not_started")
                if result.get("cleanup_failed"):
                    return incomplete(retirement=result)
                state = probe(new_engine, new_config)
            # The candidate already passed its isolated API probe. Updating
            # software never starts a knowledge worker; a granted session's
            # next operation starts its selected owner through normal admission.
            if state not in {"ready", "absent"}:
                stage = "new_owner_readiness"
                return incomplete()
            from mindie_knowledge.loop.domain_catalog import retire_changed, restore_changed
            stage = 'domain_retirement'
            domains = retire_changed(current, target, timeout=max(0, deadline - time.monotonic()))
            if domains['status'] != 'retired':
                return incomplete(domains=domains)
            # Preserve mutable profile fields that may have changed during
            # background preparation; only the complete runtime binding moves.
            value = dict(current, **runtime_binding(target))
            stage = "binding_publish"
            try:
                _atomic(config, value)
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                return incomplete(error_type=type(exc).__name__,
                                  domain_restoration=restore_changed(domains, timeout=max(0, deadline - time.monotonic())))
            return dict(status="active", unavailable_seconds=time.monotonic() - started)
    except BlockingIOError:
        if stage == "installation_lock":
            return dict(status="deferred", reason="operation_busy", execution="not_started")
        return incomplete(error_type="BlockingIOError")
    except TimeoutError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        # The candidate's ready observation survives a separate local write
        # failure. Never convert this to not_started or restore the old owner.
        return incomplete(error_type=type(exc).__name__)


def _attempt_expected(stream=None):
    stream = sys.stdin.buffer if stream is None else stream
    raw = stream.read(65537)
    if not raw or len(raw) > 65536:
        raise ValueError("activation attempt input is missing or oversized")
    value = json.loads(raw)
    if (not isinstance(value, dict) or value.get("schema") != ATTEMPT_SCHEMA
            or not isinstance(value.get("expected_binding"), dict)):
        raise ValueError("activation attempt input is invalid")
    return value["expected_binding"]


def _reconcile_published_target(config, release, receipt):
    if binding_matches_release(config, release, receipt):
        current = read_json(config)[0]
        return activate(config, dict(adapter=current,
                                     expected_binding=runtime_binding(current)))
    return dict(status="deferred", reason="installation_changed", execution="not_started")


def main(argv=None, stdin=None):
    argv = list(sys.argv if argv is None else argv)
    config, release = Path(argv[1]), Path(argv[2])
    extra = argv[3:]
    if extra and extra != ["--attempt"]:
        raise ValueError("activation requires the --attempt snapshot input")
    attempt = extra == ["--attempt"]
    expected = _attempt_expected(stdin) if attempt else None
    blocking = not attempt
    receipt = read_json(release / "runtime.json")[0]
    try:
        prepared = prepare(config, release, blocking=blocking, expected_binding=expected)
    except InstallationChanged:
        return _reconcile_published_target(config, release, receipt)
    except BlockingIOError:
        return dict(status="deferred", reason="operation_busy", execution="not_started")
    return activate(config, prepared)


if __name__ == "__main__":
    print(json.dumps(main(), sort_keys=True))
