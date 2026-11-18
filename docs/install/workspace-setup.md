# Development Workspace & First-Time Onboarding Setup

Run this flow after the deterministic bootstrap (`uv run https://github.com/mindie-agent/mindie-agent/releases/latest/download/install.py --host codex`) and present [用户须知](user-notice.md).

1. **Use native Codex `request_user_input` for the 3 dynamic questions** (fall back to numbered options only when the tool is unavailable), ordered by the `gh` probe:
   - **Workspace** (`id: "workspace"`): initialize `vllm-ascend-workspace` / keep the current directory / use an existing workspace.
   - **Experience & privacy mode** (`id: "privacy_mode"`):
     - `gh` authenticated: `contribute (recommended)` / `local-only (local only, zero upload)` / `read-only (no capture)`.
     - `gh` unavailable, unauthenticated, or unreachable: `local-only (recommended; no GitHub upload, no gh required)` / `contribute (requires gh login)` / `read-only`.
   - **Optional one-step SSH NPU access** (`id: "ssh_npu"`): connect now / later. When chosen, prefer `remote_auth(save_default=True, discover=True)`; in a terminal only, use:
     ```sh
      <python> -m remote_dev auth setup --host <REAL_IP> --port 22 --user <REAL_USER> --save-default --discover --password
     ```
     Keep `--password` last and preserve the leading space.
2. **Resolve `WORKSPACE_ROOT`**:
   - New workspace: `git clone https://github.com/mindie-agent/vllm-ascend-workspace.git "<destination>"`.
   - Existing workspace: inspect the checkout and preserve local changes.
   - Keep current directory: `WORKSPACE_ROOT="$(pwd)"`.
3. **Register the workspace**:
   - Codex CLI (preferred without Codex.app): `codex -C "<absolute-path>"`.
   - Codex Desktop (only when installed): add/open the workspace root via **Add project**. If `codex app` is unavailable, fall back to `codex -C`.
4. **Initialize repositories** (only for a new/reused `vllm-ascend-workspace`): read its `AGENTS.md`, `README.md`, and `.agents/scripts/README.md`, then run `python3 .agents/scripts/repo_init.py --no-input` (or the documented `--github-user <account>` form).
5. **Persist privacy choices** with the preheated runtime `<python>`:
   ```sh
   <python> -m mindie_codex.scripts.setup sharing --enable --project-root "$WORKSPACE_ROOT"
   <python> -m mindie_codex.scripts.setup sharing --local-only --project-root "$WORKSPACE_ROOT"
   <python> -m mindie_codex.scripts.setup sharing --disable
   ```
6. **Show readiness and finish politely**:
   - If SSH/NPU was selected, run `remote_auth(..., save_default=True, discover=True)` (or the terminal command) and then the read-only `mindie_doctor` check.
   - Report plugin status, sharing tier, workspace path, registration path, and SSH/NPU readiness.
   - End with a single-line, polite Star suggestion. Only star after explicit consent; never Star silently.
