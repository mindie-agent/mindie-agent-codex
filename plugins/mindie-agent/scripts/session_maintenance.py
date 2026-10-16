"""Run session maintenance in the complete runtime selected by the profile."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import preparing
from product_contract import read_json
from session_gate import bind_explicit_config


def maintain(config):
    bind_explicit_config(config)
    adapter = read_json(config)[0]
    from service_client import connect
    with preparing("service-start"):
        connect(adapter["engine_config"])
    from feed_sync import sync
    with preparing("feed-refresh"):
        feed = sync(adapter["engine_config"])
    from diagnostic_maintenance import maintain as diagnostics_maintain
    with preparing("diagnostic-maintenance"):
        diagnostics = diagnostics_maintain(config)
    value = dict(status="ready", feed=feed, diagnostics=diagnostics)
    print(json.dumps(value))
    if (feed.get("status") != "ok"
            or diagnostics.get("status") not in {"ok", "complete", "unchanged"}):
        raise RuntimeError("MindIE session preparation is incomplete; inspect local status")


if __name__ == "__main__":
    maintain(Path(sys.argv[1]))
