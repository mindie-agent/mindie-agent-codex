#!/usr/bin/env python3
"""Business helpers for vllm-ascend-serving: presets, progress, health probes."""

from __future__ import annotations

import json
import shlex
import sys

from pathlib import Path
for _p in Path(__file__).resolve().parents:
    if (_p / "domain-lib").is_dir():
        if str(_p / "domain-lib") not in sys.path:
            sys.path.insert(0, str(_p / "domain-lib"))
        break
else:
    raise RuntimeError("MindIE domain-lib not found; use the installed plugin")
from pathlib import Path
from typing import Any

from mindie_exec import ssh_exec as remote_ssh_exec  # noqa: E402
from mindie_target import SshEndpoint, ssh_endpoint_from_mapping  # noqa: E402
from mindie_receipt import emit_skill_json, progress as envelope_progress  # noqa: E402
from mindie_validate import parse_device_csv  # noqa: E402

SSH_CONNECT_TIMEOUT_SECONDS = 15
SSH_EXEC_DEFAULT_TIMEOUT_SECONDS = 180
PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
SERVICE_NAME = "vllm"
ROOT = Path.cwd()  # receipts record under the caller's business checkout


from mindie_receipt import measured as _diagnostic_measured

def ssh_exec(endpoint: SshEndpoint, script: str, *, check: bool = True, timeout: float | None = SSH_EXEC_DEFAULT_TIMEOUT_SECONDS):
    return remote_ssh_exec(
        endpoint,
        script,
        check=check,
        timeout=timeout,
        connect_timeout=SSH_CONNECT_TIMEOUT_SECONDS,
    )


def load_preset(name: str) -> dict[str, Any]:
    stem = name[:-5] if name.endswith(".json") else name
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        raise ValueError(f"invalid preset name {name!r}: use a bare preset name")
    path = PRESETS_DIR / f"{stem}.json"
    if not path.is_file():
        available = sorted(p.stem for p in PRESETS_DIR.glob("*.json")) if PRESETS_DIR.is_dir() else []
        raise ValueError(
            f"unknown preset {name!r}; available presets: "
            + (", ".join(available) if available else "(none)")
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"preset {name!r} must contain a JSON object")
    return data


def emit_progress(phase: str, message: str, **extra: Any) -> None:
    envelope_progress(phase, message, **extra)


def print_json(data: dict[str, Any]) -> None:
    emit_skill_json(
        data,
        skill="vllm-ascend-serving",
        entry_point="skills/vllm-ascend-serving/scripts/serving.py",
        compact=True,
        record_dir=ROOT / ".mindie" / "results",
    )


@_diagnostic_measured('business.service_probe')
def probe_service(ep: SshEndpoint, port: int, *, served_model: str | None = None,
                  timeout: float = 10) -> dict[str, Any]:
    """One remote round trip for health, models and optional completion.

    HTTP requests remain conditional; their combined curl budgets fit within
    the caller's remaining readiness budget. No log-tail or vLLM import runs.
    """
    if timeout < 0.01:
        return {"health": False, "models": None, "first_token": False}
    short = min(5, timeout / (4 if served_model is not None else 2))
    token_budget = min(120, timeout - 2 * short)
    base = f"http://127.0.0.1:{int(port)}"
    curl = f"curl --noproxy '*' -s --connect-timeout {min(3, short):.3f} --max-time {short:.3f}"
    lines = [
        'probe_dir=$(mktemp -d /tmp/mindie-probe.XXXXXX) || exit 1',
        'trap \'rm -rf -- "$probe_dir"\' EXIT',
        f'code=$({curl} -o /dev/null -w \'%{{http_code}}\' {base}/health 2>/dev/null)',
        'rc=$?; [ "$rc" = 0 ] || { echo __PROBE_FAILED__; exit 0; }',
        'echo "__HEALTH__=$code"',
        '[ "$code" = 200 ] || exit 0',
        f'code=$({curl} -o "$probe_dir/models" -w \'%{{http_code}}\' {base}/v1/models 2>/dev/null)',
        'rc=$?; [ "$rc" = 0 ] || { echo __PROBE_FAILED__; exit 0; }',
        'echo "__MODELS_CODE__=$code"',
        'echo __MODELS_BEGIN__; head -c 65536 "$probe_dir/models"; echo; echo __MODELS_END__',
        '[ "$code" = 200 ] || exit 0',
    ]
    if served_model is not None:
        payload = json.dumps({"model": served_model, "prompt": "Hello", "max_tokens": 8, "temperature": 0})
        lines.extend([
            f'code=$(curl --noproxy \'*\' -s --connect-timeout {min(3, token_budget):.3f} --max-time {token_budget:.3f} '
            f'-o "$probe_dir/token" -w \'%{{http_code}}\' -X POST {base}/v1/completions '
            f'-H \'Content-Type: application/json\' -d {shlex.quote(payload)} 2>/dev/null)',
            'rc=$?; [ "$rc" = 0 ] || { echo __PROBE_FAILED__; exit 0; }',
            'echo "__TOKEN_CODE__=$code"',
            'echo __TOKEN_BEGIN__; head -c 400 "$probe_dir/token"; echo; echo __TOKEN_END__',
        ])
    response = ssh_exec(ep, "\n".join(lines), check=False, timeout=timeout)
    out = response.stdout or ""
    models = None
    if "__MODELS_CODE__=200\n" in out and "__MODELS_BEGIN__" in out and "__MODELS_END__" in out:
        body = out.split("__MODELS_BEGIN__", 1)[1].split("__MODELS_END__", 1)[0].strip()
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("data"):
                models = data
        except ValueError:
            pass
    return {"health": "__HEALTH__=200\n" in out, "models": models,
            "first_token": "__TOKEN_CODE__=200\n" in out,
            "probe_error": response.returncode != 0 or "__PROBE_FAILED__" in out}


def now_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_devices_csv(value: str) -> list[int]:
    if not value or not str(value).strip():
        return []
    return list(parse_device_csv(str(value)) or [])


def endpoint_from_reply(reply: dict[str, Any]) -> SshEndpoint:
    target = reply.get("target") if isinstance(reply.get("target"), dict) else {}
    endpoint = target.get("endpoint") if isinstance(target.get("endpoint"), dict) else reply.get("endpoint")
    if not isinstance(endpoint, dict) or not endpoint.get("host"):
        raise RuntimeError("coordinator reply has no ordinary endpoint")
    return ssh_endpoint_from_mapping(endpoint)


def service_port_of(reply: dict[str, Any]) -> int | None:
    port = reply.get("service_port")
    if port in (None, ""):
        target = reply.get("target") if isinstance(reply.get("target"), dict) else {}
        port = target.get("service_port")
        env = target.get("environment") if isinstance(target.get("environment"), dict) else {}
        if port in (None, "") and env.get("MINDIE_SERVICE_PORT"):
            port = env["MINDIE_SERVICE_PORT"]
    if port in (None, ""):
        return None
    return int(port)
