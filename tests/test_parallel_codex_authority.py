"""Independent Codex-adapter authority, reuse, and remote-isolation tests.

Uses the shared isolated lanes from `tests.parallel_codex_support`; judges
observable authority files, process outcomes, receipts and model doubles.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path

from tests.parallel_codex_support import (
    CHOICE_PROMPT,
    CORE_COMMIT,
    ENTRY_FAST_PATH_SECONDS,
    PY,
    REPO,
    SCRIPTS,
    LaneCase,
    Sessions,
    consent,
    requirements,
    sharing,
)
from tests.process_fixtures import copy_runtime_scripts


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
                PY, str(REPO / "tests/fixtures/sharing_commands.py"), str(SCRIPTS), "configure",
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
                PY, str(REPO / "tests/fixtures/sharing_commands.py"), str(SCRIPTS), "configure",
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
                PY, str(REPO / "tests/fixtures/sharing_commands.py"), str(SCRIPTS), "configure",
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
                PY, str(REPO / "tests/fixtures/sharing_commands.py"), str(SCRIPTS), "configure",
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
