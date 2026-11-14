"""[Role] Expose deterministic component CLIs through the native MCP boundary.

[Invariants]
- Component parsers and engines own semantics; argv is passed without a shell.
- Discovery imports no component, starts no service and reads no knowledge.
- Local diagnostics never starts a reporter or uploads a support bundle.

[Timeouts & Locks]
- One owned child, a caller deadline (120s by default), a model-facing preview of
  at most max_output_tokens*4 bytes, and a 16 MiB combined local spool hard cap.
- The shared process runner owns cancellation and process-tree cleanup.

[Side-Effect Policy]
- Only explicit calls execute a component. Missing installations are errors.
- Arguments are schema-validated before any spawn. Complete output is spooled to
  private files under the adapter state directory (24h/64 files/128 MiB retention)
  and reported by path, byte count and sha256 receipt, never by raw leak.
- Optional component_commands in the adapter selects an installed executable
  and fixed arguments; it never installs or silently substitutes a runtime.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import sysconfig

from bounded_process import ProcessResult, run
from capture_spool import Capture
from session_gate import config_path, runtime_env


PATH_FIELDS = ("root", "config", "operation_id", "incident_id", "output")
MAX_STRING = 4096
MAX_ARGV = 256
MAX_ARGV_ITEM = 65536
NO_NUL = "^[^\\u0000]*$"
NO_NUL_NONEMPTY = "^[^\\u0000]+$"
TIMEOUT_MS = (1, 300000, 120000)
OUTPUT_TOKENS = (1, 262144, 16384)


DESCRIPTIONS = {
    "profiler": (
        "Inspect and analyze Ascend NPU profiling captures using mindie-profiler. "
        "Pass CLI arguments as an argv array, e.g. ['inventory', '--root', '/capture'] "
        "or ['analyze', '--root', '/capture', '--full-range']. Commands: inventory, analyze, "
        "correlate, compare, sql, camodel, and kernels. Use ['--help'] to inspect options "
        "and --output for large reports."
        " Offline analysis of existing captures requires no device lease or managed task binding."
    ),
    "sim": (
        "Compute Ascend hardware bounds (A2/A3/A5), operator/graph/serving latency, HBM capacity "
        "(including async_scheduling delayed release), Roofline efficiency, SLA concurrency "
        "frontiers, and P/D disaggregation using mindie-sim. Pass CLI arguments as an argv "
        "array: synthesize a case from model/hardware IDs or HF config (e.g. ['compose', "
        "'--model', 'qwen3.5-27b', '--hardware', 'a3', '--tp', '8', '--concurrency', '32', "
        "'--async-scheduling', '--output', '/case.json']) and run ['predict', '/case.json'], "
        "['efficiency', '/case.json'], ['sla', '/case.json'], ['sweep', '/case.json', "
        "'--mode', 'pd'], ['skew', '/case.json'], or ['graph', '/case.json', '--phase', "
        "'prefill', '--tokens', '32']. Use ['--help'] to inspect options and --output for "
        "large reports."
        " Simulation calculations require no device allocation or managed task binding."
    ),
    "coordinator": (
        "Manage Ascend host inventory, probe CPU/RAM/NPU capacity, bind containers, "
        "and acquire/inspect/release NPU and service-port leases through "
        "mindie-coordinator. Pass CLI argv, e.g. ['hosts', '--action', 'list'] "
        "or ['probe', '--json', '{\"action\":\"hosts\"}']. Use ['--help'] to inspect options."
        " Host inventory, probes, containers and bare-metal leases need no managed "
        "task binding or knowledge activation. Omit context_file for these independent "
        "operations; inspect resources before acquiring a lease."
    ),
    "debugger": (
        "Ascend NPU fault triage (`triage`, `decode-code`), ACLGraph/NPUGraph capture/replay & buffer-address "
        "debugger (`aclgraph`: `guard`, `inspect`), and operator precision/memory-stomp toolkit (`precision`: "
        "`check-call`, `replay-call`, `inspect-capsule`, `compare-traces`, `watch`) via mindie-debugger. "
        "Error-code decoding (`['decode-code', '507015']`) or inline stderr triage (`['triage', '--text', '<err>']`) "
        "runs locally. Since full CANN `plog` directories (`$ASCEND_PROCESS_LOG_PATH` or `~/ascend/log/debug/plog`) "
        "and dynamic ACLGraph/precision runtime hooks reside inside the remote NPU container, call `['remote-guide']` "
        "(or `['triage']` / `['aclgraph', ...]` / `['precision', ...]`) to stage the pure-Python `mindie_debugger` "
        "bundle locally and get the exact `remote-dev` (`remote_artifact_push` + `remote_bash`) commands to triage "
        "CANN `plog`s in-place (`python3 -m mindie_debugger.cli triage`) or run `ACLGraphCaptureGuard`, "
        "`ACLGraphAddressTracker`, `ACLGraphDumper`, `ACLGraphVllmInspector`, and `precision` inside the NPU container."
    ),
}


def _path(description):
    return dict(type="string", minLength=1, maxLength=MAX_STRING, pattern=NO_NUL_NONEMPTY,
                description=description + " 1-%d characters, no NUL." % MAX_STRING)


def catalog():
    """The public catalog contains only the deterministic Agent capabilities."""
    limits = {
        "timeout_ms": dict(type="integer", minimum=TIMEOUT_MS[0], maximum=TIMEOUT_MS[1], default=TIMEOUT_MS[2],
                           description="Execution deadline; include the component's inner budgets and cleanup grace."),
        "max_output_tokens": dict(type="integer", minimum=OUTPUT_TOKENS[0], maximum=OUTPUT_TOKENS[1], default=OUTPUT_TOKENS[2],
                                  description="Preview budget in tokens (four bytes each) shared by stdout and stderr. Complete output up to 16 MiB is spooled locally and returned as a receipt (path, bytes, sha256); use --output for larger reports."),
    }
    result = []
    for name, description in DESCRIPTIONS.items():
        result.append(dict(
            name=name, description=description,
            inputSchema=dict(type="object", additionalProperties=False, required=["argv"],
                             properties=dict(argv=dict(type="array", maxItems=MAX_ARGV,
                                                       items=dict(type="string", maxLength=MAX_ARGV_ITEM, pattern=NO_NUL),
                                                       description="Arguments for the installed CLI; no shell expansion. At most %d items of %d characters, no NUL." % (MAX_ARGV, MAX_ARGV_ITEM)),
                                             **limits)),
            annotations=dict(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
        ))
    for name, description, properties, required in (
        ("diagnostics_status", "Inspect local MindIE diagnostic/reporting status without starting a worker or contacting GitHub.",
         dict(config=_path("Optional existing diagnostics policy path.")), []),
        ("diagnostics_bundle", "Export a bounded, sanitized local diagnostic support bundle. Does not upload it.",
         dict(root=_path("Optional local diagnostics log root; defaults to the active MindIE diagnostics root."),
              operation_id=_path("Operation identifier."),
              incident_id=_path("Incident identifier alias for operation_id."),
              output=_path("Local output file.")), []),
    ):
        result.append(dict(name=name, description=description,
                           inputSchema=dict(type="object", additionalProperties=False,
                                            properties=dict(properties, **limits), required=required),
                           annotations=dict(readOnlyHint=name == "diagnostics_status",
                                            destructiveHint=False, openWorldHint=False)))
    return result


def command(adapter, component):
    configured = adapter.get("component_commands", {}).get(component)
    if configured is not None:
        if (not isinstance(configured, list) or not configured
                or not all(isinstance(value, str) and value for value in configured)):
            raise ValueError("component_commands must contain nonempty executable argv arrays")
        return configured
    if component == "diagnostics":
        return [adapter["python"], "-I", "-X", "utf8", "-m", "mindie_diagnostics.cli"]
    name = "mindie-" + component
    runtime_bins = [str(Path(adapter["python"]).parent)]
    if Path(adapter["python"]) == Path(sys.executable):
        runtime_bins.append(sysconfig.get_path("scripts"))
    executable = shutil.which(name, path=os.pathsep.join(runtime_bins)) or shutil.which(name)
    if executable is None:
        raise FileNotFoundError(
            "mindie-" + component + " is not installed on PATH; install the component "
            "or configure component_commands." + component + " in the adapter")
    return [executable]


RECEIPT_DIRECTORY = "component-captures"


def _reject(field, reason):
    from mcp_gate import InvalidToolArguments
    return InvalidToolArguments("component argument rejected: " + reason, field=field)


def _integer(arguments, field, low, high, default):
    value = arguments.get(field, default)
    if type(value) is not int or not low <= value <= high:
        raise _reject(field, field + " must be an integer between %d and %d" % (low, high))
    return value


def validate(name, arguments):
    """Check types, bounds and required keys before anything can spawn."""
    tool = next((item for item in catalog() if item["name"] == name), None)
    if tool is None:
        raise _reject(None, "unknown tool")
    if not isinstance(arguments, dict):
        raise _reject(None, "arguments must be an object")
    schema = tool["inputSchema"]
    for key in arguments:
        if not isinstance(key, str) or key not in schema["properties"]:
            raise _reject(None, "unknown argument")
    for key in schema["required"]:
        if key not in arguments:
            raise _reject(key, "missing required argument " + key)
    if name in DESCRIPTIONS:
        argv = arguments.get("argv")
        if (not isinstance(argv, list) or len(argv) > MAX_ARGV
                or not all(isinstance(item, str) and len(item) <= MAX_ARGV_ITEM and "\0" not in item
                           for item in argv)):
            raise _reject("argv", "argv must be an array of at most %d strings without NUL" % MAX_ARGV)
    for key in PATH_FIELDS:
        if key in arguments:
            value = arguments[key]
            if (not isinstance(value, str) or not value or len(value) > MAX_STRING
                    or "\0" in value):
                raise _reject(key, key + " must be a nonempty string without NUL")
    _integer(arguments, "max_output_tokens", *OUTPUT_TOKENS[:2], OUTPUT_TOKENS[2])
    _integer(arguments, "timeout_ms", *TIMEOUT_MS[:2], TIMEOUT_MS[2])


def _default_diagnostics_root() -> str:
    explicit = os.environ.get("MINDIE_DIAGNOSTICS_ROOT")
    if explicit:
        return str(Path(explicit).expanduser())
    if os.name == "nt" and not os.environ.get("XDG_STATE_HOME"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return str(Path(local) / "mindie" / "diagnostics")
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return str(Path(xdg) / "mindie" / "diagnostics")
    return str(Path.home() / ".local" / "state" / "mindie" / "diagnostics")


def _build(name, arguments):
    if name in DESCRIPTIONS:
        return name, list(arguments["argv"])
    if name == "diagnostics_status":
        argv = ["reporting", "status"]
        if arguments.get("config") is not None:
            argv.append("--config=" + arguments["config"])
        return "diagnostics", argv
    root_val = arguments["root"] if arguments.get("root") is not None else _default_diagnostics_root()
    argv = ["bundle", "--root=" + root_val]
    op_id = arguments.get("operation_id") if arguments.get("operation_id") is not None else arguments.get("incident_id")
    if op_id is not None:
        argv.append("--operation-id=" + op_id)
    if arguments.get("output") is not None:
        argv.append("--output=" + arguments["output"])
    return "diagnostics", argv


def _value(component, output):
    value = dict(component=component, exit_code=output.returncode,
                 stdout=output.stdout, stderr=output.stderr)
    if output.capture is not None:
        value["capture"] = output.capture
    if output.cleanup:
        value["cleanup_warning"] = [dict(stage=str(item.get("stage")), error_type=str(item.get("error_type")))
                                    for item in output.cleanup if isinstance(item, dict)]
    return value


def execute(name, arguments, session, *, cancel=None, timeout=None):
    validate(name, arguments)
    adapter = json.loads(config_path().read_text(encoding="utf-8"))
    component, argv = _build(name, arguments)
    budget = arguments.get("max_output_tokens", 16384)
    bound = arguments.get("timeout_ms", 120000) / 1000
    if timeout is not None:
        bound = min(bound, timeout)
    environment = runtime_env(config_path(), adapter=adapter)
    environment["CODEX_THREAD_ID"] = session
    invocation_token = os.urandom(16).hex() if name in DESCRIPTIONS else None
    if invocation_token is not None:
        environment["MINDIE_DIAGNOSTICS_INVOCATION_TOKEN"] = invocation_token
    try:
        executable = command(adapter, component)
    except FileNotFoundError as exc:
        exc.process_result = ProcessResult("not_started")
        exc.install_hint = True
        raise
    capture = Capture(config_path().parent / RECEIPT_DIRECTORY, budget * 4)
    try:
        output = run(executable + argv, "", timeout=bound,
                     max_output=budget * 4, cancel=cancel, env=environment,
                     allowed_returncodes=None, capture=capture)
    except BaseException as exc:
        capture.close()
        process = getattr(exc, "process_result", None)
        if (process is not None and process.capture is not None
                and process.execution != "not_started" and isinstance(exc, Exception)):
            exc.partial = dict(component=component, exit_code=process.returncode,
                               stdout=process.stdout, stderr=process.stderr,
                               capture=process.capture)
        raise
    value = _value(component, output)
    stream = output.capture["stdout"] if output.capture else {}
    if not stream.get("preview_truncated"):
        try:
            value["result"] = json.loads(output.stdout)
        except json.JSONDecodeError:
            pass
    if name in DESCRIPTIONS and output.returncode not in (0, 2):
        import diagnostic_support
        diag = diagnostic_support.record_component_crash(
            name, argv, output.returncode, output.stderr,
            parsed_result=value.get("result"), executable=executable[0],
            invocation_token=invocation_token,
        )
        if diag is not None:
            value["diagnostic"] = diag
    return dict(content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))],
                structuredContent=value, isError=output.returncode != 0)
