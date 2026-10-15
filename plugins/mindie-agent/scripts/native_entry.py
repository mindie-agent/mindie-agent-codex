"""Build the small native launcher shared by MCP and lifecycle hooks.

Codex 0.153.4's legacy MCP schema preserves env_vars but does not expand
PLUGIN_DATA. Keep its observed legacy layout dependency here, without a
directory scan, latest-version selector, or alternate configuration store.
"""
import base64
import json
from pathlib import Path
import shlex


def launcher_code(version, *, name="mindie-agent", marketplace="mindie-agent"):
    return (
        "import os,sys,runpy;from pathlib import Path;"
        "home=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')));"
        f"data=Path(os.environ.get('PLUGIN_DATA',str(home/'plugins/data'/{(name + '-' + marketplace)!r})));"
        f"source=Path(os.environ.get('PLUGIN_ROOT',str(home/'plugins/cache'/{marketplace!r}/{name!r}/{version!r})));"
        f"fixed=data/'releases'/{version!r}/'entry.py';"
        "prepared=fixed.is_file();"
        "entry=fixed if prepared else source/'scripts/bootstrap.py';"
        "args=sys.argv[1:];"
        "exec(\"if not prepared and args[:1]==['stop']:\\n raise RuntimeError('MindIE runtime is not prepared; start a new native session')\\n"
        "if not entry.is_file():\\n raise RuntimeError('MindIE release entry unavailable; native cache and prepared runtime are absent')\");"
        "sys.path.insert(0,str(entry.parent));"
        "sys.argv=[str(entry),*([str(data)] if not prepared else []),'--source',str(source),*args];"
        "runpy.run_path(str(entry),run_name='__main__')"
    )


def command_args(version, role, **names):
    return ["uv", "run", "--no-project",
            *(["--offline", "--no-python-downloads"] if role == "stop" else []),
            "--python", "3.13", "python",
            "-c", launcher_code(version, **names), role]


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


def hook_commands(version, role, **names):
    arguments = command_args(version, role, **names)
    if role == "stop":
        return neutral_hook_commands(arguments)
    return neutral_hook_commands(arguments, keep_stderr=True,
        failure="MindIE session preparation failed; inspect the reported stage and local diagnostics; no automatic retry.")


def write_manifests(root):
    root = Path(root)
    version = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
    hooks = {name: [{"hooks": [{"type": "command", "statusMessage": "MindIE Agent",
                               **hook_commands(version, role)}]}]
             for name, role in (("SessionStart", "session-start"), ("Stop", "stop"))}
    (root / "hooks/hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2) + "\n")
    servers = {}
    for name, role in (("mindie-knowledge", "knowledge-mcp"),
                       ("mindie-remote-dev", "remote-mcp")):
        arguments = command_args(version, role)
        servers[name] = dict(command=arguments[0], args=arguments[1:],
                             # The native startup window includes first-use uv
                             # download/build and the shared preparation lock.
                             # It does not change business tool-call deadlines.
                             startup_timeout_sec=600,
                             env_vars=["CODEX_HOME", "MINDIE_AGENT_CONFIG",
                                       "MINDIE_CODEX_BIN", "MINDIE_DIAGNOSTICS_CONFIG",
                                       "MINDIE_DIAGNOSTICS_ROOT"])
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")


if __name__ == "__main__":
    write_manifests(Path(__file__).resolve().parents[1])
