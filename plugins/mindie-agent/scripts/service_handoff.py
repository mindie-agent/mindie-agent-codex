"""Updater-only bounded handoff. Run directly with the selected interpreter.

No launcher locks, task activation, transcript work or model calls here.
The parent owns the exclusive update lock and permits restore only after this
invocation confirmed stopping its previous endpoint. Output contains no token.
"""
import json
import sys
import time
from urllib.error import URLError
from urllib.parse import urlparse

from mindie_knowledge.loop.cli import config_at, connect, ensure_service, rpc
from mindie_knowledge.loop.activation import Admission


def refused(exc):
    return isinstance(exc, ConnectionRefusedError) or (
        isinstance(exc, URLError) and isinstance(exc.reason, ConnectionRefusedError)
    )


def stop(engine):
    config = config_at(engine)
    try:
        connection = connect(config)
    except FileNotFoundError:
        return {"idle": True, "service": "absent"}
    if urlparse(connection["url"]).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("service endpoint is not loopback")
    try:
        result = rpc(connection, "stop_if_idle", timeout=1)
    except OSError as exc:
        if refused(exc):
            return {"idle": True, "service": "absent"}
        raise RuntimeError("stop acknowledgement unavailable; no restart") from None
    if not isinstance(result, dict) or type(result.get("idle")) is not bool:
        raise RuntimeError("invalid stop acknowledgement; no restart")
    if not result["idle"]:
        return {"idle": False, "service": "busy"}
    # Probe the EXACT old endpoint, not a possibly replaced connection.json.
    # A frozen status response is still alive. Only refused proves exit.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            rpc(connection, "status", timeout=min(.3, deadline-time.monotonic()))
        except OSError as exc:
            if refused(exc):
                return {"idle": True, "service": "stopped"}
            # Reset can happen during socket close; it is not proof of exit.
        time.sleep(min(.1, max(0, deadline-time.monotonic())))
    raise RuntimeError("old service exit unconfirmed; no restart")


def restore(engine):
    config = config_at(engine)
    path = config.get("admission_path")
    if not path or not Admission(path).leases():
        return {"status": "not-needed", "reason": "no-valid-lease"}
    connection = ensure_service(engine)  # core: one spawn, 5 s, three probes
    status = rpc(connection, "status", timeout=.5)
    if status.get("admission_frozen") is not False:
        raise RuntimeError("selected service remains frozen; no restart")
    return {"status": "restored"}


if __name__ == "__main__":
    try:
        action, engine = sys.argv[1:]
        result = {"stop": stop, "restore": restore}[action](engine)
        print(json.dumps(result))
    except Exception as exc:
        # Exception values can include connection tokens or provider details.
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        sys.exit(1)
