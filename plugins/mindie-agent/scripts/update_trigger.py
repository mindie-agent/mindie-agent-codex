"""Coalesce background update checks into the lifetime of the native MCP process.

[Role]
Schedules and rate-limits background candidate update checks inside the MCP
server process without blocking tool calls.

[Invariants]
- Enforces `CHECK_INTERVAL = 86400`, `FAILURE_BACKOFF = 60`, and
  `ACTIVATION_BACKOFF = 5`.
- Never blocks foreground MCP tool execution.

[Timeouts & Locks]
- Uses non-blocking `update_lock` and bounded subprocess execution for background
  update probes.

[Side-Effect Policy]
- Writes update check timestamps and status under `<config>.updates/`.
"""
import json
import math
import os
from pathlib import Path
import threading
import time
import sys

from bounded_process import run
from product_contract import read_json
from session_gate import runtime_env, runtime_scripts
from update_lock import update_lock

CHECK_INTERVAL = 86400
FAILURE_BACKOFF = 60
ACTIVATION_BACKOFF = 5


def directory(config):
    return Path(config).with_suffix(".updates")


def read_optional(path):
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return None
    return read_json(path)[0]


def next_check(config):
    """This timestamp is request throttling only, never release selection."""
    path = directory(config) / "status.json"
    value = read_optional(path)
    if value is None:
        return 0
    attempted = value.get("checked_at", 0)
    if type(attempted) not in (int, float) or not math.isfinite(attempted):
        raise ValueError("invalid update check timestamp")
    if value.get("status") in {"prepared", "deferred", "incomplete"}:
        return attempted + ACTIVATION_BACKOFF
    if value.get("status") in {"failed", "unknown", "disabled"}:
        return attempted + FAILURE_BACKOFF
    successful = value.get("last_successful_check", 0)
    if type(successful) not in (int, float) or not math.isfinite(successful):
        raise ValueError("invalid successful update check timestamp")
    return successful + CHECK_INTERVAL if successful else 0


def due(config, now=None):
    return (time.time() if now is None else now) >= next_check(config)


class Maintenance:
    def __init__(self, config):
        self.config = Path(config)
        self.cancel = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.next_attempt = 0
        self.last_failure = None

    def trigger(self):
        """Never wait for a network request, installation, or another updater."""
        with self.lock:
            if self.cancel.is_set() or self.thread is not None and self.thread.is_alive():
                return
            if time.time() < self.next_attempt:
                return
            try:
                self.next_attempt = next_check(self.config)
                if time.time() < self.next_attempt:
                    return
            except (OSError, ValueError):
                # Let the selected helper report a damaged maintenance receipt;
                # it must not block unrelated business at this trigger boundary.
                pass
            self.thread = threading.Thread(target=self._run, name="mindie-release-check")
            self.thread.start()

    def _run(self):
        failed = False
        outcome = None
        try:
            with update_lock(self.config):
                value = read_json(self.config)[0]
                argv = [value["python"], "-I",
                        str(Path(runtime_scripts(value)) / "harness_update.py"),
                        "--config", str(self.config)]
                environment = runtime_env(self.config)
            output = run(argv, "", env=environment, cancel=self.cancel,
                         allow_service=True, timeout=1500.0).checked_stdout()
            outcome = json.loads(output)
            if not isinstance(outcome, dict):
                raise ValueError("update helper returned an invalid status")
            if outcome.get("status_receipt_error_type"):
                raise RuntimeError("update helper could not save its status")
        except Exception as exc:
            failed = True
            # The owned helper emits its safe failure status. A failure before
            # it can do so is retained locally without becoming model context.
            from release_activation import _atomic
            try:
                root = directory(self.config)
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.last_failure = dict(stage="maintenance-process", status="failed",
                                         checked_at=time.time(), error_type=type(exc).__name__)
                if isinstance(outcome, dict):
                    self.last_failure["helper_result"] = outcome
                _atomic(root / "trigger-failure.json", self.last_failure)
            except (OSError, ValueError) as write_error:
                self.last_failure = dict(self.last_failure or {},
                                         receipt_error_type=type(write_error).__name__)
                print("MindIE update status storage failed; maintenance outcome unchanged.",
                      file=sys.stderr)
        finally:
            try:
                self.next_attempt = next_check(self.config)
            except (OSError, ValueError):
                self.next_attempt = time.time() + FAILURE_BACKOFF
            # A helper may fail before writing status, or lose the check lock
            # to another process. Both are attempts, not reasons to spawn at
            # every subsequent tool call while the old receipt is still due.
            if failed or self.next_attempt <= time.time():
                self.next_attempt = max(self.next_attempt, time.time() + FAILURE_BACKOFF)

    def close(self):
        self.cancel.set()
        if self.thread is not None:
            # The existing owned runner observes cancellation and contains its
            # full child tree. Do not leave a daemon writer after MCP EOF.
            self.thread.join()
