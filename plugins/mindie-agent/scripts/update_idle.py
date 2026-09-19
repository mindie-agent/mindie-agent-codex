"""Stop an idle local service for an upgrade; never start one."""

import json
from pathlib import Path
import sys
import time
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
        status = rpc(connection, "status", timeout=0.5)
    except URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return True
        raise
    if status.get("maintenance_pending") != 0:
        return False
    rpc(connection, "stop", timeout=0.5)
    time.sleep(0.7)
    try:
        rpc(connection, "status", timeout=0.5)
    except URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return True
        raise
    return False


if __name__ == "__main__":
    print(json.dumps(dict(idle=idle(json.loads(sys.stdin.read())))))
