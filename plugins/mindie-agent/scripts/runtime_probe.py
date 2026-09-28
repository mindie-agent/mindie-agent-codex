"""Shared installed-runtime contract used by setup and the updater."""

import os


PROBE_MODULES = (
    "mindie_knowledge.loop.cli",
    "mindie_knowledge.loop.documents",
    "mindie_knowledge.loop.activation",
    "remote_dev.mcp.server",
)


# One FTS capability check feeds both installation paths. The small public
# wrappers below preserve focused checks for the setup/update call sites while
# sharing this exact body with the complete runtime probe.
FTS_PROBE_BODY = r"""
db = None
try:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE probe USING fts5(body, content='', contentless_delete=1)")
    db.execute("INSERT INTO probe(rowid, body) VALUES (1, 'alpha')")
    if db.execute("SELECT rowid FROM probe WHERE probe MATCH 'alpha'").fetchall() != [(1,)]:
        raise RuntimeError("insert MATCH failed")
    db.execute("UPDATE probe SET body='beta' WHERE rowid=1")
    if db.execute("SELECT rowid FROM probe WHERE probe MATCH 'beta'").fetchall() != [(1,)]:
        raise RuntimeError("update MATCH failed")
    if db.execute("SELECT rowid FROM probe WHERE probe MATCH 'alpha'").fetchall():
        raise RuntimeError("stale MATCH survived update")
    db.execute("DELETE FROM probe WHERE rowid=1")
    if db.execute("SELECT rowid FROM probe WHERE probe MATCH 'beta'").fetchall():
        raise RuntimeError("delete MATCH failed")
except Exception as exc:
    missing.insert(0, (
        "sqlite " + sqlite3.sqlite_version
        + " lacks FTS5 contentless_delete=1 (SQLite >=3.43.0): "
        + type(exc).__name__ + ": " + str(exc)[:160]
    ))
finally:
    if db is not None:
        db.close()
"""

SETUP_FTS_PROBE = "import sqlite3\n" + FTS_PROBE_BODY
UPDATER_FTS_PROBE = (
    "import sqlite3\nmissing = []\n" + FTS_PROBE_BODY
    + "print('MISSING: ' + '; '.join(missing) if missing else 'OK')\n"
)


def build_probe_script(transcript_adapter):
    """Return a side-effect-free probe for the configured interpreter.

    Keep setup and update acceptance on one contract. In particular,
    MaintenanceBudget has per-session and per-hour quotas; it does not expose
    a failure limit or a domain-wide failure pause.
    """
    adapter = os.fspath(transcript_adapter)
    return f'''import importlib.util, inspect, math
missing = []
missing_packages = [name for name in ("mindie_knowledge", "remote_dev")
                    if importlib.util.find_spec(name) is None]
if missing_packages:
    missing.append("missing packages: " + ", ".join(missing_packages))
else:
    try:
        from mindie_knowledge.loop.cli import STARTUP_TIMEOUT, MAX_STARTUP_PROBES, load_transcript_adapter
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.budget import MaintenanceBudget
        from mindie_knowledge.loop.limits import ORGANIZER_TIMEOUT, ORGANIZER_PROCESS_TIMEOUT, ORGANIZER_LEASE_SECONDS
        from mindie_knowledge.loop.process import spawn_service
        from mindie_knowledge.loop.engine import Engine
        from mindie_knowledge.loop.transport import Service
        from mindie_knowledge.loop import documents, locks
        from mindie_knowledge.community import submit_batch, reconcile_batch
        from remote_dev.mcp.tools import call_tool
    except Exception as exc:
        missing.append(f"pinned runtime import ({{type(exc).__name__}}: {{exc}})")
    else:
        if not callable(load_transcript_adapter):
            missing.append("load_transcript_adapter is unavailable")
        if not (0 < ORGANIZER_TIMEOUT < ORGANIZER_PROCESS_TIMEOUT < ORGANIZER_LEASE_SECONDS):
            missing.append("organizer lifetime bounds are inconsistent")
        if not callable(getattr(locks, "lock_held", None)):
            missing.append("core locks lacks lock_held")
        if not callable(call_tool):
            missing.append("remote_dev call_tool is unavailable")
        if not callable(getattr(Engine, "stop_if_idle", None)):
            missing.append("Engine.stop_if_idle is unavailable")
        if not callable(getattr(Service, "_stop_if_idle", None)):
            missing.append("Service._stop_if_idle is unavailable")
        if not all(callable(getattr(documents, name, None)) for name in ("render_entry", "parse_entry", "revision_of")):
            missing.append("knowledge document API is incomplete")
        if not callable(submit_batch) or not callable(reconcile_batch):
            missing.append("community receipt API is incomplete")
        if "path" not in inspect.signature(Admission.__init__).parameters:
            missing.append("Admission does not take an explicit admission path")
        methods = ("activate", "inspect", "check", "resolve", "claim", "finish", "deactivate", "capture_lease", "active_lease", "scope_root", "allows_hash", "leases")
        if not all(callable(getattr(Admission, name, None)) for name in methods):
            missing.append("Admission API is incomplete")
        if "admission" not in inspect.signature(Service).parameters:
            missing.append("Service does not accept admission")
        for name in ("SESSION_LIMIT", "HOURLY_LIMIT"):
            value = getattr(MaintenanceBudget, name, None)
            if type(value) is not int or value <= 0:
                missing.append("MaintenanceBudget." + name + " must be a positive integer")
        value = getattr(MaintenanceBudget, "SESSION_WINDOW", None)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            missing.append("MaintenanceBudget.SESSION_WINDOW must be finite and positive")
        for name, value in (("STARTUP_TIMEOUT", STARTUP_TIMEOUT), ("MAX_STARTUP_PROBES", MAX_STARTUP_PROBES)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                missing.append(name + " must be finite and positive")
        if type(MAX_STARTUP_PROBES) is not int:
            missing.append("MAX_STARTUP_PROBES must be an integer")
        try:
            module = load_transcript_adapter({{"transcript_adapter": {adapter!r}}})
            if module is None or not all(hasattr(module, name) for name in ("FileIdentity", "identify", "read_material")):
                missing.append("transcript adapter API is incomplete")
        except Exception as exc:
            missing.append(f"transcript adapter ({{type(exc).__name__}}: {{exc}})")

import sqlite3
''' + FTS_PROBE_BODY + '''
print("MISSING: " + "; ".join(missing) if missing else "OK", flush=True)
'''
