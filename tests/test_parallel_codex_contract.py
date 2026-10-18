"""Independent Codex-adapter contract tests.

Judges observable files, process exit codes, capture rows and model-double
invocations. Does not copy production branch conditions.

Writes go to tempfile.TemporaryDirectory, or under MINDIE_TEST_ROOT when that
variable is set. Peer adapter and core checkouts are not discovered from the
directory name or a sibling layout: tests that need them require
MINDIE_KIMI_REPO / MINDIE_CORE_REPO and fail with that name if it is missing.
.github/workflows/tests.yml checks out CORE_COMMIT and KIMI_COMMIT and sets
those variables. A missing variable is still a failure, not a skip.
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


class ConsentGateTests(LaneCase):
    def test_corrupt_transcript_fails_without_claiming_complete_capture(self):
        wake = recorded_wake_runtime(self.config, self.root / "recorded-wake")
        self.write_consent("contribute", reporting="disabled")
        self.write_community(enabled=True)
        self.open_store()
        self.activate("task-main")
        transcript = self.native_transcript("corrupt-body.jsonl")
        _jsonl(transcript, [self.session_meta("task-main"),
            _user("Public before corruption", self.after_boundary("task-main", 30))])
        with transcript.open('ab') as stream:
            stream.write(b'{"type":broken}\n')
            stream.write((json.dumps(_user("Public after corruption", self.after_boundary("task-main", 31)))+'\n').encode())
        self.assertEqual(self.stop(self.event("task-main", transcript)).returncode, 0)
        self.drain_worker()
        self.assertEqual(self.saved_text(), '')
        self.assertEqual(self.capture_rows()[0]['status'], 'failed')
        from mindie_knowledge.loop.store import Store
        store = Store(self.root / "data", "test")
        try:
            self.assertIsNone(store.cursor(str(transcript.resolve())))
            row = store.capture_row(self.capture_rows()[0]['id'])
            discarded = json.loads(row['detail'])['records']
            self.assertEqual(len(discarded), 1)
            self.assertEqual(discarded[0]['reason'], 'invalid record')
        finally:
            store.close()
        self.assertEqual({row["session"] for row in wake_requests(wake)}, {"task-main"})

    def test_stop_stores_public_body_and_export_without_tool_or_hidden_material(self):
        wake = recorded_wake_runtime(self.config, self.root / "recorded-wake")
        self.write_consent("contribute", reporting="disabled")
        self.write_community(enabled=True)
        self.open_store()
        self.activate("task-main")
        transcript = self.native_transcript("public-body.jsonl")
        stamp = self.after_boundary("task-main", 30)
        records = [self.session_meta("task-main"), _user("Public request marker", stamp)]
        for channel, text in (("analysis", "hidden-only-marker"), ("commentary", "Public progress marker"), ("final_answer", "Public result marker")):
            records.append(dict(type="response_item", timestamp=stamp, payload=dict(type="message", role="assistant", phase=channel, content=[dict(type="output_text", text=text)])))
        records.append(dict(type="response_item", timestamp=stamp, payload=dict(type="function_call_output", output="tool-only-marker")))
        _jsonl(transcript, records)
        result = self.stop(self.event("task-main", transcript))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.drain_worker(summarize=True)
        expected = "### user\nPublic request marker\n\n### assistant:commentary\nPublic progress marker\n\n### assistant:final_answer\nPublic result marker"
        self.assertEqual(self.saved_text(), expected + "\n\n")
        self.assertEqual(self.model_text(), "")
        from mindie_knowledge.materials.store import validate_package_files
        from mindie_knowledge.loop.export import build_batch
        from mindie_knowledge.loop.store import Store
        from mindie_knowledge.loop import settings
        store = Store(self.root / "data", "test")
        try:
            batch = build_batch(store, settings=settings.load(self.community))
            self.assertIsNotNone(batch)
            files = {item["path"].split("/", 2)[2]: item["content"] for item in batch[2]["files"]}
            public = validate_package_files(files)
            import yaml
            header = yaml.safe_load(files["index.md"][4:].split("\n---\n\n", 1)[0])
            body = "".join(files["blocks/" + block["block_id"] + ".md"].split("\n---\n\n", 1)[1]
                           for block in header["blocks"])
            self.assertEqual(body, expected + "\n\n")
            self.assertEqual(public["entry"]["title"], "Synthetic summary")
        finally:
            store.close()
        self.assertEqual({row["session"] for row in wake_requests(wake)}, {"task-main"})

    def test_disallowed_consent_does_not_capture_or_call_the_model(self):
        """community.enabled=true must not collect when consent is not contribute."""
        cases = {
            "read-only": lambda: self.write_consent("read-only"),
            "later": lambda: self.write_consent("later"),
            "disabled": lambda: self.write_consent("disabled"),
            "corrupt": lambda: (self.root / "mindie-consent.json").write_text("{broken"),
            "missing": lambda: None,
        }
        for name, plant in cases.items():
            with self.subTest(consent=name):
                shutil.rmtree(self.root / "data", ignore_errors=True)
                self.model_log.unlink(missing_ok=True)
                thread = "task-" + name
                self.write_community(enabled=True)
                plant()
                self.open_store()
                self.activate(thread)
                transcript = self.root / f"{name}.jsonl"
                _jsonl(transcript, [
                    self.session_meta(thread),
                    _user(SENTINEL, stamp=self.after_boundary(thread, 30)),
                ])
                result = self.stop(
                    self.event(thread, transcript, turn="turn-" + name),
                    thread=thread,
                )
                rows = self.capture_rows()
                drain_error = None
                if rows:
                    try:
                        self.drain_worker()
                    except Exception as exc:
                        drain_error = f"{type(exc).__name__}: {exc}"
                observed = self.model_text()
                self.assertEqual(
                    (result.returncode, len(rows), observed.count(SENTINEL), observed.count("{")),
                    (1 if name in {"corrupt", "missing"} else 0, 0, 0, 0),
                    "\n".join([
                        f"consent={name}",
                        f"stop_rc={result.returncode}",
                        f"stop_stdout={result.stdout!r}",
                        f"stop_stderr={result.stderr[:400]!r}",
                        f"captures={rows}",
                        f"drain_error={drain_error}",
                        f"model_log={observed[:500]!r}",
                    ]),
                )

    def test_contribute_stop_is_durable_and_the_worker_runs_once(self):
        wake = recorded_wake_runtime(self.config, self.root / "recorded-wake")
        self.write_consent("contribute", reporting="disabled")
        self.write_community(enabled=True)
        self.open_store()
        self.activate("task-main")
        transcript = self.native_transcript("contribute.jsonl")
        _jsonl(transcript, [
            self.session_meta("task-main"),
            _user(SENTINEL, stamp=self.after_boundary("task-main", 30)),
        ])
        first = self.stop(self.event("task-main", transcript))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout), {})
        rows = self.capture_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["session"], "task-main")
        self.drain_worker()
        called = self.model_text()
        self.assertEqual(called, "")
        self.assertEqual(self.saved_text().count(SENTINEL), 1)
        second = self.stop(self.event("task-main", transcript))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.drain_worker()
        self.assertEqual(len(self.capture_rows()), 1, self.capture_rows())
        self.assertEqual(self.model_text(), "")
        self.assertEqual(self.saved_text().count(SENTINEL), 1)
        saved = [
            row["status"] for row in self.capture_rows()
        ]
        self.assertNotIn("queued", saved, self.capture_rows())
        self.assertEqual({row["session"] for row in wake_requests(wake)}, {"task-main"})

    def test_stop_uses_the_native_thread_not_a_parent_session_id(self):
        self.write_consent("contribute", reporting="disabled")
        self.write_community(enabled=True)
        self.open_store()
        self.activate("parent-task")
        self.activate("child-task")
        transcript = self.native_transcript("parent-secret.jsonl")
        _jsonl(transcript, [
            self.session_meta("parent-task"),
            _user(PARENT_SECRET, stamp=self.after_boundary("parent-task", 30)),
        ])
        result = self.stop(
            self.event("parent-task", transcript, turn="turn-parent"),
            thread="child-task",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        rows = self.capture_rows()
        if rows:
            self.drain_worker()
        self.assertEqual(
            [row["session"] for row in rows],
            [],
            "child CODEX_THREAD_ID still captured the parent session: "
            + repr(rows) + " model=" + self.model_text()[:300],
        )
        self.assertNotIn(PARENT_SECRET, self.model_text())

    def test_fork_worker_does_not_send_inherited_parent_text(self):
        wake = recorded_wake_runtime(self.config, self.root / "recorded-wake")
        self.write_consent("contribute", reporting="disabled")
        self.write_community(enabled=True)
        self.open_store()
        self.activate("child-task")
        # Parent text is after admission and before the fork. Exclusion is the
        # fork cut, not the admission cut. The admission cut is its own test.
        transcript = self.native_transcript("fork.jsonl")
        head = self.session_meta(
            "child-task",
            forked_from_id="parent-task",
            timestamp=self.after_boundary("child-task", 20),
        )
        _jsonl(transcript, [
            head,
            _user(PARENT_SECRET, stamp=self.after_boundary("child-task", 5)),
            _user(CHILD_FACT, stamp=self.after_boundary("child-task", 40)),
        ])
        result = self.stop(self.event("child-task", transcript), thread="child-task")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.capture_rows()), 1, self.capture_rows())
        self.drain_worker()
        observed = self.saved_text()
        self.assertEqual(self.model_text(), "")
        self.assertIn(CHILD_FACT, observed, observed[:600])
        self.assertNotIn(PARENT_SECRET, observed, observed[:600])
        self.assertEqual({row["session"] for row in wake_requests(wake)}, {"child-task"})

    def test_material_before_admission_does_not_reach_the_worker(self):
        """Records timestamped before the admission boundary stay unauthorized."""
        wake = recorded_wake_runtime(self.config, self.root / "recorded-wake")
        self.write_consent("contribute", reporting="disabled")
        self.write_community(enabled=True)
        self.open_store()
        self.activate("task-main")
        transcript = self.native_transcript("before-admission.jsonl")
        _jsonl(transcript, [
            self.session_meta("task-main"),
            _user(PRE_ADMISSION, stamp=self.before_boundary("task-main", 60)),
        ])
        result = self.stop(self.event("task-main", transcript))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.capture_rows()
        if rows:
            self.drain_worker()
        self.assertNotIn(PRE_ADMISSION, self.saved_text())
        self.assertEqual(self.model_text(), "")
        self.assertEqual({row["session"] for row in wake_requests(wake)}, {"task-main"})


class AuthorityMigrationTests(LaneCase):
    def test_status_and_load_do_not_rewrite_authority_files(self):
        legacy = self.root / "legacy" / "codex.community.json"
        legacy.parent.mkdir()
        legacy_body = {
            "schema": "mindie-community-config/1",
            "enabled": False,
            "repository": "owner/repo",
            "project_roots": [],
            "idle_seconds": 300,
        }
        legacy.write_text(json.dumps(legacy_body) + "\n")
        adapter = dict(self.adapter, sharing_choice="read-only", community_config=str(legacy))
        self.config.write_text(json.dumps(adapter) + "\n")
        before_config = self.config.read_bytes()
        before_legacy = legacy.read_bytes()
        shared = self.root / "mindie-community.json"
        consent_path = self.root / "mindie-consent.json"
        result = self.bridge("status")
        problems = []
        if self.config.read_bytes() != before_config:
            problems.append("status rewrote the adapter config")
        if legacy.read_bytes() != before_legacy:
            problems.append("status rewrote the legacy settings file")
        if shared.exists():
            problems.append("status created " + str(shared))
        if consent_path.exists():
            problems.append("status created " + str(consent_path))
        if result.returncode != 0:
            problems.append(f"status exit {result.returncode}: {result.stderr[-400:]}")
        self.assertEqual(problems, [], problems)

    def test_field_update_keeps_a_damaged_consent_file(self):
        path = self.root / "mindie-consent.json"
        path.write_text("{broken-consent")
        before = path.read_bytes()
        outcomes = []
        for operation, value in (("choice", "contribute"), ("reporting", "enabled")):
            path.write_bytes(before)
            try:
                if operation == "choice":
                    consent.record_choice(value)
                else:
                    consent.record_reporting(value)
                raised = None
            except Exception as exc:
                raised = type(exc).__name__
            outcomes.append((operation, raised, path.read_bytes() == before))
        self.assertTrue(
            all(item[1] and item[2] for item in outcomes),
            "damaged consent must fail the field update and keep its bytes: " + repr(outcomes),
        )

    def _reap(self, children):
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()

    def test_concurrent_choice_and_reporting_updates_both_survive(self):
        """Both field updates survive one overlapped pair.

        The first process to take the consent file lock pauses inside that
        lock, before its replace. The peer must be blocked on the same lock
        (not merely slow to start). Releasing the holder lets the peer merge.
        No fixed sleep is used to invent an overlap.
        """
        path = self.write_consent("later", reporting="disabled")
        script = self.root / "race_update.py"
        script.write_text(
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "os.environ['MINDIE_AGENT_CONFIG'] = sys.argv[2]\n"
            "op, value, root = sys.argv[3], sys.argv[4], Path(sys.argv[5])\n"
            "from mindie_knowledge import consent_store\n"
            "real_lock = consent_store._lock_file_nb\n"
            "def watched(fd):\n"
            "    (root / f'try-{op}').write_text('1')\n"
            "    real_lock(fd)\n"
            "    if True:\n"
            "        (root / f'got-{op}').write_text('1')\n"
            "        if not (root / 'release').exists() and not (root / 'holder').exists():\n"
            "            (root / 'holder').write_text(op)\n"
            "            deadline = time.time() + 8\n"
            "            while not (root / 'release').exists():\n"
            "                if time.time() > deadline:\n"
            "                    raise SystemExit('holder was not released')\n"
            "                time.sleep(0.01)\n"
            "consent_store._lock_file_nb = watched\n"
            "(root / f'entered-{op}').write_text('1')\n"
            "deadline = time.time() + 5\n"
            "while time.time() < deadline and len(list(root.glob('entered-*'))) < 2:\n"
            "    time.sleep(0.01)\n"
            "import consent\n"
            "if op == 'choice':\n"
            "    consent.record_choice(value)\n"
            "else:\n"
            "    consent.record_reporting(value)\n"
            "(root / f'done-{op}').write_text('1')\n"
        )
        children = []
        try:
            for operation, value in (("choice", "contribute"), ("reporting", "enabled")):
                children.append(subprocess.Popen(
                    [PY, str(script), str(SCRIPTS), str(self.config), operation, value, str(self.root)],
                    env=self.child_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                ))
            holder = self.root / "holder"
            deadline = time.time() + 5
            while time.time() < deadline and (not holder.exists() or len(list(self.root.glob("try-*"))) < 2):
                time.sleep(0.01)
            self.assertTrue(holder.is_file(), "neither update took the consent lock")
            self.assertEqual(len(list(self.root.glob("try-*"))), 2, "both writers must reach the real OS lock")
            self.assertEqual(len(list(self.root.glob("got-*"))), 1, "peer was not blocked on the lock")
            (self.root / "release").write_text("1")
            finished = [child.communicate(timeout=10) for child in children]
        finally:
            self._reap(children)
        codes = [child.returncode for child in children]
        saved = json.loads(path.read_text())
        self.assertEqual(codes, [0, 0], finished)
        self.assertEqual(saved.get("choice"), "contribute", saved)
        self.assertEqual(saved.get("reporting"), "enabled", saved)

    def test_disable_survives_a_concurrent_settings_write(self):
        """A user disable must still be the stored enabled flag.

        One settings writer pauses after it has read the file and is inside
        its replace, which is inside the adapter update lock. The disable
        runs only once that writer leaves. The published file must end
        disabled; a stale enabled rewrite is a lost update.
        """
        self.write_consent("contribute", reporting="disabled")
        self.write_community(enabled=True)
        script = self.root / "settings_race.py"
        script.write_text(
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "os.environ['MINDIE_AGENT_CONFIG'] = sys.argv[2]\n"
            "role, root = sys.argv[3], Path(sys.argv[4])\n"
            "real_replace = os.replace\n"
            "def paused(src, dst):\n"
            "    if role == 'stamp' and Path(dst).name == 'mindie-community.json':\n"
            "        (root / 'at-replace').write_text('1')\n"
            "        deadline = time.time() + 8\n"
            "        while not (root / 'release-stamp').exists():\n"
            "            if time.time() > deadline:\n"
            "                raise SystemExit('stamp was not released')\n"
            "            time.sleep(0.01)\n"
            "    return real_replace(src, dst)\n"
            "os.replace = paused\n"
            "import sharing\n"
            "(root / f'entered-{role}').write_text('1')\n"
            "if role == 'stamp':\n"
            "    with sharing.community_write_lock():\n"
            "        current=sharing.read();current['fixture_extension']='preserved';sharing.write(sharing.configured_path(),current)\n"
            "else:\n"
            "    sharing.set_enabled(False)\n"
            "(root / f'done-{role}').write_text('1')\n"
        )
        children = []
        try:
            stamp = subprocess.Popen(
                [PY, str(script), str(SCRIPTS), str(self.config), "stamp", str(self.root)],
                env=self.child_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            children.append(stamp)
            deadline = time.time() + 8
            while time.time() < deadline and not (self.root / "at-replace").exists():
                if stamp.poll() is not None:
                    break
                time.sleep(0.01)
            stamp_err = ""
            if stamp.poll() is not None and stamp.stderr is not None:
                stamp_err = stamp.stderr.read()
            self.assertTrue((self.root / "at-replace").is_file(), "stamp never reached the settings replace: " + stamp_err)
            disable = subprocess.Popen(
                [PY, str(script), str(SCRIPTS), str(self.config), "disable", str(self.root)],
                env=self.child_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            children.append(disable)
            deadline = time.time() + 2
            while time.time() < deadline and disable.poll() is None and not (self.root / "done-disable").exists():
                time.sleep(0.01)
            overlapped = (self.root / "done-disable").exists()
            (self.root / "release-stamp").write_text("1")
            finished = [child.communicate(timeout=15) for child in children]
        finally:
            self._reap(children)
        codes = [child.returncode for child in children]
        saved = json.loads(self.community.read_text())
        self.assertEqual(
            (overlapped, codes, saved.get("enabled")),
            (False, [0, 0], False),
            "settings writers overlapped={0} codes={1} enabled={2} output={3}".format(
                overlapped, codes, saved.get("enabled"), finished,
            ),
        )


    def test_configure_preserves_corrupt_community_bytes(self):
        self.write_consent("read-only", reporting="later")
        self.community.write_text("{broken-community")
        before = self.community.read_bytes()
        result = subprocess.run(
            [
                PY, str(SCRIPTS / "setup.py"), "configure",
                "--config", str(self.config),
                "--community-repository", "owner/explicit",
                "--community-project-root", str(self.work),
                "--community-visibility", "public",
            ],
            text=True, capture_output=True, timeout=20,
            env=self.child_env(), cwd=str(self.work),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout[-400:])
        self.assertEqual(self.community.read_bytes(), before, "configure replaced a damaged community file")



    def test_configure_preserves_foreign_schema_bytes(self):
        self.write_consent("read-only", reporting="later")
        foreign = b'{"schema":"foreign-format/1","payload":"preserve"}\n'
        self.community.write_bytes(foreign)
        result = subprocess.run(
            [
                PY, str(SCRIPTS / "setup.py"), "configure",
                "--config", str(self.config),
                "--community-repository", "owner/explicit",
                "--community-project-root", str(self.work),
                "--community-visibility", "public",
            ],
            text=True, capture_output=True, timeout=20,
            env=self.child_env(), cwd=str(self.work),
        )
        self.assertNotEqual(result.returncode, 0, result.stdout[-400:])
        self.assertEqual(
            self.community.read_bytes(),
            foreign,
            "configure changed a foreign schema document: " + self.community.read_text()[:240],
        )

    def test_configure_repairs_malformed_managed_values_explicitly(self):
        """An explicit configure may rewrite a schema-valid malformed document."""
        self.write_consent("read-only", reporting="later")
        self.community.write_text(json.dumps({
            "schema": "mindie-community-config/1",
            "enabled": "yes",
            "project_roots": "not-a-list",
            "idle_seconds": 300,
            "sibling_key": {"owned": "extension"},
        }) + "\n")
        result = subprocess.run(
            [
                PY, str(SCRIPTS / "setup.py"), "configure",
                "--config", str(self.config),
                "--community-repository", "owner/explicit",
                "--community-project-root", str(self.work),
                "--community-visibility", "public",
            ],
            text=True, capture_output=True, timeout=20,
            env=self.child_env(), cwd=str(self.work),
        )
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        saved = json.loads(self.community.read_text())
        self.assertEqual(saved["schema"], "mindie-community-config/1")
        self.assertIs(saved["enabled"], True)
        self.assertEqual(saved["repository"], "owner/explicit")
        self.assertEqual(saved["project_roots"], [str(self.work.resolve())])
        self.assertEqual(saved["sibling_key"], {"owned": "extension"})
        self.assertEqual(saved["consent_config"], str(self.root / "mindie-consent.json"))

    def test_current_authority_read_does_not_spawn_the_runtime(self):
        self.write_consent("contribute", reporting="disabled")
        self.write_community(enabled=True)
        before = self.community.read_bytes()
        marker = self.root / "runtime-spawned"
        wrapper = self.root / "not-a-runtime.py"
        wrapper.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).open('a').write('spawned\\n')\n"
            "raise SystemExit(86)\n"
        )
        adapter = json.loads(self.config.read_text())
        adapter["python"] = str(wrapper)
        self.config.write_text(json.dumps(adapter))
        started = time.monotonic()
        view = sharing.read(self.config)
        read_elapsed = time.monotonic() - started
        self.assertFalse(marker.exists(), "read spawned the runtime")
        self.assertEqual(self.community.read_bytes(), before)
        self.assertIsNotNone(view)
        self.assertLess(read_elapsed, ENTRY_FAST_PATH_SECONDS, "read left the entry fast path")

    def test_unreadable_specified_authority_is_not_replaced(self):
        other = self.root / "other" / "enabled.json"
        other.parent.mkdir()
        other.write_text(json.dumps({
            "schema": "mindie-community-config/1",
            "enabled": True,
            "generation": "wide",
            "enabled_at": 1.0,
            "repository": "other/wider",
            "branch": "main",
            "project_roots": [str(self.work), str(self.root / "outside")],
            "idle_seconds": 300,
        }) + "\n")
        (self.root / "outside").mkdir()
        adapter = json.loads(self.config.read_text())
        adapter["community_config"] = str(other)
        self.config.write_text(json.dumps(adapter))
        self.community.write_text("{not-the-other-file")
        # Specified shared path exists, so a reader must not go looking for
        # the other enabled document. POSIX mode bits exercise unreadability;
        # Windows mode bits cannot deny access, so malformed bytes below still
        # cover fail-closed authority selection there. Native ACL denial stays
        # an explicit Windows acceptance gap.
        if os.name == "posix":
            self.community.chmod(0)
            try:
                with self.assertRaises((OSError, ValueError)):
                    sharing.read(self.config)
            finally:
                self.community.chmod(0o600)
        else:
            with self.assertRaises(ValueError):
                sharing.read(self.config)
        with self.assertRaises(ValueError):
            sharing.capture_allowed(
                {"project_root": str(self.work), "root_session": "t", "activated_at": 1},
                str(self.work), self.config,
            )

    def _legacy_enabled(self, path, roots):
        path.write_text(json.dumps({
            "schema": "mindie-community-config/1",
            "enabled": True,
            "generation": "legacy",
            "enabled_at": 1.0,
            "repository": "other/enabled",
            "branch": "main",
            "project_roots": [str(root) for root in roots],
            "idle_seconds": 300,
        }) + "\n")




    def test_configure_rejects_unsupported_development_layout_without_writes(self):
        wide = self.root / "wide.json"
        narrow = self.root / "narrow.json"
        wide.write_text(json.dumps({
            "schema": "mindie-community-config/1",
            "enabled": True,
            "generation": "wide",
            "enabled_at": 1.0,
            "repository": "other/wide",
            "branch": "main",
            "project_roots": [str(self.work), str(self.root)],
            "idle_seconds": 300,
        }) + "\n")
        narrow.write_text(json.dumps({
            "schema": "mindie-community-config/1",
            "enabled": False,
            "repository": "owner/narrow",
            "project_roots": [str(self.work)],
            "idle_seconds": 300,
        }) + "\n")
        engine = dict(self.engine_doc, community_config=str(wide))
        self.engine.write_text(json.dumps(engine))
        adapter = dict(self.adapter, community_config=str(narrow))
        self.config.write_text(json.dumps(adapter))
        result = subprocess.run(
            [
                PY, str(SCRIPTS / "setup.py"), "configure",
                "--config", str(self.config),
                "--community-repository", "owner/explicit",
                "--community-project-root", str(self.work),
                "--community-visibility", "public",
            ],
            text=True,
            capture_output=True,
            timeout=20,
            env=self.child_env(),
            cwd=str(self.work),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unsupported community configuration", result.stderr)
        self.assertEqual(json.loads(self.config.read_text()), adapter)
        self.assertEqual(json.loads(self.engine.read_text()), engine)
        self.assertTrue(wide.is_file() and narrow.is_file())


class CrossAdapterTests(LaneCase):
    def test_running_runtime_matches_the_declared_core(self):
        import mindie_knowledge
        from importlib.metadata import distribution

        self.assertEqual(requirements(REPO)[0]["mindie-knowledge"], CORE_COMMIT)
        direct = json.loads(distribution("mindie-knowledge").read_text("direct_url.json"))
        self.assertEqual((direct.get("vcs_info") or {}).get("commit_id"), CORE_COMMIT)
        module = Path(mindie_knowledge.__file__).resolve()
        prefix = Path(sys.prefix).resolve()
        self.assertIn("site-packages", module.parts)
        self.assertTrue(str(module).startswith(str(prefix)), module)
        for rel in ("consent_store.py", "loop/settings.py"):
            self.assertTrue((module.parent / rel).is_file(), rel)

    def test_kimi_adapter_reads_the_same_profile_consent(self):
        self.write_consent("read-only", reporting="later")
        sibling = self.root / "kimi.json"
        sibling.write_text("{}\n")
        saved = consent.load(sibling)
        self.assertEqual(saved["choice"], "read-only")
        self.assertEqual(saved["reporting"], "later")
        other = self.root / "other-profile"
        other.mkdir()
        (other / "kimi.json").write_text("{}\n")
        isolated = consent.load(other / "kimi.json")
        self.assertEqual(isolated["state"], "missing")
        self.assertFalse((other / "mindie-consent.json").exists())


class EntryReuseTests(LaneCase):
    def _visible(self, payload):
        return {
            "first_use": payload.get("first_use"),
            "next": payload.get("next"),
            "recovery": payload.get("recovery"),
            "diagnostics_choice": (payload.get("diagnostics") or {}).get("choice"),
        }

    def test_second_task_and_fork_do_not_present_choices_again(self):
        self.engine.write_text(json.dumps(self.engine_doc))
        first = self.bridge("status", thread="task-first")
        self.assertEqual(first.returncode, 0, first.stderr)
        opened = json.loads(first.stdout)
        visible = json.dumps(self._visible(opened))
        self.assertIn(CHOICE_PROMPT, visible, visible)
        chosen = self.bridge("sharing-disable", thread="task-first")
        self.assertEqual(chosen.returncode, 0, chosen.stderr)
        self.assertEqual(json.loads(chosen.stdout)["sharing_choice"], "disabled")
        for thread in ("task-second", "fork-of-first"):
            with self.subTest(thread=thread):
                again = self.bridge("status", thread=thread)
                self.assertEqual(again.returncode, 0, again.stderr)
                payload = json.loads(again.stdout)
                shown = json.dumps(self._visible(payload))
                self.assertNotIn(CHOICE_PROMPT, shown, shown)
                self.assertIsNone(payload.get("first_use"), shown)
        saved = json.loads((self.root / "mindie-consent.json").read_text())
        self.assertEqual(saved["choice"], "disabled")

    def test_corrupt_consent_status_is_not_a_fresh_install(self):
        self.write_community(enabled=False)
        (self.root / "mindie-consent.json").write_text("{broken")
        result = self.bridge("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        shown = json.dumps(self._visible(payload))
        self.assertNotIn(CHOICE_PROMPT, shown, shown)
        self.assertNotEqual((payload.get("first_use") or {}).get("state"), "unconfigured")

    def test_reporting_later_is_not_offered_on_the_next_status(self):
        self.write_consent("read-only", reporting="later")
        self.write_community(enabled=False)
        result = self.bridge("status", thread="task-report")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        reporting = (payload.get("diagnostics") or {}).get("reporting") or {}
        choice = (payload.get("diagnostics") or {}).get("choice")
        self.assertIsNone(choice, json.dumps(payload.get("diagnostics"))[:800])
        self.assertIsNot(reporting.get("enabled"), True, reporting)
        again = self.bridge("status", thread="fork-report")
        self.assertEqual(again.returncode, 0, again.stderr)
        again_payload = json.loads(again.stdout)
        self.assertIsNone((again_payload.get("diagnostics") or {}).get("choice"))

    def test_saved_reporting_later_disagrees_with_an_enabled_reporter(self):
        self.write_consent("read-only", reporting="later")
        self.write_community(enabled=False)
        self.diag_config.write_text(json.dumps({
            "schema": "mindie.diagnostics.reporting.v1",
            "purpose": "tool_fault_reporting",
            "decision": "enabled",
            "repository": "mindie-agent/mindie-agent",
            "revision": "a" * 32,
            "roots": [str(self.diag_root)],
        }) + "\n")
        self.diag_config.chmod(0o600)
        result = self.bridge("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        reporting = (payload.get("diagnostics") or {}).get("reporting") or {}
        self.assertIsNot(
            reporting.get("enabled"),
            True,
            "saved reporting=later but status reports the service enabled: " + json.dumps(reporting)[:500],
        )


class RemoteIsolationTests(LaneCase):
    def test_remote_survives_knowledge_failure_and_forks_do_not_share_receipts(self):
        import mcp_gate

        scripts = copy_runtime_scripts(self.root / "runtime-fixture")
        marker = self.root / "runtime-calls.jsonl"
        (scripts / "runtime_call.py").write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "raw = sys.stdin.read()\n"
            f"Path({str(marker)!r}).open('a').write(raw + '\\n')\n"
            "payload = json.loads(raw)\n"
            "if payload.get('surface') == 'remote':\n"
            "    print(json.dumps({'content': [{'type': 'text', 'text': 'remote-ok'}], 'isError': False}))\n"
            "else:\n"
            "    print(json.dumps({'content': [{'type': 'text', 'text': 'knowledge-down'}], 'isError': True}))\n"
        )
        adapter = json.loads(self.config.read_text())
        adapter["runtime_scripts"] = str(scripts)
        self.config.write_text(json.dumps(adapter))
        self.sessions = Sessions(self.config)

        def request(thread, ident):
            return {
                "jsonrpc": "2.0",
                "id": ident,
                "method": "tools/call",
                "params": {
                    "name": "remote_job_status",
                    "arguments": {"job_id": "job-1"},
                    "_meta": {
                        "x-codex-turn-metadata": {
                            "session_id": "tree-parent",
                            "thread_id": thread,
                            "turn_id": "turn-1",
                        },
                        "threadId": thread,
                    },
                },
            }

        remote = mcp_gate.Gate("remote")
        cold = remote.call(request("parent-task", 1))
        self.assertEqual(cold.get("isError"), False, cold)
        self.assertIn("remote-ok", json.dumps(cold))
        self.assertEqual(self.capture_rows(), [])
        self.engine.write_text("{not-an-engine")
        knowledge = mcp_gate.Gate("knowledge")
        broken = knowledge.call(request("parent-task", 2) | {
            "params": {
                **request("parent-task", 2)["params"],
                "name": "knowledge_query",
                "arguments": {"query": "x"},
            }
        })
        self.assertEqual(broken.get("isError"), True, broken)
        again = remote.call(request("parent-task", 3))
        self.assertEqual(again.get("isError"), False, again)
        child = mcp_gate.Gate("remote")
        child_result = child.call(request("child-task", 1))
        self.assertEqual(child_result.get("isError"), False, child_result)
        parent_db = self.remote_state / "gate-v1" / "parent-task.sqlite3"
        child_db = self.remote_state / "gate-v1" / "child-task.sqlite3"
        self.assertTrue(parent_db.is_file(), list((self.remote_state / "gate-v1").glob("*")))
        self.assertTrue(child_db.is_file())
        with closing(sqlite3.connect(parent_db)) as db:
            parent_ids = db.execute("SELECT identity FROM attempts").fetchall()
        with closing(sqlite3.connect(child_db)) as db:
            child_ids = db.execute("SELECT identity FROM attempts").fetchall()
        self.assertTrue(parent_ids)
        self.assertTrue(child_ids)
        self.assertEqual(set(parent_ids) & set(child_ids), set())
        self.assertFalse((self.root / "mindie-consent.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
