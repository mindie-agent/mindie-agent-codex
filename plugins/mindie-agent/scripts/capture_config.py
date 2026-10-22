"""Install-time wiring for deterministic public transcript capture.

[Role]
Validates the secret scanner (`redactor_executable`), wires `summary_command` / `organizer_command` / `worker_policy` into the engine configuration, and initializes the local knowledge store (`prepare-store`).

[Invariants]
- Refuses unsupported Windows Git root path lengths before opening `Store`.
- Binds `codex_transcript.py` and `agent_worker.py` to the exact installed runtime interpreter.

[Timeouts & Locks]
- Enforces `timeout=15.0s` for scanner verification and `timeout=30.0s` for `prepare_store()`.

[Side-Effect Policy]
- `prepare_store()` initializes the domain `Store` and feed directories under `root`.
"""
from bounded_process import run
import json


def store_failure(output, code):
    try:
        result = json.loads(output)
        if (result['status'] == 'failed' and result['stage'] == 'prepare-store'
                and isinstance(result['error_type'], str) and isinstance(result['error'], str)):
            return RuntimeError(f"knowledge store preparation failed ({result['error_type']}): {result['error'][:240]}; not retried")
    except (ValueError, KeyError, TypeError):
        pass
    return RuntimeError(f'knowledge store preparation failed with exit {code}; failure receipt unavailable; not retried')


def prepare_store(python, scripts, config):
    """Initialize through the selected core before publishing an installation."""
    result = run(
        [str(python), str(scripts / 'capture_config.py'), 'prepare-store'],
        json.dumps(config), timeout=30.0, max_output=8192, on_failure=store_failure,
    ).checked_stdout()
    if json.loads(result) != {'status': 'ready'}:
        raise RuntimeError('knowledge store preparation did not confirm readiness')


def prepare(python, scripts):
    path = run(
        [str(python), '-m', 'mindie_knowledge.loop.transcript_redaction'],
        '', timeout=15.0, max_output=8192,
    ).checked_stdout().strip()
    from pathlib import Path
    if not Path(path).is_absolute() or not Path(path).is_file():
        raise RuntimeError('installed transcript redactor is missing')
    return dict(capture_mode='public-transcript', redactor_executable=path,
                summary_command=[str(python), str(Path(scripts) / 'agent_worker.py')])


def prepare_domains(python, adapter, engine, **options):
    """Use the selected core's one catalog and policy-preserving preparation."""
    output = run([str(python), '-m', 'mindie_knowledge.loop.domain_catalog'],
                 json.dumps(dict(adapter=adapter, engine=engine, **options)),
                 timeout=90, max_output=65536).checked_stdout()
    return json.loads(output)['domain_configs']


if __name__ == "__main__":
    from pathlib import Path
    import sys
    if sys.argv[1:] == ['prepare-store']:
        try:
            from mindie_knowledge.loop.store import Store
            from mindie_knowledge.loop.service_context import validate
            from mindie_knowledge.git_layout import validate_config_feed_layout
            config = json.load(sys.stdin)
            validate(config)
            validate_config_feed_layout(config)
            store = Store(config['root'], config['domain'])
            store.close()
        except Exception as exc:
            # A bounded local receipt preserves the core cause without
            # forwarding raw stderr or treating a failed initialization as ready.
            print(json.dumps(dict(status='failed', stage='prepare-store',
                                  error_type=type(exc).__name__, error=str(exc)[:240])))
            raise SystemExit(1)
        print(json.dumps({'status': 'ready'}))
    elif sys.argv[1:]:
        raise ValueError('unsupported capture preparation operation')
    else:
        print(json.dumps(prepare(sys.executable, Path(__file__).resolve().parent)))
