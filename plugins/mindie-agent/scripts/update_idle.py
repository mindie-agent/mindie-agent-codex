"""Stop an idle local service for an upgrade; never start one.

Idle is the core authenticated local RPC ``stop_if_idle``: freeze new work
and stop only when nothing is actually executing (in-flight calls, worker,
feed). Durable unknown/pending receipts and idle task grants must not block.
This adapter does not infer activity from outbox statuses or race status+stop.
A missing or invalid ``stop_if_idle`` result fails closed (not idle).
"""

import json
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.parse import urlparse

from mindie_knowledge.loop.cli import connect
from mindie_knowledge.loop.transport import rpc


def idle(config):
    engine = json.loads(Path(config["engine_config"]).read_text())
    try:
        connection = connect(engine)
    except FileNotFoundError:
        return True
    if urlparse(connection["url"]).hostname not in {"127.0.0.1", "localhost", "::1"}:
        return False
    try:
        result = rpc(connection, "stop_if_idle", timeout=1.0)
    except URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return True
        raise
    if not isinstance(result, dict) or type(result.get("idle")) is not bool:
        raise RuntimeError(
            "stop_if_idle is absent or invalid; refusing to guess idleness"
        )
    return result["idle"]


if __name__ == "__main__":
    print(json.dumps(dict(idle=idle(json.loads(sys.stdin.read())))))
