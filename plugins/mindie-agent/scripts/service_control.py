"""Unified offline status and explicit shutdown; never ensure_service().

``status`` is deterministic and works fully offline: it inspects only local
files and, when a service happens to run, one short RPC — it never starts a
service, a model or a database, and it ends with actionable recovery hints.
``shutdown`` is the explicit operator stop for a running service.
"""

import json
from pathlib import Path
import sys

from admission_ops import native_session
from session_gate import config_path, runtime_scripts
import sharing

OPERATIONS = {"status", "shutdown"}

RECOVERABLE_BATCH = {"failed", "unknown", "needs_review", "unavailable"}


def _hints(sharing_view, view):
    hints = list(view["hints"])
    state = sharing_view.get("state")
    if sharing_view.get("first_use"):
        hints.append(sharing.CHOICES.replace("\n", " | "))
    elif state in {"off", "disabled", "unconfigured", "malformed"}:
        hints.append(
            "community sharing is not enabled; for the recommended opt-in "
            "contribution run scripts/setup.py configure --community-repository "
            "OWNER/REPO --community-project-root PATH --community-visibility "
            "public, or scripts/bridge.py sharing-choice read-only|later"
        )
    for row in view["contributions"]:
        if row.get("status") in RECOVERABLE_BATCH:
            hints.append(
                f"contribution {row.get('batch_id')} is {row.get('status')}; "
                "use its listed inspect command; reconcile unknown writes "
                "before any explicit retry"
            )
    return hints


def status():
    # The bootstrap selected this complete runtime under its generation lock.
    # Core imports belong here, never in the stdlib bootstrap or Stop path.
    from mindie_knowledge.loop.diagnostics import snapshot

    adapter = json.loads(config_path().read_text())
    engine_config = adapter["engine_config"]
    sharing_view = sharing.status()
    if sharing_view.get("state") in {"malformed", "unconfigured"}:
        sharing_view["detail"] = "Inspect the configured community settings; capture remains disabled."
    try:
        session = native_session()
    except ValueError:
        session = None
    view = snapshot(engine_config, session=session)
    # Retain the existing state field, never the unscoped RPC/store payload.
    service_view = dict(view["service"], state=view["service"]["status"])
    bridge = [adapter["python"], str(Path(runtime_scripts(adapter)) / "bridge.py"),
              "--config", str(config_path())]
    commands = dict(status=bridge + ["status"])
    if view["configuration"].get("status") != "ok":
        commands["check_engine_json"] = [
            adapter["python"], "-c",
            "import json,pathlib,sys; json.loads(pathlib.Path(sys.argv[1]).read_text()); print('JSON syntax valid')",
            engine_config,
        ]
    if view["admission"].get("status") == "paused":
        commands.update(deactivate=bridge + ["deactivate"], activate=bridge + ["activate"])
    if view["maintenance"].get("paused"):
        commands["maintenance_resume"] = [
            adapter["python"], "-m", "mindie_knowledge.loop.cli",
            "maintenance-resume", "--config", engine_config,
        ]
    inspect = {
        row["batch_id"]: bridge + ["contribution-inspect", row["batch_id"]]
        for row in view["contributions"] if row.get("status") in RECOVERABLE_BATCH
    }
    if inspect:
        commands["contribution_inspect"] = inspect
    first_use = sharing_view.get("first_use") or sharing.first_use()
    result = dict(
        adapter=dict(
            config=str(config_path()),
            engine_config=engine_config,
            sharing_choice=adapter.get("sharing_choice"),
        ),
        sharing=sharing_view,
        admission=view["admission"],
        service=service_view,
        configuration=view["configuration"],
        store=view["store"],
        maintenance=view["maintenance"],
        startup=view["startup"],
        captures=view["captures"],
        contributions=view["contributions"],
        recovery=_hints(sharing_view, view),
        commands=commands,
        first_use=first_use,
    )
    if first_use:
        result["next"] = (
            "Present the three choices to the user and wait; do not default "
            "yes, do not edit JSON, do not reinstall. Then activate."
        )
    elif view["admission"].get("status") == "paused":
        result["next"] = (
            "Inspect the reported failures and fix the cause before explicit "
            "deactivate/reactivate. Status does not reset admission or replay work."
        )
    elif (view["configuration"].get("status") != "ok"
          or view["startup"].get("status") in {"failed", "unavailable"}
          or view["store"].get("status") == "unavailable"
          or view["admission"].get("status") == "unavailable"):
        result["next"] = (
            "Inspect the reported configuration/component stage and error class, "
            "then run the listed status command. Native shell/SSH or independent "
            "remote-dev remains available; no reactivation or reinstall is implied."
        )
    elif view["admission"].get("status") == "active":
        result["next"] = (
            "This native task remains admitted. Inspect any reported component "
            "failure before recovery; native shell/SSH or independent remote-dev "
            "work remains available."
        )
    elif session is None:
        result["next"] = (
            "Task records are omitted without native CODEX_THREAD_ID. Run status "
            "inside the original native task; no activation was inferred."
        )
    else:
        result["next"] = (
            "Run scripts/bridge.py activate in this native task after the "
            "user has a sharing choice; native CODEX_THREAD_ID is required."
        )
    return result


def shutdown():
    from mindie_knowledge.loop.cli import config_at, connect
    from mindie_knowledge.loop.transport import rpc

    config = json.loads(config_path().read_text())
    return rpc(connect(config_at(config["engine_config"])), "stop", timeout=2)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in OPERATIONS:
        raise SystemExit(1)
    result = status() if sys.argv[1] == "status" else shutdown()
    print(json.dumps(result, ensure_ascii=False))
