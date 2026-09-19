"""Submit explicit operator callables through the real coordinator TaskClient.

Ported domain logic: input bundling, script rendering, result decoding and
submission. The retired workspace owner handoff is gone; callers run in their
native task and pass --context-file when the coordinator cannot resolve it.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import zlib

from mindie_receipt import measured as _diagnostic_measured

MARKER = "__MINDIE_OPERATOR_RESULT__"


def owner_arguments(arguments, convert):
    """Translate only the path portion of explicit callable/source options."""
    names = {"--kernel", "--reference", "--cases", "--source"}
    pending = None
    result = []

    def value(name, text):
        if name == "--source":
            logical, sep, path = text.partition("=")
            return logical + sep + (convert(path) if path.startswith("/") else path) if sep else text
        path, sep, function = text.rpartition(":")
        return (convert(path) if path.startswith("/") else path) + sep + function if sep else text

    for argument in arguments:
        if pending:
            result.append(value(pending, argument))
            pending = None
            continue
        name, equal, text = argument.partition("=")
        if name in names:
            result.append(name + "=" + value(name, text) if equal else argument)
            pending = None if equal else name
        else:
            result.append(argument)
    return result


@_diagnostic_measured('business.bundle_inputs')
def bundle_inputs(kernel, reference, cases, **options):
    files, entries, origins = {}, {}, {}
    for role, text in (("kernel", kernel), ("reference", reference), ("cases", cases)):
        filename, sep, function = str(text).rpartition(":")
        if not sep or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", function):
            raise ValueError(f"{role} must be an explicit file.py:callable")
        path = Path(filename).expanduser().resolve(strict=True)
        if path.suffix != ".py" or path.stat().st_size > 1024 * 1024:
            raise ValueError(f"{role} must be a Python file of at most 1 MiB")
        source = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        ast.parse(source, filename=path.name)
        if path.name in files and files[path.name] != source:
            raise ValueError("entry files with the same basename have different contents; use distinct names")
        files[path.name] = source
        entries[role] = {"file": path.name, "function": function}
        origins[role] = {"path": str(path), "sha256": hashlib.sha256(source.encode()).hexdigest()}
    return {"files": files, "entries": entries, **options}, origins


@_diagnostic_measured('business.render')
def render_script(payload):
    program = Path(__file__).with_name("mindie_operator_payload.py").read_text(encoding="utf-8")
    program += "\nraise SystemExit(run(json.loads(" + repr(json.dumps(payload, ensure_ascii=False)) + ")))\n"
    return 'set -euo pipefail\n"$MINDIE_PYTHON" - <<\'MINDIE_OPERATOR_PY\'\n' + program + '\nMINDIE_OPERATOR_PY\n'


@_diagnostic_measured('business.decode')
def decode_business(reply):
    for line in reversed(str(reply.get("stdout") or reply.get("tail") or "").splitlines()):
        if line.startswith(MARKER):
            raw = base64.b64decode(line[len(MARKER):], validate=True)
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(raw, 1024 * 1024 + 1)
            if len(decoded) > 1024 * 1024 or not decoder.eof:
                raise ValueError("operator result is truncated or exceeds 1 MiB")
            result = json.loads(decoded)
            if not isinstance(result, dict) or result.get("status") not in ("passed", "failed"):
                raise ValueError("operator result has an invalid shape")
            return result
    return None


@_diagnostic_measured('business.submit_wait')
def submit(payload, origins, *, client, output_dir, run_options):
    output_dir.mkdir(parents=True, exist_ok=False)
    script = output_dir / "operator-case.sh"
    script.write_text(render_script(payload), encoding="utf-8", newline="\n")
    (output_dir / "inputs.json").write_text(json.dumps({"entries": origins,
        "options": {k: v for k, v in payload.items() if k not in ("files", "entries")},
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest()}, indent=2) + "\n", encoding="utf-8")
    reply = client.run(script_file=str(script), **run_options)
    record = {"execution": reply, "inputs": origins, "script_file": str(script)}
    try:
        business = decode_business(reply)
        if business and business.get("source_hashes") != {name: hashlib.sha256(text.encode()).hexdigest() for name, text in payload["files"].items()}:
            raise ValueError("returned callable source hashes differ from submitted files")
        record["business"] = business
    except (ValueError, TypeError, zlib.error) as exc:
        record["evidence_error"] = str(exc)
        business = None
    path = output_dir / "result.json"
    warning = None
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        warning = f"Local result could not be saved: {exc}"
    complete = reply.get("state") in ("succeeded", "failed", "cancelled", "timeout", "inconclusive") and reply.get("resources_released") is True
    passed = complete and reply.get("state") == "succeeded" and business is not None and business.get("status") == "passed"
    result = {"status": "passed" if passed else "failed" if complete else "pending",
        "execution_id": reply.get("execution_id"), "state": reply.get("state"),
        "resources_released": reply.get("resources_released"), "wait_timed_out": reply.get("wait_timed_out", False),
        "record_ref": str(path) if warning is None else None, "business": business,
        "evidence_error": record.get("evidence_error"),
        **{k: reply[k] for k in ("wait_error", "tail_error", "logs_pending") if k in reply}}
    if complete and business is None:
        result["evidence_error"] = result["evidence_error"] or "No complete operator result in the owned log reply; use the same execution reference, do not resubmit."
    if warning:
        result["warning"] = warning
    return result, 0 if passed or not complete else 1


def main(argv=None, *, benchmark=False, workspace_root=None):
    parser = argparse.ArgumentParser(description="Run explicit candidate/reference callables and compare their outputs in one managed execution.")
    for name in ("kernel", "reference", "cases"):
        parser.add_argument("--" + name, required=True, help="Local self-contained file.py:callable; cases(device) yields id/args/kwargs")
    parser.add_argument("--context-file")
    parser.add_argument("--source", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--no-project-sources", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--image", help="Explicit coordinator environment image constraint")
    parser.add_argument("--device", choices=("npu", "cpu"), default="npu")
    parser.add_argument("--atol", type=float, default=1e-5)
    parser.add_argument("--rtol", type=float, default=1e-5)
    parser.add_argument("--warmups", type=int, default=3 if benchmark else 0)
    parser.add_argument("--repeats", type=int, default=20 if benchmark else 0)
    parser.add_argument("--max-cases", type=int, default=64)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--wait-timeout-seconds", type=float, default=180)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if any(not math.isfinite(v) or v < 0 for v in (args.atol, args.rtol)):
            raise ValueError("tolerances must be finite and nonnegative")
        if not 0 <= args.warmups <= 100 or not 0 <= args.repeats <= 1000 or not 1 <= args.max_cases <= 256:
            raise ValueError("warmups/repeats/max-cases outside supported bounds (100/1000/256)")
        if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0 or not 0 <= args.wait_timeout_seconds <= 600:
            raise ValueError("timeout must be positive and wait budget must be 0..600 seconds")
        sources = {}
        for item in args.source:
            name, sep, value = item.partition("=")
            if not sep or not name or not value or name in sources:
                raise ValueError("--source needs a unique NAME=PATH")
            sources[name] = str(Path(value).expanduser().resolve(strict=True))
        if args.no_project_sources and sources:
            raise ValueError("--no-project-sources conflicts with --source")
        payload, origins = bundle_inputs(args.kernel, args.reference, args.cases,
            device=args.device, atol=args.atol, rtol=args.rtol, warmups=args.warmups,
            repeats=args.repeats, max_cases=args.max_cases)
        from mindie_jobs import task_client
        client = task_client(args.context_file)
        from mindie_state import state_root
        root = Path(workspace_root) if workspace_root else None
        parent = state_root(str(root)) / "operator-runs" if root else state_root() / "operator-runs"
        parent.mkdir(parents=True, exist_ok=True)
        # Reserve only a unique name; submit creates it before admission.
        import uuid
        output = args.output_dir or parent / uuid.uuid4().hex
        options = {"resources": {"npu_count": 1 if args.device == "npu" else 0},
                   "timeout_seconds": args.timeout_seconds, "wait_until": "released",
                   "wait_timeout_seconds": args.wait_timeout_seconds}
        if sources or args.no_project_sources:
            options["sources"] = sources
        if args.host:
            options["topology"] = {"host": args.host}
        if args.image:
            options["environment"] = {"image": args.image}
        result, code = submit(payload, origins, client=client, output_dir=output, run_options=options)
    except (OSError, ValueError, RuntimeError, SyntaxError) as exc:
        result, code = {"status": "failed", "error": str(exc)}, 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
