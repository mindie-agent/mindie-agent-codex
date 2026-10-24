"""Project one eligible Release into Codex's native installer, then activate it.

[Role]
Validates compatibility with the installed `codex --version` (`harness_version`), projects a single-candidate local marketplace catalog (`catalog`), runs `codex plugin install` (`_native_install`), preserves approved hook trust (`NativeHooks`), and activates the new release (`release_activation.activate`).

[Invariants]
- Never installs a release whose `harness-compatibility.json` does not cover the actual host `codex --version`.
- Carries forward only previously user-approved hook trust entries via compare-and-swap (`NativeHooks`).

[Timeouts & Locks]
- Enforces `timeout=15.0s` for `codex --version`, `timeout=300.0s` for `codex plugin install`, and `timeout=330.0s` for activation (outer 330s >= inner 300s session maintenance + 30s grace).

[Side-Effect Policy]
- Updates the local marketplace catalog, native Codex plugin cache/config, and MindIE release binding only after candidate validation succeeds.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bounded_process import ProcessResult, run
from native_context import capture, environment
from product_contract import native_version, read_json
from release_activation import _atomic
from release_metadata import REPOSITORY
from session_gate import runtime_env
from update_lock import file_lock, update_lock
from update_trigger import directory, due, read_optional

PLUGIN = "mindie-agent@mindie-agent"
MARKETPLACE = "mindie-agent"


def harness_version(native, env):
    value = run([native, "--version"], "", env=env, timeout=15.0).checked_stdout().strip()
    match = re.fullmatch(r"codex-cli (\S+)", value)
    if match is None:
        raise ValueError("native Harness version is unavailable")
    return match.group(1)


def installed(native, env):
    value = json.loads(run([native, "plugin", "list", "--marketplace",
                           MARKETPLACE, "--json"], "", env=env, timeout=30.0).checked_stdout())
    rows = value.get("installed")
    if not isinstance(rows, list):
        raise ValueError("native installed-plugin response is invalid")
    rows = [row for row in rows if row.get("pluginId") == PLUGIN]
    if len(rows) > 1:
        raise ValueError("native plugin installation is ambiguous")
    if not rows:
        return None
    row = rows[0]
    if (row.get("installed") is not True or type(row.get("enabled")) is not bool
            or not isinstance(row.get("version"), str)):
        raise ValueError("native installed-plugin identity is invalid")
    return row


def catalog(metadata):
    """The local catalog is derived input, never a second release selector."""
    return dict(name=MARKETPLACE, plugins=[dict(
        name="mindie-agent", source=dict(source="git-subdir",
            url="https://github.com/" + REPOSITORY + ".git",
            path="plugins/mindie-agent", sha=metadata["source"]["sha"]),
        policy=dict(installation="AVAILABLE", authentication="ON_INSTALL"),
        category="Productivity")])


def _process_fact(result):
    if not isinstance(result, ProcessResult):
        return dict(execution="unknown", returncode=None, cleanup_failed=None)
    return dict(execution=result.execution, returncode=result.returncode,
                cleanup_failed=bool(result.cleanup))


def _native_mutation(path, pending, action, argv, env):
    """Persist one intent before native mutation; unknown never means retry."""
    pending.update(stage=action, process=None)
    _atomic(path, pending)
    result = None
    try:
        result = run(argv, "", env=env, timeout=300.0)
        output = json.loads(result.checked_stdout())
        if not isinstance(output, dict):
            raise ValueError("native administrative result is not an object")
    except Exception as exc:
        pending["process"] = _process_fact(getattr(exc, "process_result", result))
        pending["error_type"] = type(exc).__name__
        exc.native_effect = dict(stage=action, process=pending["process"])
        try:
            _atomic(path, pending)
        except TimeoutError:
            raise
        except (OSError, ValueError) as record_error:
            exc.receipt_error_type = type(record_error).__name__
            exc.add_note("Native operation receipt also failed: " + type(record_error).__name__)
        raise
    pending.update(process=_process_fact(result), native_output=output)
    try:
        _atomic(path, pending)
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        exc.native_effect = dict(stage=action, process=pending["process"])
        exc.receipt_error_type = type(exc).__name__
        raise
    return output


def _complete_install(native, env, pending):
    metadata, output = pending["metadata"], pending["native_output"]
    expected = Path(env["CODEX_HOME"]) / "plugins/cache/mindie-agent/mindie-agent" / metadata["version"]
    if (output.get("pluginId") != PLUGIN or output.get("version") != metadata["version"]
            or output.get("installedPath") != str(expected)):
        raise ValueError("native install did not return the selected plugin identity")
    current = installed(native, env)
    if current is None or current["version"] != metadata["version"] or not current["enabled"]:
        raise ValueError("native installed readback differs from the selected Release")
    if (native_version(expected) != metadata["version"]
            or read_json(expected / "harness-compatibility.json")[0] != metadata["compatibility"]):
        raise ValueError("installed payload declaration differs from the verified Release")
    return dict(pending, stage="installed", source=str(expected))


def _select_catalog(native, env, root, pending):
    from native_hooks import NativeHooks
    pending.update(stage="catalog_select", process=None, writer_settled=False)
    _atomic(root / "native.json", pending)
    try:
        with NativeHooks(native, env) as api:
            outcome = api.select_catalog(root / "catalog")
    except Exception as exc:
        pending["error_type"] = type(exc).__name__
        process = getattr(exc, "process_result", None)
        pending["process"] = _process_fact(process)
        pending["writer_settled"] = getattr(exc, "native_writer_settled", False) is True
        try:
            _atomic(root / "native.json", pending)
        except TimeoutError:
            raise
        except (OSError, ValueError) as record_error:
            exc.receipt_error_type = type(record_error).__name__
        raise
    pending.update(stage="catalog_selected", catalog=outcome,
                   process=dict(execution="completed", returncode=0, cleanup_failed=False))
    _atomic(root / "native.json", pending)


def _native_install(native, env, root, metadata, before):
    pending_path = root / "native.json"
    catalog_root = root / "catalog"
    target = catalog_root / ".agents/plugins/marketplace.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if before is not None:
        source = before.get("marketplaceSource")
        if source not in (
                dict(sourceType="local", source=str(catalog_root)),
                dict(sourceType="git", source="https://github.com/" + REPOSITORY + ".git")):
            raise ValueError("native marketplace is not the selected-release catalog")
        if before["enabled"] is False:
            return None
    from native_hooks import snapshot
    approved = snapshot(native, env,
        Path(env["CODEX_HOME"]) / "plugins/cache/mindie-agent/mindie-agent" / before["version"]
    ) if before is not None else []
    pending = dict(schema="mindie-native-install/1", metadata=metadata,
                   before=before, stage="selected", approved_hooks=approved)
    _atomic(pending_path, pending)
    _atomic(target, catalog(metadata))
    if before is not None and before["marketplaceSource"]["sourceType"] == "git":
        _select_catalog(native, env, root, pending)
    if before is None:
        _native_mutation(pending_path, pending, "marketplace_add",
            [native, "plugin", "marketplace", "add", str(catalog_root), "--json"], env)
    _native_mutation(pending_path, pending, "plugin_add",
                     [native, "plugin", "add", PLUGIN, "--json"], env)
    pending = _complete_install(native, env, pending)
    _atomic(pending_path, pending)
    return pending


def _prepare_and_activate(config, pending, env, root, *, first_install=False):
    previous_activation = pending.get("activation")
    if (isinstance(previous_activation, dict)
            and (previous_activation.get("status") == "unknown"
                 or "process" in previous_activation)
            and (previous_activation.get("process") or {}).get("cleanup_failed") is not False):
        return dict(status="unknown", stage="activation_writer",
                    activation=previous_activation)
    source = Path(pending["source"])
    if (native_version(source) != pending["metadata"]["version"]
            or read_json(source / "harness-compatibility.json")[0] != pending["metadata"]["compatibility"]):
        raise ValueError("installed candidate source declaration changed")
    data = Path(env["CODEX_HOME"]) / "plugins/data/mindie-agent-mindie-agent"
    if "runtime" not in pending:
        pending["preparation"] = dict(execution="unknown", cleanup_failed=None)
        _atomic(root / "native.json", pending)
        code = (
            "import sys,json;from pathlib import Path;"
            "sys.path.insert(0,str(Path(sys.argv[1])/'scripts'));"
            "from bootstrap import ensure_runtime;"
            "p,s,r=ensure_runtime(sys.argv[1],sys.argv[2]);"
            "print(json.dumps({'python':str(p),'scripts':str(s),'release':str(r)}))")
        result = None
        try:
            result = run(["uv", "run", "--no-project", "--python", "3.13", "python",
                          "-I", "-c", code, str(source), str(data)], "", env=env, timeout=1800.0)
            runtime = json.loads(result.checked_stdout())
            if (not isinstance(runtime, dict) or set(runtime) != {"python", "scripts", "release"}
                    or any(not isinstance(value, str) or not Path(value).is_absolute()
                           for value in runtime.values())):
                raise ValueError("prepared runtime response is invalid")
        except Exception as exc:
            fact = _process_fact(getattr(exc, "process_result", result))
            pending["preparation"] = fact
            if fact.get("cleanup_failed") is True:
                pending["stage"] = "runtime_prepare_unknown"
            exc.native_effect = dict(stage="runtime_prepare", process=fact)
            try:
                _atomic(root / "native.json", pending)
            except TimeoutError:
                raise
            except (OSError, ValueError) as record_error:
                exc.receipt_error_type = type(record_error).__name__
            raise
        pending["runtime"] = runtime
        pending["preparation"] = _process_fact(result)
        pending["stage"] = "prepared"
        pending["activation"] = dict(status="not_started")
        _atomic(root / "native.json", pending)
    if "hook_trust" not in pending:
        from native_hooks import preserve
        try:
            pending["hook_trust"] = preserve(env["MINDIE_CODEX_BIN"], env, source,
                                             pending.get("approved_hooks", []))
        except Exception as exc:
            process = getattr(exc, "process_result", None)
            if isinstance(process, ProcessResult) and process.cleanup:
                pending.update(stage="hook_trust_unknown", process=_process_fact(process))
                try:
                    _atomic(root / "native.json", pending)
                except TimeoutError:
                    raise
                except (OSError, ValueError) as record_error:
                    exc.add_note("Hook management receipt also failed: " + type(record_error).__name__)
                    exc.receipt_error_type = type(record_error).__name__
            raise
        _atomic(root / "native.json", pending)
    runtime = pending["runtime"]
    if first_install:
        argv = [runtime["python"], "-I", str(Path(runtime["scripts"]) / "entry.py"),
                "--release", runtime["release"], "session-start"]
    else:
        argv = [runtime["python"], "-I", str(Path(runtime["scripts"]) / "release_activation.py"),
                str(config), runtime["release"]]
    pending["activation"] = dict(status="unknown", process=None)
    _atomic(root / "native.json", pending)
    result, value = None, None
    try:
        result = run(argv, "", env=env, allow_service=True, timeout=330.0)
        value = dict(status="active") if first_install else json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("activation control response is invalid")
        result.checked_stdout()
    except Exception as exc:
        result = getattr(exc, "process_result", result)
        pending["activation"] = dict(value or {"status": "unknown"},
                                     process=_process_fact(result))
        exc.native_effect = dict(stage="runtime_activation", activation=pending["activation"])
        try:
            _atomic(root / "native.json", pending)
        except TimeoutError:
            raise
        except (OSError, ValueError) as record_error:
            exc.receipt_error_type = type(record_error).__name__
        raise
    pending["activation"] = dict(value, process=_process_fact(result))
    try:
        _atomic(root / "native.json", pending)
    except TimeoutError:
        raise
    except (OSError, ValueError) as exc:
        exc.native_effect = dict(stage="runtime_activation", activation=pending["activation"])
        exc.receipt_error_type = type(exc).__name__
        raise
    return value


def _can_supersede(pending):
    settled = pending.get("process") == dict(
        execution="completed", returncode=0, cleanup_failed=False)
    preparation = pending.get("preparation")
    activation = pending.get("activation", {})
    control_settled = ("process" not in activation
                       or (activation["process"] or {}).get("cleanup_failed") is False)
    return settled and control_settled and (preparation is None or preparation.get("cleanup_failed") is not True) and (
        pending.get("stage") == "installed"
        or (pending.get("stage") == "prepared"
            and (pending.get("activation") == {"status": "not_started"}
                 or pending.get("activation", {}).get("execution") == "not_started"))
    )


def check(config, *, first_install=False, force=False):
    from mindie_knowledge.releases import select_release, supports_harness, validate_release_metadata
    from mindie_knowledge.releases_io import fetch_releases, verify_release_source

    if force:
        os.environ["MINDIE_RELEASE_FORCE"] = "1"
    config = Path(config).expanduser().absolute()
    root = directory(config)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        with file_lock(root / "check.lock", exclusive=True):
            if not first_install and not force and not due(config):
                return dict(status="throttled")
            if first_install:
                env = dict(os.environ, **capture())
                env["MINDIE_AGENT_CONFIG"] = str(config)
            else:
                with update_lock(config):
                    adapter = read_json(config)[0]
                    env = runtime_env(config)
                    env.update(environment(adapter))
            native = env["MINDIE_CODEX_BIN"]
            version = harness_version(native, env)
            status_path = root / "status.json"
            previous = read_optional(status_path) or {}
            status = dict(checked_at=time.time(),
                          last_successful_check=previous.get("last_successful_check", 0))
            stage = "installed_readback"
            try:
                before = installed(native, env)
                if before is not None and before["enabled"] is False:
                    status.update(status="disabled")
                else:
                    pending = read_optional(root / "native.json")
                    if pending is not None and pending.get("stage") in {"active", "superseded"}:
                        pending = None
                    if pending is not None:
                        if (pending.get("stage") == "installed"
                                and pending.get("preparation", {}).get("cleanup_failed") is True):
                            status.update(status="unknown", stage="runtime_prepare",
                                          process=pending["preparation"])
                            _atomic(status_path, status)
                            return status
                        if (pending.get("stage") == "catalog_select"
                                and pending.get("writer_settled") is True):
                            # This single-key CAS is idempotent administrative
                            # control. A settled caller permits exact readback;
                            # an interrupted or live writer never enters here.
                            _select_catalog(native, env, root, pending)
                        metadata = validate_release_metadata(
                            pending["metadata"], repository=REPOSITORY, harness_name="codex")
                        if not supports_harness(metadata["compatibility"],
                                                harness_name="codex", harness_version=version):
                            if not _can_supersede(pending):
                                raise ValueError("incompatible pending candidate has unresolved activation")
                            # Preserve the previous settled attempt as evidence,
                            # without using it as a release-selection authority.
                            _atomic(root / ("superseded-" + str(time.time_ns()) + ".json"), pending)
                            pending["stage"] = "superseded"
                            _atomic(root / "native.json", pending)
                            pending = None
                    if pending is not None:
                        if (pending.get("stage") == "plugin_add"
                                and pending.get("process") == dict(
                                    execution="completed", returncode=0, cleanup_failed=False)):
                            pending = _complete_install(native, env, pending)
                            _atomic(root / "native.json", pending)
                        if (pending.get("stage") == "marketplace_add"
                                and pending.get("process") == dict(
                                    execution="completed", returncode=0, cleanup_failed=False)):
                            if pending.get("native_output", {}).get("marketplaceName") != MARKETPLACE:
                                raise ValueError("native marketplace receipt differs from the selection")
                            # Only the completed first administrative operation
                            # is reused. An unknown marketplace write stays
                            # unknown; no registration is repeated here.
                            stage = "native_install"
                            _native_mutation(root / "native.json", pending, "plugin_add",
                                [native, "plugin", "add", PLUGIN, "--json"], env)
                            pending = _complete_install(native, env, pending)
                            _atomic(root / "native.json", pending)
                        if pending.get("stage") == "selected":
                            stage = "native_install"
                            pending = _native_install(native, env, root, metadata, before)
                        if pending.get("stage") == "catalog_selected":
                            if pending.get("process") != dict(
                                    execution="completed", returncode=0, cleanup_failed=False):
                                raise ValueError("native catalog selection writer is not settled")
                            stage = "native_install"
                            _native_mutation(root / "native.json", pending, "plugin_add",
                                [native, "plugin", "add", PLUGIN, "--json"], env)
                            pending = _complete_install(native, env, pending)
                            _atomic(root / "native.json", pending)
                        if pending.get("stage") not in {"installed", "prepared"}:
                            # A matching path cannot prove an unknown writer
                            # settled or that native's checkout check completed.
                            status.update(status="unknown", stage=pending.get("stage"),
                                          process=pending.get("process"))
                            _atomic(status_path, status)
                            return status
                        current = installed(native, env)
                        if current is None or current["version"] != metadata["version"]:
                            raise ValueError("native installation changed before candidate activation")
                    if pending is None:
                        stage = "release_selection"
                        selected = select_release(fetch_releases(REPOSITORY,
                            minimum_version=before["version"] if before is not None else None),
                            repository=REPOSITORY, harness_name="codex", harness_version=version,
                            installed_version=before["version"] if before is not None else None)
                        if selected["status"] != "candidate":
                            if before is not None:
                                from component_update import check as component_check
                                source = (Path(env["CODEX_HOME"]) / "plugins/cache/mindie-agent/mindie-agent"
                                          / before["version"])
                                data = Path(env["CODEX_HOME"]) / "plugins/data/mindie-agent-mindie-agent"
                                status["components"] = component_check(config, source, data, force=force)
                            status["last_successful_check"] = time.time()
                            status.update(selected)
                            _atomic(status_path, status)
                            return status
                        stage = "release_source"
                        metadata = verify_release_source(selected["candidate"]["metadata"],
                            repository=REPOSITORY, harness_name="codex")
                        status["last_successful_check"] = time.time()
                        stage = "native_install"
                        pending = _native_install(native, env, root, metadata, before)
                    stage = "runtime_activation"
                    status.update(_prepare_and_activate(config, pending, env, root,
                                                        first_install=first_install))
                    if status["status"] == "active":
                        pending["stage"] = "active"
                        _atomic(root / "native.json", pending)
                _atomic(status_path, status)
                return status
            except Exception as exc:
                status.update(status="failed", stage=stage, error_type=type(exc).__name__,
                              code=getattr(exc, "code", None))
                if getattr(exc, "native_effect", None) is not None:
                    status["native_effect"] = exc.native_effect
                if getattr(exc, "receipt_error_type", None) is not None:
                    status["receipt_error_type"] = exc.receipt_error_type
                try:
                    _atomic(status_path, status)
                except TimeoutError:
                    raise
                except (OSError, ValueError) as record_error:
                    status["status_receipt_error_type"] = type(record_error).__name__
                return status
    except BlockingIOError:
        return dict(status="deferred", reason="check_busy", execution="not_started")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--force", action="store_true", help="refresh release metadata now; idle and compatibility gates still apply")
    args = parser.parse_args()
    print(json.dumps(check(args.config, first_install=args.install, force=args.force), sort_keys=True))
