# Development Workspace Setup

Run this flow during initial installation and first-use onboarding right after presenting [用户须知](user-notice.md) (including when community contribution is disabled):

1. **Offer workspace options**: Recommend [mindie-agent/vllm-ascend-workspace](https://github.com/mindie-agent/vllm-ascend-workspace.git). Offer to (a) create and initialize a new checkout, (b) reuse an existing directory, or (c) keep the current directory. Creating or reusing a workspace also registers it in the client's native project list.
2. **Verify GitHub authentication**: Before cloning or configuring forks, ensure GitHub authentication is ready via [GitHub Sign-In](github-authentication.md).
3. **Clone or inspect**:
   - New directory: `git clone https://github.com/mindie-agent/vllm-ascend-workspace.git "<destination>"`.
   - Existing directory: inspect the checkout and preserve local changes and configuration.
4. **Register in the native client**:
   - Codex Desktop: add/open the workspace root via **Add project** (on macOS, `codex app "<absolute-path>"`; on Windows, complete selection in the folder picker if needed).
   - Codex CLI: launch the next interactive session with `codex -C "<absolute-path>"`.
5. **Initialize repositories**: On approval, read the workspace's `AGENTS.md`, `README.md`, and `.agents/scripts/README.md`, then run:
   - `python3 .agents/scripts/repo_init.py --no-input` (reusing saved `workspace.json` fork settings), or
   - `python3 .agents/scripts/repo_init.py --github-user <account> --no-input` (for first-time setup with the user's forks).
6. **Report status**: Report the workspace path, native client registration status, and repository initialization status clearly.

Suggested prompt (in the user's language):
“是否创建并初始化 vllm-ascend-workspace，同时添加到当前客户端工作区？也可以使用已有目录，或继续当前目录。”
