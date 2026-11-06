"""Native catalog arguments reach the shipped metadata worker without SSH/NPU."""
import base64
import json
from importlib.metadata import version
from pathlib import Path
import shlex
import subprocess
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'))
import mcp_gate
import runtime_call
from remote_dev.core import context_snapshot
from remote_dev.core.ssh_transport import RemoteCompleted


def test_native_probe_arguments_reach_actual_interpreter_worker(tmp_path, monkeypatch):
    arguments = dict(host='example.invalid', port=22, root='/', cwd='/', runtime_env=False,
                     python_executable='/business/venv/bin/python',
                     python_candidates=['/custom/bin/python'],
                     distributions=['remote-dev'], required_distributions=['remote-dev'])
    name, accepted = mcp_gate.Gate('remote')._tool_args({'params': {
        'name': 'remote_probe', 'arguments': arguments}})
    observed = []

    def local_worker(endpoint, script, *, timeout_ms):
        argv = shlex.split(script[script.index('exec '):])[1:]
        packet = json.loads(zlib.decompress(base64.b64decode(argv[-1])))
        request = packet['request']
        observed.append({key: request[key] for key in (
            'python_executable', 'python_candidates', 'distributions', 'required_distributions')})
        # Map the remote namespace to this real isolated installed interpreter.
        request.update(python_executable=sys.executable, python_candidates=[],
                       root=str(tmp_path), cwd=str(tmp_path))
        proc = subprocess.run([sys.executable, '-I', '-c', packet['source'], json.dumps(request)],
                              capture_output=True, text=True, check=True, timeout=10,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return RemoteCompleted(proc.returncode, proc.stdout, proc.stderr, timed_out=False)

    monkeypatch.setattr(context_snapshot, 'run_script', local_worker)
    response = runtime_call.remote({'remote_session_id': 'probe-boundary', 'name': name,
                                    'arguments': accepted})
    assert response['isError'] is False, response
    assert observed == [{key: arguments[key] for key in observed[0]}]
    probe = response['structuredContent']['probe']
    assert probe['status'] == 'ok'
    assert Path(probe['summary']['python_executable']) == Path(sys.executable)
    assert probe['summary']['distributions']['remote-dev']['version'] == version('remote-dev')
