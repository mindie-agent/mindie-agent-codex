"""Cross-platform storage lifecycle regressions for the Codex adapter.

Every test owns an isolated host tree and never touches the developer's real
HOME, CODEX_HOME, MindIE state, uv cache or spool directory.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))

import bootstrap
import capture_spool
import receipt_owners
import storage_gc
from update_lock import file_lock

pytest.importorskip("mindie_knowledge.storage", reason="Wave 1 shared storage is required")


class Host:
    def __init__(self, tmp_path):
        self.root = tmp_path / "host"
        self.codex = self.root / "codex"
        self.config = self.root / "config" / "mindie-agent"
        self.data = self.root / "share" / "mindie-agent"
        self.plugin = self.codex / "plugins/data/mindie-agent-mindie-agent"
        for path in (self.codex, self.config, self.data, self.plugin):
            path.mkdir(parents=True, exist_ok=True)

    def env(self, monkeypatch):
        directories = {
            "HOME": self.root / "home",
            "USERPROFILE": self.root / "home",
            "CODEX_HOME": self.codex,
            "PLUGIN_DATA": self.plugin,
            "MINDIE_CONFIG_HOME": self.config,
            "MINDIE_DATA_HOME": self.data,
            "MINDIE_AGENT_CONFIG": self.root / "mindie-agent.json",
            "TMPDIR": self.root / "tmp",
            "TEMP": self.root / "tmp",
            "TMP": self.root / "tmp",
            "UV_CACHE_DIR": self.root / "uv-cache",
            "UV_PROJECT_ENVIRONMENT": self.root / "uv-venv",
        }
        for name, path in directories.items():
            path.mkdir(parents=True, exist_ok=True)
            monkeypatch.setenv(name, str(path))

    def reclaim(self, **options):
        return storage_gc.reclaim_storage(
            data=self.plugin, config_home=self.config, data_home=self.data,
            codex_home=self.codex, budget_seconds=options.pop("budget_seconds", 10.0),
            force=options.pop("force", True), **options)


def _write(path, text="{}"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _age(path, seconds):
    timestamp = time.time() - seconds
    os.utime(path, (timestamp, timestamp))


def _generations(root, count, *, marker="components.json", age_start=10_000):
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        generation = root / f"generation-{index:02d}"
        generation.mkdir()
        if marker:
            _write(generation / marker)
        _age(generation, age_start - index)
        paths.append(generation)
    return paths


def test_historical_backlog_is_reclaimed_to_bounded_counts(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    components = _generations(host.plugin / "component-runtimes", 12)
    runtimes = _generations(host.plugin / "runtimes", 24, marker="bin/python")
    for index in range(10):
        release = host.plugin / "releases" / f"0.0.{index}"
        release.mkdir(parents=True)
        _write(release / "runtime.json")
        _age(release, 20_000 - index)
    # Active native version makes the sweeper retain exactly current + prior.
    _write(host.plugin / "codex.updates/native.json",
           json.dumps({"native_version": "0.0.9"}))
    superseded = host.config / "codex.updates"
    superseded.mkdir(parents=True)
    for index in range(20):
        _write(superseded / f"superseded-{index}.json")
    releases_root = host.plugin / "releases"
    for index in range(8):
        old = _write(releases_root / f"prepare-old-{index}.log", "old")
        _age(old, 2 * 24 * 3600)
    for index in range(4):
        nested = _write(releases_root / "0.0.9" / f"prepare-live-{index}.log", "recent")
        _age(nested, 60)

    result = host.reclaim()

    remaining_components = sorted((host.plugin / "component-runtimes").iterdir())
    remaining_runtimes = sorted((host.plugin / "runtimes").iterdir())
    remaining_releases = sorted(
        entry.name for entry in releases_root.iterdir()
        if entry.is_dir() and entry.name.startswith("0.0."))
    assert result["status"] == "completed", result
    assert 1 <= len(remaining_components) <= 2, remaining_components
    assert 1 <= len(remaining_runtimes) <= 2, remaining_runtimes
    assert "0.0.9" in remaining_releases and "0.0.8" in remaining_releases, remaining_releases
    assert len(remaining_releases) <= 3, remaining_releases
    assert len(list((superseded).glob("superseded-*.json"))) <= 8
    assert not list(releases_root.glob("prepare-old-*.log"))
    assert components[0].exists() is False and runtimes[0].exists() is False

    # Constructor logs inside a retained release are bounded by the bootstrap
    # helper even when their mtimes are recent.
    bootstrap._prune_prepare_logs(host.plugin, releases_root / "0.0.9")
    nested_logs = list(releases_root.glob("*/prepare-*.log"))
    assert len(nested_logs) <= bootstrap.PREPARE_LOG_RETENTION


def test_prepare_logs_retain_only_current_and_newest_three(tmp_path):
    data = tmp_path / "plugin"
    release = data / "releases" / "0.1.34"
    release.mkdir(parents=True)
    current = _write(release / "prepare-current.log", "current")
    _age(current, 1)
    for index in range(8):
        old = _write(release / f"prepare-{index:02d}.log", "x")
        _age(old, 2 * 24 * 3600 - index)
    for index in range(5):
        recent = _write(release / f"prepare-recent-{index}.log", "x")
        _age(recent, 10 + index)
    other = data / "releases" / "0.1.33"
    other.mkdir()
    for index in range(5):
        old = _write(other / f"prepare-old-{index}.log", "x")
        _age(old, 3 * 24 * 3600)

    bootstrap._prune_prepare_logs(data, release, keep=current)

    retained = list(data.glob("releases/*/prepare-*.log"))
    assert current.exists()
    assert len(retained) == bootstrap.PREPARE_LOG_RETENTION + 1, retained
    assert current in retained


def test_component_floor_upgrade_builds_only_one_managed_environment(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    config = host.config / "codex.json"
    config.write_text(json.dumps({
        "python": sys.executable,
        "runtime_scripts": str(SCRIPTS),
        "engine_config": str(tmp_path / "engine.json"),
        "component_commands": {"sim": [sys.executable, "-I", "-m", "mindie_sim"]},
        "product_validation": {"source_sha256": "old-source"},
    }))
    effective = {
        "python": str(tmp_path / "merged" / "bin/python"),
        "scripts": str(tmp_path / "merged" / "scripts"),
        "release": str(tmp_path / "merged" / "release"),
    }
    seen = []

    class Result:
        def checked_stdout(self):
            return json.dumps(effective)

    def fake_run(argv, payload, **options):
        seen.append((list(argv), dict(options)))
        return Result()

    monkeypatch.setattr("session_gate.config_path", lambda: config)
    monkeypatch.setattr(bootstrap, "run", fake_run)
    monkeypatch.setattr(bootstrap, "_postbuild_cleanup", lambda *a, **k: {})
    monkeypatch.setattr(
        bootstrap, "_baseline_runtime",
        lambda *a, **k: pytest.fail("orphan baseline runtime must not be built"))

    python, scripts, release = bootstrap.ensure_runtime(SCRIPTS.parent, host.plugin)

    assert (str(python), str(scripts), str(release)) == (
        effective["python"], effective["scripts"], effective["release"])
    assert len(seen) == 1
    argv = seen[0][0]
    assert argv[3].endswith("component_update.py")
    # The healthy installed floor is used for probing and building, and it is
    # also the current binding passed to component_update.
    assert argv[0] == sys.executable and argv[5] == sys.executable and argv[7] == sys.executable


def test_missing_floor_interpreter_builds_one_baseline(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    config = host.config / "codex.json"
    config.write_text(json.dumps({
        "python": str(tmp_path / "missing" / "bin/python"),
        "component_commands": {"sim": ["/absent/python", "-m", "mindie_sim"]},
    }))
    baseline = (tmp_path / "baseline" / "bin/python", tmp_path / "baseline" / "scripts",
                tmp_path / "baseline" / "release")
    calls = []
    monkeypatch.setattr("session_gate.config_path", lambda: config)
    monkeypatch.setattr(bootstrap, "run",
                        lambda *a, **k: pytest.fail("component update cannot run without an interpreter"))
    monkeypatch.setattr(bootstrap, "_baseline_runtime",
                        lambda source, data, cleanup_python=None: calls.append(cleanup_python) or baseline)

    assert bootstrap.ensure_runtime(SCRIPTS.parent, host.plugin) == baseline
    assert calls == [None]


def test_no_component_override_builds_only_the_baseline(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    config = host.config / "codex.json"
    config.write_text(json.dumps({
        "python": sys.executable,
        "component_commands": {"sim": [sys.executable, "-m", "mindie_sim"]},
    }))
    baseline = (tmp_path / "baseline" / "bin/python", tmp_path / "baseline" / "scripts",
                tmp_path / "baseline" / "release")
    calls = []

    class Result:
        def checked_stdout(self):
            return "null"

    monkeypatch.setattr("session_gate.config_path", lambda: config)
    monkeypatch.setattr(bootstrap, "run", lambda *a, **k: Result())
    monkeypatch.setattr(bootstrap, "_baseline_runtime",
                        lambda source, data, cleanup_python=None: calls.append(cleanup_python) or baseline)

    assert bootstrap.ensure_runtime(SCRIPTS.parent, host.plugin) == baseline
    assert calls == [Path(sys.executable)]


def _terminate_tree(process):
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=20)
        else:
            process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=20)


def test_real_child_lease_protects_runtime_until_it_exits(tmp_path):
    root = tmp_path / "component-runtimes"
    generation = root / "leased-generation"
    generation.mkdir(parents=True)
    _write(generation / "components.json")
    _age(generation, 10_000)
    marker = tmp_path / "leased"
    code = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from mindie_knowledge.storage import acquire_runtime_lease\n"
        "with acquire_runtime_lease(sys.argv[1]):\n"
        "    Path(sys.argv[2]).write_text('ready')\n"
        "    time.sleep(120)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", code, str(generation), str(marker)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while not marker.exists():
            if process.poll() is not None:
                _out, err = process.communicate(timeout=5)
                pytest.fail("lease child exited early: " + err)
            if time.monotonic() > deadline:
                pytest.fail("lease child did not acquire its runtime lease")
            time.sleep(0.02)

        from mindie_knowledge.storage import prune_component_runtimes
        protected = prune_component_runtimes(
            root, keep_paths=(), keep_recent_verified=0, staging_grace_seconds=0,
            budget_seconds=5.0, include_global_roots=False,
            live_paths=set(), inspection_complete=True)
        assert generation.exists(), protected
        assert protected["kept_dirs"] >= 1 and protected["reclaimed_dirs"] == 0
    finally:
        _terminate_tree(process)

    reclaimed = prune_component_runtimes(
        root, keep_paths=(), keep_recent_verified=0, staging_grace_seconds=0,
        budget_seconds=10.0, include_global_roots=False,
        live_paths=set(), inspection_complete=True)
    assert not generation.exists(), reclaimed
    assert reclaimed["reclaimed_dirs"] >= 1
    assert not list(root.glob(".trash-*"))


def test_ntfs_permission_error_defers_without_deleting_any_file(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    components = _generations(host.plugin / "component-runtimes", 5)
    target = components[0]
    sentinel = _write(target / "sentinel.bin", "keep-me")
    # Writing the sentinel refreshes the directory mtime; re-age it so the
    # verified-excess staging grace is not what keeps the candidate.
    _age(target, 20_000)
    import mindie_knowledge.storage.lifecycle as lifecycle
    from mindie_knowledge.filesystem import path_identity

    real_replace = lifecycle.os.replace
    target_key = path_identity(target)

    def deny(source, destination):
        if path_identity(source) == target_key:
            raise PermissionError("NTFS directory is in use")
        return real_replace(source, destination)

    monkeypatch.setattr(lifecycle.os, "replace", deny)
    result = host.reclaim(budget_seconds=5.0)

    assert target.is_dir() and sentinel.read_text() == "keep-me"
    assert result["status"] == "deferred_in_use", result
    assert result["deferred_in_use"] >= 1
    assert not list((host.plugin / "component-runtimes").glob(".trash-*"))


def test_uv_cache_is_pinned_only_when_default_crosses_volume(tmp_path, monkeypatch):
    env = {"UV_CACHE_DIR": "/explicit/cache"}
    assert storage_gc.configure_same_volume_uv_cache(
        tmp_path / "runtime", env)["UV_CACHE_DIR"] == "/explicit/cache"

    data = tmp_path / "data"
    runtime = data / "runtimes" / "key"
    data.mkdir()
    runtime.mkdir(parents=True)
    monkeypatch.setattr(storage_gc, "_default_uv_cache", lambda env: tmp_path / "other" / "cache")
    monkeypatch.setattr(storage_gc, "_device",
                        lambda path: 1 if "other" in str(path) else 2)
    configured = {}
    storage_gc.configure_bootstrap_uv_cache(data, runtime, configured)
    assert configured["UV_CACHE_DIR"] == str(data / ".uv-cache")

    monkeypatch.setattr(storage_gc, "_device", lambda path: 7)
    same_volume = {}
    storage_gc.configure_bootstrap_uv_cache(data, runtime, same_volume)
    assert "UV_CACHE_DIR" not in same_volume


def test_remote_receipts_owner_lock_is_removed_and_dead_locks_swept(tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_gate.remote_state_dir", lambda: tmp_path)
    import mcp_gate

    receipts = mcp_gate.RemoteReceipts("task")
    assert receipts.claim("request")
    owner_lock = receipt_owners.owner_path(receipts.path.parent, receipts.owner)
    assert owner_lock.is_file()
    receipts.close()
    assert not owner_lock.exists()
    assert not list(receipts.path.parent.glob("*.owner.lock"))

    dead = receipt_owners.owner_path(receipts.path.parent, "a" * 32)
    dead.write_text("")
    _age(dead, 3600)
    fresh = receipt_owners.owner_path(receipts.path.parent, "b" * 32)
    fresh.write_text("")
    assert receipt_owners.sweep_idle_owner_locks(receipts.path.parent) == [dead.name]
    assert not dead.exists() and fresh.exists()

    # A held owner sidecar is live evidence and must never be swept.
    live = receipt_owners.owner_path(receipts.path.parent, "c" * 32)
    live.write_text("")
    _age(live, 3600)
    with file_lock(live, exclusive=True):
        receipt_owners.sweep_idle_owner_locks(receipts.path.parent, min_age=0)
        assert live.exists()
    receipt_owners.sweep_idle_owner_locks(receipts.path.parent, min_age=0)
    assert not live.exists()

    # A historical dead owner discovered during DB initialization is removed.
    historical = receipt_owners.owner_path(receipts.path.parent, "d" * 32)
    historical.write_text("")
    _age(historical, 3600)
    db = receipts._open_db()
    db.close()
    assert not historical.exists()


def test_capture_reclaims_stale_active_and_abandons_its_own(tmp_path):
    stale = tmp_path / "crashed-stdout.active"
    stale.write_text("partial")
    _age(stale, capture_spool.ACTIVE_ORPHAN_SECONDS + 60)
    fresh = tmp_path / "recent-stdout.active"
    fresh.write_text("live")

    live_capture = capture_spool.Capture(tmp_path, 16)
    live_capture.add("out", b"live")
    live_path = Path(live_capture.streams["out"].path)
    _age(live_path, capture_spool.ACTIVE_ORPHAN_SECONDS + 60)
    assert not stale.exists() and fresh.exists() and live_path.exists()

    second = capture_spool.Capture(tmp_path, 16)
    assert live_path.exists(), "an owned active capture is never reclaimed"
    second.close()

    live_capture.streams["out"]._abandon()
    assert not live_path.exists()
    assert "path" not in live_capture.streams["out"].receipt()
    live_capture.close()
    fresh.unlink()
    assert not list(tmp_path.glob("*.active"))


def _release_with_runtime(host, version, key):
    """One published release and the exact runtime its immutable receipt names."""
    runtime = host.plugin / "runtimes" / key
    python = runtime / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    release = host.plugin / "releases" / version
    release.mkdir(parents=True)
    receipt = release / "runtime.json"
    receipt.write_text(json.dumps(dict(key=key, python=str(python))), encoding="utf-8")
    age = 50_000 - int(version.rsplit(".", 1)[-1])
    for path in (release, runtime, python.parent, python.parent.parent):
        _age(path, age)
    return release, python


def test_staged_newer_release_keeps_bound_release_and_one_rollback(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    releases = {
        version: _release_with_runtime(host, f"0.1.{version}", f"key-{version}")
        for version in range(30, 35)
    }
    # A newer 0.1.34 is staged, while codex.json still executes 0.1.33's
    # runtime. component_release is absent/None, so only the python binding
    # identifies the active release.
    (host.config / "codex.json").write_text(json.dumps(dict(
        component_release=None, python=str(releases[33][1]))), encoding="utf-8")

    result = host.reclaim()

    retained = {entry.name for entry in (host.plugin / "releases").iterdir()
                if entry.is_dir()}
    assert {"0.1.34", "0.1.33", "0.1.32"} <= retained, retained
    assert "0.1.31" not in retained and "0.1.30" not in retained, retained
    assert releases[33][0].is_dir() and releases[32][0].is_dir()
    assert (host.plugin / "runtimes" / "key-33").is_dir()
    assert (host.plugin / "runtimes" / "key-32").is_dir()
    assert result["status"] == "completed", result


def test_explicit_keep_paths_survive_a_sweep_that_would_drop_them(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    releases = {
        version: _release_with_runtime(host, f"0.1.{version}", f"key-{version}")
        for version in range(30, 35)
    }
    old_release, old_python = releases[30]
    old_runtime = host.plugin / "runtimes" / "key-30"

    result = host.reclaim(keep_paths=[old_release, old_python])

    assert old_release.is_dir() and old_runtime.is_dir()
    assert old_python.is_file()
    assert result["status"] == "completed", result


def test_native_metadata_version_protects_candidate_and_rollback(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    releases = {
        version: _release_with_runtime(host, f"0.1.{version}", f"key-{version}")
        for version in range(30, 36)
    }
    # 0.1.35 is a staged newer directory; only the in-flight 0.1.34 metadata
    # identifies 0.1.33 as the rollback that must not be reclaimed.
    native = host.plugin / "codex.updates" / "native.json"
    native.parent.mkdir(parents=True)
    native.write_text(json.dumps(dict(
        stage="installed", metadata=dict(version="0.1.34"))), encoding="utf-8")

    result = host.reclaim()

    assert releases[34][0].is_dir() and releases[33][0].is_dir()
    assert (host.plugin / "runtimes" / "key-34").is_dir()
    assert (host.plugin / "runtimes" / "key-33").is_dir()
    assert result["status"] == "completed", result


def test_serving_runtime_lease_holds_the_owning_generation(tmp_path, monkeypatch):
    import mindie_knowledge.storage as storage
    from storage_gc import serving_runtime_lease

    generation = tmp_path / "managed-generation"
    generation.mkdir()
    monkeypatch.setattr(storage, "owning_dir", lambda value: generation)
    with serving_runtime_lease() as lease:
        assert lease is not None
        assert lease.directory == generation
        if os.name == "nt":
            assert list(generation.glob(".inuse.*.lease"))
        else:
            assert (generation / ".inuse.lock").is_file()
    assert not list(generation.glob(".inuse.*.lease"))


def test_windows_extended_paths_and_short_runtime_keys_survive_reclaim(tmp_path, monkeypatch):
    host = Host(tmp_path)
    host.env(monkeypatch)
    created = {}
    for ver in range(28, 35):
        full_key = f"{ver:02d}" + ("abcdef0123456789" * 4)[:62]
        short_dir = host.plugin / "runtimes" / full_key[:24]
        py = short_dir / "Scripts" / "python.exe"
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        rel = host.plugin / "releases" / f"0.1.{ver}"
        rel.mkdir(parents=True, exist_ok=True)
        (rel / "runtime.json").write_text(json.dumps({
            "key": full_key,
            "python": "\\\\?\\" + str(py),
        }), encoding="utf-8")
        age = 50_000 - ver
        for path in (rel, short_dir, py.parent):
            _age(path, age)
        created[ver] = (rel, short_dir, py)

    (host.config / "codex.json").write_text(json.dumps({
        "component_release": None,
        "python": "\\\\?\\" + str(created[33][2]),
    }), encoding="utf-8")
    keep_dir, keep_rt, keep_py = created[28]

    result = storage_gc.reclaim_storage(
        data="\\\\?\\" + str(host.plugin),
        config_home="\\\\?\\" + str(host.config),
        data_home="\\\\?\\" + str(host.data),
        codex_home="\\\\?\\" + str(host.codex),
        force=True,
        keep_paths=["\\\\?\\" + str(keep_dir), "\\\\?\\" + str(keep_py)],
    )

    assert created[34][0].is_dir() and created[34][1].is_dir()
    assert created[33][0].is_dir() and created[33][1].is_dir()
    assert created[32][0].is_dir() and created[32][1].is_dir()
    assert keep_dir.is_dir() and keep_rt.is_dir()
    assert not created[29][0].exists() and not created[29][1].exists()
    assert result["status"] == "completed", result
