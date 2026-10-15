"""Prepare a complete runtime binding, then publish it after exact idle handoff."""
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

from product_contract import read_json
from session_gate import runtime_binding
from update_lock import update_lock

CONTROL_SECONDS = 30.0


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


def prepare(config, release):
    """No running service or canonical configuration is changed here."""
    from capture_config import prepare as capture_prepare
    from native_context import service_launch_context
    from mindie_knowledge.loop.cli import config_at
    from mindie_knowledge.loop.service_context import validate
    from mindie_knowledge.state_layout import state_root
    from mindie_knowledge.git_layout import validate_config_feed_layout
    from summary_provider import required_environment

    config, release = Path(config).absolute(), Path(release).absolute()
    receipt = read_json(release / "runtime.json")[0]
    if receipt["python"] != sys.executable:
        raise ValueError("candidate preparation must run in its exact installed runtime")
    scripts = Path(__file__).resolve().parent
    with update_lock(config, blocking=True):
        old = read_json(config)[0]
        old_engine = config_at(old["engine_config"])
    digest = hashlib.sha256(os.fsencode(str(config))).hexdigest()
    path = release / ("profile-" + digest + ".engine.json")
    engine = dict(old_engine)
    engine.update(capture_prepare(sys.executable, scripts))
    engine.update(transcript_adapter=str(scripts / "codex_transcript.py"),
                  product_validation=receipt["validation"])
    adapter = dict(old, python=sys.executable, runtime_scripts=str(scripts),
                   engine_config=str(path), product_validation=receipt["validation"])
    engine["service_launch_context"] = service_launch_context(config, adapter)
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
    return dict(adapter=adapter, expected_binding=runtime_binding(old))


def activate(config, prepared, *, control_seconds=CONTROL_SECONDS):
    """Only the update path calls this; loaded facades never select a release."""
    from mindie_knowledge.loop.cli import config_at, prepare_service
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
                        if state == "ready" else incomplete(binding_published=True))
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
            stage = "new_owner_start"
            if state == "absent":
                if time.monotonic() >= deadline:
                    return incomplete()
                state = prepare_service(new_engine, probe_timeout=deadline - time.monotonic())
            stage = "new_owner_ready"
            while state != "ready":
                if state in {"unknown", "worker-dead", "stopping"} or time.monotonic() >= deadline:
                    return incomplete()
                time.sleep(min(.05, max(0, deadline - time.monotonic())))
                state = probe(new_engine, new_config)
            # Preserve mutable profile fields that may have changed during
            # background preparation; only the complete runtime binding moves.
            value = dict(current, **runtime_binding(target))
            stage = "binding_publish"
            _atomic(config, value)
            return dict(status="active", unavailable_seconds=time.monotonic() - started)
    except BlockingIOError:
        if stage == "installation_lock":
            return dict(status="deferred", reason="operation_busy", execution="not_started")
        return incomplete(error_type="BlockingIOError")
    except (OSError, ValueError, RuntimeError) as exc:
        # The candidate's ready observation survives a separate local write
        # failure. Never convert this to not_started or restore the old owner.
        return incomplete(error_type=type(exc).__name__)


if __name__ == "__main__":
    config, release = Path(sys.argv[1]), Path(sys.argv[2])
    print(json.dumps(activate(config, prepare(config, release)), sort_keys=True))
