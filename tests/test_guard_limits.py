"""A real unframed producer must be terminated at the protocol byte limit."""
import importlib.util
from pathlib import Path
import sys,time
import pytest
SCRIPTS=Path(__file__).resolve().parents[1]/'plugins/mindie-agent/scripts'
spec=importlib.util.spec_from_file_location('mindie_guard_limit_case',SCRIPTS/'process_guard.py')
guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)

def test_no_newline_output_is_bounded_and_stopped(monkeypatch):
    monkeypatch.delenv('MINDIE_MAINTENANCE_GROUP',raising=False)
    start=time.monotonic()
    with pytest.raises(ValueError,match='exceeds limit'):
        guard.run_codex([sys.executable,'-c','import os,time; os.write(1,b"x"*1048576); time.sleep(10)'],'')
    assert time.monotonic()-start<3
