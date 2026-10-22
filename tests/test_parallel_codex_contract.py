"""Independent Codex-adapter consent and capture contract tests.

Judges observable files, process exit codes, capture rows and model-double
invocations. Does not copy production branch conditions. Shared lanes and
fixtures live in `tests.parallel_codex_support`.
"""

import json
import shutil
import unittest

from tests.parallel_codex_support import (
    CHILD_FACT,
    PARENT_SECRET,
    PRE_ADMISSION,
    SENTINEL,
    LaneCase,
    _jsonl,
    _user,
    installed_scanner,
    recorded_wake_runtime,
    wake_requests,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
