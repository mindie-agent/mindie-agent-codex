"""Unified offline status and explicit shutdown; never ensure_service().

``status`` is deterministic and works fully offline: it inspects only local
files and, when a service happens to run, one short RPC — it never starts a
service, a model or a database, and it ends with actionable recovery hints.
``shutdown`` is the explicit operator stop for a running service.
"""

import json
from pathlib import Path
import sys

from session_gate import config_path
import sharing

OPERATIONS = {"status", "shutdown"}

RECOVERABLE_BATCH = {"failed", "unknown", "needs_review", "unavailable"}


def _service_view(engine_config):
    """Live service status, or an honest not-running view with store summary."""
    from mindie_knowledge.loop.cli import _open_existing_store, config_at, connect
    from mindie_knowledge.loop.transport import rpc

    config = config_at(engine_config)
    try:
        return dict(state="running", **rpc(connect(config), "status", timeout=1.0))
    except (OSError, ValueError, RuntimeError):
        view = dict(state="not-running")
        store = _open_existing_store(config)
        if store is not None:
            try:
                view["store"] = store.status()
            finally:
                store.close()
        return view


def _admission_view(adapter):
    path = adapter.get("admission_path")
    if not isinstance(path, str):
        return dict(configured=False)
    view = dict(configured=True, path=path)
    try:
        from mindie_knowledge.loop.activation import Admission

        view["active_tasks"] = len(Admission(path).leases())
    except Exception:
        view["active_tasks"] = None
        view["detail"] = "admission store unreadable; calls fail closed"
    return view


def _hints(sharing_view, service_view):
    hints = []
    state = sharing_view.get("state")
    if sharing_view.get("first_use"):
        hints.append(sharing.CHOICES.replace("\n", " | "))
    elif state in {"off", "unconfigured", "malformed"}:
        hints.append(
            "community sharing is not enabled; for the recommended opt-in "
            "contribution run scripts/setup.py configure --community-repository "
            "OWNER/REPO --community-project-root PATH --community-visibility "
            "public, or scripts/bridge.py sharing-choice read-only|later"
        )
    if service_view.get("state") == "not-running":
        hints.append(
            "knowledge service is not running; it starts automatically on the "
            "first admitted call, no action needed"
        )
    budget = service_view.get("maintenance_budget")
    if isinstance(budget, dict) and budget.get("paused"):
        hints.append(
            "maintenance circuit is paused; after fixing the cause resume "
            "explicitly with: <python> -m mindie_knowledge.loop.cli "
            "maintenance-resume --config <engine_config>"
        )
    outbox = service_view.get("outbox")
    if not isinstance(outbox, list):
        store = service_view.get("store")
        outbox = store.get("outbox") if isinstance(store, dict) else None
    for row in (outbox or [])[:5]:
        if isinstance(row, dict) and row.get("status") in RECOVERABLE_BATCH:
            hints.append(
                f"contribution {row.get('batch_id')} is {row.get('status')}; "
                "inspect with scripts/bridge.py contribution-inspect "
                f"{row.get('batch_id')}, then reconcile/retry/compact explicitly"
            )
    if service_view.get("errors"):
        hints.append(
            "the service recorded bounded errors; inspect them in the service "
            "field above before any recovery action"
        )
    return hints


def status():
    adapter = json.loads(config_path().read_text())
    engine_config = adapter["engine_config"]
    sharing_view = sharing.status()
    service_view = _service_view(engine_config)
    first_use = sharing_view.get("first_use") or sharing.first_use()
    result = dict(
        adapter=dict(
            config=str(config_path()),
            engine_config=engine_config,
            sharing_choice=adapter.get("sharing_choice"),
        ),
        sharing=sharing_view,
        admission=_admission_view(adapter),
        service=service_view,
        recovery=_hints(sharing_view, service_view),
        first_use=first_use,
    )
    if first_use:
        result["next"] = (
            "Present the three choices to the user and wait; do not default "
            "yes, do not edit JSON, do not reinstall. Then activate."
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
