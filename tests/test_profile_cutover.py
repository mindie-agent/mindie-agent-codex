"""A canonical cutover between launcher selection and entry is not corruption."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import site
import subprocess
import sys
import time
import venv

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
from bootstrap import publish_entry


def share_dependency_sites(library, directories):
    # Dependencies such as pywin32 need their own .pth paths and bootstrap.
    Path(library, "locked-dependencies.pth").write_text(
        "import site; " + "; ".join(f"site.addsitedir({str(path)!r})" for path in directories) + "\n",
        encoding="utf-8", newline="\n")


@pytest.fixture
def installation(tmp_path):
    data = tmp_path / "data"
    config = tmp_path / "codex.json"
    runtime = tmp_path / "new-runtime"
    builder = venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt")
    builder.create(runtime)
    context = builder.ensure_directories(runtime)
    # Both fixture runtimes use the exact locked dependencies; the second
    # interpreter has its own executable/prefix and imports fresh modules.
    share_dependency_sites(context.lib_path, site.getsitepackages())
    Path(context.lib_path, "runtime-probe.pth").write_text(
        "import fixture_runtime_probe\n",
        encoding="utf-8", newline="\n")
    Path(context.lib_path, "fixture_runtime_probe.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        f"with Path({str(data / 'runtime-starts.jsonl')!r}).open('a',encoding='utf-8',newline='\\n') as stream:\n"
        " stream.write(json.dumps({'python':sys.executable,'argv':sys.argv})+'\\n')\n",
        encoding="utf-8", newline="\n")
    releases = []
    for version, python in (("0.1.20", sys.executable), ("0.1.23", context.env_exe)):
        scripts = tmp_path / version / "scripts"
        shutil.copytree(SCRIPTS, scripts, ignore=shutil.ignore_patterns("__pycache__"))
        release = data / "releases" / version
        release.mkdir(parents=True)
        validation = dict(schema="mindie-candidate-check/1", status="validated", native_version=version)
        (release / "runtime.json").write_text(json.dumps(dict(
            python=python, validation=validation)), encoding="utf-8", newline="\n")
        publish_entry(Path(python), scripts, release)
        engine = release / "profile.engine.json"
        engine.write_text(json.dumps(dict(
            root=str(tmp_path / "corpus"), domain="test",
            admission_path=str(tmp_path / "admission.sqlite3"),
            transcript_adapter=str(scripts / "codex_transcript.py"), product_validation=validation)),
            encoding="utf-8", newline="\n")
        adapter = dict(python=python, runtime_scripts=str(scripts),
                       engine_config=str(engine), product_validation=validation,
                       admission_path=str(tmp_path / "admission.sqlite3"))
        releases.append((release, adapter))
    old, new = releases
    config.write_text(json.dumps(old[1]), encoding="utf-8", newline="\n")
    updates = config.with_suffix(".updates")
    updates.mkdir()
    (updates / "status.json").write_text(json.dumps(dict(
        status="current", last_successful_check=time.time())), encoding="utf-8", newline="\n")
    return config, data, old, new


def launch_after_cutover(installation, role, payload="", *, target=None):
    config, data, old, new = installation
    # Intercept only the process boundary, after the real native facade has
    # selected its fixed release. Publication itself uses the real atomic API.
    code = (
        "import runpy,sys;from pathlib import Path\n"
        f"sys.path.insert(0,{str(SCRIPTS)!r})\n"
        "from hook_launcher import launch\n"
        "from release_activation import _atomic\n"
        "original=runpy.run_path\n"
        "def cutover(path,**kw):\n"
        f" assert Path(path)==Path({str(old[0] / 'entry.py')!r})\n"
        f" _atomic(Path({str(config)!r}),{target if target is not None else new[1]!r})\n"
        " runpy.run_path=original\n"
        " return original(path,**kw)\n"
        "runpy.run_path=cutover\n"
        "launch()\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, role], input=payload,
        text=True, encoding="utf-8", capture_output=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=dict(os.environ, MINDIE_AGENT_CONFIG=str(config), PLUGIN_DATA=str(data)))


def test_session_start_uses_the_committed_runtime_after_launcher_selection(installation):
    result = launch_after_cutover(installation, "session-start")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr)["status"] == "inactive"
    starts = [json.loads(line) for line in (installation[1] / "runtime-starts.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert all(row["python"] == installation[3][1]["python"] for row in starts)
    assert any(row["argv"][0] == str(Path(installation[3][1]["runtime_scripts"]) / "session_maintenance.py")
               for row in starts)
    assert result.stdout == ""


@pytest.mark.parametrize("role", ["remote-mcp", "tools-mcp"])
def test_mcp_preserves_unread_stdio_and_serves_the_committed_catalog(installation, role):
    payload = "\n".join(json.dumps(dict(jsonrpc="2.0", id=n, method=method))
                        for n, method in enumerate(("initialize", "tools/list"), 1)) + "\n"
    result = launch_after_cutover(installation, role, payload)
    assert result.returncode == 0, result.stderr
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    assert rows[0]["id"] == 1
    tools = rows[1]["result"]["tools"]
    assert any(tool["name"] == ("remote_probe" if role == "remote-mcp" else "diagnostics_status")
               for tool in tools)
    assert not any(tool["name"].startswith("knowledge_") for tool in tools)


def test_explicit_prompt_is_forwarded_once_after_it_was_read(installation, tmp_path):
    from mindie_knowledge.loop.activation import Admission
    authority = Admission(installation[3][1]["admission_path"])
    authority.activate("cutover-session", project_root=str(tmp_path), root_session="cutover-session")
    authority.activate("other-session", project_root=str(tmp_path), root_session="other-session")
    event = dict(hook_event_name="UserPromptSubmit", session_id="cutover-session",
                 cwd=str(tmp_path), prompt="/mindie-agent deactivate")
    result = launch_after_cutover(installation, "user-prompt-submit", json.dumps(event))
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "systemMessage" not in response
    assert authority.knowledge_lease("cutover-session") is None
    assert authority.knowledge_lease("other-session")
    starts = [json.loads(line) for line in (installation[1] / "runtime-starts.jsonl").read_text(
        encoding="utf-8").splitlines()]
    assert [row["argv"][0] for row in starts] == [str(installation[3][0] / "entry.py"),
        str(Path(installation[3][1]["runtime_scripts"]) / "entry.py")]


def test_inconsistent_profile_fails_without_reinstalling_or_dispatching(installation):
    config, _data, old, new = installation
    damaged = dict(new[1], product_validation=old[1]["product_validation"])
    result = launch_after_cutover(installation, "session-start", target=damaged)
    assert result.returncode == 1
    assert "unsupported installation profile" in result.stderr
    assert "\"scripts\"" not in result.stderr
    assert json.loads(config.read_text(encoding="utf-8")) == damaged
    events = [json.loads(line) for path in Path(os.environ["MINDIE_DIAGNOSTICS_ROOT"]).glob("events/*/*.jsonl")
              for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    assert events[0]["attributes"]["category"] == "configuration"
    assert events[0]["attributes"]["error_type"] == "ProfileUnavailable"
    assert "error_code" not in events[0]["attributes"]


def test_component_cutover_uses_its_explicit_release(installation):
    config, _data, _old, new = installation
    adapter = dict(new[1], component_release=str(new[0]))
    result = launch_after_cutover(installation, "session-start", target=adapter)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr)["status"] == "inactive"
    assert json.loads(config.read_text(encoding="utf-8")) == adapter


def test_first_profile_cutover_uses_the_release_identity_not_the_engine_directory(installation):
    config, _data, _old, new = installation
    engine = config.with_name("codex.engine.json")
    Path(new[1]["engine_config"]).replace(engine)
    adapter = dict(new[1], engine_config=str(engine))
    result = launch_after_cutover(installation, "session-start", target=adapter)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr)["status"] == "inactive"
    assert json.loads(config.read_text(encoding="utf-8")) == adapter


@pytest.mark.parametrize("fault", ["missing_entry", "missing_receipt", "receipt_mismatch", "same_release"])
def test_invalid_replacement_never_loops_or_dispatches(installation, fault):
    config, _data, old, new = installation
    adapter = dict(new[1])
    if fault == "missing_entry":
        (new[0] / "entry.py").unlink()
    elif fault == "missing_receipt":
        (new[0] / "runtime.json").unlink()
    elif fault == "receipt_mismatch":
        (new[0] / "runtime.json").write_text(json.dumps(dict(
            python=old[1]["python"], validation=old[1]["product_validation"])),
            encoding="utf-8", newline="\n")
    else:
        adapter["component_release"] = str(old[0])
    result = launch_after_cutover(installation, "session-start", target=adapter)
    assert result.returncode == 1
    assert "ProfileUnavailable" in result.stderr
    assert "\"scripts\"" not in result.stderr
    assert json.loads(config.read_text(encoding="utf-8")) == adapter


@pytest.mark.parametrize("role", ["session-start", "user-prompt-submit"])
def test_transfer_preserves_the_selected_entry_failure(tmp_path, role):
    fixture = tmp_path / "failed_entry_fixture.py"
    fixture.write_text(
        "import sys\nprint('selected-entry-failed',file=sys.stderr)\nraise SystemExit(17)\n",
        encoding="utf-8", newline="\n")
    code = ("import os,sys\nfrom types import SimpleNamespace\n"
            f"sys.path.insert(0,{str(SCRIPTS)!r})\nfrom entry import transfer_entry\n"
            f"args=SimpleNamespace(role={role!r},arguments=[],prompt_event={{}})\n"
            f"raise SystemExit(transfer_entry([sys.executable,{str(fixture)!r}],args,dict(os.environ)))\n")
    result = subprocess.run([sys.executable, "-I", "-c", code],
                            capture_output=True, text=True, encoding="utf-8", timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 17, result.stderr
    assert result.stderr == "selected-entry-failed\n"
    assert result.stdout == ""


def test_transfer_preserves_utf8_event_at_the_native_input_byte_limit(tmp_path):
    fixture = tmp_path / "read_event_fixture.py"
    fixture.write_text(
        "import hashlib,json,sys\n"
        f"sys.path.insert(0,{str(SCRIPTS)!r})\n"
        "from manual_entry import read_event\n"
        "event=read_event(sys.stdin.buffer)\n"
        "print(json.dumps({'session_id':event['session_id'],'metadata':event['metadata'],"
        "'prompt_sha256':hashlib.sha256(event['prompt'].encode('utf-8')).hexdigest()}))\n",
        encoding="utf-8", newline="\n")
    code = ("import os,sys\nfrom types import SimpleNamespace\n"
            f"sys.path.insert(0,{str(SCRIPTS)!r})\n"
            "from entry import transfer_entry\nfrom manual_entry import read_event\n"
            "args=SimpleNamespace(role='user-prompt-submit',arguments=[],"
            "prompt_event=read_event(sys.stdin.buffer))\n"
            f"raise SystemExit(transfer_entry([sys.executable,{str(fixture)!r}],args,dict(os.environ)))\n")
    event = dict(hook_event_name="UserPromptSubmit", session_id="utf8-cutover",
                 cwd=str(tmp_path), prompt="/mindie-agent ", metadata="中文🙂")
    overhead = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    remaining = 32 * 1024 * 1024 - overhead
    event["prompt"] += "中" * (remaining // 3) + "x" * (remaining % 3)
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(payload) == 32 * 1024 * 1024

    def launch(data):
        return subprocess.run([sys.executable, "-I", "-c", code], input=data,
                              capture_output=True, timeout=15,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    result = launch(payload)
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert result.stderr == b""
    assert json.loads(result.stdout) == dict(
        session_id=event["session_id"], metadata=event["metadata"],
        prompt_sha256=hashlib.sha256(event["prompt"].encode("utf-8")).hexdigest())
    oversized = launch(payload + b" ")
    assert oversized.returncode == 1
    assert b"native prompt hook exceeds its byte budget" in oversized.stderr
    assert oversized.stdout == b""


def test_shared_dependency_site_runs_its_path_and_bootstrap_hooks(tmp_path):
    runtime = tmp_path / "runtime"
    builder = venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt")
    builder.create(runtime)
    context = builder.ensure_directories(runtime)
    dependency = tmp_path / "dependency-site"
    extensions = dependency / "native-extensions"
    extensions.mkdir(parents=True)
    # The same installation shape as pywin32: import paths plus startup work
    # are declared by the dependency's own .pth rather than its root directory.
    (dependency / "dependency.pth").write_text(
        "native-extensions\nimport fixture_dependency_bootstrap\n", encoding="utf-8", newline="\n")
    (extensions / "fixture_dependency_bootstrap.py").write_text(
        "import os\nos.environ['FIXTURE_DEPENDENCY_READY']='initialized'\n",
        encoding="utf-8", newline="\n")
    def launch():
        return subprocess.run(
            [context.env_exe, "-I", "-c", "import os; print(os.environ['FIXTURE_DEPENDENCY_READY'])"],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    Path(context.lib_path, "locked-dependencies.pth").write_text(
        str(dependency) + "\n", encoding="utf-8", newline="\n")
    uninitialized = launch()
    assert uninitialized.returncode == 1
    assert "FIXTURE_DEPENDENCY_READY" in uninitialized.stderr
    share_dependency_sites(context.lib_path, [dependency])
    result = launch()
    assert result.returncode == 0, result.stderr
    assert result.stdout == "initialized\n"
    assert result.stderr == ""


def test_transfer_preserves_pipe_bytes_through_crlf_text_streams(tmp_path):
    output, error = "结果\r\n继续\n".encode("utf-8"), b"selected-entry-failed\r\n"
    fixture = tmp_path / "pipe_output_fixture.py"
    fixture.write_text(
        f"import sys\nsys.stdout.buffer.write({output!r})\nsys.stderr.buffer.write({error!r})\n"
        "raise SystemExit(17)\n", encoding="utf-8", newline="\n")
    code = ("import os,sys\nfrom types import SimpleNamespace\n"
            "sys.stdout.reconfigure(newline='\\r\\n');sys.stderr.reconfigure(newline='\\r\\n')\n"
            f"sys.path.insert(0,{str(SCRIPTS)!r})\nfrom entry import transfer_entry\n"
            "args=SimpleNamespace(role='user-prompt-submit',arguments=[],prompt_event={})\n"
            f"raise SystemExit(transfer_entry([sys.executable,{str(fixture)!r}],args,dict(os.environ)))\n")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 17, result.stderr
    assert result.stdout == output
    assert result.stderr == error
