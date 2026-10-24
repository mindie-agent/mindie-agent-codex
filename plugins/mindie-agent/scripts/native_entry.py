"""Build native launchers for MCP and lifecycle hooks.

[Role]
Generates `.mcp.json` and `hooks/hooks.json` definitions for SessionStart,
UserPromptSubmit, Stop, Interrupt and the MCP servers.

[Invariants]
- Hook commands use a version-independent facade reading `~/.config/mindie-agent/codex.json` so running app-servers remain valid across plugin cache updates.
- The remote-dev MCP launcher carries the literal `REMOTE_DEV_HARNESS_PROFILE=codex` env value; the native profile is never read from tool arguments.
- `Stop` never bootstraps an unprepared runtime and always returns neutral `{}` stdout to the host.
- `Interrupt` dispatches `mindie_interrupt` with exact `thread_id`/`turn_id` to the remote and deterministic-component MCP servers (`timeout=3s`).

- Knowledge is private to the manual Skill; its server is absent from the default MCP manifest.

[Timeouts & Locks]
- MCP `startup_timeout_sec=1080` covers first-use `uv` sync and shared preparation lock without altering tool-call deadlines.

[Side-Effect Policy]
- Importing this module is side-effect free; `write_manifests(root)` updates `hooks/hooks.json` and `.mcp.json`.
"""
import base64
import json
from pathlib import Path
import shlex


def launcher_code(version, *, name="mindie-agent", marketplace="mindie-agent"):
    return hook_launcher_code(name=name, marketplace=marketplace, seed_version=version)


def command_args(version, role, **names):
    return ["uv", "--native-tls", "run", "--no-project",
            *(["--offline", "--no-python-downloads"] if role == "stop" else []),
            "--python", "3.13", "python", "-B",
            "-c", launcher_code(version, **names), role]


def hook_launcher_code(*, name="mindie-agent", marketplace="mindie-agent", seed_version=None):
    """Select only the prepared entry named by the canonical installed binding."""
    return (
        "import os,sys,json,re,runpy\nfrom pathlib import Path\n"
        "def read(p):\n"
        " if not p.is_file():raise ValueError('MindIE metadata must be regular')\n"
        " with p.open('rb') as f:\n"
        "  raw=f.read(65537)\n"
        " if len(raw)>65536:raise ValueError('MindIE metadata exceeds its byte bound')\n"
        " value=json.loads(raw)\n"
        " if not isinstance(value,dict):raise ValueError('MindIE metadata must be an object')\n"
        " return value\n"
        "home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))\n"
        f"data=Path(os.environ.get('PLUGIN_DATA',str(home/'plugins/data'/{(name + '-' + marketplace)!r})))\n"
        "config=Path(os.environ.get('MINDIE_AGENT_CONFIG',str(Path.home()/'.config/mindie-agent/codex.json'))).expanduser().absolute()\n"
        "args=sys.argv[1:]\n"
        "if config.exists():\n"
        " adapter=read(config);validation=adapter['product_validation'];version=validation['native_version']\n"
        " if not isinstance(version,str) or not re.fullmatch('[0-9A-Za-z][0-9A-Za-z.+!-]{0,79}',version):raise ValueError('MindIE release identity is invalid')\n"
        " release=Path(adapter['component_release']) if adapter.get('component_release') else data/'releases'/version\n"
        " receipt=read(release/'runtime.json');entry=release/'entry.py'\n"
        " if receipt['python']!=adapter['python'] or receipt['validation']!=validation or validation.get('status')!='validated' or validation.get('schema')!='mindie-candidate-check/1':raise ValueError('MindIE installed runtime receipt differs')\n"
        "else:\n"
        f" seed={seed_version!r};fixed=data/'releases'/seed/'entry.py' if seed else None\n"
        " if fixed is not None and fixed.is_file():entry=fixed\n"
        " else:\n"
        "  if args[:1]==['stop']:raise RuntimeError('MindIE runtime is not prepared; start a new native session')\n"
        f"  source=Path(os.environ.get('PLUGIN_ROOT',str(home/'plugins/cache'/{marketplace!r}/{name!r}/(seed or 'unprepared'))))\n"
        "  entry=source/'scripts/bootstrap.py';args=[str(data),'--source',str(source),*args]\n"
        "if not entry.is_file():raise RuntimeError('MindIE installed entry is unavailable')\n"
        "sys.path.insert(0,str(entry.parent));sys.argv=[str(entry),*args]\n"
        "runpy.run_path(str(entry),run_name='__main__')"
    )


def neutral_hook_commands(argv, *, failure="MindIE Stop helper failed; capture completion is unconfirmed; no automatic retry.",
                          keep_stderr=False):
    """Keep Stop neutral while exposing failures before diagnostics can start.

    Codex accepts a Windows-specific command override. The POSIX command and
    its Windows counterpart discard helper protocol output, but expose a fixed
    Stop warning through the native hook JSON when the helper fails. Missing executables or scripts cannot
    leave an incident in the Python diagnostic channel.
    """
    argv = [str(value) for value in argv]
    def posix_arg(value):
        if value.startswith("${PLUGIN_ROOT}/"):
            return '"${PLUGIN_ROOT}"' + shlex.quote(value[len('${PLUGIN_ROOT}'):])
        return shlex.quote(value)
    failure_output = json.dumps({"systemMessage": failure})
    posix = ("if ! " + " ".join(posix_arg(arg) for arg in argv)
             + (" >/dev/null" if keep_stderr else " >/dev/null 2>&1")
             + "; then "
             + ("printf '%s\\n' " + shlex.quote(failure) + " >&2; " if keep_stderr else "")
             + "printf '%s\\n' " + shlex.quote(failure_output)
             + "; else printf '{}\\n'; fi")
    # Native Codex can dispatch Windows hooks through PowerShell. CMD's
    # `& echo` becomes a background job there and loses the event on stdin.
    # An encoded PowerShell command has one unambiguous argv under either
    # host shell; the child inherits stdin and every exit remains neutral.
    def ps_arg(value):
        if value.startswith("${PLUGIN_ROOT}/"):
            return "(Join-Path $env:PLUGIN_ROOT '" + value[len('${PLUGIN_ROOT}/'):].replace("'", "''") + "')"
        return "'" + value.replace("'", "''") + "'"
    warning = ("[Console]::Error.WriteLine(" + ps_arg(failure) + "); " if keep_stderr else "")
    # Join-Path may auto-load its module and serialize first-use progress to
    # stderr. Progress is not a helper failure; keep the actual error channel.
    body = ("$ProgressPreference = 'SilentlyContinue'; $failed = $false; try { & "
            + " ".join(ps_arg(arg) for arg in argv)
            + (" 1>$null" if keep_stderr else " 1>$null 2>$null")
            + "; $failed = ($LASTEXITCODE -ne 0) } catch { $failed = $true }; "
            + "if ($failed) { " + warning + "[Console]::Out.WriteLine(" + ps_arg(failure_output)
            + ") } else { [Console]::Out.WriteLine('{}') }; exit 0")

    windows = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(body.encode("utf-16le")).decode("ascii")
    return {"command": posix, "commandWindows": windows, "statusMessage": "MindIE Agent"}


def hook_commands(role, **names):
    arguments = ["uv", "--native-tls", "run", "--no-project",
                 *(["--offline", "--no-python-downloads"] if role == "stop" else []),
                 "--python", "3.13", "python", "-B", "-c", hook_launcher_code(**names), role]
    if role == "user-prompt-submit":
        # Preserve the hook's one JSON response: only an explicit entry may
        # attach knowledge context. Ordinary prompts emit exactly {}.
        body = "& " + " ".join("'" + str(arg).replace("'", "''") + "'" for arg in arguments)
        body += "; exit $LASTEXITCODE"
        return dict(command=shlex.join(arguments), commandWindows=
                    "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "
                    + base64.b64encode(body.encode("utf-16le")).decode("ascii"),
                    # Cold preparation (540s), maintenance (300s), admission
                    # and process cleanup fit inside this native deadline.
                    timeout=1380, statusMessage="MindIE Agent")
    if role == "stop":
        return neutral_hook_commands(arguments)
    return neutral_hook_commands(arguments, keep_stderr=True,
        failure="MindIE session preparation failed; inspect the reported stage and local diagnostics; no automatic retry.")


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
                      # The native startup window includes first-use uv
                      # download/build and the shared preparation lock.
                      # It does not change business tool-call deadlines.
                      startup_timeout_sec=1080,
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
            # 300s maximum component deadline + owned-process cleanup grace.
            server["tool_timeout_sec"] = 330
        servers[name] = server

    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n",
                                 encoding='utf-8', newline='\n')


if __name__ == "__main__":
    write_manifests(Path(__file__).resolve().parents[1])
