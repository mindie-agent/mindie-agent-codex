"""Local retention and consent-gated handoff; diagnostics owns its own service.

[Role]
Runs `python -m mindie_diagnostics.cli reporting maintain` (adding `--update-running` when saved consent is `enabled`) during `SessionStart` maintenance.

[Invariants]
- Propagates subprocess cleanup issues or non-zero exit status as structured errors rather than masking them.

[Timeouts & Locks]
- Executes under `bounded_process.run` with clean `runtime_env(config)`.

[Side-Effect Policy]
- Prunes expired local diagnostic events and, when reporting is enabled, refreshes the reporter daemon.
"""
import json
import sys

import consent
from bounded_process import run
from session_gate import runtime_env


def maintain(config):
    command = [sys.executable, '-m', 'mindie_diagnostics.cli', 'reporting', 'maintain']
    if consent.load(config).get('reporting') == 'enabled':
        command.append('--update-running')
    result = run(command, '', allowed_returncodes=(0, 1), env=runtime_env(config))
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get('status'), str):
        raise ValueError('diagnostics maintenance returned an invalid receipt')
    if result.cleanup:
        value = dict(value, cleanup=result.cleanup, status='degraded')
    return value
