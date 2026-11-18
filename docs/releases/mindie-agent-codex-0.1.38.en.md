# MindIE Agent Codex v0.1.38 — Great Subtraction & UX Upgrade

## Summary

`v0.1.38` is a structural subtraction and onboarding upgrade: the Codex plugin shrinks from `17,734` lines of self-maintained runtime code to **`< 1,200` lines** (`1,099` in this release), delegating knowledge, remote execution, diagnostics, MCP serving, locking, and runtime provisioning to `mindie-harness` (`API_VERSION = 1`) and the shared components while preserving hook trust hashes, the `codex.json` layout, and upgrade continuity.

## Great Subtraction

- **Deleted duplicate runtime modules**: removed `bridge.py`, `mcp_gate.py`, `service_control.py`, `storage_gc.py`, `release_activation.py`, `release_migration.py`, `harness_update.py`, `component_update.py`, `agent_worker.py`, and 50+ other historical modules plus their internal tests.
- **Kept 10 thin files**: `__init__.py`, `with_runtime.py`, `mcp_entry.py`, `hook_launcher.py`, `bootstrap.py`, `entry.py`, `host_adapter.py`, `native_hooks.py`, `codex_transcript.py`, `setup.py`.
- **One MCP contract**: `remote-mcp`, `tools-mcp`, and `knowledge-mcp` delegate to `mindie_harness.api.serve_mcp` and `ManagedMCPAdapter`; native `mindie_interrupt` cancellation is owned by the shared `MCPStdioServer`.
- **Shared-component wheelhouse**: `setup.py update` delegates to `mindie_harness.runtime.wheelhouse.update_runtime`, stages prebuilt wheels, validates a candidate, and atomically cuts over while idle.
- **Upgrade continuity**: during `0.1.23 -> 0.1.38`, `bootstrap.py` reads the previous interpreter's component inventory and reinstalls any strictly newer shared component at its exact `commit_id`, so independently upgraded component floors are never downgraded.
- **Byte-frozen hook manifest**: `hooks/hooks.json` remains `25,650` bytes with `sha256 == ea80d0815485ba5f762ff5275f1e40817130694870405237dbd65d03f28f9981`.

## Performance Fast Paths

- `UserPromptSubmit`: < 15ms `{}` response with no stdin read.
- Unconfigured, `disabled`, `read-only`, or out-of-scope `Stop`: < 30ms `{}` response, zero SQLite, zero `mindie_harness` import.
- `SessionStart`: with an active interpreter, only an atomic `pending-update-check.json` marker; no build.
- The `/work/app-other` vs `/work/app` `commonpath` prefix trap is rejected on POSIX and on the same Windows drive.

## UX Upgrade

- **Deterministic install**: `uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host codex` installs the plugin, preheats the locked runtime, and initializes config in one step.
- **`gh` is optional**: recommend `contribute` when `gh` is authenticated; recommend `local-only` when `gh` is missing, unauthenticated, or unreachable (zero GitHub upload, no `gh` required). Installation and remote NPU capabilities are unaffected.
- **3-question dynamic onboarding**: native Codex `request_user_input` asks for the workspace, experience & privacy mode (`contribute` / `local-only` / `read-only`), and optional one-step SSH NPU access.
- **Read-only `mindie_doctor`**: shows runtime, config, sharing, and SSH/NPU readiness. A failed probe reports one concrete stage and never blocks the engineering task.
- **10-tool `codex` remote-dev profile**: `remote_exec_command`, `remote_write_stdin`, `remote_view_image`, `remote_apply_patch`, `remote_auth`, `remote_probe`, `remote_artifact_pull`, `remote_artifact_push`, `remote_job_status`, `remote_job_stop`.
- **Polite Star postscript**: only after explicit user consent; never silent.

## Compatibility

- Codex CLI: exactly `0.153.4`, plus `>=0.162.0-alpha.2` and `<=0.999.999` (prereleases included).
- `mindie-harness API_VERSION = 1`; `mindie-harness-seed.pyz` is updated to `v0.1.3`.
- Python `>= 3.11`; macOS, native Windows, and Linux / WSL.
- Existing `codex.json`, `codex.engine.json`, `releases/<version>/runtime.json`, and saved sharing choices remain compatible.

## Verification

```sh
python3 .github/scripts/check_lf_eol.py
pytest -q -m "not live_npu" tests
python3 plugins/mindie-agent/scripts/setup.py release-metadata --source plugins/mindie-agent --tests tests --revision "$(git rev-parse HEAD)"
```

Release contract tests cover the hook SHA-256, script line ceiling, seed verification and `HostAdapter` type, `.mcp.json` locator, `release-metadata`, stop/prompt fast paths, `migrate` rebinding, MCP surface separation with `mindie_interrupt`, `NativeHooks` CAS, and transcript public-message/fork boundaries.
