# Capture, Sharing, and Runtime Operations

## Default-On Tools, Opt-In Capture

- `mindie-remote-dev` and `mindie-tools` are available immediately in CLI, Desktop, Work mode, Automations, and Subagents. No session prefix, activation gate, `_meta` prerequisite, or `request_nonce` is required.
- `SessionStart` and `UserPromptSubmit` are passive, sub-15ms hooks. They never create SQLite state.
- `Stop` first reads the saved consent + `project_roots`. Unconfigured, `disabled`, `read-only`, or out-of-scope sessions print `{}` in under 30ms without importing `mindie_harness` or opening SQLite.
- When `contribute` or `local-only` is enabled and the event `cwd` is inside an absolute authorized root, `Stop` ensures a default admission lease (`not_before=0.0`), prepares the store schema, and hands the completed turn's redacted public transcript slice to `mindie_knowledge.loop.cli.capture_hook` (6s bound).
- `local-only` captures locally with zero GitHub upload and does not require `gh`.

## Privacy Modes

- `contribute`: scoped community knowledge PRs plus sanitized fault reporting. Recommended when `gh` is authenticated.
- `local-only`: local capture and retrieval only; no community PR and no GitHub upload. Recommended when `gh` is unavailable.
- `read-only`: no transcript capture, no outbound publication.
- A saved choice persists across upgrades. An explicit `disabled`/`read-only` choice is never silently widened. `Stop` never re-enables a disabled hook.

## Readiness, SSH, and Sharing Operations

Use the installed runtime interpreter (`<python>`) for these commands.

- **Read-only health card**:
  ```sh
  <python> -m mindie_harness.cli --host codex doctor
  ```
  The same check is exposed as the `mindie_doctor` MCP tool. It reports runtime, config, consent, and optional SSH/NPU probe facts without mutating profile state.

- **One-step SSH NPU setup** (password is requested at the end and is never stored in argv history):
  ```sh
  <python> -m remote_dev auth setup --host <REAL_IP> --port 22 --user <REAL_USER> --save-default --discover --password
  ```
  MCP callers can use `remote_auth(save_default=True, discover=True)` instead. After enrollment, run `mindie_doctor` to show the readiness card.

- **Sharing state**:
  ```sh
  <python> plugins/mindie-agent/scripts/setup.py sharing --status
  <python> plugins/mindie-agent/scripts/setup.py sharing --enable --project-root /absolute/workspace
  <python> plugins/mindie-agent/scripts/setup.py sharing --local-only --project-root /absolute/workspace
  <python> plugins/mindie-agent/scripts/setup.py sharing --disable
  ```

- **Runtime inspection and repair**:
  ```sh
  <python> -m mindie_harness.cli --host codex inspect
  <python> plugins/mindie-agent/scripts/setup.py update --offline
  ```
  `update` uses the prebuilt wheelhouse and preserves the active generation until a validated candidate is ready.

## Failure Stages

- Readiness failures are reported as concrete stages (`runtime`, `config`, `consent`, `ssh`, `npu`) by `mindie_doctor`. A failed readiness probe is not a reason to block the engineering task; continue with native SSH or local scripts after reporting it once.
- Remote writes with an `unknown` outcome must be reconciled before any repeat. Never resubmit an uncertain mutation automatically.
- Historical experience import is never automatic; only an explicit user request may process a selected transcript source.
