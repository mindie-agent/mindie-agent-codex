"""Component checks retain native ownership and the selected runtime on failure."""
import json
from pathlib import Path
import sys
import os
import subprocess
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import bootstrap
import component_update
from product_contract import identity
import native_entry


def test_an_unchanged_independent_runtime_is_not_rebuilt(tmp_path):
    baseline = identity(SCRIPTS.parent)
    with patch("mindie_knowledge.component_runtime.discover",
               return_value={"changed": False}) as discover, \
            patch("mindie_knowledge.component_runtime.build") as build:
        assert component_update.prepare(SCRIPTS.parent, sys.executable, tmp_path,
                                        current_source=baseline["source_sha256"]) is None
    discover.assert_called_once_with(sys.executable, baseline["runtime"], force=False)
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
