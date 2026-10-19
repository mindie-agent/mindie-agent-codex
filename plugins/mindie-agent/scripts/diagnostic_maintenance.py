"""Local retention and consent-gated handoff; diagnostics owns its own service.

[Role]
Activates the reporter from saved unified consent, then runs local retention.

[Invariants]
- Propagates subprocess cleanup issues or non-zero exit status as structured errors rather than masking them.

[Timeouts & Locks]
- Executes under `bounded_process.run` with clean `runtime_env(config)`.

[Side-Effect Policy]
- Prunes expired local diagnostic events and, when reporting is enabled, refreshes the reporter daemon.
"""
import json
from pathlib import Path
import sys

from bounded_process import run
from session_gate import runtime_env


def maintain(config):
    environment = runtime_env(config)
    activation_result = run(
        [sys.executable, str(Path(__file__).with_name("diagnostic_support.py")),
         "activate", "--config", str(config)], "",
        timeout=90.0, allowed_returncodes=(0, 1), env=environment, allow_service=True)
    activation = json.loads(activation_result.checked_stdout())
    command = [sys.executable, '-m', 'mindie_diagnostics.cli', 'reporting', 'maintain',
               '--config', environment["MINDIE_DIAGNOSTICS_CONFIG"]]
    result = run(command, '', timeout=5.0, allowed_returncodes=(0, 1), env=environment)
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get('status'), str):
        raise ValueError('diagnostics maintenance returned an invalid receipt')
    if result.cleanup:
        value = dict(value, cleanup=result.cleanup, status='degraded')
    value["reporting"] = activation
    if activation_result.cleanup or activation.get("status") not in {"running", "disabled", "needs_choice"}:
        value["status"] = "degraded"
    return value
