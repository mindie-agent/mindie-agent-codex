"""Installation-only process evidence; no network, native install or model."""

import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
RUNNER = "bounded_process"
sys.path.insert(0, str(SCRIPTS))
import candidate_validate as candidate
import product_contract as contract

PRIVATE = "PRIVATE_ARGV_STDOUT_STDERR_SENTINEL"
EXPECTED = {"schema": contract.RECEIPT_SCHEMA, "candidate_revision": "a" * 40}


class PublicationFailureTests(unittest.TestCase):
    def setUp(self):
        self.runner = importlib.import_module(RUNNER)

    def completed(self, kwargs, output="", returncode=0, cleanup=False):
        if RUNNER == "bounded_process":
            return self.runner.ProcessResult(
                "completed", stdout=output, returncode=returncode, stderr=PRIVATE,
                cleanup=[{"stage": "private-" + PRIVATE}] if cleanup else [],
            )
        kwargs["receipt"].update(
            started=True, completed=True, returncode=returncode,
            cleanup_failed=cleanup, cleanup=[{"message": PRIVATE}] if cleanup else [],
        )
        return output

    def projected(self, failure):
        output = json.dumps(dict(EXPECTED, status="failed", failure=failure))
        error = contract.failure_receipt(output, EXPECTED, 1)
        self.assertNotIn(PRIVATE, output)
        self.assertNotIn(PRIVATE, str(error))
        return error

    def test_each_git_substep_stops_once_and_reaches_the_reader(self):
        publication = contract.product(ROOT)[0]["publication"]
        for failing_step, code in enumerate(
            ("init_failed", "fetch_failed", "revision_read_failed"),
        ):
            calls = []

            def run(args, data, **kwargs):
                calls.append(args)
                self.assertEqual(kwargs["timeout"], 60.0)
                return self.completed(
                    kwargs, PRIVATE, 17 if len(calls) - 1 == failing_step else 0,
                )

            with self.subTest(code=code), patch.object(self.runner, "run", side_effect=run), \
                    patch("mindie_knowledge.publication_contract.read_git_contract") as read:
                with self.assertRaises(candidate.CheckFailure) as caught:
                    candidate.publication_contract(publication)
                error = self.projected(caught.exception.failure)
                self.assertEqual((error.stage, error.code), ("publication_fetch", code))
                self.assertEqual(error.process, dict(
                    execution="completed", reason="nonzero_exit", returncode=17,
                    cleanup_failed=False,
                ))
                self.assertEqual(len(calls), failing_step + 1)
                read.assert_not_called()

    def test_success_keeps_exact_revision_and_contract_read(self):
        publication = contract.product(ROOT)[0]["publication"]
        calls = []

        def run(args, data, **kwargs):
            calls.append(args)
            self.assertEqual(kwargs["env"]["GIT_CONFIG_GLOBAL"], "enterprise.gitconfig")
            self.assertEqual(kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "0")
            output = publication["verified_commit"] + "\n" if len(calls) == 3 else ""
            return self.completed(kwargs, output)

        with patch.dict(os.environ, GIT_CONFIG_GLOBAL="enterprise.gitconfig", GIT_CONFIG_NOSYSTEM="0"), \
                patch.object(self.runner, "run", side_effect=run), \
                patch("mindie_knowledge.publication_contract.read_git_contract", return_value={"ok": True}) as read:
            self.assertEqual(candidate.publication_contract(publication), {"ok": True})
        self.assertEqual(len(calls), 3)
        self.assertEqual(read.call_args.args[1], publication["verified_commit"])

    def test_completed_process_and_cleanup_failure_are_both_preserved(self):
        for code in (0, 17):
            with self.subTest(returncode=code), patch.object(
                self.runner, "run",
                side_effect=lambda *args, **kw: self.completed(kw, PRIVATE, code, True),
            ) as run:
                with self.assertRaises(candidate.CheckFailure) as caught:
                    candidate.publication_git(["private-" + PRIVATE], "fetch_failed", {})
                error = self.projected(caught.exception.failure)
                self.assertEqual(error.process, dict(
                    execution="completed",
                    reason="nonzero_exit" if code else "cleanup_failed",
                    returncode=code, cleanup_failed=True,
                ))
                run.assert_called_once()

    def test_spawn_and_unknown_failure_never_echo_exception_text(self):
        for started, cleanup in ((False, False), (False, True), (True, True)):
            def run(*args, **kwargs):
                error = TimeoutError(PRIVATE) if started else FileNotFoundError(PRIVATE)
                if RUNNER == "bounded_process":
                    error.process_result = self.runner.ProcessResult(
                        "unknown" if started else "not_started", stdout=PRIVATE,
                        cleanup=[{"message": PRIVATE}] if cleanup else [],
                    )
                else:
                    kwargs["receipt"].update(started=started, cleanup_failed=cleanup)
                raise error

            with self.subTest(started=started, cleanup=cleanup), patch.object(self.runner, "run", side_effect=run) as process:
                with self.assertRaises(candidate.CheckFailure) as caught:
                    candidate.publication_git(["private-" + PRIVATE], "init_failed", {})
                error = self.projected(caught.exception.failure)
                self.assertEqual(error.process, dict(
                    execution="unknown" if started or RUNNER == "bounded" else "not_started",
                    reason="timeout" if started else "spawn_failed",
                    returncode=None, cleanup_failed=cleanup,
                ))
                process.assert_called_once()

    def test_exception_preserves_explicit_pre_start_and_completed_facts(self):
        for completed in (False, True):
            def run(*args, **kwargs):
                if RUNNER == "bounded_process":
                    error = RuntimeError(PRIVATE)
                    error.process_result = self.runner.ProcessResult(
                        "completed" if completed else "not_started",
                        stdout=PRIVATE, returncode=0 if completed else None,
                    )
                else:
                    error = self.runner.CommandCancelled(PRIVATE)
                    kwargs["receipt"].update(
                        started=completed, completed=completed,
                        returncode=0 if completed else None, cleanup_failed=False,
                    )
                raise error

            with self.subTest(completed=completed), patch.object(self.runner, "run", side_effect=run):
                with self.assertRaises(candidate.CheckFailure) as caught:
                    candidate.publication_git(["private-" + PRIVATE], "fetch_failed", {})
                error = self.projected(caught.exception.failure)
                reason = ("cancelled" if RUNNER == "bounded"
                          else "process_failed" if completed else "spawn_failed")
                self.assertEqual(error.process, dict(
                    execution="completed" if completed else "not_started", reason=reason,
                    returncode=0 if completed else None, cleanup_failed=False,
                ))

    def test_transport_text_is_reduced_to_one_closed_category(self):
        cases = (
            ("fatal: unable to access remote: Could not resolve host: github.com",
             "resolver"),
            ("fatal: unable to access remote: SSL certificate problem: "
             "unable to get local issuer certificate", "certificate"),
            ("fatal: unable to access remote: Recv failure: Connection reset by peer",
             "temporary_network"),
        )
        for text, expected in cases:
            with self.subTest(category=expected), patch.object(
                self.runner, "run",
                return_value=self.runner.ProcessResult(
                    "completed", stdout=PRIVATE, returncode=128, stderr=text),
            ):
                with self.assertRaises(candidate.CheckFailure) as caught:
                    candidate.publication_git(["private-" + PRIVATE], "fetch_failed", {})
                error = self.projected(caught.exception.failure)
                self.assertEqual(error.category, expected)
                self.assertEqual(error.reason_code, "fetch_failed")
                self.assertEqual(error.substage, "publication_fetch")
                self.assertNotIn(text, str(error))
                self.assertEqual(error.process, dict(
                    execution="completed", reason="nonzero_exit", returncode=128,
                    cleanup_failed=False,
                ))

    def test_timeout_and_spawn_failure_use_closed_fallback_categories(self):
        timeout = TimeoutError(PRIVATE)
        timeout.process_result = self.runner.ProcessResult("unknown", stdout=PRIVATE)
        for error, expected in (
            (timeout, "temporary_network"),
            (TimeoutError(PRIVATE), "temporary_network"),
            (FileNotFoundError(PRIVATE), "prerequisite"),
        ):
            with self.subTest(category=expected), patch.object(
                self.runner, "run", side_effect=error,
            ):
                with self.assertRaises(candidate.CheckFailure) as caught:
                    candidate.publication_git(["private-" + PRIVATE], "fetch_failed", {})
                self.assertEqual(caught.exception.failure["category"], expected)
                self.assertNotIn(PRIVATE, str(caught.exception))

    def test_unclassified_output_keeps_the_three_key_process_failure(self):
        with patch.object(self.runner, "run", return_value=self.runner.ProcessResult(
                "completed", stdout=PRIVATE, returncode=17, stderr=PRIVATE)):
            with self.assertRaises(candidate.CheckFailure) as caught:
                candidate.publication_git(["private-" + PRIVATE], "fetch_failed", {})
        self.assertEqual(set(caught.exception.failure),
                         {"stage", "code", "process"})
        self.projected(caught.exception.failure)

    def test_receipt_rejects_an_unclassified_category_label(self):
        failure = dict(stage="publication_fetch", code="fetch_failed", process=dict(
            execution="completed", reason="nonzero_exit", returncode=128,
            cleanup_failed=False,
        ), category=PRIVATE)
        error = contract.failure_receipt(
            json.dumps(dict(EXPECTED, status="failed", failure=failure)), EXPECTED, 1,
        )
        self.assertEqual((error.stage, error.code), ("process", "nonzero_exit"))
        self.assertIsNone(error.category)
        accepted = dict(failure, category="resolver")
        error = contract.failure_receipt(
            json.dumps(dict(EXPECTED, status="failed", failure=accepted)), EXPECTED, 1,
        )
        self.assertEqual((error.stage, error.code), ("publication_fetch", "fetch_failed"))
        self.assertEqual(error.category, "resolver")
        self.assertEqual(error.reason_code, "fetch_failed")
        self.assertEqual(error.substage, "publication_fetch")

    @staticmethod
    def _identity():
        return dict(
            schema=contract.RECEIPT_SCHEMA, candidate_revision="a" * 40,
            source_sha256="b" * 64, product_sha256="c" * 64,
            requirements_sha256="d" * 64,
            runtime={"mindie-knowledge": "e" * 40},
            publication=dict(repository="mindie-agent/knowledge", ref="main",
                             domain="vllm-ascend", verified_commit="f" * 40),
            component_updates=dict(
                schema="mindie-component-runtime/1", adapter_source_sha256="b" * 64,
                runtime={"mindie-knowledge": {"revision": "1" * 40}},
                lock_sha256="2" * 64),
        )

    def test_pure_component_update_reuses_verified_publication_without_network(self):
        actual = self._identity()
        verified = dict(actual, status="validated", runtime={"mindie-knowledge": "3" * 40},
                        component_updates=dict(actual["component_updates"],
                                               lock_sha256="4" * 64))
        calls = []
        for options in (dict(verified_publication=verified),
                        dict(verified_receipt=verified)):
            with self.subTest(options=sorted(options)), \
                    patch.object(candidate, "identity", return_value=actual), \
                    patch.object(candidate, "installed_revisions",
                                 side_effect=lambda pins: calls.append("pins")), \
                    patch("mindie_knowledge.runtime_inventory.verify_lock",
                          side_effect=lambda value: calls.append("lock")), \
                    patch("mindie_knowledge.runtime_inventory.verify",
                          side_effect=lambda value: calls.append("verify")), \
                    patch.object(candidate, "adapter_check",
                                 side_effect=lambda plugin: calls.append("adapter")), \
                    patch.object(candidate, "runtime_check",
                                 side_effect=lambda scripts: calls.append("runtime")), \
                    patch.object(candidate, "publication_contract",
                                 side_effect=AssertionError("network read repeated")):
                result = candidate.validate("source", actual, **options)
            self.assertEqual(result, dict(actual, status="validated"))
        self.assertEqual(calls, ["pins", "lock", "verify", "adapter", "runtime"] * 2)

    def test_verified_publication_mismatch_is_a_receipt_failure(self):
        actual = self._identity()
        mismatch = dict(actual, status="validated",
                        publication=dict(actual["publication"], verified_commit="9" * 40))
        with patch.object(candidate, "identity", return_value=actual), \
                patch.object(candidate, "installed_revisions"), \
                patch("mindie_knowledge.runtime_inventory.verify_lock"), \
                patch("mindie_knowledge.runtime_inventory.verify"), \
                patch.object(candidate, "adapter_check"), \
                patch.object(candidate, "runtime_check"), \
                patch.object(candidate, "publication_contract") as network:
            with self.assertRaises(candidate.CheckFailure) as caught:
                candidate.validate("source", actual, verified_publication=mismatch)
        self.assertEqual(caught.exception.failure, dict(
            stage="receipt", code="receipt_mismatch"))
        network.assert_not_called()

    def test_probe_forwards_a_pure_component_baseline_as_verified_publication(self):
        actual = self._identity()
        verified = dict(actual, status="validated", runtime={"mindie-knowledge": "3" * 40})
        seen = []

        def command(argv, **kwargs):
            seen.append((list(argv), dict(kwargs)))
            return json.dumps(dict(actual, status="validated"))

        with patch.object(contract, "source_root", return_value=ROOT), \
                patch.object(contract, "identity", return_value=actual):
            self.assertEqual(
                contract.probe(sys.executable, SCRIPTS, command,
                               verified_receipt=verified,
                               components=actual["component_updates"]),
                dict(actual, status="validated"))
        argv = seen[0][0]
        self.assertIn("--verified-publication", argv)
        self.assertNotIn("--verified-receipt", argv)
        self.assertEqual(seen[0][1]["timeout"], 180.0)

    def test_real_local_git_init_failure_keeps_exit_status_without_paths(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("Git is a product dependency")
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / PRIVATE
            invalid.write_text("a file cannot be a repository directory", encoding="utf-8")
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
            with self.assertRaises(candidate.CheckFailure) as caught:
                candidate.publication_git(
                    [git, "-C", str(invalid), "init", "--bare", "--quiet"],
                    "init_failed", env,
                )
            error = self.projected(caught.exception.failure)
            self.assertEqual(error.process["execution"], "completed")
            self.assertEqual(error.process["reason"], "nonzero_exit")
            self.assertIs(type(error.process["returncode"]), int)
            self.assertNotEqual(error.process["returncode"], 0)
            self.assertFalse(error.process["cleanup_failed"])
            self.assertNotIn(directory, str(error))

    def test_main_prints_only_safe_failure_protocol(self):
        failure = candidate.CheckFailure("publication_fetch", "fetch_failed", process=dict(
            execution="completed", reason="nonzero_exit", returncode=128,
            cleanup_failed=True,
        ))
        output = io.BytesIO()
        with patch.object(candidate, "validate", side_effect=failure), \
                patch.object(sys, "argv", ["candidate_validate.py", "--source", PRIVATE,
                                          "--identity", json.dumps(EXPECTED)]), \
                patch.object(sys, "stdout", SimpleNamespace(buffer=output)):
            self.assertEqual(candidate.main(), 1)
        value = json.loads(output.getvalue())
        error = self.projected(value["failure"])
        self.assertEqual(error.process["returncode"], 128)
        self.assertNotIn(PRIVATE.encode(), output.getvalue())

    def test_subprocess_failure_reaches_installer_probe_once(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            scripts = source / SCRIPTS.relative_to(ROOT)
            scripts.mkdir(parents=True)
            for name in ("product-contract.json", "pyproject.toml", "uv.lock"):
                shutil.copyfile(SCRIPTS.parent / name, scripts.parent / name)
            shutil.copytree(SCRIPTS.parent / ".codex-plugin", scripts.parent / ".codex-plugin")
            failure = dict(stage="publication_fetch", code="revision_read_failed", process=dict(
                execution="completed", reason="nonzero_exit", returncode=128,
                cleanup_failed=True,
            ))
            (scripts / "candidate_validate.py").write_text(
                "import json,sys\n"
                "value=json.loads(sys.argv[sys.argv.index('--identity')+1])\n"
                "value.update(status='failed', failure=" + repr(failure) + ")\n"
                "print(" + repr(PRIVATE) + ", file=sys.stderr)\n"
                "print(json.dumps(value), flush=True)\nraise SystemExit(1)\n",
                encoding="utf-8",
            )
            calls = []

            def command(args, **kwargs):
                calls.append(args)
                result = self.runner.run(args, "", **kwargs)
                return result.checked_stdout() if RUNNER == "bounded_process" else result

            with self.assertRaises(contract.CandidateValidationError) as caught:
                contract.probe(sys.executable, scripts, command, revision="a" * 40)
            self.assertEqual(caught.exception.code, "revision_read_failed")
            self.assertEqual(caught.exception.process, failure["process"])
            self.assertNotIn(PRIVATE, str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_strict_parser_rejects_malformed_or_contradictory_process_facts(self):
        original = dict(execution="completed", reason="nonzero_exit", returncode=128,
                        cleanup_failed=False)
        changes = [
            {"returncode": True}, {"returncode": "128"}, {"returncode": 1.5},
            {"returncode": -(2 ** 31) - 1}, {"returncode": 2 ** 32},
            {"returncode": None}, {"returncode": 0}, {"cleanup_failed": 1},
            {"reason": PRIVATE}, {"reason": []}, {"execution": []},
            {"execution": "unknown"}, {"execution": "not_started"},
            {"reason": "spawn_failed"}, {"reason": "timeout"},
            {"reason": "owner_exited"}, {"reason": "process_failed"},
            {"reason": "cleanup_failed"},
            {"reason": "cleanup_failed", "returncode": 0},
            {"execution": "not_started", "returncode": 0,
             "reason": "spawn_failed", "cleanup_failed": True},
            {"stderr": PRIVATE},
        ]
        for change in changes:
            failure = dict(stage="publication_fetch", code="fetch_failed",
                           process=dict(original, **change))
            with self.subTest(change=change):
                error = contract.failure_receipt(
                    json.dumps(dict(EXPECTED, status="failed", failure=failure)), EXPECTED, 1,
                )
                self.assertEqual((error.stage, error.code), ("process", "nonzero_exit"))
                self.assertNotIn(PRIVATE, str(error))
        failure = dict(stage="runtime_api", code="api_incompatible", process=original)
        error = self.projected(failure)
        self.assertEqual(error.stage, "process")

    def test_legacy_receipts_and_exit_boundaries_remain_supported(self):
        for code in (-(2 ** 31), 2 ** 32 - 1):
            failure = dict(stage="publication_fetch", code="fetch_failed", process=dict(
                execution="completed", reason="nonzero_exit", returncode=code,
                cleanup_failed=False,
            ))
            self.assertEqual(self.projected(failure).process["returncode"], code)
        error = self.projected(dict(stage="publication_fetch", code="fetch_failed"))
        self.assertEqual((error.stage, error.code, error.process),
                         ("publication_fetch", "fetch_failed", None))
        success = dict(EXPECTED, status="validated")
        self.assertEqual(contract.validate_receipt(json.dumps(success), EXPECTED), success)
        self.assertEqual(contract.failure_receipt(json.dumps(success), EXPECTED, 1).stage, "process")


if __name__ == "__main__":
    unittest.main()
