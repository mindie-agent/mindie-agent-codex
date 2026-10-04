"""A killed launcher cannot make an executing helper's generation collectible."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'plugins/mindie-agent/scripts'
sys.path.insert(0, str(SCRIPTS))
from auto_update import Updater, atomic
from update_lock import file_lock


@unittest.skipUnless(os.name == 'posix', 'POSIX flock inheritance; Windows owns children with a Job')
class GenerationLeaseLifetimeTests(unittest.TestCase):
    def test_both_stable_launchers_transfer_their_lease_before_parent_death(self):
        for role in ('runtime', 'updater'):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary).resolve()
                root = base / 'updates'; root.mkdir()
                config, settings, ready = base / 'adapter.json', base / 'settings.json', base / 'child.pid'
                for name in ('runtime_launcher.py', 'update_launcher.py', 'update_lock.py'):
                    shutil.copy2(SCRIPTS / name, root / name)
                for revision in ('old', 'new'):
                    generation = root / 'generations' / revision
                    scripts = generation / 'plugin/scripts'; scripts.mkdir(parents=True)
                    atomic(generation / 'ownership.json', dict(schema='mindie-runtime-generation/2', revision=revision))
                    for name in ('bounded_process.py', 'windows_process.py'):
                        shutil.copy2(SCRIPTS / name, scripts / name)
                    child_code = ('from pathlib import Path;import os,time;Path(' + repr(str(ready))
                                  + ').write_text(str(os.getpid()));time.sleep(30)')
                    body = 'import sys\nfrom bounded_process import run\nrun([sys.executable,"-c",' + repr(child_code) + '],"",timeout=None)\n'
                    (scripts / 'remote_bridge.py').write_text(body)
                    (scripts / 'auto_update.py').write_text(body)
                atomic(config, dict(runtime_scripts=str(root / 'generations/old/plugin/scripts')))
                atomic(settings, dict(root=str(root), adapter_config=str(config)))
                atomic(root / 'state.json', dict(current=dict(revision='old', plugin=str(root / 'generations/old/plugin')), candidate='old'))
                command = ([sys.executable, str(root / 'runtime_launcher.py'), '--config', str(config), 'remote-mcp']
                           if role == 'runtime' else [sys.executable, str(root / 'update_launcher.py'), str(settings)])
                launcher = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                child = None
                try:
                    until = time.monotonic() + 5
                    while not ready.exists() and time.monotonic() < until:
                        time.sleep(.01)
                    self.assertTrue(ready.exists(), 'fixture helper did not start')
                    child = int(ready.read_text())
                    launcher.kill(); launcher.wait(timeout=3)
                    atomic(config, dict(runtime_scripts=str(root / 'generations/new/plugin/scripts')))
                    atomic(root / 'state.json', dict(current=dict(revision='new', plugin=str(root / 'generations/new/plugin')), candidate='new'))
                    os.kill(child, 0)
                    first = Updater(settings).collect_generations()
                    self.assertIn('old', first['kept'])
                    self.assertTrue((root / 'generations/old').is_dir())
                    os.killpg(child, signal.SIGKILL)
                    until = time.monotonic() + 5
                    while True:
                        try:
                            with file_lock(root / 'generation-locks/old.lock', exclusive=True):
                                break
                        except BlockingIOError:
                            if time.monotonic() >= until:
                                self.fail('child did not release inherited generation lease')
                            time.sleep(.01)
                    self.assertIn('old', Updater(settings).collect_generations()['removed'])
                finally:
                    if child:
                        try:
                            os.killpg(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if launcher.poll() is None:
                        launcher.kill(); launcher.wait(timeout=3)
