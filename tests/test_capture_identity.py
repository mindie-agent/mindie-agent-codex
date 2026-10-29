"""Actual filesystem identity gates; no model, capture queue or publication."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import codex_transcript as parser
from capture_errors import TranscriptSourceError
from mindie_knowledge.filesystem import filesystem_path, path_identity, git_path


class CaptureIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = filesystem_path(tempfile.mkdtemp(prefix="mindie-capture-identity-"))
        self.home = self.root / "profile"
        self.project = self.root / "project"
        self.project.mkdir()
        self.environment = patch.dict(os.environ, CODEX_HOME=str(self.home))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(lambda: shutil.rmtree(self.root))

    def transcript(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(type="session_meta", payload=dict(
            id="synthetic", cwd=str(self.project), timestamp="2026-10-15T00:00:00Z"))) + "\n",
            encoding="utf-8")
        return path

    def test_foreign_profile_and_parent_escape_fail_with_safe_code(self):
        foreign = self.transcript(self.root / "other/sessions/task.jsonl")
        (self.home / "sessions").mkdir(parents=True)
        for path in (foreign, self.home / "sessions/../../other/sessions/task.jsonl"):
            with self.subTest(path=str(path)), self.assertRaises(TranscriptSourceError) as caught:
                parser.capture_source(path, "synthetic")
            self.assertEqual(caught.exception.failure["code"], "transcript_outside_profile")
            self.assertNotIn(str(self.root), str(caught.exception))

    def test_file_symlink_escape_is_not_a_profile_artifact(self):
        foreign = self.transcript(self.root / "other/task.jsonl")
        link = self.home / "sessions/link.jsonl"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(foreign)
        except TimeoutError:
            raise
        except OSError as exc:
            if os.name == "nt":
                self.skipTest("file symlinks unavailable: " + type(exc).__name__)
            raise
        with self.assertRaises(TranscriptSourceError) as caught:
            parser.capture_source(link, "synthetic")
        self.assertEqual(caught.exception.failure["code"], "transcript_outside_profile")

    def test_session_directory_symlink_cannot_escape_profile(self):
        foreign = self.transcript(self.root / "other/task.jsonl")
        self.home.mkdir()
        try:
            (self.home / "sessions").symlink_to(foreign.parent, target_is_directory=True)
        except TimeoutError:
            raise
        except OSError as exc:
            if os.name == "nt":
                self.skipTest("directory symlinks unavailable: " + type(exc).__name__)
            raise
        with self.assertRaises(TranscriptSourceError) as caught:
            parser.capture_source(self.home / "sessions/task.jsonl", "synthetic")
        self.assertEqual(caught.exception.failure["code"], "transcript_outside_profile")

    @unittest.skipUnless(os.name == "nt", "native Windows namespaces")
    def test_extended_case_aliases_preserve_profile_and_file_identity(self):
        path = self.transcript(self.home / "sessions/task.jsonl")
        ordinary = git_path(path)
        values = [ordinary, str(path), ordinary.swapcase()]
        sources = [parser.capture_source(value, "synthetic") for value in values]
        self.assertEqual(len({path_identity(value["transcript_path"]) for value in sources}), 1)
        identities = [parser.identify(value) for value in values]
        self.assertTrue(all(parser.same_file(identities[0], value) for value in identities))
        read = parser.read_material(values[-1], 0, session_id="synthetic", expected=identities[0])
        self.assertEqual(read["status"], "ok")
        self.assertEqual(read["end"], path.stat().st_size)

    @unittest.skipUnless(os.name == "nt", "native Windows MAX_PATH")
    def test_deep_native_transcript_keeps_extended_io(self):
        self.home = self.root.joinpath(*(["segment-" + "x" * 40] * 5), "profile")
        os.environ["CODEX_HOME"] = git_path(self.home)
        path = self.transcript(self.home / "sessions/task.jsonl")
        self.assertGreater(len(git_path(path)), 260)
        source = parser.capture_source(git_path(path), "synthetic")
        self.assertTrue(source["transcript_path"].startswith("\\\\?\\"))
        identity = parser.identify(git_path(path))
        self.assertIsNotNone(identity)
        read = parser.read_material(git_path(path), 0, session_id="synthetic", expected=identity)
        self.assertEqual(read["status"], "ok")
        self.assertEqual(read["end"], path.stat().st_size)

    @unittest.skipUnless(os.name == "nt", "native Windows junction")
    def test_session_junction_cannot_escape_profile(self):
        foreign = self.transcript(self.root / "outside/task.jsonl")
        self.home.mkdir()
        link = self.home / "sessions"
        result = subprocess.run(
            ["cmd.exe", "/u", "/c", "mklink", "/J", git_path(link), git_path(foreign.parent)],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout.decode("utf-16le", errors="replace"))
        self.addCleanup(link.rmdir)
        with self.assertRaises(TranscriptSourceError) as caught:
            parser.capture_source(link / "task.jsonl", "synthetic")
        self.assertEqual(caught.exception.failure["code"], "transcript_outside_profile")
