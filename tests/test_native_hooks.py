"""Only prior enabled/trusted own hooks inherit native trust after update."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import native_hooks
NativeHooks = native_hooks.NativeHooks


class HookTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "hooks").mkdir()
        (self.root / "hooks/hooks.json").write_text(json.dumps(dict(hooks={
            name: [dict(hooks=[dict(type="command", command="fixture command")])]
            for name in ("SessionStart", "Stop")})))
        self.rows = [dict(
            pluginId=native_hooks.PLUGIN, key="fixture:" + event,
            eventName=event, handlerType="command", command="fixture command",
            currentHash="new-" + event, trustStatus="modified", enabled=True,
            sourcePath=str(self.root / "hooks/hooks.json"),
        ) for event in ("sessionStart", "stop")]
        self.approved = [dict(key=row["key"], event=row["eventName"],
                              hash="old-" + row["eventName"]) for row in self.rows]
        fixture = self

        class API:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def hooks(self):
                return copy.deepcopy(fixture.rows)

            def write_trust(self, hook, previous):
                fixture.writes.append((hook["key"], previous))
                for row in fixture.rows:
                    if row["key"] == hook["key"]:
                        row["trustStatus"] = "trusted"
                return True

        self.api = API()
        self.writes = []
        context = patch.object(native_hooks, "NativeHooks", return_value=self.api)
        context.start()
        self.addCleanup(context.stop)

    def test_exact_approved_hooks_are_written_and_read_back(self):
        result = native_hooks.preserve("/native", {}, self.root, self.approved)
        self.assertEqual(result, dict(status="complete", trusted=2, user_choices_preserved=0))
        self.assertEqual(self.writes, [(row["key"], row["hash"]) for row in self.approved])
        self.writes.clear()
        self.assertEqual(native_hooks.preserve("/native", {}, self.root, self.approved), result)
        self.assertEqual(self.writes, [])

    def test_snapshot_includes_only_enabled_trusted_hooks(self):
        self.rows[0]["trustStatus"] = "trusted"
        self.rows[1].update(trustStatus="trusted", enabled=False)
        result = native_hooks.snapshot("/native", {}, self.root)
        self.assertEqual(result, [dict(key=self.rows[0]["key"], event="sessionStart",
                                      hash="new-sessionStart")])

    def test_explicit_disable_or_revoked_trust_is_preserved(self):
        self.rows[0]["enabled"] = False
        with patch.object(self.api, "write_trust", return_value=False) as write:
            result = native_hooks.preserve("/native", {}, self.root, self.approved)
        self.assertEqual(result, dict(status="complete", trusted=0, user_choices_preserved=2))
        write.assert_called_once()
        self.assertEqual(self.writes, [])

    def test_successful_write_with_untrusted_readback_is_not_complete(self):
        with patch.object(self.api, "write_trust", return_value=True):
            with self.assertRaisesRegex(ValueError, "readback differs"):
                native_hooks.preserve("/native", {}, self.root, self.approved)

    def test_missing_or_changed_source_never_receives_trust(self):
        original = copy.deepcopy(self.rows)
        for change in ("missing", "command", "source"):
            with self.subTest(change=change):
                self.rows = copy.deepcopy(original)
                self.writes.clear()
                if change == "missing":
                    self.rows.pop()
                elif change == "command":
                    self.rows[0]["command"] = "other command"
                else:
                    self.rows[0]["sourcePath"] = str(self.root / "other/hooks.json")
                with self.assertRaises(ValueError):
                    native_hooks.preserve("/native", {}, self.root, self.approved)

    def test_public_api_uses_exact_user_layer_and_compare_and_swap(self):
        api = object.__new__(NativeHooks)
        api.home = self.root
        state = {self.rows[0]["key"]: dict(enabled=True, trusted_hash="old-sessionStart")}
        result = dict(config=dict(hooks=dict(state=state)), layers=[
            dict(name=dict(type="user", file=str(self.root / "config.toml"), profile=None),
                 version="observed-version")])
        with patch.object(api, "call", side_effect=[result, {}]) as call:
            self.assertTrue(api.write_trust(self.rows[0], "old-sessionStart"))
        method, params = call.call_args.args
        self.assertEqual(method, "config/value/write")
        self.assertEqual(params["expectedVersion"], "observed-version")
        self.assertEqual(params["value"], self.rows[0]["currentHash"])
        self.assertEqual(params["keyPath"],
                         'hooks.state."fixture:sessionStart".trusted_hash')
        state[self.rows[0]["key"]]["trusted_hash"] = "user-revoked"
        with patch.object(api, "call", return_value=result) as call:
            self.assertFalse(api.write_trust(self.rows[0], "old-sessionStart"))
        call.assert_called_once()

    def test_catalog_cas_preserves_other_fields_and_only_official_source(self):
        api = object.__new__(NativeHooks)
        api.home = self.root
        previous = dict(source_type="git", source=native_hooks.OFFICIAL_SOURCE,
                        ref="stable", sparse_paths=["plugins"], last_revision="a" * 40)
        target = self.root / "catalog"
        selected = dict(previous, source_type="local", source=str(target))

        def view(table):
            return dict(config=dict(marketplaces={"mindie-agent": table}), layers=[
                dict(name=dict(type="user", file=str(self.root / "config.toml")),
                     version="observed-version", config=dict(marketplaces={"mindie-agent": table}))])

        with patch.object(api, "call", side_effect=[view(previous), {}, view(selected)]) as call:
            self.assertEqual(api.select_catalog(target),
                             dict(status="selected", changed=True, response_lost=False))
        self.assertEqual(call.call_args_list[1].args[0], "config/value/write")
        self.assertEqual(call.call_args_list[1].args[1]["value"], selected)
        self.assertEqual(call.call_args_list[1].args[1]["expectedVersion"], "observed-version")
        with patch.object(api, "call", return_value=view(
                dict(previous, source="https://other.invalid/repository"))) as call:
            with self.assertRaisesRegex(ValueError, "not the official"):
                api.select_catalog(target)
        call.assert_called_once()

    def test_catalog_lost_response_reads_exact_result_without_repeating_write(self):
        api = object.__new__(NativeHooks)
        api.home = self.root
        old = dict(source_type="git", source=native_hooks.OFFICIAL_SOURCE)
        new = dict(source_type="local", source=str(self.root / "catalog"))

        def user(table):
            return {}, dict(version="v", config=dict(marketplaces={"mindie-agent": table})), self.root / "config.toml"

        with patch.object(api, "user_config", side_effect=[user(old), user(new)]), \
                patch.object(api, "call", side_effect=TimeoutError("response lost")) as write:
            self.assertTrue(api.select_catalog(self.root / "catalog")["response_lost"])
        write.assert_called_once()
        with patch.object(api, "user_config", side_effect=[user(old), user(old)]), \
                patch.object(api, "call", side_effect=RuntimeError("CAS refused")) as write:
            with self.assertRaisesRegex(RuntimeError, "CAS refused"):
                api.select_catalog(self.root / "catalog")
        write.assert_called_once()


if __name__ == "__main__":
    unittest.main()
