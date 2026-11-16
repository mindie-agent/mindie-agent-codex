"""Run session maintenance in the complete runtime selected by the profile.

[Role]
Coordinates post-activation session maintenance: service connection, feed
refresh, and diagnostic maintenance.

[Invariants]
- Binds explicit config before invoking runtime services.
- Reporter activation precedes knowledge readiness so a knowledge startup
  failure cannot prevent the authorized diagnostic worker from starting.
- Wraps each stage (`service-start`, `feed-refresh`, `diagnostic-maintenance`)
  with `bootstrap.preparing` stage markers.

[Timeouts & Locks]
- Executed within the caller's bounded subprocess budget. One 2s bounded
  storage reclamation runs after diagnostic maintenance and never changes the
  maintenance status.

[Side-Effect Policy]
- Starts/connects to the knowledge service, refreshes feed state, runs
  diagnostic maintenance, and silently reclaims disposable storage history
  under the profile directory.
"""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bootstrap import preparing
from product_contract import read_json
from session_gate import bind_explicit_config


def maintain(config):
    bind_explicit_config(config)
    adapter = read_json(config)[0]
    from diagnostic_maintenance import maintain as diagnostics_maintain
    with preparing("diagnostic-maintenance"):
        diagnostics = diagnostics_maintain(config)
    from storage_gc import cleanup_fact
    storage_cleanup = cleanup_fact(
        keep_paths=[sys.executable, adapter.get("python")], budget_seconds=2.0)
    from mindie_knowledge.loop.activation import Admission
    if Admission(adapter["admission_path"]).knowledge_lease(os.environ.get("CODEX_THREAD_ID")) is None:
        print(json.dumps(dict(status="inactive", diagnostics=diagnostics,
                              storage_cleanup=storage_cleanup)))
        return
    from service_client import connect
    with preparing("service-start"):
        connect(adapter["engine_config"])
    from feed_sync import sync
    with preparing("feed-refresh"):
        feed = sync(adapter["engine_config"])
    ready = (feed.get("status") == "ok"
             and diagnostics.get("status") in {"ok", "complete", "unchanged"})
    value = dict(status="ready" if ready else "incomplete", feed=feed,
                 diagnostics=diagnostics, storage_cleanup=storage_cleanup)
    print(json.dumps(value))
    if not ready:
        raise RuntimeError("MindIE session preparation is incomplete; inspect local status")


if __name__ == "__main__":
    maintain(Path(sys.argv[1]))
