"""Install-time wiring for deterministic public transcript capture."""
import subprocess


def prepare(python, scripts):
    result = subprocess.run(
        [str(python), '-m', 'mindie_knowledge.loop.transcript_redaction'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if result.returncode:
        raise RuntimeError('could not install the verified transcript redactor')
    path = result.stdout.decode('utf-8').strip()
    from pathlib import Path
    if not Path(path).is_absolute() or not Path(path).is_file():
        raise RuntimeError('installed transcript redactor is missing')
    return dict(capture_mode='public-transcript', redactor_executable=path,
                summary_command=[str(python), str(Path(scripts) / 'agent_worker.py')])
