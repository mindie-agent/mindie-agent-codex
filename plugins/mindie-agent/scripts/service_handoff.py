"""Exact-generation service retirement and restoration owned by the core.

The updater holds its exclusive adapter lock. The core's persisted retirement
receipt also excludes wake helpers spawned before that lock was taken.
"""
import json
import sys

from mindie_knowledge.loop.cli import config_at, ensure_service, rpc
from mindie_knowledge.loop.activation import Admission
from mindie_knowledge.loop.lifecycle import retire_service, restore_service, inspect_retirement


def stop(engine):
    return retire_service(engine)


def restore(engine, retirement=None):
    result = restore_service(engine, retirement)
    config = config_at(engine)
    path = config.get("admission_path")
    if not path or not Admission(path).leases():
        return dict(result, reason="no-valid-lease")
    # Both rollback and a successful switch must wake the selected generation
    # when existing authorization requires it, even if the old service was absent.
    connection = ensure_service(engine)
    status = rpc(connection, "status")
    if status.get("admission_frozen") is not False:
        raise RuntimeError("selected service remains frozen")
    return dict(result, status="restored")


if __name__ == "__main__":
    try:
        action, engine = sys.argv[1:3]
        if action in {"restore", "unretire"}:
            receipt = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
            result = restore(engine, receipt) if action == "restore" else restore_service(engine, receipt)
        else:
            result = {"stop": stop, "inspect": inspect_retirement}[action](engine)
        print(json.dumps(result))
    except Exception as exc:
        result = dict(status="failed", error=type(exc).__name__, automatic_retry=False)
        try:
            result["retirement"] = inspect_retirement(engine)
        except Exception as inspect_error:
            result["inspection_error"] = type(inspect_error).__name__
        print(json.dumps(result))
        sys.exit(1)
