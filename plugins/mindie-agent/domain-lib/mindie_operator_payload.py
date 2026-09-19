"""Small callable comparison payload, embedded in an owned coordinator script.

No transport or task discovery lives here. The three supplied Python files are
business code; this invokes their explicit callables, not arbitrary kernel ABIs.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time
import traceback
import zlib

RESULT_MARKER = "__MINDIE_OPERATOR_RESULT__"


def _load_callable(path, name, module_name, modules):
    # One entry file can contain all three callables. Preserve its class and
    # module identities, and do not repeat import-time business initialization.
    if path not in modules:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        modules[path] = module
    module = modules[path]
    value = module
    for part in name.split("."):
        value = getattr(value, part)
    if not callable(value):
        raise ValueError(f"{path.name}:{name} is not callable")
    return value


def _describe(value, torch):
    if torch is not None and isinstance(value, torch.Tensor):
        return {"shape": list(value.shape), "dtype": str(value.dtype),
                "stride": list(value.stride()), "device": str(value.device)}
    if isinstance(value, dict):
        return {str(k): _describe(v, torch) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_describe(v, torch) for v in value]
    return repr(value)[:200]


def _has_npu(value, torch):
    if isinstance(value, torch.Tensor):
        return value.device.type == "npu"
    if isinstance(value, dict):
        return any(_has_npu(v, torch) for v in value.values())
    if isinstance(value, (tuple, list)):
        return any(_has_npu(v, torch) for v in value)
    return False


def _maximum_difference(left, right, torch):
    if left.dtype == torch.bool:
        difference = left != right
    elif left.dtype.is_complex or left.dtype.is_floating_point:
        dtype = torch.complex128 if left.dtype.is_complex else torch.float64
        difference = (left.to(dtype) - right.to(dtype)).abs()
    elif left.dtype in (torch.int64, getattr(torch, "uint64", None)):
        # float64 rounds large integers and int64 subtraction may overflow.
        # Python integers preserve the exact diagnostic (outside timed calls).
        return max((abs(a - b) for a, b in zip(left.reshape(-1).tolist(), right.reshape(-1).tolist())), default=0)
    else:
        difference = (left.to(torch.int64) - right.to(torch.int64)).abs()
    return difference.max().item() if difference.numel() else 0


def _compare(actual, expected, *, torch, atol, rtol, path="output"):
    if actual is None or expected is None:
        raise AssertionError(f"{path}: return the values to compare; None is not numerical evidence")
    if torch is not None and isinstance(actual, torch.Tensor):
        if not isinstance(expected, torch.Tensor):
            raise AssertionError(f"{path}: reference is not a Tensor")
        torch.testing.assert_close(actual, expected, atol=atol, rtol=rtol,
                                   check_device=False, equal_nan=False)
        left, right = actual.detach().cpu(), expected.detach().cpu()
        return [{"output": path, "max_abs": _maximum_difference(left, right, torch)}]
    if isinstance(actual, dict):
        if not isinstance(expected, dict) or actual.keys() != expected.keys():
            raise AssertionError(f"{path}: mapping keys differ")
        return [m for key in actual for m in _compare(actual[key], expected[key], torch=torch,
                atol=atol, rtol=rtol, path=f"{path}.{key}")]
    if isinstance(actual, (tuple, list)):
        if type(actual) is not type(expected) or len(actual) != len(expected):
            raise AssertionError(f"{path}: sequence type or length differs")
        return [m for i, (a, b) in enumerate(zip(actual, expected)) for m in
                _compare(a, b, torch=torch, atol=atol, rtol=rtol, path=f"{path}[{i}]")]
    if isinstance(actual, (int, float, complex)) and not isinstance(actual, bool):
        if not isinstance(expected, (int, float, complex)) or isinstance(expected, bool):
            raise AssertionError(f"{path}: numeric type differs")
        difference = abs(actual - expected)
        if not math.isfinite(difference) or difference > atol + rtol * abs(expected):
            raise AssertionError(f"{path}: numerical mismatch (absolute error {difference})")
        return [{"output": path, "max_abs": difference}]
    if type(actual) is not type(expected) or actual != expected:
        raise AssertionError(f"{path}: values differ")
    return [{"output": path, "equal": True}]


def run(payload):
    root = Path.cwd().resolve()
    stage = Path(tempfile.mkdtemp(prefix=".mindie-operator-", dir=root))
    record = {"status": "failed", "device": payload["device"], "cases": [],
              "execution_root": str(root), "bundle_dir": str(stage),
              "source_hashes": {}, "candidate_invocations": 0,
              "scope": "Explicit Python callable invocation and output comparison; no claim about internal kernel dispatch or absence of fallback."}
    exit_code = 1
    try:
        for name, text in payload["files"].items():
            if Path(name).name != name or name in (".", ".."):
                raise ValueError("bundle file must be a basename")
            (stage / name).write_text(text, encoding="utf-8", newline="\n")
            record["source_hashes"][name] = hashlib.sha256(text.encode()).hexdigest()
        sys.path.insert(0, str(stage))
        try:
            import torch
        except ImportError:
            torch = None
        if payload["device"] == "npu":
            import torch_npu  # noqa: F401
            if torch is None or not torch.npu.is_available():
                raise RuntimeError("requested NPU is not available; no CPU fallback")
            torch.npu.set_device(0)
        sync = torch.npu.synchronize if payload["device"] == "npu" else lambda: None
        modules = {}
        functions = {role: _load_callable(stage / entry["file"], entry["function"], "_mindie_" + role, modules)
                     for role, entry in payload["entries"].items()}
        seen = set()
        for index, case in enumerate(functions["cases"](payload["device"])):
            if index >= payload["max_cases"]:
                raise ValueError("case factory exceeds --max-cases; completed results are preserved")
            if not isinstance(case, dict):
                raise ValueError("case factory must yield dictionaries with id, args and optional kwargs")
            name = case.get("id", str(index))
            if not isinstance(name, str) or not name or len(name) > 128 or name in seen:
                raise ValueError("case IDs must be unique nonempty strings of at most 128 characters")
            seen.add(name)
            row = {"id": name, "status": "failed", "candidate_invoked": False}
            record["cases"].append(row)
            try:
                args, kwargs = case.get("args", ()), case.get("kwargs", {})
                if not isinstance(args, (tuple, list)) or not isinstance(kwargs, dict):
                    raise ValueError("case args must be a tuple/list and kwargs a dictionary")
                if payload["device"] == "npu" and not _has_npu((args, kwargs), torch):
                    raise ValueError("NPU cases must supply at least one actual NPU tensor input")
                row["inputs"] = _describe((args, kwargs), torch)
                atol, rtol = case.get("atol", payload["atol"]), case.get("rtol", payload["rtol"])
                if any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) or x < 0 for x in (atol, rtol)):
                    raise ValueError("atol/rtol must be finite nonnegative numbers")
                row.update(atol=atol, rtol=rtol)

                def invoke(role):
                    # Deepcopy preserves shared references within one input tree.
                    # Unsupported non-leaf/custom inputs fail visibly, not coerced.
                    copied_args, copied_kwargs = copy.deepcopy((args, kwargs))
                    sync()
                    started = time.perf_counter()
                    if role == "kernel":
                        row["candidate_invoked"] = True
                        record["candidate_invocations"] += 1
                    output = functions[role](*copied_args, **copied_kwargs)
                    sync()
                    return output, time.perf_counter() - started

                expected, reference_first = invoke("reference")
                actual, candidate_first = invoke("kernel")
                row["comparisons"] = _compare(actual, expected, torch=torch, atol=atol, rtol=rtol)
                if not row["comparisons"]:
                    raise AssertionError("return at least one value to compare; an empty output tree is not evidence")
                row["first_call_seconds"] = {"candidate": candidate_first, "reference": reference_first}
                if payload["repeats"]:
                    for _ in range(payload["warmups"]):
                        invoke("kernel")
                        invoke("reference")
                    samples = {"candidate": [], "reference": []}
                    for repeat in range(payload["repeats"]):
                        order = ("kernel", "reference") if repeat % 2 == 0 else ("reference", "kernel")
                        outputs = {}
                        for role in order:
                            outputs[role], elapsed = invoke(role)
                            samples["candidate" if role == "kernel" else role].append(elapsed)
                        _compare(outputs["kernel"], outputs["reference"], torch=torch, atol=atol, rtol=rtol)
                    row["timings"] = {key: {"samples_seconds": values, "median_seconds": statistics.median(values),
                                           "min_seconds": min(values), "max_seconds": max(values)} for key, values in samples.items()}
                row["status"] = "passed"
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"[:1500]
                row["traceback"] = traceback.format_exc()
        if not record["cases"]:
            raise ValueError("case factory produced no cases")
        record["status"] = "passed" if all(x["status"] == "passed" for x in record["cases"]) else "failed"
        exit_code = 0 if record["status"] == "passed" else 1
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"[:1500]
        record["traceback"] = traceback.format_exc()
    path = stage / "results.json"
    summary = {key: value for key, value in record.items() if key not in ("cases", "traceback")}
    try:
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary["record_path"] = str(path)
        summary["record_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        summary["record_path"] = None
        summary["record_error"] = str(exc)
    summary["cases"] = [{k: v for k, v in row.items() if k not in ("inputs", "traceback", "comparisons", "timings")}
                        | ({"max_abs": max((m.get("max_abs", 0) for m in row.get("comparisons", [])), default=0)} if row["status"] == "passed" else {})
                        | ({"timings": {k: {a: b for a, b in v.items() if a != "samples_seconds"}
                                        for k, v in row["timings"].items()}} if "timings" in row else {}) for row in record["cases"]]
    summary["passed_cases"] = sum(row["status"] == "passed" for row in record["cases"])
    summary["total_cases"] = len(record["cases"])
    # One final bounded line survives ordinary owned log tailing. Full details
    # and tracebacks stay in the current execution directory.
    encoded = base64.b64encode(zlib.compress(json.dumps(summary, ensure_ascii=False).encode())).decode()
    if len(encoded) > 16000:
        summary["cases"] = []
        summary["case_details_omitted"] = True
        encoded = base64.b64encode(zlib.compress(json.dumps(summary, ensure_ascii=False).encode())).decode()
    print(RESULT_MARKER + encoded, flush=True)
    return exit_code
