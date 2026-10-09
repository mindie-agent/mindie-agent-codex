"""Install-time wiring for deterministic public transcript capture."""
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
        json.dumps(config), max_output=8192, on_failure=store_failure,
    ).checked_stdout()
    if json.loads(result) != {'status': 'ready'}:
        raise RuntimeError('knowledge store preparation did not confirm readiness')


def prepare(python, scripts):
    path = run(
        [str(python), '-m', 'mindie_knowledge.loop.transcript_redaction'],
        '', max_output=8192,
    ).checked_stdout().strip()
    from pathlib import Path
    if not Path(path).is_absolute() or not Path(path).is_file():
        raise RuntimeError('installed transcript redactor is missing')
    return dict(capture_mode='public-transcript', redactor_executable=path,
                summary_command=[str(python), str(Path(scripts) / 'agent_worker.py')])


if __name__ == "__main__":
    from pathlib import Path
    import sys
    if sys.argv[1:] == ['prepare-store']:
        try:
            from mindie_knowledge.loop.store import Store
            config = json.load(sys.stdin)
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
