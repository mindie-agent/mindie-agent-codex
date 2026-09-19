"""Native subprocess download/resume/verify lifecycle with offline SDK/API fixtures."""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from pathlib import Path
for _p in Path(__file__).resolve().parents:
    if (_p / "domain-lib").is_dir():
        if str(_p / "domain-lib") not in sys.path:
            sys.path.insert(0, str(_p / "domain-lib"))
        break
else:
    raise RuntimeError("MindIE domain-lib not found; use the installed plugin")
ROOT = Path(__file__).resolve().parents[1]  # the skill package directory
import time

import pytest

SCRIPTS = ROOT / "modelscope/scripts"
pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows process regression")


def test_status_preserves_worker_and_resume_verification(tmp_path):
    fixture = tmp_path / "fixtures"
    fixture.mkdir()
    ready = fixture / "sdk-ready"
    release = fixture / "sdk-release"
    data = b"partial bytes continued by the SDK fixture\n"
    metadata = {"Success": True, "Data": {"Files": [
        {"Type": "blob", "Path": "weights.bin", "Size": len(data), "Sha256": hashlib.sha256(data).hexdigest()}
    ]}}
    (fixture / "requests.py").write_text(
        "class Response:\n    def raise_for_status(self): pass\n    def json(self): return " + repr(metadata) +
        "\nclass Session:\n    def get(self,*a,**k): return Response()\n", encoding="utf-8")
    (fixture / "modelscope.py").write_text(
        "from pathlib import Path\nimport json, os, time\n"
        "def snapshot_download(model_id,revision,local_dir,max_workers=None,**kwargs):\n"
        "    root=Path(local_dir); path=root/'weights.bin'; data=" + repr(data) + "\n"
        "    previous=path.read_bytes() if path.exists() else b''\n"
        "    assert data.startswith(previous)\n"
        "    control=Path(__file__).parent\n"
        "    (control/'sdk-ready.tmp').write_text(json.dumps({'pid': os.getpid(), 'parent_pid': os.getppid()}),encoding='utf-8')\n"
        "    (control/'sdk-ready.tmp').replace(control/'sdk-ready')\n"
        "    deadline=time.monotonic()+90\n"
        "    while not (control/'sdk-release').is_file():\n"
        "        if time.monotonic()>=deadline: raise TimeoutError('SDK fixture release marker was not received')\n"
        "        time.sleep(.05)\n"
        "    with path.open('ab') as f: f.write(data[len(previous):])\n"
        "    return str(root)\n", encoding="utf-8")
    local = tmp_path / "中文 🧪 model"
    local.mkdir()
    (local / "weights.bin").write_bytes(data[:7])
    diagnostic_root = tmp_path / "diagnostics"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(fixture), os.environ.get("PYTHONPATH", ""))),
           "VAWS_DIAGNOSTICS_ROOT": str(diagnostic_root)}
    sys.path.insert(0, str(ROOT.parents[1] / "domain-lib"))
    from mindie_exec import owned_process, pid_alive
    stack = ExitStack()
    commands = []

    def run(action):
        started = time.monotonic()
        entry = {"action": action}
        commands.append(entry)
        # Keep every Job alive through the whole case, including detached
        # workers launched before ensure returns or times out.
        process = stack.enter_context(owned_process(
            [sys.executable, str(SCRIPTS / "modelscope_auto.py"), action,
             "--model", "fixture/tiny=" + str(local), "--max-retries", "1"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE))
        try:
            out, err = process.communicate(timeout=20)
        except subprocess.TimeoutExpired as exc:
            entry.update(timeout=True,
                         stdout=(exc.output or b"")[-4096:].decode("utf-8", "replace"),
                         stderr=(exc.stderr or b"")[-4096:].decode("utf-8", "replace"))
            raise
        else:
            entry.update(returncode=process.returncode,
                         stdout=out[-4096:].decode("utf-8", "replace"),
                         stderr=err[-4096:].decode("utf-8", "replace"))
            return process.returncode, out.decode("utf-8"), err.decode("utf-8")
        finally:
            entry["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)

    pid = None
    sdk_pid = None
    try:
        code, out, err = run("ensure")
        assert code == 0 and "download-started" in out, (out, err)
        record = json.loads((local / "download.pid").read_text(encoding="utf-8"))
        pid = record["pid"]
        deadline = time.monotonic() + 15
        while not ready.is_file() and pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(.1)
        assert ready.is_file(), "SDK worker did not reach the controlled active phase"
        sdk = json.loads(ready.read_text(encoding="utf-8"))
        sdk_pid = sdk["pid"]
        assert pid_alive(sdk_pid), "SDK exited before release"
        assert not release.exists()
        code, out, err = run("status")
        assert code == 0 and out.split("\t")[1] == "active", (out, err)
        assert pid_alive(pid), "status terminated the running worker"
        code, out, err = run("ensure")
        assert code == 0 and out.split("\t")[1] == "active", (out, err)
        assert json.loads((local / "download.pid").read_text(encoding="utf-8"))["pid"] == pid
        assert (local / "weights.bin").read_bytes() == data[:7]
        release.touch()
        deadline = time.monotonic() + 15
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(.1)
        assert not pid_alive(pid)
        assert (local / "weights.bin").read_bytes() == data
        assert json.loads((local / "modelscope_sha256.report.json").read_text(encoding="utf-8"))["all_ok"]
        assert not any((local / name).exists() for name in ("download.log", "verify.log", "download.launch.log"))
        diagnostic_files = list(diagnostic_root.glob("events/vaws-workspace/*.jsonl"))
        assert diagnostic_files
        events = [json.loads(line) for path in diagnostic_files for line in path.read_text(encoding="utf-8").splitlines()]
        assert any(event["event"] == "process.stderr" for event in events)
        assert (local / "SHA256SUMS").is_file()
        code, out, err = run("status")
        assert code == 0 and "verified" in out, (out, err)
        (local / "weights.bin").write_bytes(b"X" * len(data))
        code, out, err = run("status")
        assert code == 0 and "needs-verify" in out and "verify=stale" in out, (out, err)
        code, out, err = run("verify")
        assert code == 1 and "verify=failed" in out, (out, err)
        code, out, err = run("ensure")
        assert code == 0 and "verify-failed" in out, (out, err)
        assert (local / "weights.bin").read_bytes() == b"X" * len(data)
    except BaseException as exc:
        # Snapshot the failing state before release/Job cleanup can change it.
        files = {}
        for path in (ready, release, local / "download.pid", local / "download.launch.log",
                     local / "download.log", local / "verify.log"):
            try:
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 8192))
                    files[path.name] = stream.read(8192).decode("utf-8", "replace")
            except OSError as error:
                files[path.name] = f"{type(error).__name__}: {error}"
        diagnostic = json.dumps({"commands": commands, "files": files,
                                 "worker_alive": pid is not None and pid_alive(pid),
                                 "sdk_alive": sdk_pid is not None and pid_alive(sdk_pid)},
                                ensure_ascii=False, indent=2)
        exc.add_note("ModelScope lifecycle state before cleanup:\n" + diagnostic)
        try:
            (tmp_path / "lifecycle-failure.json").write_text(diagnostic, encoding="utf-8")
        except OSError as error:
            exc.add_note(f"Could not retain lifecycle-failure.json: {error}")
        raise
    finally:
        try:
            release.touch()
            deadline = time.monotonic() + 5
            while pid is not None and pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(.1)
        finally:
            stack.close()
