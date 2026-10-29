"""Component checks retain native ownership and the selected runtime on failure."""
import json
from pathlib import Path
import sys
import os
import subprocess
import shutil
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


def test_bound_component_versions_are_read_once_per_interpreter_without_running_custom_commands():
    adapter = dict(component_commands={
        "profiler": ["/tools/python", "-I", "-X", "utf8", "-m", "mindie_profiler"],
        "sim": ["/tools/python", "-m", "mindie_sim"],
        "coordinator": ["/user/custom-wrapper", "--production"],
    })
    versions = {"mindie-profiler": "0.5.0", "mindie-sim": "0.7.1"}
    with patch("mindie_knowledge.component_runtime.read_versions", return_value=versions) as read:
        assert component_update.required_components(adapter) == versions
    assert read.call_count == 1
    assert read.call_args.args == ("/tools/python", ["mindie-profiler", "mindie-sim"])
    assert 0 < read.call_args.kwargs["timeout"] <= 15
    rebound = component_update.rebind_commands(adapter, "/candidate/python", versions)
    assert rebound["profiler"] == ["/candidate/python", "-I", "-X", "utf8", "-m", "mindie_profiler"]
    assert rebound["sim"] == ["/candidate/python", "-I", "-X", "utf8", "-m", "mindie_sim"]
    assert rebound["coordinator"] == adapter["component_commands"]["coordinator"]
    assert adapter["component_commands"]["profiler"][0] == "/tools/python"


def test_bound_versions_reach_release_selection_before_candidate_build(tmp_path):
    adapter = dict(component_commands={
        "sim": ["/external/python", "-I", "-m", "mindie_sim"],
    })
    with patch("mindie_knowledge.component_runtime.read_versions",
               return_value={"mindie-sim": "0.7.1"}), \
            patch("mindie_knowledge.component_runtime.discover",
                  side_effect=ValueError("formal release below bound version")) as discover, \
            patch("mindie_knowledge.component_runtime.build") as build:
        with pytest.raises(ValueError, match="below bound version"):
            component_update.prepare(SCRIPTS.parent, sys.executable, tmp_path, adapter=adapter)
    assert discover.call_args.kwargs["required_components"] == {"mindie-sim": "0.7.1"}
    build.assert_not_called()


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


def test_native_cold_launcher_does_not_change_the_hook_trusted_source_payload(tmp_path):
    source = tmp_path / 'published-plugin'
    scripts = source / 'scripts'
    scripts.mkdir(parents=True)
    (scripts / 'helper.py').write_text('VALUE = 42\n')
    (scripts / 'bootstrap.py').write_text("import helper\nprint(helper.VALUE)\n")
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
