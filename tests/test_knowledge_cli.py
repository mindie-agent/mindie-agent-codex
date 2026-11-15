"""The private CLI starts under isolated Python and retains its manual gate."""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from mindie_knowledge.loop.activation import Admission


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/mindie-agent/scripts"
sys.path.insert(0, str(SCRIPTS))
import knowledge_cli


SECRET = "SECRET-CLI-CANARY-7731"


class _Input:
    """Binary stdin stand-in that counts reads and serves fixed bytes."""

    def __init__(self, raw=b""):
        self.buffer = self
        self.raw = raw
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        if size is None or size < 0:
            return self.raw
        return self.raw[:size]


class _Output:
    def __init__(self):
        self.buffer = io.BytesIO()

    def flush(self):
        pass


def _run_cli(argv, stdin=b""):
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPTS / "knowledge_cli.py"), *argv],
        input=stdin, capture_output=True, timeout=15)


def _failure(completed, reason, stage="cli_input", label=""):
    assert completed.returncode == 1, (label, completed.stderr)
    assert b"Traceback" not in completed.stderr, label
    value = json.loads(completed.stdout.decode("utf-8"))
    assert value["isError"] is True, label
    structured = value["structuredContent"]
    assert structured["code"] == "invalid_arguments", label
    assert structured["stage"] == stage, label
    assert structured["reason"] == reason, label
    assert structured["execution"] == "not_started", label
    assert structured["automatic_retry"] is False, label
    return value


def _dispatch(argv, raw=b""):
    seen = {}

    def fake_invoke(operation, arguments):
        seen["operation"] = operation
        seen["arguments"] = arguments
        return dict(content=[], isError=False, structuredContent={})

    stdin, stdout = _Input(raw), _Output()
    with patch.object(knowledge_cli, "invoke", side_effect=fake_invoke), \
            patch.object(knowledge_cli.sys, "stdin", stdin), \
            patch.object(knowledge_cli.sys, "stdout", stdout):
        code = knowledge_cli.main(argv)
    return code, seen, json.loads(stdout.buffer.getvalue()), stdin


def test_isolated_describe_requires_the_current_session_grant(tmp_path):
    admission = tmp_path / "admission.sqlite3"
    config = tmp_path / "adapter.json"
    config.write_text(json.dumps(dict(admission_path=str(admission))))
    environment = dict(os.environ, MINDIE_AGENT_CONFIG=str(config),
                       CODEX_THREAD_ID="manual-cli")
    command = [sys.executable, "-I", str(SCRIPTS / "knowledge_cli.py"), "describe"]

    denied = subprocess.run(command, cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=5)
    assert denied.returncode == 1, denied.stderr
    assert json.loads(denied.stdout)["isError"] is True
    assert "tools" not in json.loads(denied.stdout)
    gate = Admission(admission)
    assert gate.knowledge_lease("manual-cli") is None

    gate.activate("manual-cli", project_root=str(tmp_path))
    allowed = subprocess.run(command, cwd=tmp_path, env=environment,
                             capture_output=True, text=True, timeout=5)
    assert allowed.returncode == 0, allowed.stderr
    tools = json.loads(allowed.stdout)["tools"]
    explain = next(tool for tool in tools if tool["name"] == "knowledge_explain")
    assert "refs" in explain["inputSchema"]["properties"]
    assert gate.knowledge_lease("manual-cli") is not None


def test_stdin_round_trips_chinese_and_non_bmp_emoji():
    arguments = {"query": "中文查询 🚀🧪", "limit": 3}
    raw = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
    code, seen, result, stdin = _dispatch(["knowledge_query"], raw)
    assert code == 0
    assert result["isError"] is False
    assert stdin.reads == 1
    assert seen == {"operation": "knowledge_query", "arguments": arguments}


def test_input_file_round_trips_chinese_emoji_bom_crlf_and_spaces(tmp_path):
    arguments = {"query": "中文 查询 🚀", "token_budget": 4000}
    path = tmp_path / "知识 query 数据.json"
    path.write_bytes(b"\xef\xbb\xbf"
                     + json.dumps(arguments, ensure_ascii=False).encode("utf-8")
                     + b"\r\n")
    code, seen, result, stdin = _dispatch(
        ["knowledge_query", "--input-file", str(path)],
        json.dumps({"query": SECRET}, ensure_ascii=False).encode("utf-8"))
    assert code == 0
    assert result["isError"] is False
    assert stdin.reads == 0
    assert seen == {"operation": "knowledge_query", "arguments": arguments}


def test_stdin_accepts_utf8_bom_and_crlf():
    arguments = {"query": "中文 🚀"}
    raw = (b"\xef\xbb\xbf" + json.dumps(arguments, ensure_ascii=False).encode("utf-8")
           + b"\r\n")
    code, seen, _result, _stdin = _dispatch(["knowledge_query"], raw)
    assert code == 0
    assert seen["arguments"] == arguments


def test_describe_reads_no_stdin_and_rejects_extra_arguments(tmp_path):
    code, seen, result, stdin = _dispatch(["describe"])
    assert code == 0
    assert stdin.reads == 0
    assert seen["operation"] == "describe"
    assert result["isError"] is False

    _failure(_run_cli(["describe", "--input-file", str(tmp_path / "missing.json")]),
             "argv_invalid", label="describe --input-file")
    _failure(_run_cli(["describe", "extra"]), "argv_invalid", label="describe extra")

    code, _seen, result, stdin = _dispatch(["describe", "extra"], b'{"query":"secret"}')
    assert code == 1
    assert result["structuredContent"]["reason"] == "argv_invalid"
    assert stdin.reads == 0


def test_argv_grammar_is_exact_and_reports_argv_invalid(tmp_path):
    path = tmp_path / "input.json"
    path.write_bytes(b'{"query":"x"}')
    for argv in (
        [],
        ["knowledge_query", "extra"],
        ["knowledge_query", "--input-file"],
        ["knowledge_query", "--input-file", ""],
        ["knowledge_query", "--input-file", str(path), "extra"],
        ["knowledge_query", "--json", "{}"],
        ["knowledge_query", str(path)],
        ["--input-file", str(path)],
    ):
        _failure(_run_cli(argv, b'{"query":"ignored"}'), "argv_invalid", label=repr(argv))


def test_input_byte_failures_are_distinct_and_leak_nothing(tmp_path):
    cases = (
        ("empty", b"", "input_empty"),
        ("whitespace", b" \t\r\n\v\f", "input_empty"),
        ("bom-only", b"\xef\xbb\xbf", "input_empty"),
        ("bom-whitespace", b"\xef\xbb\xbf \r\n", "input_empty"),
        ("utf16", b"\xff\xfe" + json.dumps(
            {"query": SECRET}, ensure_ascii=False).encode("utf-16-le"), "input_encoding"),
        ("gbk", json.dumps(
            {"query": "中文查询"}, ensure_ascii=False).encode("gbk"), "input_encoding"),
        ("oversized", b'{"query":"' + b"a" * (1024 * 1024), "input_too_large"),
        ("malformed", b'{"query":"' + SECRET.encode("utf-8") + b'", "limit": ', "input_json"),
        ("array", b"[1, 2]", "input_not_object"),
        ("scalar", b'"text"', "input_not_object"),
        ("null", b"null", "input_not_object"),
    )
    for name, raw, reason in cases:
        completed = _run_cli(["knowledge_query"], raw)
        value = _failure(completed, reason, label=name)
        if reason == "input_json":
            assert type(value["structuredContent"]["line"]) is int, name
            assert type(value["structuredContent"]["column"]) is int, name
        output = completed.stdout.decode("utf-8", "replace")
        error = completed.stderr.decode("utf-8", "replace")
        assert SECRET not in output, name
        assert SECRET not in error, name

    path = tmp_path / "whitespace.json"
    path.write_bytes(b" \r\n\t")
    _failure(_run_cli(["knowledge_query", "--input-file", str(path)]), "input_empty",
             label="file whitespace")

    path = tmp_path / "oversized.json"
    path.write_bytes(b'{"query":"' + b"a" * (1024 * 1024))
    _failure(_run_cli(["knowledge_query", "--input-file", str(path)]), "input_too_large",
             label="file oversized")

    path.write_bytes(b'{"query": ')
    value = _failure(_run_cli(["knowledge_query", "--input-file", str(path)]), "input_json",
                     label="file malformed")
    assert type(value["structuredContent"]["line"]) is int
    assert type(value["structuredContent"]["column"]) is int


def test_digit_limit_value_error_is_a_closed_input_rejection():
    completed = _run_cli(["knowledge_query"], b'{"n":' + b"9" * 5000 + b"}")
    value = _failure(completed, "input_json", label="digit-limit")
    assert "line" not in value["structuredContent"]
    assert "column" not in value["structuredContent"]


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "mkfifo"),
                    reason="POSIX FIFO input check")
def test_input_file_rejects_a_fifo_without_blocking_or_reading_stdin(tmp_path):
    fifo = tmp_path / "input.fifo"
    os.mkfifo(fifo)
    started = time.monotonic()
    code, seen, result, stdin = _dispatch(
        ["knowledge_query", "--input-file", str(fifo)],
        b'{"query":"stdin-must-not-be-read"}')
    assert time.monotonic() - started < 5
    assert code == 1
    assert seen == {}
    assert stdin.reads == 0
    structured = result["structuredContent"]
    assert result["isError"] is True
    assert structured["code"] == "input_unavailable"
    assert structured["stage"] == "input_read"
    assert structured["execution"] == "not_started"


def test_unreadable_or_missing_input_file_never_falls_back_to_stdin(tmp_path):
    environment = dict(os.environ, MINDIE_AGENT_CONFIG=str(tmp_path / "absent.json"),
                       CODEX_THREAD_ID="fixture-task")
    fallback = json.dumps({"query": SECRET}, ensure_ascii=False).encode("utf-8")
    missing = tmp_path / "不存在 的输入.json"
    for target in (missing, tmp_path):
        completed = subprocess.run(
            [sys.executable, "-I", str(SCRIPTS / "knowledge_cli.py"),
             "knowledge_query", "--input-file", str(target)],
            input=fallback, capture_output=True, timeout=15, env=environment)
        assert completed.returncode == 1, target
        assert b"Traceback" not in completed.stderr, target
        value = json.loads(completed.stdout.decode("utf-8"))
        structured = value["structuredContent"]
        assert value["isError"] is True, target
        assert structured["code"] == "input_unavailable", target
        assert structured["stage"] == "input_read", target
        assert structured["execution"] == "not_started", target
        assert structured["automatic_retry"] is False, target
        output = completed.stdout.decode("utf-8", "replace")
        assert SECRET not in output, target
        assert "不存在" not in output, target
        assert str(target) not in output, target


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 launcher check")
def test_windows_powershell_51_round_trips_chinese_through_input_file(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell 5.1 is unavailable")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import knowledge_cli\n"
        "seen = {}\n"
        "def invoke(operation, arguments):\n"
        "    seen.update(operation=operation, arguments=arguments)\n"
        "    return {'content': [], 'isError': False, 'structuredContent': {}}\n"
        "knowledge_cli.invoke = invoke\n"
        "code = knowledge_cli.main(['knowledge_query', '--input-file', sys.argv[2]])\n"
        "sys.stdout.buffer.write(b'\\n' + json.dumps(seen, ensure_ascii=False).encode('utf-8'))\n"
        "raise SystemExit(code)\n",
        encoding="utf-8")
    payload = tmp_path / "知识 输入.json"
    command = (
        f"$payload = '{{\"query\":\"中文 查询 🚀\",\"limit\":3}}'\n"
        f"$payload | Set-Content -LiteralPath '{_ps_quote(payload)}' -Encoding UTF8 -ErrorAction Stop\n"
        f"& '{_ps_quote(sys.executable)}' '{_ps_quote(probe)}' "
        f"'{_ps_quote(SCRIPTS)}' '{_ps_quote(payload)}'\n")
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    seen = json.loads(completed.stdout.decode("utf-8").splitlines()[-1])
    assert seen == {"operation": "knowledge_query",
                    "arguments": {"query": "中文 查询 🚀", "limit": 3}}
    assert payload.read_bytes().startswith(b"\xef\xbb\xbf")


def _ps_quote(path):
    return str(path).replace("'", "''")
