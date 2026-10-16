"""Offline retention is preserved after removal of the code updater."""
import sys
from pathlib import Path
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/mindie-agent/scripts'))
from bounded_process import ProcessResult
import diagnostic_maintenance


class DiagnosticMaintenanceTests(unittest.TestCase):
    def test_local_retention_runs_but_reporter_handoff_needs_saved_consent(self):
        for choice in (None,'later','disabled','enabled'):
            with self.subTest(choice=choice), \
                 patch('consent.load',return_value={'reporting':choice}), \
                 patch.object(diagnostic_maintenance,'runtime_env',return_value={}), \
                 patch.object(diagnostic_maintenance,'run',return_value=ProcessResult(
                     'completed',returncode=0,stdout='{"status":"ok","network":false}')) as command:
                self.assertEqual(diagnostic_maintenance.maintain(Path('/fixture.json')),
                                 {'status':'ok','network':False})
                args=command.call_args.args[0]
                self.assertEqual('--update-running' in args,choice=='enabled')
                self.assertNotIn('ensure',args)
                self.assertNotIn('configure',args)
