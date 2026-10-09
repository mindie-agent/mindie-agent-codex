#!/usr/bin/env python3
"""Codex plugin boundary.

The Stop hook path is a no-op unless every capture precondition holds:
an active lease and community sharing enabled. Scope is the lease's project
root, not the event cwd. Sharing off means no capture row and no wake.
A passing Stop commits through the shared handoff; it does not claim an
attempt or report forwarded. The hook never parses transcripts, never
blocks the original task and never uses exit-2 continuation.

Explicit operator entries also cover unified offline status, service
shutdown and the deterministic core contribution-recovery operations
(contribution-inspect/-reconcile/-retry/-compact); none of them starts a
service, a model or an uncertain write.
"""

import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time

from bounded_process import run
from session_gate import (
    Inactive,
    Sessions,
    bind_explicit_config,
    config_path,
    generation_env,
    runtime_scripts,
)
import sharing
from update_lock import update_lock

OPERATIONS = {
    "stop",
    "mcp",
    "status",
    "init",
    "shutdown",
    "activate",
    "deactivate",
    "config",
    "sharing-enable",
    "sharing-disable",
    "sharing-status",
    "sharing-choice",
    "reporting-status",
    "reporting-enable",
    "reporting-disable",
    "reporting-ensure",
    "reporting-maintain",
    "history-import",
}
# Deterministic core recovery surface (documented exact names; each takes one
# existing contribution batch id and never reruns organizer/model work).
CONTRIBUTION_OPERATIONS = {
    "contribution-inspect",
    "contribution-reconcile",
    "contribution-retry",
    "contribution-compact",
}
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
BATCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
# Includes JSON escaping of the native final-answer copy; only references
# are forwarded. This byte cap bounds memory without imposing a run deadline.
MAX_HOOK_BYTES = 32 * 1024 * 1024


def _bounded_path(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {name}")
    if not os.path.isabs(value):
        raise ValueError(f"{name} must be absolute")
    return value


_HOOK_TOKEN = re.compile(rb'["{}\[\]]')


def _read_hook_stdin():
    """Frame one byte-bounded event; wait for the caller while it owns us."""
    buf = bytearray()
    finished = threading.Event()
    errors = []
    owner = os.getppid()

    def reader():
        try:
            fd = sys.stdin.fileno()
            depth = 0
            quoted = escaped = False
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return
                buf.extend(chunk)
                if len(buf) > MAX_HOOK_BYTES:
                    raise ValueError('hook event exceeds its byte bound')
                pos = int(escaped)
                escaped = False
                while pos < len(chunk):
                    if quoted:
                        quote = chunk.find(b'"', pos)
                        if quote < 0:
                            if chunk.endswith(b'\\'):
                                tail = len(chunk) - max(pos, len(chunk.rstrip(b'\\')))
                                escaped = bool(tail % 2)
                            break
                        before = quote
                        while before > pos and chunk[before - 1] == 92:
                            before -= 1
                        if (quote - before) % 2 == 0:
                            quoted = False
                        pos = quote + 1
                    else:
                        match = _HOOK_TOKEN.search(chunk, pos)
                        if match is None:
                            break
                        pos = match.end()
                        token = match.group()
                        if token == b'"':
                            quoted = True
                        elif token in (b'{', b'['):
                            depth += 1
                        else:
                            depth -= 1
                            if depth <= 0:
                                return
        except (OSError, ValueError) as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    while not finished.wait(0.1):
        if os.name == 'posix' and os.getppid() != owner:
            raise ConnectionError('native hook owner exited')
    if errors:
        raise errors[0]
    return bytes(buf)


def hook_event(raw):
    """Validate native identity and forward only the transcript reference.

    A valid transcript event is never rejected for a missing final summary:
    transcript_path alone is enough. The transcript itself is never opened
    here. The optional final-answer copy has no role in transcript capture.
    """
    event = json.loads(raw)
    if not isinstance(event, dict) or event.get("hook_event_name") != "Stop":
        raise ValueError("unexpected hook event")
    for key in ("session_id", "turn_id"):
        if not isinstance(event.get(key), str) or not IDENTITY.fullmatch(event[key]):
            raise ValueError("invalid hook identity")
    if event.get("stop_hook_active", False) is not False:
        raise ValueError("recursive Stop is not a capture")
    cwd = _bounded_path(event.get("cwd"), "cwd")
    transcript = _bounded_path(event.get("transcript_path"), "transcript_path")
    forwarded = dict(
        hook_event_name="Stop",
        identity_kind="turn",
        session_id=event["session_id"],
        turn_id=event["turn_id"],
        cwd=cwd,
    )
    forwarded["transcript_path"] = transcript
    return forwarded


def bind(lease):
    """Attach this lease's session to the domain store via one bounded call.

    Only runs when community sharing is enabled: activation prepares the
    admitted local collection endpoint, and a failed bind leaves the lease
    usable for read tools while marking capture unbound. It is never retried
    in the background, and sharing off never cold-starts collection here.
    """
    payload = dict(
        surface="knowledge",
        name="knowledge_attach",
        internal=True,
        arguments={},
        mindie_session_id=lease["mindie_session_id"],
        mindie_activation=lease["mindie_activation"],
    )
    try:
        config_file = config_path()
        with update_lock(config_file):
            config = json.loads(config_file.read_text(encoding='utf-8'))
            output = run(
                [
                    config["python"],
                    str(Path(runtime_scripts(config)) / "runtime_call.py"),
                ],
                json.dumps(payload),
                timeout=None,
                env=generation_env(config_file),
                allow_service=True,
            ).checked_stdout()
        result = json.loads(output)
        if isinstance(result, dict) and result.get("isError") is not True:
            return "bound"
        return "unbound:service-error"
    except Exception as exc:
        return f"unbound:{type(exc).__name__}"


def _entry_migration(config_file):
    """One-time convergence at the explicit entry-attach boundary.

    Imports validated legacy consent evidence into the profile authority and
    converges the community settings path; both are idempotent. A failure is
    reported in the activation result (capture stays fail-closed), never
    hidden and never blocking the read-only binding itself.
    """
    import consent

    notes = {}
    try:
        migrated = consent.migrate_legacy(config_file)
        if migrated.get("status") not in {"kept", "absent"}:
            notes["consent"] = migrated
    except Exception as exc:
        notes["consent"] = dict(status="failed", error=type(exc).__name__)
    try:
        moved = sharing.migrate_community_path(config_file)
        if moved.get("status") != "current" or moved.get("detail"):
            notes["community"] = moved
    except Exception as exc:
        notes["community"] = dict(status="failed", error=type(exc).__name__)
    return notes or None


def _generation_identity():
    """The actually running plugin generation, for verifiable binding proof.

    ``scripts`` is the resolved directory of this running bridge.py; ``build``
    carries the packaged generation's revision/version stamp when present.
    A test profile can check these against the selected candidate instead of
    trusting whichever stale cache copy a host or model happened to open.
    """
    scripts = Path(__file__).resolve().parent
    build = None
    stamp = scripts / "diagnostic-build.json"
    try:
        data = json.loads(stamp.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            build = {
                key: data[key]
                for key in ("revision", "version")
                if isinstance(data.get(key), str)
            } or None
    except (OSError, ValueError):
        build = None
    return scripts, build


def activate(operation):
    migration = None
    if operation == "activate":
        migration = _entry_migration(config_path())
    result = getattr(Sessions(), operation)()
    if operation != "activate":
        return result
    scripts, build = _generation_identity()
    result["scripts"] = str(scripts)
    if build:
        result["build"] = build
    if migration:
        result["migration"] = migration
    return _prepare_capture(result)


def _prepare_capture(result):
    """Report the effective loop state for this already verified binding."""
    try:
        settings = sharing.read()
        view = sharing.status()
        if (
            settings is not None
            and settings["enabled"]
            and sharing.consent_allows(settings) is not False
        ):
            if sharing.capture_allowed(dict(result, root_session=result["mindie_session_id"]), result.get("project_root")):
                result["capture"] = bind(result)
            else:
                result["capture"] = "out-of-scope"
        else:
            # Sharing off/unconfigured or consent-blocked: ordinary activation
            # only. No cold start, no bind, no collection preparation.
            result["capture"] = "disabled"
    except (OSError, ValueError) as exc:
        # Binding may already exist. Preserve its receipt and migration details
        # while reporting that required capture preparation did not complete.
        return dict(result, status="degraded", capture="unavailable", experience="unavailable",
                    error=dict(stage="capture-configuration", type=type(exc).__name__),
                    next="Inspect the existing capture configuration; do not activate again to repair it.")
    result["sharing"] = view
    result["experience"] = (
        "capture-ready" if result["capture"] == "bound"
        else "out-of-scope" if result["capture"] == "out-of-scope"
        else "unavailable" if result["capture"].startswith("unbound:")
        else "disabled" if view["state"] == "disabled"
        else "unavailable" if view["state"] == "malformed"
        else "needs-configuration"
    )
    if result["experience"] == "needs-configuration":
        result["next"] = sharing.CHOICES
    elif result["experience"] == "out-of-scope":
        result["next"] = "This task is outside the configured project scope; capture is not active."
    elif result["experience"] == "unavailable":
        result["next"] = "Capture could not be prepared; inspect status. Task binding alone does not establish capture readiness."
    return result


def unconfigured_status():
    """Stdlib-only offline first-use payload. Creates no files or services.

    A profile that already holds a saved choice (e.g. from a sibling adapter)
    is an existing installation: setup reuses the choice, no onboarding."""
    import consent

    saved = consent.load()
    first_use = dict(
        state="unconfigured",
        prompt=sharing.CHOICES,
        choices=[],
        required=["runtime", "repository", "project_roots", "public_visibility"],
    )
    if saved["state"] == "ok" and saved["choice"]:
        first_use = dict(state="chosen", choice=saved["choice"])
    elif saved["state"] in {"corrupt", "unreadable"} or consent.install_traces():
        first_use = dict(state="existing", detail=saved.get("error"))
    return dict(
        configured=False,
        experience="needs-configuration",
        sharing=dict(state="unconfigured"),
        first_use=first_use,
        next=(
            "Run scripts/setup.py install --knowledge-python PYTHON "
            "(headless leaves sharing off). The saved install-level choice is "
            "reused; configure contribution via setup.py configure "
            "--community-repository OWNER/REPO --community-project-root PATH "
            "--community-visibility public. Installation alone does not enable "
            "the experience loop. Reuse existing approved values."
        ),
        recovery=[],
        service=dict(state="not-running"),
        diagnostics=_reporting_choice(),
    )


def _status_failure(state, stage, exc, config_file, selected=None):
    """Local operator diagnostic; never print helper stderr or config values."""
    python = selected["python"] if selected else sys.executable
    scripts = Path(runtime_scripts(selected)) if selected else Path(__file__).parent
    commands = {
        "status": [python, str(scripts / "bridge.py"), "--config", str(config_file), "status"],
        "check_config_json": [
            sys.executable, "-c",
            "import json,pathlib,sys; json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); print('JSON syntax valid')",
            str(config_file),
        ],
    }
    recovery = {
        "invalid_config": "Inspect the named config and correct its JSON or required absolute runtime paths, then run status. Existing state has not been reinitialized.",
        "update_busy": "An update holds the generation lock. Let that operation finish, then explicitly run status; installation is not missing.",
        "helper_failed": "Inspect the selected runtime and the reported helper stage, then explicitly run status. No recovery action was started.",
    }
    if selected:
        commands["check_runtime"] = [
            python, "-c",
            "import mindie_knowledge,remote_dev; print('runtime imports available')",
        ]
    result = dict(
        status=state,
        configured=None,
        config=str(config_file),
        first_use=None,
        sharing=dict(state="unknown"),
        service=dict(state="unknown"),
        error=dict(stage=stage, type=type(exc).__name__),
        commands=commands,
        recovery=[recovery[state]],
        next="Use the listed status/check commands. Native shell/SSH or separately configured remote-dev remain available without knowledge activation.",
    )
    # Config and lock contention are expected. Only a failed status helper
    # is recorded; a bad response is a protocol failure.
    if state == "helper_failed" and stage in {"helper_run", "helper_response"}:
        from diagnostic_support import attach, failure

        category = "helper_protocol" if stage == "helper_response" else "helper_failed"
        updated = attach(
            result,
            failure("status", stage, category, exception=exc),
        )
        if isinstance(updated, dict):
            result = updated
    return result


def offline_status():
    """Distinguish missing installation from unreadable or busy existing state."""
    config_file = config_path()
    selected = None
    stage = "config_stat"
    try:
        try:
            config_file.stat()
        except FileNotFoundError:
            return unconfigured_status(), 0
        stage = "update_lock"
        with update_lock(config_file):
            stage = "config_read"
            # A corrupt config path may be a FIFO/device. Do not wait for a
            # writer before the bounded helper is even started.
            fd = os.open(config_file, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("adapter config must be a regular file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read()
                config = json.loads(raw)
            finally:
                os.close(fd)
            stage = "config_validate"
            if not isinstance(config, dict):
                raise ValueError("adapter config must be an object")
            for key in ("python", "engine_config"):
                _bounded_path(config.get(key), key)
            if "runtime_scripts" in config:
                _bounded_path(config["runtime_scripts"], "runtime_scripts")
            selected = config
            stage = "helper_run"
            output = run(
                [config["python"], str(Path(runtime_scripts(config)) / "service_control.py"), "status"],
                "", timeout=None, max_output=32768, env=generation_env(config_file),
            ).checked_stdout()
            stage = "helper_response"
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("status response must be an object")
            return payload, 0
    except Exception as exc:
        if stage == "update_lock" and isinstance(exc, BlockingIOError):
            state = "update_busy"
        elif stage.startswith("config") or stage == "update_lock":
            state = "invalid_config"
        else:
            state = "helper_failed"
        return _status_failure(state, stage, exc, config_file, selected), 1


def _record_stop(stage, category, exc=None):
    """Local diagnostic only. No transcript, token, or exception text."""
    try:
        from diagnostic_support import failure

        failure(
            "capture.stop", stage=stage, category=category,
            exception=exc, reportable=False,
        )
    except Exception:
        pass


def _observe_stop(result):
    if not isinstance(result, dict):
        _record_stop("handoff", "internal")
        return False
    stage = result.get("stage")
    if stage in {"inert", "organized", "no-new-material", "no-shareable-material",
                 "cancelled", "discarded", "processing", "accepted-runtime",
                 "apply-pending", "accepted-local"}:
        return True
    if stage not in {"unavailable", "rejected", "failed"}:
        _record_stop("handoff", "internal")
        return False
    reason = result.get("reason")
    if not isinstance(reason, str) or not reason.replace("-", "").replace("_", "").isalnum():
        reason = "handoff"
    if not reason[:1].isalpha():
        reason = "handoff"
    _record_stop(stage, reason)
    return False


def stop():
    try:
        settings = sharing.read()
        # Missing setup is an Agent diagnostic; an explicit disable is inert.
        # Neither branch reads stdin, starts a helper or creates a capture row.
        if settings is None:
            disabled = sharing.status().get("state") == "disabled"
            if not disabled:
                _record_stop("configuration", "missing_configuration")
            print("{}")
            return 0 if disabled else 1
        if not settings["enabled"] or sharing.consent_allows(settings) is False:
            print("{}")
            return 0
    except (OSError, ValueError) as exc:
        _record_stop("configuration", "unavailable", exc)
        print("{}")
        return 1
    try:
        event = hook_event(_read_hook_stdin())
        # This host delivers no native thread identity to the hook process
        # (verified on codex-cli 0.153.4: the hook env carries CODEX_HOME
        # only). When a host does supply CODEX_THREAD_ID, a disagreement with
        # the event's session is a genuine anomaly — fail closed before any
        # capture. The transcript-artifact ownership check in the helper is
        # the second, always-available layer.
        native = os.environ.get("CODEX_THREAD_ID")
        if native and native != event["session_id"]:
            _record_stop("envelope", "identity_mismatch")
            print("{}")
            return 1
    except ValueError as exc:
        if str(exc) in {"unexpected hook event", "recursive Stop is not a capture"}:
            print("{}")
            return 0
        _record_stop("envelope", "invalid_envelope", exc)
        print("{}")
        return 1
    except TimeoutError as exc:
        _record_stop("envelope", "timeout", exc)
        print("{}")
        return 1
    except (OSError, TypeError, RecursionError) as exc:
        _record_stop("envelope", "invalid_envelope", exc)
        print("{}")
        return 1
    failed = False
    try:
        result = Sessions()._op(
            "stop_capture", {"event": event, "session": event["session_id"]}
        )
        failed = not _observe_stop(result)
    except Inactive as exc:
        _record_stop("helper", "unavailable", exc)
        failed = True
    except Exception as exc:
        # The hook never propagates a failure into the original task.
        _record_stop("helper", "unavailable", exc)
        failed = True
    print("{}")
    # The wrapper returns the neutral hook response; internal failures remain
    # in the local machine diagnostic channel for a natural capability call.
    return 1 if failed else 0


def sharing_operation(operation, extra=None):
    if operation == "sharing-status":
        return sharing.status()
    if operation == "sharing-choice":
        raise ValueError(
            "read-only/later product modes were removed. Configure the "
            "destination and scope with bridge.py config, or explicitly "
            "disable capture with sharing-disable. Saved legacy settings are preserved."
        )
    if operation == "sharing-enable":
        settings = sharing.set_enabled(True)
        result = dict(
            status="enabled",
            generation=settings["generation"],
            enabled_at=settings["enabled_at"],
            note="only newly authorized material is captured; no backfill",
        )
        return _refresh_capture(result)
    if not sharing.configured_path().exists():
        import consent
        consent.record_choice("disabled")
        return dict(status="disabled", sharing_choice="disabled")
    settings = sharing.set_enabled(False)
    return dict(
        status="disabled",
        generation=settings["generation"],
        cancel="the running service rereads this generation on its bounded "
        "idle tick and cancels matching queued capture/organization/outbound "
        "work (core-owned); drafts and published data are kept",
    )


def contribution(operation, batch_id):
    """Explicit operator recovery for one existing contribution batch.

    Thin wrapper over the deterministic core CLI operations; it starts no
    service or model, never rebuilds a payload and never replays failed work.
    """
    config_file = config_path()
    with update_lock(config_file):
        config = json.loads(config_file.read_text(encoding='utf-8'))
        output = run(
            [
                config["python"],
                "-m",
                "mindie_knowledge.loop.cli",
                operation,
                "--config",
                config["engine_config"],
                "--batch",
                batch_id,
            ],
            "",
            timeout=None,
            max_output=65536,
            env=generation_env(config_file),
        ).checked_stdout()
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError("contribution helper returned no result object")
    return result


def history_import(argv):
    """Foreground, explicitly requested import; no timer or Hook dispatch.

    Stream per-file receipts rather than buffering a whole library or imposing
    a Hook deadline on a user-requested bulk operation. Ctrl-C stops the import.
    The generation lock keeps scripts and interpreter coherent until it exits.
    """
    import subprocess

    config_file = config_path()
    with update_lock(config_file):
        config = json.loads(config_file.read_text(encoding='utf-8'))
        command = [config['python'], str(Path(runtime_scripts(config)) / 'history_import.py'), *argv]
        options = dict(stdin=subprocess.DEVNULL, env=generation_env(config_file))
        if os.name != 'nt':
            return subprocess.call(command, **options)
        # The explicit import may prepare the domain service. Give its helper
        # the same narrow breakaway permission as knowledge_attach, retaining
        # inherited output streams and the foreground operation's lifetime.
        import windows_process
        process = windows_process.spawn(command, allow_service=True, **options)
        code = None
        original_error = None
        try:
            code = process.wait()
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            try:
                windows_process.close_tree(process)
            except Exception as exc:
                receipt = dict(status='cleanup-failed', stage='history-import-helper-cleanup',
                               error=type(exc).__name__, helper_exit_code=code)
                # Source receipts have already streamed to the caller. Keep
                # their result and any original interruption ahead of cleanup.
                if original_error is not None:
                    original_error.add_note(json.dumps(receipt))
                else:
                    print(json.dumps(receipt), file=sys.stderr, flush=True)
                    code = code or 1
        return code


def configure(argv):
    """Post-install sharing configuration; never refuses an existing engine."""
    config_file = config_path()
    with update_lock(config_file):
        config = json.loads(config_file.read_text(encoding='utf-8'))
        output = run(
            [
                config["python"],
                str(Path(runtime_scripts(config)) / "setup.py"),
                "configure",
                "--config",
                str(config_file),
                *argv,
            ],
            "",
            timeout=None,
            max_output=65536,
            env=generation_env(config_file),
        ).checked_stdout()
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError("configuration helper returned no result object")
    return _refresh_capture(result)


def _refresh_capture(result):
    """Finish configuration in the already-bound native task; never infer one."""
    session = os.environ.get("CODEX_THREAD_ID")
    if session:
        try:
            lease = Sessions().active_lease(session)
        except (Inactive, ValueError) as exc:
            result["configuration_status"] = result["status"]
            result["status"] = "degraded"
            result["activation"] = dict(status="unavailable", error=str(exc)[:240])
        else:
            if lease is None:
                return result
            result["activation"] = _prepare_capture(dict(
                status="active", mindie_session_id=lease["session"],
                mindie_activation=lease["token"], activated_at=lease["activated_at"],
                project_root=lease["project_root"],
            ))
            if result["activation"].get("status") == "degraded":
                result["configuration_status"] = result["status"]
                result["status"] = "degraded"
    return result


def _reporting_choice():
    """Optional, independent reporting recommendation. Never installs.

    Offered once inside the first setup, never repeatedly afterwards. The
    effective view never reports an enabled reporter when the saved
    reporting choice disagrees."""
    from diagnostic_support import (
        effective_reporting,
        reporting_offer,
        reporting_status,
    )

    import consent

    saved = consent.load()
    view = effective_reporting(reporting_status(), saved.get("reporting"))
    result = dict(reporting=view)
    offer = reporting_offer(view, saved, consent.install_traces())
    if offer is not None:
        result["choice"] = offer
    return result


def _reporting_unavailable(stage, exc):
    print(json.dumps(dict(
        status="unavailable",
        error=dict(type=type(exc).__name__, stage=stage),
    )))
    raise SystemExit(1)


def _adapter_python(config_file):
    """Read the selected interpreter under the generation lock, then release it."""
    with update_lock(config_file):
        fd = os.open(config_file, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("adapter config must be a regular file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read()
        finally:
            os.close(fd)
        config = json.loads(raw)
        if not isinstance(config, dict):
            raise ValueError("adapter config must be an object")
        python = _bounded_path(config.get("python"), "python")
        scripts = runtime_scripts(config)
        if "runtime_scripts" in config:
            _bounded_path(config["runtime_scripts"], "runtime_scripts")
    return python, scripts


def _print_reporting_json(output, stage):
    try:
        payload = json.loads(output)
    except ValueError as exc:
        _reporting_unavailable(stage, exc)
    if not isinstance(payload, dict):
        _reporting_unavailable(stage, ValueError("reporting response must be an object"))
    print(json.dumps(payload))
    if payload.get("status") in {"unavailable", "failed", "degraded", "configuration_unavailable", "error"}:
        raise SystemExit(1)


def reporting_operation(operation):
    """Explicit reporting ops. Enable does not spawn ensure; Stop never calls this."""
    config_file = config_path()
    verb = operation.split("-", 1)[1]
    if verb == "status":
        stage = "config_read"
        try:
            try:
                config_file.stat()
            except FileNotFoundError:
                print(json.dumps(_reporting_choice()["reporting"]))
                return
            python, _scripts = _adapter_python(config_file)
        except BlockingIOError as exc:
            _reporting_unavailable("update_lock", exc)
        except Exception:
            # Unconfigured or malformed adapter: local shim only, no install.
            print(json.dumps(_reporting_choice()["reporting"]))
            return
        stage = "helper_run"
        try:
            output = run(
                [python, "-m", "mindie_diagnostics.cli", "reporting", "status"],
                "",
                timeout=None,
                max_output=65536,
                allowed_returncodes=(0, 1),
                env=generation_env(config_file),
            ).checked_stdout()
            _print_reporting_json(output, "helper_response")
        except SystemExit:
            raise
        except Exception as exc:
            _reporting_unavailable(stage, exc)
        return
    if verb in {"enable", "disable"}:
        stage = "config_read"
        try:
            python, scripts = _adapter_python(config_file)
            # The saved preference is recorded before the reporter policy is
            # touched; a refused consent write (damaged authority) leaves the
            # policy untouched, so the real service choice and the saved
            # preference never diverge.
            import consent

            consent.record_reporting(
                "enabled" if verb == "enable" else "disabled"
            )
            stage = "helper_run"
            output = run(
                [
                    python,
                    "-c",
                    "import json,sys; sys.path.insert(0, sys.argv[2]); "
                    "from diagnostic_support import configure_reporting; "
                    "print(json.dumps(configure_reporting("
                    "sys.argv[1]=='true', sys.executable)))",
                    "true" if verb == "enable" else "false",
                    str(Path(scripts)),
                ],
                "",
                timeout=None,
                max_output=65536,
                allowed_returncodes=(0, 1),
                env=generation_env(config_file),
            ).checked_stdout()
            _print_reporting_json(output, "helper_response")
        except SystemExit:
            raise
        except Exception as exc:
            _reporting_unavailable(stage, exc)
        return
    stage = "config_read"
    try:
        # Preparing or maintaining the reporter requires the saved reporting
        # choice to be exactly "enabled" — a saved later/disabled constrains
        # the real service, not just the prompts.
        import consent

        saved = consent.load()
        if saved.get("reporting") != "enabled":
            print(json.dumps(dict(
                status="unavailable",
                error=dict(type="ConsentError", stage="consent"),
                detail=(
                    "the saved reporting choice is "
                    + str(saved.get("reporting") or saved.get("state"))
                    + "; reporting ensure/maintain runs only after an explicit "
                    "reporting-enable"
                ),
            )))
            raise SystemExit(1)
        python, _scripts = _adapter_python(config_file)
        stage = "helper_run"
        output = run(
            [python, "-m", "mindie_diagnostics.cli", "reporting", verb],
            "",
            timeout=None,
            max_output=65536,
            allowed_returncodes=(0, 1),
            env=generation_env(config_file),
        ).checked_stdout()
        _print_reporting_json(output, "helper_response")
    except SystemExit:
        raise
    except Exception as exc:
        _reporting_unavailable(stage, exc)


def _optional_config_prefix(argv):
    """Accept `--config PATH` before the operation; leave host identity alone.

    Sets the process-local explicit override and MINDIE_AGENT_CONFIG for
    child dispatch. Default invocation without the prefix is unchanged.
    Native task identity is not set here.
    """
    if len(argv) >= 2 and argv[0] == "--config":
        value = argv[1]
        if not isinstance(value, str) or not os.path.isabs(value):
            print("MindIE --config requires an absolute path", file=sys.stderr)
            raise SystemExit(1)
        bind_explicit_config(value)
        return argv[2:]
    return argv


def main():
    argv = _optional_config_prefix(sys.argv[1:])
    if not argv or argv[0] not in OPERATIONS | CONTRIBUTION_OPERATIONS:
        print("Unsupported MindIE entry operation", file=sys.stderr)
        raise SystemExit(1)
    operation = argv[0]
    if operation == "history-import":
        raise SystemExit(history_import(argv[1:]))
    if operation == "config":
        try:
            result = configure(argv[1:])
            print(json.dumps(result))
            if result.get("status") in {"degraded", "failed", "unavailable", "error"}:
                raise SystemExit(1)
        except Exception as exc:
            print(
                f"MindIE config failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation == "sharing-choice":
        if len(argv) != 2:
            print("sharing-choice requires read-only or later", file=sys.stderr)
            raise SystemExit(1)
        try:
            print(json.dumps(sharing_operation(operation, argv[1])))
        except Exception as exc:
            print(
                f"MindIE sharing operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation in CONTRIBUTION_OPERATIONS:
        if len(argv) != 2:
            print("contribution operations require --batch id as argv", file=sys.stderr)
            raise SystemExit(1)
        batch_id = argv[1]
        if not BATCH.fullmatch(batch_id):
            print("Invalid contribution batch id", file=sys.stderr)
            raise SystemExit(1)
        try:
            print(json.dumps(contribution(operation, batch_id)))
        except Exception as exc:
            print(
                f"MindIE contribution recovery failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if len(argv) != 1:
        print("Unsupported MindIE entry operation", file=sys.stderr)
        raise SystemExit(1)
    if operation.startswith("reporting-"):
        reporting_operation(operation)
        return
    if operation == "mcp":
        from mcp_gate import serve

        return serve("knowledge")
    if operation in {"activate", "deactivate"}:
        try:
            result = activate(operation)
            print(json.dumps(result))
            if result.get("status") == "degraded":
                raise SystemExit(1)
        except Exception as exc:
            print(
                f"MindIE session operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation == "stop":
        raise SystemExit(stop())
    if operation.startswith("sharing-"):
        try:
            result = sharing_operation(operation)
            print(json.dumps(result))
            if (result.get("status") in {"degraded", "failed", "unavailable", "error"}
                    or result.get("state") in {"malformed", "unavailable"}):
                raise SystemExit(1)
        except Exception as exc:
            print(
                f"MindIE sharing operation failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return
    if operation in {"init", "status"}:
        payload, code = offline_status()
        print(json.dumps(payload))
        if code:
            raise SystemExit(code)
        return
    try:
        config_file = config_path()
        with update_lock(config_file):
            config = json.loads(config_file.read_text(encoding='utf-8'))
            control = [
                config["python"],
                str(Path(runtime_scripts(config)) / "service_control.py"),
                operation,
            ]
            print(
                run(
                    control,
                    "",
                    timeout=None,
                    max_output=32768,
                    env=generation_env(config_file),
                ).checked_stdout(),
                end="",
            )
    except Exception as exc:
        print(
            "MindIE Agent is not configured: "
            + type(exc).__name__
            + ". Run scripts/setup.py with the knowledge runtime interpreter.",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
