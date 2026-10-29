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
import shutil

from bounded_process import ProcessResult, run
from capture_spool import Capture
from session_gate import config_path, runtime_env


PATH_FIELDS = ("root", "config", "operation_id", "output")
MAX_STRING = 4096
MAX_ARGV = 256
MAX_ARGV_ITEM = 65536
NO_NUL = "^[^\\u0000]*$"
NO_NUL_NONEMPTY = "^[^\\u0000]+$"
TIMEOUT_MS = (1, 300000, 120000)
OUTPUT_TOKENS = (1, 262144, 16384)


DESCRIPTIONS = {
    "profiler": (
        "Inspect and analyze Ascend profiling captures using mindie-profiler. "
        "Pass CLI arguments as an argv array, e.g. ['inventory', '--root', '/capture'] "
        "or ['analyze', '--root', '/capture', '--full-range']. Commands: inventory, analyze, correlate, "
        "compare and sql. ['-h'] or ['--help'] enumerates the installed commands "
        "and a subcommand's --help documents its options; an unknown first "
        "non-option word exits 2 listing the valid choices, and legacy "
        "option-first analyze arguments are still accepted. Use --output for "
        "large reports. Offline parsing needs no NPU lease; device capture still "
        "requires one. Profiler exports need explicit identity and boundary "
        "mapping before sim consumption; they are not automatically a sim Case "
        "or calibration input."
    ),
    "sim": (
        "Model hardware-neutral operators, pure graphs/models and serving constraints "
        "using mindie-sim and explicit input files. Pass CLI arguments as an argv "
        "array, e.g. ['graph', '/case.json', '--phase', 'prefill', '--tokens', '32', "
        "'--scope', 'model', '--output', '/report.json']. Use ['--help'] for the "
        "installed parser; operators, graph and predict have distinct boundaries. "
        "Study enumerates declared candidates; accuracy compares independent "
        "matched observations. A partial study keeps its report with a nonzero exit. "
        "Output-file calls may have empty stdout; the exit code and stderr remain "
        "available. Predictions do not establish measured hardware accuracy. "
        "Check CPU/RAM needs before large runs and use remote-dev "
        "on a suitable host when local resources are insufficient."
    ),
    "coordinator": (
        "Manage Ascend host inventory, probe CPU/RAM/NPU capacity, bind containers "
        "and acquire/inspect/release NPU and service-port leases through "
        "mindie-coordinator. Pass CLI argv, e.g. ['hosts', '--action', 'list'] "
        "or ['probe', '--json', '{\"action\":\"hosts\"}']. Use ['--help'] "
        "for the installed parser. Preserve lease IDs and uncertain external "
        "outcomes; never resubmit an uncertain mutation."
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
         dict(root=_path("Local diagnostics log root."),
              operation_id=_path("Operation identifier."), output=_path("Local output file.")), ["root"]),
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
    executable = shutil.which("mindie-" + component)
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


def _build(name, arguments):
    if name in DESCRIPTIONS:
        return name, list(arguments["argv"])
    if name == "diagnostics_status":
        argv = ["reporting", "status"]
        if arguments.get("config") is not None:
            argv.append("--config=" + arguments["config"])
        return "diagnostics", argv
    argv = ["bundle", "--root=" + arguments["root"]]
    for key in ("operation_id", "output"):
        if arguments.get(key) is not None:
            argv.append("--" + key.replace("_", "-") + "=" + arguments[key])
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
    return dict(content=[dict(type="text", text=json.dumps(value, ensure_ascii=False))],
                structuredContent=value, isError=output.returncode != 0)
