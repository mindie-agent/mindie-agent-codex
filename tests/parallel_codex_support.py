"""Shared lanes and small fixtures for the Codex adapter contract tests.

Kept outside the two collected test modules so each contract file stays below
its redline while observing the same isolated host profile and runtime lanes.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from tests.process_fixtures import (
    cleanup_temporary_directory,
    copy_runtime_scripts,
    extract_git_archive,
    stop_owned_knowledge_service,
    installed_scanner,
    recorded_wake_runtime,
    wake_requests,
    public_engine_config,
)


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins/mindie-agent/scripts"

sys.path.insert(0, str(SCRIPTS))
from product_contract import requirements
CORE_COMMIT = requirements(REPO)[0]["mindie-knowledge"]


def _test_root():
    value = os.environ.get("MINDIE_TEST_ROOT")
    if not value:
        return None
    root = Path(value).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    _refuse_user_config(root)
    return root
PLUGIN = REPO / "plugins/mindie-agent"
PY = sys.executable
sys.path.insert(0, str(SCRIPTS))

import consent  # noqa: E402
import session_gate  # noqa: E402
import sharing  # noqa: E402
from session_gate import Sessions  # noqa: E402

CHOICE_PROMPT = "Experience capture is not configured."
SENTINEL = "CODEX-CONTRACT-SENTINEL-7f3a"
PARENT_SECRET = "PARENT-ONLY-SECRET-9c2e"
CHILD_FACT = "CHILD-FORK-FACT-1b80"
PRE_ADMISSION = "PRE-ADMISSION-SECRET-4e11"
# Same bound as the sharing-off entry path. A runtime subprocess does not fit it.
ENTRY_FAST_PATH_SECONDS = 0.75


def _refuse_user_config(path):
    resolved = Path(path).resolve()
    home = Path.home().resolve()
    for name in (".codex", ".config", ".local"):
        blocked = (home / name).resolve()
        if resolved == blocked or blocked in resolved.parents:
            raise AssertionError("refusing a real user config path: " + str(resolved))
    return resolved


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _session_meta(session, **payload):
    body = {"id": session, "cwd": "/work"}
    body.update(payload)
    return {"type": "session_meta", "payload": body}


def _iso_at(moment):
    """UTC second at or after moment. Transcript stamps are whole seconds."""
    whole = math.ceil(moment - 1e-6)
    return datetime.fromtimestamp(whole, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_before(moment, seconds):
    """UTC second strictly before moment, by at least `seconds`."""
    whole = math.floor(moment) - seconds
    return datetime.fromtimestamp(whole, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user(text, stamp):
    return {
        "timestamp": stamp,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.platform_env = {
            key: os.environ[key]
            for key in (
                "PATH", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
                "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
            )
            if key in os.environ
        }
        self.temp = tempfile.TemporaryDirectory(dir=_test_root())
        self.root = _refuse_user_config(self.temp.name)
        self.home = self.root / "home"
        self.codex_home = self.root / "codex-home"
        self.diag_config = self.root / "diagnostics.json"
        self.diag_root = self.root / "diagnostics-state"
        self.remote_state = self.root / "remote"
        self.config = self.root / "codex.json"
        self.engine = self.root / "engine.json"
        self.admission = self.root / "admission.sqlite3"
        self.community = self.root / "mindie-community.json"
        self.work = self.root / "work"
        self.model_log = self.root / "model-calls.jsonl"
        self.double = self.root / "model_double.py"
        for path in (
            self.home, self.codex_home, self.diag_root, self.remote_state, self.work,
        ):
            path.mkdir(parents=True)
            _refuse_user_config(path)
        self.double.write_text(
            "import json, sys\n"
            "from pathlib import Path\n"
            "raw = sys.stdin.read()\n"
            "Path(sys.argv[1]).open('a').write(raw + '\\n')\n"
            "print(json.dumps({'entries': [{'title': 'observed', "
            "'summary': 'observed', 'content': 'observed', 'conditions': {}}]}))\n"
        )
        self.engine_doc = public_engine_config(
            self.root / "data", adapter_profile=self.config,
            admission_path=str(self.admission), community_config=str(self.community),
        )
        self.engine.write_text(json.dumps(self.engine_doc))
        self.adapter = {
            "python": PY,
            "engine_config": str(self.engine),
            "community_config": str(self.community),
            "admission_path": str(self.admission),
            "runtime_scripts": str(SCRIPTS),
        }
        self.config.write_text(json.dumps(self.adapter))
        self.env_patch = patch.dict(os.environ, self.child_env(), clear=True)
        self.env_patch.start()
        # tempfile caches the first usable directory for the whole process.
        # Point that cache at this test's lane directory after the env patch,
        # and drop it before the directory is removed.
        tempfile.tempdir = None
        tempfile.gettempdir()
        session_gate.bind_explicit_config(None)
        self.sessions = Sessions(self.config)

    def _stop_owned_engine(self):
        """Stop only the service reached through this fixture's engine file."""
        # Fault tests deliberately corrupt the adapter's engine document.
        # Cleanup retains the original store identity rather than treating
        # that expected configuration failure as a second product failure.
        cleanup_config = self.root / "cleanup-engine.json"
        cleanup_config.write_text(json.dumps(self.engine_doc))
        stop_owned_knowledge_service(cleanup_config)

    def tearDown(self):
        try:
            self._stop_owned_engine()
        except BaseException as exc:
            # Preserve the shutdown failure and its files. Deleting a still
            # owned SQLite tree can both damage the fixture and hide the cause.
            self.temp._finalizer.detach()
            exc.add_note(f"Test engine cleanup failed; retained {self.root}")
            raise
        finally:
            session_gate.bind_explicit_config(None)
            tempfile.tempdir = None
            self.env_patch.stop()
        cleanup_temporary_directory(self.temp)

    def child_env(self, **extra):
        env = dict(self.platform_env)
        env.update({
            "PATH": env.get("PATH", os.defpath),
            "HOME": str(self.home),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "XDG_DATA_HOME": str(self.root / "xdg-data"),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "CODEX_HOME": str(self.codex_home),
            "MINDIE_AGENT_CONFIG": str(self.config),
            "MINDIE_DIAGNOSTICS_CONFIG": str(self.diag_config),
            "MINDIE_DIAGNOSTICS_ROOT": str(self.diag_root),
            "MINDIE_REMOTE_STATE_DIR": str(self.remote_state),
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CODEX_THREAD_ID": "task-main",
        })
        if os.name == "nt":
            env.update(
                USERPROFILE=str(self.home),
                HOMEDRIVE=self.home.drive,
                HOMEPATH=str(self.home)[len(self.home.drive):],
                TMP=str(self.root / "tmp"),
                TEMP=str(self.root / "tmp"),
            )
        for key in ("MINDIE_KIMI_REPO", "MINDIE_CORE_REPO"):
            if key in os.environ:
                env[key] = os.environ[key]
        (self.root / "tmp").mkdir(exist_ok=True)
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            Path(env[key]).mkdir(exist_ok=True)
        env.update(extra)
        for value in env.values():
            if isinstance(value, str) and value.startswith(str(self.root)):
                _refuse_user_config(value)
        return env

    def write_community(self, *, enabled=True, roots=None, **extra):
        body = {
            "schema": "mindie-community-config/1",
            "enabled": enabled,
            "generation": "g-contract",
            "enabled_at": 1.0 if enabled else None,
            "repository": "mindie-agent/knowledge",
            "branch": "main",
            "project_roots": [str(root) for root in (roots or [self.work])],
            "idle_seconds": 300,
            "visibility": "public",
            "consent_config": str(self.root / "mindie-consent.json"),
        }
        body.update(extra)
        self.community.parent.mkdir(parents=True, exist_ok=True)
        self.community.write_text(json.dumps(body) + "\n")
        return body

    def write_consent(self, choice, reporting="later"):
        path = self.root / "mindie-consent.json"
        path.write_text(json.dumps({
            "schema": "mindie-consent/1",
            "choice": choice,
            "reporting": reporting,
        }) + "\n")
        return path

    def activate(self, thread):
        with (
            patch.dict(os.environ, {"CODEX_THREAD_ID": thread}),
            patch.object(Path, "cwd", return_value=self.work),
        ):
            return self.sessions.activate()

    def authorization_boundary(self, session):
        """The same max(enabled_at, activated_at) the engine uses."""
        from mindie_knowledge.loop.activation import Admission

        lease = Admission(str(self.admission)).active_lease(session)
        if not lease or not isinstance(lease.get("activated_at"), (int, float)):
            raise AssertionError("no admission boundary for " + session)
        try:
            enabled_at = json.loads(self.community.read_text()).get("enabled_at") or 0
        except TimeoutError:
            raise
        except (OSError, ValueError):
            enabled_at = 0
        if isinstance(enabled_at, bool) or not isinstance(enabled_at, (int, float)):
            enabled_at = 0
        return max(float(enabled_at), float(lease["activated_at"]))

    def after_boundary(self, session, seconds):
        return _iso_at(self.authorization_boundary(session) + seconds)

    def before_boundary(self, session, seconds):
        return _iso_before(self.authorization_boundary(session), seconds)

    def open_store(self):
        from mindie_knowledge.loop.store import Store

        store = Store(self.root / "data", "test")
        store.close()

    def stop(self, event, thread="task-main"):
        result = subprocess.run(
            [PY, str(SCRIPTS / "bridge.py"), "--config", str(self.config), "stop"],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            timeout=8,
            cwd=str(self.work),
            env=self.child_env(CODEX_THREAD_ID=thread),
        )
        return result

    def session_meta(self, session, **payload):
        return _session_meta(session, cwd=str(self.work),
                             **dict({"timestamp": _iso_before(time.time(), 1)}, **payload))

    def native_transcript(self, name):
        return self.codex_home / 'sessions' / name

    def event(self, session, transcript, turn="turn-1"):
        return {
            "hook_event_name": "Stop",
            "session_id": session,
            "turn_id": turn,
            "cwd": str(self.work),
            "transcript_path": str(transcript),
            "last_assistant_message": "bounded summary",
        }

    def capture_rows(self):
        path = self.root / "data" / "test" / "state-v1" / "state-v4.sqlite3"
        if not path.exists():
            return []
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in db.execute(
                "SELECT id, session, status, transcript, summary FROM captures"
            )]
        except sqlite3.Error:
            return []
        finally:
            db.close()

    def model_text(self):
        if not self.model_log.exists():
            return ""
        return self.model_log.read_text()

    def saved_text(self):
        from mindie_knowledge.loop.store import Store
        store = Store(self.root / "data", "test")
        try:
            return "\n".join(doc["content"] for doc in store.drafts_changed())
        finally:
            store.close()

    def drain_worker(self, *, summarize=False):
        from tests.k3_material_fixture import summary_command
        from mindie_knowledge.loop.activation import Admission
        from mindie_knowledge.loop.cli import load_transcript_adapter
        from mindie_knowledge.loop.engine import Engine
        from mindie_knowledge.loop.store import Store

        store = Store(self.root / "data", "test")
        try:
            engine = Engine(
                store,
                capture_mode="public-transcript", redactor_executable=installed_scanner(),
                summary_command=summary_command() if summarize else None,
                settings_path=str(self.community),
                admission=Admission(str(self.admission)),
                transcript_adapter=load_transcript_adapter(
                    {"transcript_adapter": str(SCRIPTS / "codex_transcript.py")}
                ),
            )
            for row in self.capture_rows():
                if row["status"] in {"queued", "pending", "deferred"}:
                    engine._process(row["id"])
            if summarize:
                from mindie_knowledge.loop.transcript_capture import summarize_due
                engine.last_activity = time.monotonic() - 10
                with store._write_txn():
                    store.db.execute('UPDATE transcript_tasks SET summary_due=0')
                summarize_due(engine)
        finally:
            store.close()

    def bridge(self, *args, thread="task-main", stdin=""):
        return subprocess.run(
            [PY, str(SCRIPTS / "bridge.py"), "--config", str(self.config), *args],
            input=stdin,
            text=True,
            capture_output=True,
            timeout=15,
            cwd=str(self.work),
            env=self.child_env(CODEX_THREAD_ID=thread),
        )
