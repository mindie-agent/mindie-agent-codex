"""Activate a newer prepared seed before its MCP entry serves.

[Role]
One-shot startup controller for a newly installed seed whose canonical binding
is still older. It serializes on the existing update check lock, verifies that
native administrative writes are settled, probes the existing update lock, and
uses `release_activation.prepare`/`activate` with a persisted expected binding.
It then returns to the fixed entry so the matching target runtime serves.

[Invariants]
- Never replays native plugin_add/catalog writes; unresolved native writers are
  reported as unknown and left untouched.
- Never serves an old backend under a new catalog; the fixed entry revalidates
  that canonical points at these exact scripts after this controller returns.
- Never writes `active` by hand. Success requires a canonical readback matching
  the target runtime receipt.
- Mutable profile fields, domains, consent, and hook trust are preserved by the
  existing activation path and are not rewritten here.

[Timeouts & Locks]
- One startup migration deadline covers the real 720s native call drain plus
  15s capture, 90s domain preparation, 30s activation, and cleanup reserve.
  It is shared by the check.lock follower/leader and the update-lock wait; the
  caller may inject a shorter deadline for focused tests.
- Holds `<config>.updates/check.lock` only while preparing/activating and
  releases every lock before returning to the fixed entry.

[Side-Effect Policy]
- Writes the immutable target engine/domain profiles through
  `release_activation.prepare` and canonical `codex.json` through its atomic
  activation. No native plugin, catalog, hook-trust, status, or lock-file edits.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from product_contract import read_json
from release_activation import (
    InstallationChanged,
    activate,
    binding_matches_release,
    prepare,
)
from session_gate import config_path, runtime_binding
from update_lock import file_lock, update_lock
from update_trigger import directory, read_optional

MIGRATION_SECONDS = 900.0
COMPLETED = dict(execution="completed", returncode=0, cleanup_failed=False)


def _version_key(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", value)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _deferred(reason, **details):
    return dict(status="deferred", stage="migration", reason=reason,
                execution="not_started", **details)


def _unconfirmed(reason, **details):
    return dict(status="unknown", stage="migration", reason=reason,
                execution="not_started", **details)


def _receipt(release):
    receipt = read_json(release / "runtime.json")[0]
    if not isinstance(receipt, dict):
        raise ValueError("migration release receipt is invalid")
    validation = receipt.get("validation")
    if not isinstance(validation, dict):
        raise ValueError("migration release receipt is invalid")
    if receipt.get("python") != sys.executable:
        raise ValueError("migration must run in its exact prepared runtime")
    if (validation.get("status") != "validated"
            or validation.get("schema") != "mindie-candidate-check/1"):
        raise ValueError("migration release is not validated")
    if _version_key(validation.get("native_version")) is None:
        raise ValueError("migration release identity is invalid")
    return receipt


def _native_writer_unsettled(root):
    pending = read_optional(root / "native.json")
    if pending is None:
        return False
    if not isinstance(pending, dict):
        return True
    if pending.get("stage") in {"active", "superseded"}:
        return False
    if pending.get("stage") not in {"installed", "prepared"}:
        return True
    if pending.get("process") != COMPLETED:
        return True
    preparation = pending.get("preparation")
    if preparation is not None and (
            not isinstance(preparation, dict)
            or preparation.get("cleanup_failed") is not False):
        return True
    trust = pending.get("hook_trust")
    if trust is not None and (
            not isinstance(trust, dict)
            or trust.get("status") not in {"complete", "unchanged"}):
        return True
    return False


def _canonical(config):
    try:
        with update_lock(config, blocking=False):
            return read_json(config)[0]
    except BlockingIOError:
        return None


def _exclusive_probe(config, deadline):
    while True:
        try:
            with update_lock(config, exclusive=True):
                return True
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.1, remaining))


def _migrate_locked(config, release, receipt, deadline):
    if binding_matches_release(config, release, receipt):
        return dict(status="ready")
    adapter = _canonical(config)
    if adapter is None:
        return _deferred("operation_busy")
    target = _version_key(receipt["validation"]["native_version"])
    current = _version_key(adapter.get("product_validation", {}).get("native_version"))
    if target is None or current is None:
        return _deferred("installation_changed")
    if target < current:
        return _deferred("installation_changed")
    if target == current:
        return _deferred("binding_not_visible")
    if _native_writer_unsettled(directory(config)):
        return _unconfirmed("native_writer")
    expected = runtime_binding(adapter)
    if not _exclusive_probe(config, deadline):
        return _deferred("startup_deadline")
    prepared = None
    while True:
        if binding_matches_release(config, release, receipt):
            return dict(status="ready")
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            return _deferred("startup_deadline")
        if prepared is None:
            try:
                prepared = prepare(config, release, blocking=False,
                                   expected_binding=expected, deadline=deadline)
            except InstallationChanged:
                if binding_matches_release(config, release, receipt):
                    return dict(status="ready")
                return _deferred("installation_changed")
            except BlockingIOError:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                continue
            except TimeoutError:
                return _deferred("startup_deadline")
        if binding_matches_release(config, release, receipt):
            return dict(status="ready")
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            return _deferred("startup_deadline")
        result = activate(config, prepared, control_seconds=min(30.0, remaining))
        if binding_matches_release(config, release, receipt):
            return dict(status="ready", service=result.get("service") or result.get("status"))
        if (result.get("status") == "deferred"
                and result.get("reason") in {"operation_busy", "service_busy"}):
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            continue
        if result.get("status") in {"deferred", "incomplete"}:
            return dict(result, stage="migration")
        return _deferred("binding_not_visible")


def run(release, role, arguments, *, deadline=None):
    """Activate release for the original startup role; return a small status."""
    del role, arguments
    if deadline is None:
        deadline = time.monotonic() + MIGRATION_SECONDS
    release = Path(release).absolute()
    receipt = _receipt(release)
    config = config_path()
    target = _version_key(receipt["validation"]["native_version"])
    adapter = _canonical(config)
    if adapter is not None:
        current = _version_key(adapter.get("product_validation", {}).get("native_version"))
        if target is None or current is None:
            return _deferred("installation_changed")
        if target < current:
            return _deferred("installation_changed")
        if target == current and binding_matches_release(config, release, receipt):
            return dict(status="ready")
    # If an exclusive activation currently owns the lock, do not fail the
    # startup: the check.lock wait below observes its publication.

    root = directory(config)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    while True:
        if binding_matches_release(config, release, receipt):
            return dict(status="ready")
        try:
            with file_lock(root / "check.lock", exclusive=True):
                return _migrate_locked(config, release, receipt, deadline)
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _deferred("startup_deadline")
            time.sleep(min(0.1, remaining))


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit("usage: release_migration.py RELEASE ROLE [ARG ...]")
    print(run(Path(sys.argv[1]), sys.argv[2], sys.argv[3:]))
