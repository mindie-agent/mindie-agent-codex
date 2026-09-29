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
    elif state in {"off", "unconfigured"}:
        hints.append(sharing.CHOICES)
    elif state == "disabled":
        hints.append("Experience capture is explicitly disabled; the saved setting is preserved.")
    elif state == "malformed":
        hints.append("Experience capture is unavailable because its saved configuration is damaged.")
    for row in view["contributions"]:
        if row.get("status") in RECOVERABLE_BATCH:
            hints.append(
                f"contribution {row.get('batch_id')} is {row.get('status')}; "
                "the worker handles eligible transient recovery automatically. "
                "Inspect the listed record only for an actionable fault; "
                "never blindly repeat an uncertain write"
            )
    return hints


def status():
    # The bootstrap selected this complete runtime under its generation lock.
    # Core imports belong here, never in the stdlib bootstrap or Stop path.
    from mindie_knowledge.loop.diagnostics import snapshot

    adapter = json.loads(config_path().read_text(encoding='utf-8'))
    engine_config = adapter["engine_config"]
    import consent

    # One consent authority read feeds the sharing view and the reporting view.
    saved_consent = consent.load()
    sharing_view = sharing.status(saved=saved_consent)
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
            "import json,pathlib,sys; json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')); print('JSON syntax valid')",
            engine_config,
        ]
    inspect = {
        row["batch_id"]: bridge + ["contribution-inspect", row["batch_id"]]
        for row in view["contributions"] if row.get("status") in RECOVERABLE_BATCH
    }
    if inspect:
        commands["contribution_inspect"] = inspect
    # Optional and independent of knowledge consent. Status never ensures.
    from diagnostic_support import (
        effective_reporting,
        reporting_offer,
        reporting_status,
    )

    commands["reporting_status"] = bridge + ["reporting-status"]
    commands["reporting_enable"] = bridge + ["reporting-enable"]
    commands["reporting_disable"] = bridge + ["reporting-disable"]
    diagnostic_view = effective_reporting(
        reporting_status(), saved_consent.get("reporting")
    )
    diagnostics = dict(reporting=diagnostic_view)
    offer = reporting_offer(
        diagnostic_view, saved_consent, consent.install_traces()
    )
    if offer is not None:
        diagnostics["choice"] = offer
    first_use = sharing_view.get("first_use")
    result = dict(
        experience=("needs-configuration" if sharing_view.get("state") in {"off", "unconfigured"}
                    else "disabled" if sharing_view.get("state") == "disabled"
                    else "unavailable" if sharing_view.get("state") == "malformed"
                    else "configured"),
        adapter=dict(
            config=str(config_path()),
            engine_config=engine_config,
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
        diagnostics=diagnostics,
    )
    if sharing_view.get("state") in {"off", "unconfigured"}:
        result["next"] = sharing.CHOICES
    elif sharing_view.get("state") == "disabled":
        result["next"] = "Experience capture is explicitly disabled. Task binding does not enable it."
    elif (sharing_view.get("state") == "malformed"
          or view["configuration"].get("status") != "ok"
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

    config = json.loads(config_path().read_text(encoding='utf-8'))
    return rpc(connect(config_at(config["engine_config"])), "stop", timeout=2)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in OPERATIONS:
        raise SystemExit(1)
    result = status() if sys.argv[1] == "status" else shutdown()
    print(json.dumps(result, ensure_ascii=False))
