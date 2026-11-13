"""Build native launchers for MCP and lifecycle hooks.

[Role]
Generates `.mcp.json` and `hooks/hooks.json` definitions for SessionStart,
UserPromptSubmit, Stop, Interrupt and the MCP servers.

[Invariants]
- Hook commands use a version-independent facade reading `~/.config/mindie-agent/codex.json` so running app-servers remain valid across plugin cache updates.
- The remote-dev MCP launcher carries the literal `REMOTE_DEV_HARNESS_PROFILE=codex` env value; the native profile is never read from tool arguments.
- `Stop` and every hook remain stable canonical facades and never start migration; only MCP startup may route to the newer seed controller.
- `Interrupt` dispatches `mindie_interrupt` with exact `thread_id`/`turn_id` to the remote and deterministic-component MCP servers (`timeout=3s`).
- Hook and MCP commands invoke `scripts/hook_launcher.py` rather than inlining the selector state machine as `python -c`.
- After plugin-cache eviction, hooks resolve `$PLUGIN_DATA/hook_launcher.py` (published with the release) when `$PLUGIN_ROOT/scripts/hook_launcher.py` is gone. POSIX commands read those Codex env vars at shell runtime (`"$PLUGIN_ROOT"`), not the `${PLUGIN_ROOT}` manifest token Codex rewrites in `hooks/list`.

- Knowledge is private to the manual Skill; its server is absent from the default MCP manifest.

[Timeouts & Locks]
- MCP `startup_timeout_sec=2760` covers baseline preparation, component inheritance, the bounded 900s migration/lock-drain window, and cleanup without altering tool-call deadlines.
- SessionStart, UserPromptSubmit and Stop each declare an outer hook timeout covering inner budgets, first-use profile setup where that path can run, `uv` startup and cleanup.

[Side-Effect Policy]
- Importing this module is side-effect free; `write_manifests(root)` updates `hooks/hooks.json` and `.mcp.json`.
"""
import base64
import json
from pathlib import Path
import shlex


# 900 baseline runtime + 830 component inheritance + 900 migration (720 max
# native call drain + 15 capture + 90 domain preparation + 30 activation + 45
# reserve) + 130 process/cleanup reserve.
MCP_STARTUP_SECONDS = 2760

# Inner budgets owned by bootstrap.py / entry.py / setup.py.
BASELINE_RUNTIME_SECONDS = 900
# First-use profile(..., prepare=True) -> setup.install: scanner 15s,
# store 30s, domain catalog 90s, reporter activation 90s.
PROFILE_SETUP_SECONDS = 15 + 30 + 90 + 90
SESSION_MAINTENANCE_SECONDS = 300
PROMPT_FORWARD_SECONDS = 420  # 300s maintenance + admission/entry + cleanup
STOP_DISPATCH_SECONDS = 120
UV_STARTUP_SECONDS = 60
CLEANUP_GRACE_SECONDS = 60

# outer >= sum(inner) + uv_startup + cleanup_grace
SESSION_START_TIMEOUT = (
    BASELINE_RUNTIME_SECONDS + PROFILE_SETUP_SECONDS + SESSION_MAINTENANCE_SECONDS
    + UV_STARTUP_SECONDS + CLEANUP_GRACE_SECONDS)
USER_PROMPT_TIMEOUT = (
    BASELINE_RUNTIME_SECONDS + PROFILE_SETUP_SECONDS + PROMPT_FORWARD_SECONDS
    + UV_STARTUP_SECONDS + CLEANUP_GRACE_SECONDS)
STOP_TIMEOUT = (
    STOP_DISPATCH_SECONDS + UV_STARTUP_SECONDS + CLEANUP_GRACE_SECONDS)

PLUGIN_LAUNCHER = "$_hl"
# Headless Windows PowerShell 5.1 (`CREATE_NO_WINDOW`) has no console handle and
# throws `IOException: The handle is invalid` on `[Console]::OutputEncoding = ...`
# unless guarded. `$OutputEncoding` and `PYTHONIOENCODING` still govern pipes.
PS_PROTOCOL_ENCODING = (
    "$ProgressPreference = 'SilentlyContinue'; "
    "$ErrorActionPreference = 'SilentlyContinue'; "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "try { [Console]::OutputEncoding = $OutputEncoding; [Console]::InputEncoding = $OutputEncoding } catch {}; "
    "$env:PYTHONIOENCODING = 'utf-8'; "
)
# Use "$PLUGIN_ROOT" / "$PLUGIN_DATA" (shell env) rather than ${PLUGIN_ROOT}.
# Codex substitutes the braced manifest token when listing hooks, so a literal
# ${PLUGIN_ROOT} never matches hooks.json and breaks trust identity checks.
POSIX_LAUNCHER_RESOLVE = (
    '_hl="$PLUGIN_ROOT/scripts/hook_launcher.py"; '
    'if [ ! -f "$_hl" ]; then _hl="$PLUGIN_DATA/hook_launcher.py"; fi; ')
PS_LAUNCHER_RESOLVE = (
    "$root = [string]$env:PLUGIN_ROOT; $hl = ''; "
    "if ($root) { $hl = Join-Path $root 'scripts/hook_launcher.py' }; "
    "if (-not $hl -or -not (Test-Path -LiteralPath $hl)) { "
    "$hl = Join-Path $env:PLUGIN_DATA 'hook_launcher.py' }; ")
# Single-line, double-quoted Python so shlex.quote wraps it in one pair of
# single quotes. Newlines inside `python3 -c` are rewritten by hooks/list.
_FAILURE_ENCODER = (
    "import json,pathlib,sys;"
    "st,path,prefix=sys.argv[1],sys.argv[2],sys.argv[3];"
    "err=pathlib.Path(path).read_text(encoding=\"utf-8\",errors=\"replace\")[:800].strip();"
    "msg=prefix+\" (exit \"+st+\")\";"
    "print(json.dumps({\"systemMessage\":(msg+chr(10)+err) if err else msg}))"
)


def posix_arg(value):
    if value == "$_hl":
        return '"$_hl"'
    if value.startswith("${PLUGIN_ROOT}/"):
        return '"${PLUGIN_ROOT}"' + shlex.quote(value[len("${PLUGIN_ROOT}"):])
    return shlex.quote(value)


def ps_arg(value):
    if value == "$_hl":
        return "$hl"
    if value.startswith("${PLUGIN_ROOT}/"):
        return "(Join-Path $env:PLUGIN_ROOT '" + value[len("${PLUGIN_ROOT}/"):].replace("'", "''") + "')"
    return "'" + value.replace("'", "''") + "'"


def _offline_args(role):
    if role in {"stop", "user-prompt-submit"}:
        return ["--offline", "--no-python-downloads"]
    return []


def _uv_python(role):
    return ["uv", "--native-tls", "run", "--no-project", *_offline_args(role),
            "--python", "3.13", "python", "-B"]


def _launcher_flags(*, name="mindie-agent", marketplace="mindie-agent", seed_version=None):
    flags = []
    if name != "mindie-agent":
        flags.extend(["--name", name])
    if marketplace != "mindie-agent":
        flags.extend(["--marketplace", marketplace])
    if seed_version is not None:
        flags.extend(["--seed", seed_version])
    return flags


def mcp_locator_code(*, name="mindie-agent", marketplace="mindie-agent", seed_version):
    """Locate `hook_launcher.py` without inlining the selector state machine.

    Codex expands `${PLUGIN_ROOT}` in hook commands but not in `.mcp.json`
    args, so MCP keeps this short path finder and falls back to the durable
    published release copy after plugin-cache eviction. Hook commands avoid
    that manifest token and read the `PLUGIN_ROOT` environment variable.
    """
    return (
        "import os,runpy,sys,json\nfrom pathlib import Path\n"
        "def load(p):\n"
        " with p.open('rb') as f:b=f.read(65537)\n"
        " if len(b)>65536:raise ValueError('MindIE metadata exceeds its byte bound')\n"
        " v=json.loads(b)\n"
        " if not isinstance(v,dict):raise ValueError('MindIE metadata must be an object')\n"
        " return v\n"
        "home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))\n"
        f"data=Path(os.environ.get('PLUGIN_DATA',str(home/'plugins/data'/{(name + '-' + marketplace)!r})))\n"
        "config=Path(os.environ.get('MINDIE_AGENT_CONFIG',str(Path.home()/'.config/mindie-agent/codex.json'))).expanduser().absolute()\n"
        f"seed={seed_version!r}\n"
        "cands=[]\n"
        "root=os.environ.get('PLUGIN_ROOT')\n"
        "if root:cands.append(Path(root)/'scripts'/'hook_launcher.py')\n"
        "cands.append(data/'hook_launcher.py')\n"
        "if seed:cands.append(data/'releases'/seed/'hook_launcher.py')\n"
        "if config.is_file():\n"
        " a=load(config);v=a.get('product_validation',{}).get('native_version')\n"
        " if a.get('component_release'):cands.append(Path(a['component_release'])/'hook_launcher.py')\n"
        " elif isinstance(v,str):cands.append(data/'releases'/v/'hook_launcher.py')\n"
        f"cands.append(Path(os.environ.get('PLUGIN_ROOT',str(home/'plugins/cache'/{marketplace!r}/{name!r}/(seed or 'unprepared'))))/'scripts'/'hook_launcher.py')\n"
        "p=next((c for c in cands if c.is_file()),None)\n"
        "if p is None:raise RuntimeError('MindIE launcher is unavailable')\n"
        "sys.argv=[str(p),*sys.argv[1:]]\n"
        "runpy.run_path(str(p),run_name='__main__')"
    )


def command_args(version, role, **names):
    return [*_uv_python(role), "-c", mcp_locator_code(seed_version=version, **names),
            *_launcher_flags(seed_version=version, **names), role]


def _failure_json_parts(failure):
    encoded = json.dumps({"systemMessage": failure + " (exit PLACEHOLDER)"})
    left, right = encoded.split("PLACEHOLDER")
    return left, right


def _posix_failure_json(failure):
    """Stdout JSON must carry helper stderr: Codex drops stderr when Stop exits 0."""
    left, right = _failure_json_parts(failure)
    fallback = "printf '%s\\n' " + shlex.quote(left) + '"$_st"' + shlex.quote(right)
    return (
        "python3 -c " + shlex.quote(_FAILURE_ENCODER) + " \"$_st\" \"$_err\" "
        + shlex.quote(failure) + " || " + fallback
    )


def _ps_failure_json(failure):
    prefix = failure.replace("'", "''")
    return (
        "$detail = ((Get-Content -LiteralPath $errFile -Raw -ErrorAction SilentlyContinue) + '').Trim(); "
        "$msg = '" + prefix + " (exit ' + $code + ')'; "
        "if ($detail) { $msg = $msg + [char]10 + $detail }; "
        "[Console]::Out.WriteLine((@{systemMessage=$msg} | ConvertTo-Json -Compress))"
    )


def neutral_hook_commands(argv, *, failure="MindIE Stop helper failed; capture completion is unconfirmed; no automatic retry.",
                          keep_stderr=False, pass_exit=False):
    """Keep Stop protocol-neutral while exposing helper failures.

    Codex treats Stop exit code 2 as a request to continue the conversation,
    and Python uses 2 for missing scripts, so Stop itself stays `exit 0`.
    Helper stderr is copied into the host JSON (and replayed) on failure so
    the detail remains visible after that protocol exit. SessionStart may
    pass the helper exit through.
    """
    argv = [str(value) for value in argv]
    resolve = POSIX_LAUNCHER_RESOLVE if "$_hl" in argv else ""
    resolve_ps = PS_LAUNCHER_RESOLVE if "$_hl" in argv else ""
    left, right = _failure_json_parts(failure)
    posix_json = (
        "printf '%s\\n' " + shlex.quote(left) + '"$_st"' + shlex.quote(right)
    )
    posix_cmd = " ".join(posix_arg(arg) for arg in argv)
    warning = ("printf '%s\\n' " + shlex.quote(failure) + " >&2; ") if keep_stderr else ""
    exit_stmt = "exit \"$_st\"; " if pass_exit else ""
    if keep_stderr:
        posix = (
            resolve + posix_cmd + " >/dev/null; _st=$?; "
            + "if [ \"$_st\" -ne 0 ]; then " + warning + posix_json + "; " + exit_stmt
            + "else printf '{}\\n'; fi"
        )
    else:
        posix = (
            resolve + "_err=$(mktemp) || exit 1; " + posix_cmd + " >/dev/null 2>\"$_err\"; _st=$?; "
            + "if [ \"$_st\" -ne 0 ] || [ -s \"$_err\" ]; then cat \"$_err\" >&2; "
            + _posix_failure_json(failure) + "; rm -f \"$_err\"; " + exit_stmt
            + "else rm -f \"$_err\"; printf '{}\\n'; fi"
        )

    warning_ps = ("[Console]::Error.WriteLine(" + ps_arg(failure) + "); " if keep_stderr else "")
    exit_ps = "exit $code" if pass_exit else "exit 0"
    invoke = " ".join(ps_arg(arg) for arg in argv)
    if keep_stderr:
        body = (
            resolve_ps
            + "$ProgressPreference = 'SilentlyContinue'; $failed = $false; $code = 0; try { & "
            + invoke + " 1>$null; $code = $LASTEXITCODE; $failed = ($code -ne 0) } catch { $failed = $true; $code = 1 }; "
            + "if ($failed) { " + warning_ps
            + "[Console]::Out.WriteLine('"
            + left.replace("'", "''") + "' + $code + '"
            + right.replace("'", "''") + "')"
            + " } else { [Console]::Out.WriteLine('{}') }; " + exit_ps
        )
    else:
        body = (
            resolve_ps
            + "$ProgressPreference = 'SilentlyContinue'; $failed = $false; $code = 0; "
            "$errFile = [System.IO.Path]::GetTempFileName(); try { & "
            + invoke + " 1>$null 2>$errFile; $code = $LASTEXITCODE; $failed = ($code -ne 0) } catch { $failed = $true; $code = 1 }; "
            + "$hasErr = ((Get-Item -LiteralPath $errFile -ErrorAction SilentlyContinue).Length -gt 0); "
            + "if ($failed -or $hasErr) { Get-Content -LiteralPath $errFile -ErrorAction SilentlyContinue | "
            + "ForEach-Object { [Console]::Error.WriteLine($_) }; " + warning_ps + _ps_failure_json(failure)
            + " } else { [Console]::Out.WriteLine('{}') }; "
            + "Remove-Item -LiteralPath $errFile -ErrorAction SilentlyContinue; " + exit_ps
        )
    body = PS_PROTOCOL_ENCODING + body
    windows = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(
        body.encode("utf-16le")).decode("ascii")
    return {"command": posix, "commandWindows": windows, "statusMessage": "MindIE Agent"}


def hook_commands(role, **names):
    arguments = [*_uv_python(role), PLUGIN_LAUNCHER, *_launcher_flags(**names), role]
    if role == "user-prompt-submit":
        # Preserve the hook's one JSON response: only an explicit entry may
        # attach knowledge context, and ordinary prompts emit {}. If the outer
        # uv/launcher command crashes before Python can emit JSON, return a UI
        # systemMessage so the failure is visible instead of silently dropped.
        left, right = _failure_json_parts(
            "MindIE UserPromptSubmit launcher failed; inspect local diagnostics and do not repeat /mindie-agent in a loop.")
        posix_cmd = " ".join(posix_arg(arg) for arg in arguments)
        posix = (
            POSIX_LAUNCHER_RESOLVE + posix_cmd + "; _st=$?; "
            + "if [ \"$_st\" -ne 0 ]; then printf '%s\\n' "
            + shlex.quote(left) + '"$_st"' + shlex.quote(right)
            + "; fi; exit 0"
        )
        body = (
            PS_PROTOCOL_ENCODING + PS_LAUNCHER_RESOLVE
            + "$failed = $false; $code = 0; try { & "
            + " ".join(ps_arg(arg) for arg in arguments)
            + "; $code = $LASTEXITCODE; $failed = ($code -ne 0) } catch { $failed = $true; $code = 1 }; "
            + "if ($failed) { [Console]::Out.WriteLine('"
            + left.replace("'", "''") + "' + $code + '" + right.replace("'", "''")
            + "') }; exit 0"
        )
        return dict(command=posix,
                    commandWindows="powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "
                    + base64.b64encode(body.encode("utf-16le")).decode("ascii"),
                    # Cold baseline (900s) + first-use profile setup (225s) +
                    # explicit-entry forward (420s) + uv startup and owned cleanup.
                    timeout=USER_PROMPT_TIMEOUT, statusMessage="MindIE Agent")
    if role == "stop":
        return dict(neutral_hook_commands(arguments, keep_stderr=False, pass_exit=False),
                    timeout=STOP_TIMEOUT)
    return dict(neutral_hook_commands(
        arguments, keep_stderr=True, pass_exit=True,
        failure="MindIE session preparation failed; inspect the reported stage and local diagnostics; no automatic retry."),
        timeout=SESSION_START_TIMEOUT)


def write_manifests(root):
    from native_context import TRANSPORT_ENV
    root = Path(root)
    version = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
    hooks = {name: [{"hooks": [{"type": "command", "statusMessage": "MindIE Agent",
                               **hook_commands(role)}]}]
             for name, role in (("SessionStart", "session-start"),
                                ("UserPromptSubmit", "user-prompt-submit"), ("Stop", "stop"))}
    hooks["Interrupt"] = [{"hooks": [
        dict(type="mcp_tool", server=server, tool="mindie_interrupt",
             input=dict(thread_id="${session_id}", turn_id="${turn_id}"),
             timeout=3, statusMessage="MindIE cancellation")
        for server in ("mindie-remote-dev", "mindie-tools")
    ]}]
    (root / "hooks/hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2) + "\n",
                                         encoding='utf-8', newline='\n')
    servers = {}
    for name, role in (("mindie-remote-dev", "remote-mcp"),
                       ("mindie-tools", "tools-mcp")):
        arguments = command_args(version, role)
        server = dict(command=arguments[0], args=arguments[1:],
                      # Covers baseline preparation, component inheritance,
                      # migration drain/activation, and owned cleanup; it
                      # does not change business tool-call deadlines.
                      startup_timeout_sec=MCP_STARTUP_SECONDS,
                      env_vars=["CODEX_HOME", "MINDIE_AGENT_CONFIG",
                                "MINDIE_CODEX_BIN", "MINDIE_DIAGNOSTICS_CONFIG",
                                "MINDIE_DIAGNOSTICS_ROOT", *TRANSPORT_ENV])
        if name == "mindie-remote-dev":
            # Codex 0.153.4 plugin `.mcp.json` accepts a literal `env` object
            # (the same RawMcpServerConfig used by `codex mcp add --env`). This
            # is a trusted launcher value, not an `env_vars` pass-through.
            server["env"] = {"REMOTE_DEV_HARNESS_PROFILE": "codex"}
            # 600s native Bash poll + 60s helper grace + bounded cleanup.
            server["tool_timeout_sec"] = 720
        if role == "tools-mcp":
            # 300s component + 30s selected-helper envelope + native drain.
            server["tool_timeout_sec"] = 360
        servers[name] = server

    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n",
                                 encoding='utf-8', newline='\n')


if __name__ == "__main__":
    write_manifests(Path(__file__).resolve().parents[1])
