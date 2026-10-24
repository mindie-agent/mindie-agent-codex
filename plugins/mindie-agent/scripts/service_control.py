"""Unified offline status and explicit shutdown; never ensure_service().

[Role]
Implements deterministic offline `status` inspection and explicit operator
`shutdown` for the local knowledge service.

[Invariants]
- `status` inspects local files and at most one short RPC if the service is
  already running; it never starts a service, model, or database.
- Returns component facts without knowledge setup or flywheel guidance.

[Timeouts & Locks]
- Uses bounded non-starting status probes via `mindie_knowledge.loop.cli`.

[Side-Effect Policy]
- `status` is strictly read-only; `shutdown` stops a running service explicitly.
"""

import json
from pathlib import Path
import sys

from admission_ops import native_session
from session_gate import config_path, runtime_scripts
import sharing

OPERATIONS = {"status", "shutdown"}

RECOVERABLE_BATCH = {"failed", "unknown", "needs_review", "unavailable"}


def status():
    # The bootstrap selected this complete runtime under its profile lock.
    # Core imports belong here, never in the stdlib bootstrap or Stop path.
    from mindie_knowledge.loop.diagnostics import snapshot

    adapter = json.loads(config_path().read_text(encoding='utf-8'))
    engine_config = adapter["engine_config"]
    import consent

    # One consent authority read feeds the sharing view and the reporting view.
    saved_consent = consent.load()
    sharing_view = sharing.status(saved=saved_consent)
    sharing_view.pop("first_use", None)
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
    # Status reads the single contribution choice and never starts a worker.
    from diagnostic_support import reporting_status

    commands["reporting_status"] = bridge + ["reporting-status"]
    diagnostics = dict(reporting=reporting_status())
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
        summaries=view["summaries"],
        contributions=view["contributions"],
        commands=commands,
        diagnostics=diagnostics,
    )
    return result


def shutdown():
    from mindie_knowledge.loop.cli import config_at, connect
    from mindie_knowledge.loop.transport import rpc

    config = json.loads(config_path().read_text(encoding='utf-8'))
    return rpc(connect(config_at(config["engine_config"])), "stop")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in OPERATIONS:
        raise SystemExit(1)
    result = status() if sys.argv[1] == "status" else shutdown()
    print(json.dumps(result, ensure_ascii=False))
