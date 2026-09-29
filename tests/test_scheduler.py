"""Isolated scheduler registration and readback contracts."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests.process_fixtures import cleanup_temporary_directory

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import auto_update


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # OS simulations must never write to the real user's scheduler files,
        # even when a simulated platform branch is incomplete or regresses.
        self.home = patch.object(Path, 'home', return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)
        self.updates = self.root / "updates"
        self.updates.mkdir()
        self.settings = self.root / "updater.json"
        self.settings.write_text(json.dumps({
            "root": str(self.updates),
            "adapter_config": str(self.root / "adapter.json"),
            "codex_home": str(self.root / "codex"),
            "codex": "codex-fixture",
            "python": sys.executable,
            "schedule_mode": "auto",
        }))
        self.updater = auto_update.Updater(self.settings)

    def tearDown(self):
        cleanup_temporary_directory(self.temp)

    def test_systemd_timer_enable_and_disable_uses_exact_owned_units(self):
        unit_root = self.root / "profile with spaces" / "systemd" / "user"
        state = {"load": "not-found", "active": "inactive", "enabled": ""}
        calls = []

        def native(_updater, argv, timeout):
            argv = list(map(str, argv))
            calls.append(argv)
            tail = argv[2:]
            if tail[:1] == ["show"]:
                out = (
                    f"LoadState={state['load']}\n"
                    f"ActiveState={state['active']}\n"
                    f"UnitFileState={state['enabled']}\n"
                )
                return 0, out, ""
            if tail == ["daemon-reload"]:
                if not (unit_root / auto_update.SYSTEMD_TIMER).exists():
                    state.update(load="not-found", active="inactive", enabled="")
                return 0, "", ""
            if tail == ["enable", "--now", auto_update.SYSTEMD_TIMER]:
                state.update(load="loaded", active="active", enabled="enabled")
                return 0, "", ""
            if tail == ["disable", "--now", auto_update.SYSTEMD_TIMER]:
                state.update(load="loaded", active="inactive", enabled="disabled")
                return 0, "", ""
            raise AssertionError(argv)

        with (
            patch.object(auto_update, "_native_run", side_effect=native),
            patch.object(
                auto_update,
                "os",
                SimpleNamespace(**{**vars(os), "name": "posix"}),
            ),
            patch.object(auto_update.sys, "platform", "linux"),
        ):
            result = auto_update._schedule_systemd_enable(
                self.updater,
                self.root / "updates" / "launcher.py",
                self.settings,
                schedule_root=unit_root,
            )
            self.assertEqual(auto_update._systemd_state(self.updater)[0], "active")
            auto_update.schedule_disable(
                self.updater,
                systemd_root=unit_root,
            )
            self.assertEqual(auto_update._systemd_state(self.updater)[0], "absent")

        self.assertEqual(result, f"systemd user timer: {auto_update.SYSTEMD_TIMER}")
        timer_file = unit_root / auto_update.SYSTEMD_TIMER
        service_file = unit_root / auto_update.SYSTEMD_SERVICE
        self.assertFalse(timer_file.exists())
        self.assertFalse(service_file.exists())
        self.assertIn(
            ["systemctl", "--user", "enable", "--now", auto_update.SYSTEMD_TIMER],
            calls,
        )
        self.assertIn(
            ["systemctl", "--user", "disable", "--now", auto_update.SYSTEMD_TIMER],
            calls,
        )

    def test_windows_task_registration_and_removal_use_exact_user_task(self):
        states = iter(("1", "1", "0"))
        commands = []

        def native(_updater, argv, _timeout):
            argv = list(map(str, argv))
            commands.append(argv)
            if "Get-ScheduledTask" in argv[-1]:
                return 0, next(states) + "\n", ""
            if "Unregister-ScheduledTask" in argv[-1]:
                return 0, "", ""
            raise AssertionError(argv)

        with (
            patch.object(auto_update.sys, 'platform', 'win32'),
            patch.object(
                auto_update,
                "os",
                SimpleNamespace(**{**vars(os), "name": "nt"}),
            ),
            patch.object(auto_update, "_native_run", side_effect=native),
            patch.object(self.updater, "command") as command,
        ):
            result = auto_update.schedule_enable(
                self.updater,
                self.root / "updates" / "launcher.py",
                self.settings,
            )
            self.assertEqual(result, auto_update.WIN_TASK)
            auto_update.schedule_disable(self.updater)

        command.assert_called_once()
        task_command = command.call_args.args[0]
        self.assertEqual(task_command[:4], [
            "schtasks", "/Create", "/TN", auto_update.WIN_TASK,
        ])
        self.assertEqual(
            sum("Get-ScheduledTask" in argv[-1] for argv in commands), 3
        )
        self.assertTrue(any("Unregister-ScheduledTask" in argv[-1] for argv in commands))

    def test_manual_schedule_disable_does_not_touch_a_scheduler(self):
        self.updater.settings["schedule_mode"] = "manual"
        with patch.object(auto_update, "_native_run") as native:
            auto_update.schedule_disable(self.updater)
        native.assert_not_called()

    def test_manual_mode_status_is_truthful_without_a_scheduler(self):
        self.updater.settings["schedule_mode"] = "manual"
        self.assertEqual(
            auto_update.schedule_status(self.updater),
            {"mode": "manual", "registered": False},
        )


if __name__ == "__main__":
    unittest.main()
