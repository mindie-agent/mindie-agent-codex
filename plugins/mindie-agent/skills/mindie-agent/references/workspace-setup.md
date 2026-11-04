# Development Workspace Setup

Run this flow during initial installation and first-use onboarding right after presenting [用户须知](user-notice.md) (including when community contribution is disabled):

1. **Offer workspace options**: Recommend [mindie-agent/vllm-ascend-workspace](https://github.com/mindie-agent/vllm-ascend-workspace.git). Offer to (a) create and initialize a new checkout, (b) reuse an existing directory, or (c) keep the current directory. Creating or reusing a workspace also records that path for the host that is actually installed (`codex -C` for CLI-only, Desktop **Add project** when Codex.app is present; see step 4).
2. **Verify GitHub authentication**: Before cloning or configuring forks, ensure GitHub authentication is ready via [GitHub Sign-In](github-authentication.md).
3. **Clone or inspect**:
   - New directory: `git clone https://github.com/mindie-agent/vllm-ascend-workspace.git "<destination>"`.
   - Existing directory: inspect the checkout and preserve local changes and configuration.
4. **Register the workspace with the installed host**:
   - **Codex CLI (preferred when Codex.app is absent)**: launch the next interactive session with `codex -C "<absolute-path>"`. This is a complete success path for a CLI-only install, including macOS Homebrew `codex` with no Codex.app. Do not run `codex app`, and do not treat a missing Desktop app as a failed registration.
   - **Codex Desktop (only when the app is installed)**: add/open the workspace root via **Add project**. On macOS this may be `codex app "<absolute-path>"`; on Windows, complete selection in the folder picker if needed. If `codex app` fails because Codex.app is not installed, fall back to `codex -C "<absolute-path>"` and continue.
5. **Initialize repositories**: On approval, read the workspace's `AGENTS.md`, `README.md`, and `.agents/scripts/README.md`, then run:
   - `python3 .agents/scripts/repo_init.py --no-input` (reusing saved `workspace.json` fork settings), or
   - `python3 .agents/scripts/repo_init.py --github-user <account> --no-input` (for first-time setup with the user's forks).
6. **Report status**: Report the workspace path, how it was registered (`codex -C` and/or Desktop **Add project**), and repository initialization status. CLI `codex -C` alone is a successful registration.

Suggested prompt (in the user's language):
“是否创建并初始化 vllm-ascend-workspace，同时添加到当前客户端工作区？也可以使用已有目录，或继续当前目录。”
