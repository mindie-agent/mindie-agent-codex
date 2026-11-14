"""Component checks retain native ownership and the selected runtime on failure."""
import io
import json
from pathlib import Path
import sys
import os
import subprocess
import shutil
import urllib.error
from importlib.metadata import version
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import bootstrap
import component_update
from product_contract import identity
import native_entry
import candidate_validate


def test_component_cli_starts_isolated_from_a_unicode_path(tmp_path, monkeypatch):
    scripts = tmp_path / "插件 with spaces" / "scripts"
    shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    # Ambient modules must not hide a missing candidate-owned import under -I.
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "bounded_process.py").write_text("raise RuntimeError('ambient import')\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(ambient))
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(scripts / "component_update.py"), "--help"],
        cwd=ambient, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert "current_python" in result.stdout
    assert not list(scripts.rglob("*.pyc"))


def test_candidate_probe_rejects_a_broken_isolated_update_entry(tmp_path):
    scripts = tmp_path / "candidate" / "scripts"
    shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (scripts / "bounded_process.py").unlink()
    with patch.object(candidate_validate, "build_probe_script", return_value="print('OK')"):
        with pytest.raises(RuntimeError) as caught:
            candidate_validate.runtime_check(scripts)
    assert caught.value.process_result.returncode != 0
    assert "bounded_process" in caught.value.process_result.stderr


def test_an_unchanged_independent_runtime_is_not_rebuilt(tmp_path):
    baseline = identity(SCRIPTS.parent)
    with patch("mindie_knowledge.component_runtime.discover",
               return_value={"changed": False}) as discover, \
            patch("mindie_knowledge.component_runtime.build") as build:
        assert component_update.prepare(SCRIPTS.parent, sys.executable, tmp_path,
                                        current_source=baseline["source_sha256"]) is None
    discover.assert_called_once_with(sys.executable, baseline["runtime"], force=False,
                                    required_components={})
    build.assert_not_called()


def test_candidate_build_failure_does_not_change_the_active_native_binding(tmp_path):
    config = tmp_path / "config.json"
    original = json.dumps(dict(python=sys.executable, product_validation=identity(SCRIPTS.parent)))
    config.write_text(original)
    with patch.object(component_update, "prepare", side_effect=RuntimeError("build failed")), \
            patch.object(component_update, "run") as activate:
        with pytest.raises(RuntimeError, match="build failed"):
            component_update.check(config, SCRIPTS.parent, tmp_path / "data")
    assert config.read_text() == original
    activate.assert_not_called()


def test_bound_versions_reach_release_selection_before_candidate_build(tmp_path):
    adapter = dict(component_commands={
        "sim": ["/external/python", "-I", "-m", "mindie_sim"],
    })
    with patch("mindie_knowledge.component_bindings.read_versions",
               return_value={"mindie-sim": "0.7.1"}), \
            patch("mindie_knowledge.component_runtime.discover",
                  side_effect=ValueError("formal release below bound version")) as discover, \
            patch("mindie_knowledge.component_runtime.build") as build:
        with pytest.raises(ValueError, match="below bound version"):
            component_update.prepare(SCRIPTS.parent, sys.executable, tmp_path, adapter=adapter)
    assert discover.call_args.kwargs["required_components"] == {"mindie-sim": "0.7.1"}
    build.assert_not_called()


def test_plugin_upgrade_rebinds_a_core_component_even_when_pins_match_the_baseline(tmp_path):
    baseline = identity(SCRIPTS.parent)
    old_python, candidate_python = "/old/python", "/candidate/python"
    adapter = dict(component_commands={
        "diagnostics": [old_python, "-I", "-m", "mindie_diagnostics.cli"],
    })
    repositories = {
        "mindie-knowledge": "mindie-agent/knowledge",
        "remote-dev": "mindie-agent/remote-dev",
        "mindie-diagnostics": "mindie-agent/diagnostics",
        "mindie-debugger": "mindie-agent/debugger",
        "mindie-profiler": "mindie-agent/profiler",
        "mindie-sim": "mindie-agent/sim",
        "mindie-coordinator": "mindie-agent/coordinator",
    }
    selected = {name: dict(revision=revision, version=version(name), repository=repositories[name])
                for name, revision in baseline["runtime"].items()}
    with patch("mindie_knowledge.component_bindings.read_versions",
               return_value={"mindie-diagnostics": "0.5.4"}), \
            patch("mindie_knowledge.component_runtime.discover",
                  return_value=dict(changed=False, runtime=selected)), \
            patch("mindie_knowledge.component_runtime.build",
                  side_effect=RuntimeError("candidate build reached")) as build:
        with pytest.raises(RuntimeError, match="candidate build reached"):
            component_update.prepare(
                SCRIPTS.parent, candidate_python, tmp_path,
                current_python=old_python, adapter=adapter,
            )
    assert build.call_args.kwargs["python"] == candidate_python
    assert build.call_args.args[3] == selected


def test_upgrade_drops_retired_mindie_bench_from_inherited_inventory_before_catalog_lookup(tmp_path):
    from mindie_knowledge import component_runtime as cr
    baseline = identity(SCRIPTS.parent)
    old_python, candidate_python = "/old/python", "/candidate/python"
    adapter = dict(component_commands={
        "diagnostics": [old_python, "-I", "-m", "mindie_diagnostics.cli"],
    })
    repositories = {
        "mindie-knowledge": "mindie-agent/knowledge",
        "remote-dev": "mindie-agent/remote-dev",
        "mindie-diagnostics": "mindie-agent/diagnostics",
        "mindie-debugger": "mindie-agent/debugger",
        "mindie-profiler": "mindie-agent/profiler",
        "mindie-sim": "mindie-agent/sim",
        "mindie-coordinator": "mindie-agent/coordinator",
    }
    installed = {
        name: dict(revision=revision, version=version(name), repository=repositories[name])
        for name, revision in baseline["runtime"].items()
    }
    installed_with_bench = dict(
        installed,
        **{"mindie-bench": dict(
            repository="mindie-agent/mindie-bench",
            version="0.4.6",
            revision="c7d0c8a4969b3816925c0405931ae17e28ce7a34",
        )},
    )
    queried_packages = []

    class FakeCatalog:
        def source(self, name, rev):
            queried_packages.append(("source", name))
            return dict(installed[name], revision=rev)

        def latest(self, name, force=False):
            queried_packages.append(("latest", name))
            if name == "mindie-bench":
                raise AssertionError("retired mindie-bench must not be queried in release catalog")
            return installed[name]

    real_discover = cr.discover
    with patch("mindie_knowledge.component_bindings.read_versions",
               return_value={"mindie-diagnostics": version("mindie-diagnostics")}), \
            patch.object(cr, "read_inventory", return_value=installed_with_bench), \
            patch.object(cr, "discover",
                         side_effect=lambda *a, **kw: real_discover(*a, **kw, catalog=FakeCatalog())), \
            patch("mindie_knowledge.component_runtime.build",
                  side_effect=RuntimeError("candidate build reached")) as build:
        with pytest.raises(RuntimeError, match="candidate build reached"):
            component_update.prepare(
                SCRIPTS.parent, candidate_python, tmp_path,
                current_python=old_python, adapter=adapter,
            )
    assert all(pkg != "mindie-bench" for _, pkg in queried_packages)
    assert "mindie-bench" not in build.call_args.args[3]
    assert set(build.call_args.args[3]) == set(installed)


def test_component_activation_carries_the_binding_used_for_release_selection(tmp_path):
    config = tmp_path / "adapter.json"
    original = dict(python=sys.executable, product_validation={"source_sha256": "source"},
                    component_commands={"sim": ["/old/python", "-m", "mindie_sim"]})
    config.write_text(json.dumps(original))
    runtime = dict(python=sys.executable, scripts=str(SCRIPTS), release=str(tmp_path / "candidate"))

    def prepare(*args, **kwargs):
        changed = dict(original, component_commands={"sim": ["/newer/python", "-m", "mindie_sim"]})
        config.write_text(json.dumps(changed))
        return runtime

    def activate(argv, stdin, **kwargs):
        from bounded_process import ProcessResult
        from session_gate import runtime_binding
        assert argv[-1] == "--attempt"
        attempt = json.loads(stdin)
        assert attempt["expected_binding"] == runtime_binding(original)
        assert attempt["expected_binding"] != runtime_binding(json.loads(config.read_text()))
        return ProcessResult("completed", json.dumps(
            {"status": "deferred", "reason": "installation_changed"}), 0)

    with patch.object(component_update, "prepare", side_effect=prepare), \
            patch.object(component_update, "run", side_effect=activate):
        assert component_update.check(config, SCRIPTS.parent, tmp_path)["status"] == "deferred"
    assert json.loads(config.read_text())["component_commands"]["sim"][0] == "/newer/python"


def test_native_bootstrap_keeps_the_effective_component_release_on_reload(tmp_path, monkeypatch):
    release = tmp_path / "effective-release"
    release.mkdir()
    validation = identity(SCRIPTS.parent)
    (release / "runtime.json").write_text(json.dumps(dict(python=sys.executable, validation=validation)))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(dict(component_release=str(release), python=sys.executable)))
    monkeypatch.setenv("MINDIE_AGENT_CONFIG", str(config))
    with patch.object(bootstrap, "readback", return_value=SCRIPTS), \
            patch.object(bootstrap, "_baseline_runtime") as baseline:
        python, scripts, selected = bootstrap.ensure_runtime(SCRIPTS.parent, tmp_path / "data")
    assert (python, scripts, selected) == (Path(sys.executable), SCRIPTS, release)
    baseline.assert_not_called()


def test_stdlib_bootstrap_leaves_custom_only_bindings_to_the_installed_helper(tmp_path):
    config = tmp_path / "adapter.json"
    original = dict(python=sys.executable,
                    component_commands={"sim": ["/user/custom-wrapper", "--production"]})
    config.write_text(json.dumps(original))
    data = tmp_path / "data"
    code = """
import json, sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import bootstrap
python, scripts, data = Path(sys.argv[2]), Path(sys.argv[1]), Path(sys.argv[3])
baseline = (python, scripts, data / "release")
with patch.object(bootstrap, "_baseline_runtime", return_value=baseline):
    assert bootstrap.ensure_runtime(scripts.parent, data) == baseline
assert "mindie_knowledge" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(SCRIPTS), sys.executable, str(data)],
        env=dict(os.environ, MINDIE_AGENT_CONFIG=str(config)),
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(config.read_text()) == original
    assert not data.exists()


def test_native_cold_launcher_does_not_change_the_hook_trusted_source_payload(tmp_path):
    source = tmp_path / 'published-plugin'
    scripts = source / 'scripts'
    scripts.mkdir(parents=True)
    (scripts / 'helper.py').write_text('VALUE = 42\n')
    (scripts / 'bootstrap.py').write_text("import helper\nprint(helper.VALUE)\n")
    shutil.copy2(SCRIPTS / 'hook_launcher.py', scripts / 'hook_launcher.py')
    arguments = native_entry.command_args('0.1.2', 'knowledge-mcp')
    flags = arguments[arguments.index('python') + 1:]
    before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    env = dict(os.environ, PLUGIN_ROOT=str(source), CODEX_HOME=str(tmp_path / 'codex'),
               MINDIE_AGENT_CONFIG=str(tmp_path / 'absent-config.json'))
    # -I discards PYTHONDONTWRITEBYTECODE: the generated native argv must carry
    # its actual interpreter flag through runpy and the first source import.
    result = subprocess.run([sys.executable, '-I', *flags], env=env, capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '42'
    after = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    assert after == before


def _http_error(status=403):
    return urllib.error.HTTPError(
        "https://api.github.com/repos/mindie-agent/knowledge/releases/latest",
        status, "Forbidden", hdrs=None, fp=io.BytesIO(),
    )


def test_typed_catalog_error_rebuilds_http_malformed_and_host_misses():
    from mindie_knowledge.releases_io import ReleaseFetchError

    rebuilt = component_update.typed_catalog_error(_http_error(403))
    assert isinstance(rebuilt, component_update.CatalogError)
    assert rebuilt.code == "release_http_error"
    assert rebuilt.category == "temporary_network"
    assert rebuilt.status == 403

    limited = component_update.typed_catalog_error(_http_error(429))
    assert limited.category == "rate_limited"
    assert limited.code == "release_http_error"

    feed = ReleaseFetchError("invalid_release_json")
    rebuilt = component_update.typed_catalog_error(feed)
    assert rebuilt.code == "invalid_release_json"
    assert rebuilt.category == "temporary_network"

    child = RuntimeError("MindIE runtime failed; not retried")
    child.returncode = 1
    child.mindie_output = json.dumps({
        "error": {"type": "ReleaseFetchError", "code": "release_http_error", "status": 403},
    })
    rebuilt = component_update.typed_catalog_error(child)
    assert isinstance(rebuilt, component_update.CatalogError)
    assert rebuilt.code == "release_http_error"
    assert rebuilt.category == "temporary_network"
    assert rebuilt.returncode == 1

    host = RuntimeError("MindIE runtime failed; not retried")
    host.mindie_output = json.dumps({"error": {"type": "CalledProcessError"}})
    assert isinstance(component_update.typed_catalog_error(host), FileNotFoundError)

    plain = RuntimeError("MindIE runtime failed; not retried")
    assert component_update.typed_catalog_error(plain) is plain


def test_discover_http_error_is_a_typed_catalog_miss(tmp_path):
    with patch("mindie_knowledge.component_runtime.discover", side_effect=_http_error(403)), \
            patch("mindie_knowledge.component_runtime.build") as build:
        with pytest.raises(component_update.CatalogError) as caught:
            component_update.prepare(SCRIPTS.parent, sys.executable, tmp_path)
    assert caught.value.code == "release_http_error"
    assert caught.value.category == "temporary_network"
    assert caught.value.status == 403
    build.assert_not_called()


def test_select_main_emits_static_fetch_error_without_traceback(tmp_path, monkeypatch):
    from mindie_knowledge.releases_io import ReleaseFetchError

    stdout = io.StringIO()
    err = ReleaseFetchError("release_http_error", status=403)
    monkeypatch.setattr(sys, "argv", [
        "component_update.py", str(SCRIPTS.parent), sys.executable, str(tmp_path), sys.executable,
    ])
    with patch.object(component_update, "prepare", side_effect=err), \
            patch("sys.stdout", stdout):
        with pytest.raises(SystemExit) as ctx:
            component_update.main()
    assert ctx.value.code == 1
    assert json.loads(stdout.getvalue()) == {
        "error": {"code": "release_http_error", "status": 403, "type": "CatalogError"},
    }


def test_check_rebuilds_child_fetch_code_from_process_stdout(tmp_path):
    from bounded_process import ProcessResult

    config = tmp_path / "config.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, product_validation=identity(SCRIPTS.parent),
    )))
    child = RuntimeError("MindIE runtime failed; not retried")
    child.process_result = ProcessResult(
        "completed",
        json.dumps({"error": {"type": "ReleaseFetchError", "code": "release_http_error", "status": 403}}),
        1,
    )
    runtime = dict(python=sys.executable, scripts=str(SCRIPTS), release=str(tmp_path / "candidate"))
    with patch.object(component_update, "prepare", return_value=runtime), \
            patch.object(component_update, "run", side_effect=child):
        with pytest.raises(component_update.CatalogError) as caught:
            component_update.check(config, SCRIPTS.parent, tmp_path / "data")
    assert caught.value.code == "release_http_error"
    assert caught.value.category == "temporary_network"
    assert caught.value.process_result.returncode == 1


def test_unclassified_helper_crash_stays_internal_runtimeerror(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(dict(
        python=sys.executable, product_validation=identity(SCRIPTS.parent),
    )))
    crash = RuntimeError("MindIE runtime failed; not retried")
    with patch.object(component_update, "prepare", side_effect=crash), \
            patch.object(component_update, "run") as activate:
        with pytest.raises(RuntimeError) as caught:
            component_update.check(config, SCRIPTS.parent, tmp_path / "data")
    assert caught.value is crash
    activate.assert_not_called()


def test_ssl_and_incomplete_http_transport_errors_stay_non_reportable_while_local_bugs_report(tmp_path, monkeypatch):
    import http.client
    import socket
    import ssl
    import harness_update
    from mindie_diagnostics.ingestion import ingest
    from mindie_diagnostics.integration import configure_reporting
    from mindie_diagnostics.outbox import Outbox

    cert_err = ssl.SSLCertVerificationError(1, "self-signed certificate in chain")
    typed_cert = component_update.typed_catalog_error(cert_err)
    assert isinstance(typed_cert, component_update.CatalogError)
    assert typed_cert.code == "release_certificate_error"
    assert typed_cert.category == "certificate"

    for transient in (
        ssl.SSLError("TLS EOF mid-stream"),
        http.client.IncompleteRead(b"part", 64),
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        socket.gaierror(8, "nodename nor servname provided"),
    ):
        rebuilt = component_update.typed_catalog_error(transient)
        assert isinstance(rebuilt, component_update.CatalogError)
        assert rebuilt.code == "release_transport_unavailable"
        assert rebuilt.category == "temporary_network"

    for local_bug in (
        http.client.InvalidURL("nonnumeric port"),
        http.client.ImproperConnectionState("cannot send request"),
        http.client.NotConnected(),
    ):
        assert component_update.typed_catalog_error(local_bug) is local_bug
    timeout_err = TimeoutError("probe timed out")
    assert component_update.typed_catalog_error(timeout_err) is timeout_err

    logs = tmp_path / "diag-logs"
    policy = tmp_path / "diagnostics.json"
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_ROOT", str(logs))
    monkeypatch.setenv("MINDIE_DIAGNOSTICS_CONFIG", str(policy))
    configure_reporting(True, config=policy, roots=[str(logs)])
    config = tmp_path / "codex.json"
    env = {"CODEX_HOME": str(tmp_path / "native"), "MINDIE_CODEX_BIN": str(tmp_path / "codex-bin")}
    config.write_text(json.dumps({"native_context": env}), encoding="utf-8")
    before = {"version": "0.0.1", "enabled": True}
    with patch.object(harness_update, "runtime_env", return_value=env), \
            patch.object(harness_update, "harness_version", return_value="0.153.4"), \
            patch.object(harness_update, "installed", return_value=before), \
            patch("mindie_knowledge.releases_io.fetch_releases", side_effect=ssl.SSLError("TLS drop")):
        res = harness_update.check(config)
    assert res["status"] == "failed"
    assert ingest(logs, Outbox(tmp_path / "queue.db"), config=policy)["enqueued"] == 0


def test_wsl_host_binary_resolution_rejects_windows_drvfs_and_pe_shims(tmp_path, monkeypatch):
    import struct
    import native_context

    if os.name != "posix":
        pytest.skip("POSIX/WSL PATH shadowing check")

    win_dir = tmp_path / "mnt_c_npm"
    lin_dir = tmp_path / "linux_bin"
    win_dir.mkdir()
    lin_dir.mkdir()
    # Extensionless PE binary + .exe shim in fake PATH ahead of Linux binary.
    pe_shim = win_dir / "codex"
    header = bytearray(128)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 64)
    header[64:68] = b"PE\x00\x00"
    pe_shim.write_bytes(bytes(header))
    pe_shim.chmod(0o755)
    exe_shim = win_dir / "uv.exe"
    exe_shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe_shim.chmod(0o755)
    lin_codex = lin_dir / "codex"
    lin_codex.write_text("#!/bin/sh\necho linux\n", encoding="utf-8")
    lin_codex.chmod(0o755)

    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
    monkeypatch.setenv("PATH", f"/mnt/c/Users/demo/AppData/Roaming/npm:{win_dir}:{lin_dir}")
    assert native_context.is_windows_host_binary("/mnt/c/Users/demo/AppData/Roaming/npm/codex") is True
    assert native_context.is_windows_host_binary(pe_shim) is True
    assert native_context.is_windows_host_binary(exe_shim) is True
    assert native_context.is_windows_host_binary(lin_codex) is False
    assert native_context.which_host_binary("codex") == str(lin_codex)
    monkeypatch.delenv("MINDIE_CODEX_BIN", raising=False)
    monkeypatch.setenv("CODEX_APP_RUNTIME_CLI_PATH", "/mnt/c/Program Files/Codex/codex.exe")
    captured = native_context.capture(home=tmp_path / "codex-home")
    assert captured["MINDIE_CODEX_BIN"] == str(lin_codex)
