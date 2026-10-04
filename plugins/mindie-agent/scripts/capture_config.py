"""Install-time wiring for deterministic public transcript capture."""
from bounded_process import run


def prepare(python, scripts):
    path = run(
        [str(python), '-m', 'mindie_knowledge.loop.transcript_redaction'],
        '', max_output=8192,
    ).strip()
    from pathlib import Path
    if not Path(path).is_absolute() or not Path(path).is_file():
        raise RuntimeError('installed transcript redactor is missing')
    return dict(capture_mode='public-transcript', redactor_executable=path,
                summary_command=[str(python), str(Path(scripts) / 'agent_worker.py')])


if __name__ == "__main__":
    import json
    from pathlib import Path
    import sys
    print(json.dumps(prepare(sys.executable, Path(__file__).resolve().parent)))
