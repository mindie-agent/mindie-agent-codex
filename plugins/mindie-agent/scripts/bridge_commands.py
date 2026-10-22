"""Contribution, history, reporting, sharing, and status CLI helpers for bridge.py.

[Role]
Implements the operator-facing contribution recovery, history import, reporting,
sharing, and offline status commands dispatched by `bridge.py`.

[Invariants]
- Resolution goes through the live `bridge` module globals, so callers and tests
  that patch `bridge.run`, `bridge.runtime_env`, or `bridge.os` keep observing
  the same dispatch boundary.
- Contribution recovery never rebuilds a payload, starts a model, or replays
  failed organizer work.
- Status and sharing reads never cold-start a service or create state.

[Timeouts & Locks]
- Contribution/history/reporting helper budgets stay bounded (60.0s
  contribution, 300.0s history import, 90.0s reporting activation, 180.0s
  configure); profile reads and mutations hold `update_lock`.

[Side-Effect Policy]
- Status and reporting status are read-only; explicit enable/disable/configure
  operations write only their existing managed settings.
"""

import json
from pathlib import Path
import sys


def _bridge():
    """Resolve the dispatcher module at call time to preserve patch points."""
    import bridge
    return bridge


def unconfigured_status():
    """Stdlib-only offline first-use payload. Creates no files or services.

    A profile that already holds a saved choice (e.g. from a sibling adapter)
    is an existing installation: setup reuses the choice, no onboarding."""
    b = _bridge()
    import consent

    saved = consent.load()
    first_use = dict(
        state="unconfigured",
        prompt=b.sharing.CHOICES,
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
    b = _bridge()
    python = selected["python"] if selected else b.sys.executable
    scripts = Path(b.runtime_scripts(selected)) if selected else Path(__file__).parent
    commands = {
        "status": [python, str(scripts / "bridge.py"), "--config", str(config_file), "status"],
        "check_config_json": [
            b.sys.executable, "-c",
            "import json,pathlib,sys; json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); print('JSON syntax valid')",
            str(config_file),
        ],
    }
    recovery = {
        "invalid_config": "Inspect the named config and correct its JSON or required absolute runtime paths, then run status. Existing state has not been reinitialized.",
        "update_busy": "An update holds the profile lock. Let that operation finish, then explicitly run status; installation is not missing.",
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
    b = _bridge()
    config_file = b.config_path()
    selected = None
    stage = "config_stat"
    try:
        try:
            config_file.stat()
        except FileNotFoundError:
            return unconfigured_status(), 0
        stage = "update_lock"
        with b.update_lock(config_file):
            stage = "config_read"
            if not config_file.is_file():
                raise ValueError("adapter config must be a regular file")
            config = json.loads(config_file.read_text(encoding="utf-8"))
            stage = "config_validate"
            if not isinstance(config, dict):
                raise ValueError("adapter config must be an object")
            for key in ("python", "engine_config"):
                b._bounded_path(config.get(key), key)
            if "runtime_scripts" in config:
                b._bounded_path(config["runtime_scripts"], "runtime_scripts")
            selected = config
            stage = "helper_run"
            output = b.run(
                [config["python"], str(Path(b.runtime_scripts(config)) / "service_control.py"), "status"],
                "", timeout=30.0, max_output=32768, env=b.runtime_env(config_file),
            ).checked_stdout()
            stage = "helper_response"
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("status response must be an object")
            stage = "update_status"
            from update_trigger import directory, read_optional
            updates = directory(config_file)
            try:
                payload["updates"] = dict(
                    check=read_optional(updates / "status.json"),
                    trigger_failure=read_optional(updates / "trigger-failure.json"),
                )
            except TimeoutError:
                raise
            except (OSError, ValueError) as exc:
                payload["updates"] = dict(status="unavailable", stage=stage,
                                          error_type=type(exc).__name__)
                return payload, 1
            return payload, 0
    except Exception as exc:
        if stage == "update_lock" and isinstance(exc, BlockingIOError):
            state = "update_busy"
        elif stage.startswith("config") or stage == "update_lock":
            state = "invalid_config"
        else:
            state = "helper_failed"
        return _status_failure(state, stage, exc, config_file, selected), 1


def sharing_operation(operation, extra=None):
    b = _bridge()
    if operation == "sharing-status":
        return b.sharing.status()
    if operation == "sharing-choice":
        raise ValueError(
            "read-only/later product modes were removed. Configure the "
            "destination and scope with bridge.py config, or explicitly "
            "disable capture with sharing-disable. Saved settings are preserved."
        )
    if operation == "sharing-enable":
        settings = b.sharing.set_enabled(True)
        result = dict(
            status="enabled",
            generation=settings["generation"],
            enabled_at=settings["enabled_at"],
            note="only newly authorized material is captured; no backfill",
        )
        return b._refresh_capture(b._with_reporting(result))
    if not b.sharing.configured_path().exists():
        import consent
        consent.record_choice("disabled")
        return b._with_reporting(dict(status="disabled", sharing_choice="disabled"))
    settings = b.sharing.set_enabled(False)
    return b._with_reporting(dict(
        status="disabled",
        generation=settings["generation"],
        cancel="the running service rereads this generation on its bounded "
        "idle tick and cancels matching queued capture/organization/outbound "
        "work (core-owned); drafts and published data are kept",
    ))


def sharing_enable():
    return sharing_operation("sharing-enable")


def sharing_disable():
    return sharing_operation("sharing-disable")


def sharing_status():
    return sharing_operation("sharing-status")


def contribute_args(argv):
    """Validate one explicit contribution recovery invocation."""
    b = _bridge()
    if len(argv) != 2:
        raise ValueError("contribution operations require --batch id as argv")
    operation, batch_id = argv
    if operation not in b.CONTRIBUTION_OPERATIONS:
        raise ValueError("unknown contribution recovery operation")
    if not b.BATCH.fullmatch(batch_id):
        raise ValueError("Invalid contribution batch id")
    return operation, batch_id


def contribute_command(config, operation, batch_id):
    """Build the deterministic core recovery command without rebuilding work."""
    return [
        config["python"],
        "-m",
        "mindie_knowledge.loop.cli",
        operation,
        "--config",
        config["engine_config"],
        "--batch",
        batch_id,
    ]


def contribute_result(output):
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError("contribution helper returned no result object")
    return result


def contribution(operation, batch_id):
    """Explicit operator recovery for one existing contribution batch.

    Thin wrapper over the deterministic core CLI operations; it starts no
    service or model, never rebuilds a payload and never replays failed work.
    """
    b = _bridge()
    config_file = b.config_path()
    with b.update_lock(config_file):
        config = json.loads(config_file.read_text(encoding='utf-8'))
        output = b.run(
            contribute_command(config, operation, batch_id),
            "",
            timeout=60.0,
            max_output=65536,
            env=b.runtime_env(config_file),
        ).checked_stdout()
    return contribute_result(output)


def contribute(operation, batch_id):
    return contribution(operation, batch_id)


def contribute_Now(operation, batch_id):
    return contribution(operation, batch_id)


def history_import(argv):
    """Foreground, explicitly requested import; no timer or Hook dispatch.

    Stream per-file receipts rather than buffering a whole library or imposing
    a Hook deadline on a user-requested bulk operation. Ctrl-C stops the import.
    The profile lock keeps scripts and interpreter coherent until it exits.
    """
    import subprocess

    b = _bridge()
    config_file = b.config_path()
    with b.update_lock(config_file):
        config = json.loads(config_file.read_text(encoding='utf-8'))
        command = [config['python'], str(Path(b.runtime_scripts(config)) / 'history_import.py'), *argv]
        options = dict(stdin=subprocess.DEVNULL, env=b.runtime_env(config_file))
        if b.os.name != 'nt':
            return subprocess.call(command, timeout=300.0, **options)
        # The explicit import may prepare the domain service. Give its helper
        # the same narrow breakaway permission as knowledge_attach, retaining
        # inherited output streams and the foreground operation's lifetime.
        import windows_process
        process = windows_process.spawn(command, allow_service=True, **options)
        code = None
        original_error = None
        try:
            code = process.wait(timeout=300.0)
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
    b = _bridge()
    config_file = b.config_path()
    with b.update_lock(config_file):
        config = json.loads(config_file.read_text(encoding='utf-8'))
        output = b.run(
            [
                config["python"],
                str(Path(b.runtime_scripts(config)) / "setup.py"),
                "configure",
                "--config",
                str(config_file),
                *argv,
            ],
            "",
            timeout=180.0,
            max_output=65536,
            env=b.runtime_env(config_file),
            allow_service=True,
            allowed_returncodes=(0, 1),
        ).checked_stdout()
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError("configuration helper returned no result object")
    return b._refresh_capture(result)


def _refresh_capture(result):
    """Finish configuration in the already-bound native task; never infer one."""
    b = _bridge()
    session = b.os.environ.get("CODEX_THREAD_ID")
    if session:
        try:
            lease = b.Sessions().active_lease(session)
        except (b.Inactive, ValueError) as exc:
            result.setdefault("configuration_status", result["status"])
            result["status"] = "degraded"
            result["activation"] = dict(status="unavailable", error=str(exc)[:240])
        else:
            if lease is None:
                return result
            result["activation"] = b._prepare_capture(dict(
                status="active", mindie_session_id=lease["session"],
                mindie_activation=lease["token"], activated_at=lease["activated_at"],
                project_root=lease["project_root"],
            ))
            if result["activation"].get("status") == "degraded":
                result.setdefault("configuration_status", result["status"])
                result["status"] = "degraded"
    return result


def _reporting_choice():
    """Read the single contribution authority without configuring a worker."""
    from diagnostic_support import reporting_status
    return dict(reporting=reporting_status())


def _reporting_unavailable(stage, exc):
    print(json.dumps(dict(
        status="unavailable",
        error=dict(type=type(exc).__name__, stage=stage),
    )))
    raise SystemExit(1)


def _adapter_python(config_file):
    """Read the selected interpreter under the profile lock, then release it."""
    b = _bridge()
    with b.update_lock(config_file):
        if not config_file.is_file():
            raise ValueError("adapter config must be a regular file")
        config = json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("adapter config must be an object")
        python = b._bounded_path(config.get("python"), "python")
        scripts = b.runtime_scripts(config)
        if "runtime_scripts" in config:
            b._bounded_path(config["runtime_scripts"], "runtime_scripts")
    return python, scripts


def _activate_reporting():
    b = _bridge()
    config_file = b.config_path()
    python, scripts = _adapter_python(config_file)
    output = b.run(
        [python, str(Path(scripts) / "diagnostic_support.py"), "activate",
         "--config", str(config_file)], "", timeout=90.0, max_output=65536,
        allowed_returncodes=(0, 1), env=b.runtime_env(config_file), allow_service=True,
    ).checked_stdout()
    value = json.loads(output)
    if not isinstance(value, dict):
        raise ValueError("reporting activation returned no result object")
    return value


def _with_reporting(result):
    b = _bridge()
    try:
        reporting = b._activate_reporting()
    except Exception as exc:
        reporting = dict(status="unavailable", error_type=type(exc).__name__)
    result["reporting"] = reporting
    if reporting.get("status") not in {"running", "disabled"}:
        result["configuration_status"] = result["status"]
        result["status"] = "degraded"
    return result


def reporting_operation(operation):
    """Legacy mutators guide to the whole contribution flow; status stays inert."""
    b = _bridge()
    verb = operation.split("-", 1)[1]
    if verb in {"enable", "disable"}:
        print(json.dumps(dict(
            status="contribution_choice_required", enabled=False,
            detail="Knowledge sharing and sanitized fault Issues use one contribution switch.",
            next="bridge.py config --community-repository OWNER/REPO --community-project-root PATH --community-visibility public"
                 if verb == "enable" else "bridge.py sharing-disable",
        )))
        return
    if verb == "status":
        print(json.dumps(b._reporting_choice()["reporting"]))
        return
    try:
        result = b._activate_reporting()
        print(json.dumps(result))
        if result.get("status") not in {"running", "disabled"}:
            raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as exc:
        _reporting_unavailable("reporting_activation", exc)


def reporting_enable():
    return reporting_operation("reporting-enable")


def reporting_disable():
    return reporting_operation("reporting-disable")


def reporting_status():
    return reporting_operation("reporting-status")


def doctor():
    """Read-only installation diagnosis; never initializes or starts a service."""
    return offline_status()
